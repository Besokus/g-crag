import gzip
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from llm_user_suite.db import SessionLocal
from llm_user_suite.dreamer import dream
from llm_user_suite.metric_store import ingest_otlp_metrics
from llm_user_suite.models import (
    BehaviorEvent,
    BehaviorSession,
    MetricAggregate,
    Scenario,
    ScenarioVersion,
)
from llm_user_suite.models import TestRun as SuiteRun
from llm_user_suite.schemas import BehaviorEventIn
from llm_user_suite.telemetry import (
    close_idle_sessions,
    decode_otlp_json,
    decode_otlp_protobuf,
    ingest_event,
)
from sqlalchemy import select


def test_decode_otlp_span_event():
    document = {
        "resourceSpans": [{"scopeSpans": [{"spans": [{
            "traceId": "a" * 32,
            "attributes": [
                {"key": "grid.tenant.id", "value": {"stringValue": "tenant-a"}},
                {"key": "grid.event.kind", "value": {"stringValue": "feedback.submitted"}},
            ],
            "events": [{"name": "grid.user.feedback.submitted", "attributes": [
                {"key": "grid.event.id", "value": {"stringValue": "evt-1"}},
                {"key": "grid.event.payload.feedback", "value": {"stringValue": "dislike"}},
            ]}],
        }]}]}],
    }
    rows = decode_otlp_json("traces", document)
    assert len(rows) == 1
    assert rows[0].kind == "feedback.submitted"
    assert rows[0].tenantId == "tenant-a"
    assert rows[0].payload["feedback"] == "dislike"


def test_decode_otlp_protobuf_span_event():
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    request = ExportTraceServiceRequest()
    span = request.resource_spans.add().scope_spans.add().spans.add()
    span.trace_id = bytes.fromhex("ab" * 16)
    event = span.events.add()
    event.name = "grid.user.qa.started"
    attr = event.attributes.add()
    attr.key = "grid.event.id"
    attr.value.string_value = "protobuf-event"
    rows = decode_otlp_protobuf("traces", request.SerializeToString())
    assert len(rows) == 1
    assert rows[0].eventId == "protobuf-event"
    assert rows[0].traceId == "ab" * 16


@pytest.mark.asyncio
async def test_otlp_http_accepts_gzip_compressed_protobuf():
    from llm_user_suite.main import app
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
    )

    request = ExportLogsServiceRequest()
    resource_log = request.resource_logs.add()
    resource_attr = resource_log.resource.attributes.add()
    resource_attr.key = "grid.tenant.id"
    resource_attr.value.string_value = "default"
    record = resource_log.scope_logs.add().log_records.add()
    record.trace_id = bytes.fromhex("cd" * 16)
    for key, value in (
        ("grid.event.kind", "qa.completed"),
        ("grid.event.id", "gzip-protobuf-event"),
    ):
        attr = record.attributes.add()
        attr.key = key
        attr.value.string_value = value

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/logs",
            content=gzip.compress(request.SerializeToString()),
            headers={
                "content-type": "application/x-protobuf",
                "content-encoding": "gzip",
            },
        )

    assert response.status_code == 200
    async with SessionLocal() as db:
        row = (await db.execute(select(BehaviorEvent).where(
            BehaviorEvent.source_event_id == "gzip-protobuf-event",
        ))).scalar_one()
    assert row.trace_id == "cd" * 16


@pytest.mark.asyncio
async def test_otlp_metrics_are_allowlisted_and_minute_aggregated(monkeypatch):
    monkeypatch.setattr("llm_user_suite.config.settings.METRIC_ALLOWLIST", "grid_qa_total")
    document = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
        {"name": "grid_qa_total", "sum": {"dataPoints": [
            {"asInt": "4", "attributes": [{"key": "model", "value": {"stringValue": "ollama"}}]},
        ]}},
        {"name": "secret_high_cardinality", "gauge": {"dataPoints": [{"asDouble": 1.0}]}},
    ]}]}]}
    async with SessionLocal() as db:
        assert await ingest_otlp_metrics(db, document) == 1
        rows = (await db.execute(select(MetricAggregate))).scalars().all()
        events = (await db.execute(select(BehaviorEvent).where(BehaviorEvent.kind == "metric.snapshot"))).scalars().all()
    assert len(rows) == 1 and rows[0].metric_name == "grid_qa_total"
    assert rows[0].labels == {"model": "ollama"}
    assert not events


@pytest.mark.asyncio
async def test_conversation_id_is_backfilled_into_same_trace_session():
    occurred_at = datetime.now(UTC)
    async with SessionLocal() as db:
        started = await ingest_event(db, BehaviorEventIn(
            eventId="started", tenantId="tenant-a", userHash="user-hash",
            traceId="trace-1", kind="qa.stream.started", method="POST",
            path="/api/qa/answer/stream", occurredAt=occurred_at,
        ))
        completed = await ingest_event(db, BehaviorEventIn(
            eventId="completed", tenantId="tenant-a", userHash="user-hash",
            traceId="trace-1", conversationId="conversation-1",
            kind="qa.stream.completed", method="POST",
            path="/api/qa/answer/stream", occurredAt=occurred_at + timedelta(seconds=2),
        ))
        sessions = (await db.execute(select(BehaviorSession))).scalars().all()

    assert started.session_id == completed.session_id
    assert len(sessions) == 1
    assert sessions[0].conversation_id == "conversation-1"


@pytest.mark.asyncio
async def test_retry_detection_does_not_treat_every_conversation_turn_as_retry():
    occurred_at = datetime.now(UTC)
    async with SessionLocal() as db:
        for index, query in enumerate(("first question", "second question", "second question")):
            await ingest_event(db, BehaviorEventIn(
                eventId=f"turn-{index}", tenantId="tenant-a", userHash="user-hash",
                conversationId="conversation-1", traceId=f"trace-{index}",
                kind="qa.stream.started", method="POST", path="/api/qa/answer/stream",
                occurredAt=occurred_at + timedelta(seconds=index * 10),
                payload={"query": query},
            ))
        session = (await db.execute(select(BehaviorSession))).scalar_one()

    assert session.retry_count == 1


@pytest.mark.asyncio
async def test_sessionize_close_and_dream_idempotently(monkeypatch):
    monkeypatch.setattr("llm_user_suite.dreamer.configured", lambda role: False)
    old = datetime.now(UTC) - timedelta(hours=2)
    async with SessionLocal() as db:
        for index in range(3):
            session_id = f"session-{index}"
            await ingest_event(db, BehaviorEventIn(
                eventId=f"qa-{index}", sessionId=session_id, kind="qa.stream.started",
                userId=f"user-{index}", method="POST", path="/api/qa/answer/stream",
                occurredAt=old, payload={"query": "SF6 断路器漏气如何处理", "request": {"query": "SF6 断路器漏气如何处理"}},
            ))
            await ingest_event(db, BehaviorEventIn(
                eventId=f"fb-{index}", sessionId=session_id, kind="feedback.submitted",
                userId=f"user-{index}", method="POST", path="/api/qa/feedback",
                occurredAt=old + timedelta(seconds=20), payload={"feedback": "dislike", "reason": "引用不足"},
            ))
        assert await close_idle_sessions(db) == 3
        result = await dream(db)
        assert result["created"] == 1
        assert (await dream(db))["created"] == 0
        scenarios = (await db.execute(select(Scenario))).scalars().all()
        versions = (await db.execute(select(ScenarioVersion))).scalars().all()
        assert len(scenarios) == len(versions) == 1
        assert versions[0].spec["stages"]
        assert versions[0].spec["stages"][0]["completion"]["streamCompleted"] is True


@pytest.mark.asyncio
async def test_approved_same_version_auto_regresses_only_for_new_sessions(monkeypatch):
    monkeypatch.setattr("llm_user_suite.dreamer.configured", lambda role: False)
    monkeypatch.setattr("llm_user_suite.config.settings.TARGET_BASE_URL", "https://testserver")
    old = datetime.now(UTC) - timedelta(hours=2)
    async with SessionLocal() as db:
        for index in range(3):
            await ingest_event(db, BehaviorEventIn(
                eventId=f"initial-{index}", sessionId=f"initial-session-{index}",
                kind="qa.stream.started", userId=f"user-{index}", method="POST",
                path="/api/qa/answer/stream", occurredAt=old,
                payload={"query": "断路器漏气", "request": {"query": "断路器漏气"}},
            ))
        await close_idle_sessions(db)
        await dream(db)
        scenario = (await db.execute(select(Scenario))).scalar_one()
        version = (await db.execute(select(ScenarioVersion))).scalar_one()
        scenario.status = "active"
        version.status = "approved"
        await db.commit()

        await ingest_event(db, BehaviorEventIn(
            eventId="new-session", sessionId="new-session", kind="qa.stream.started",
            userId="new-user", method="POST", path="/api/qa/answer/stream",
            occurredAt=old, payload={"query": "断路器漏气", "request": {"query": "断路器漏气"}},
        ))
        await close_idle_sessions(db)
        result = await dream(db)
        assert result["created"] == 0 and result["scheduled"] == 1
        assert len((await db.execute(select(ScenarioVersion))).scalars().all()) == 1
        runs = (await db.execute(select(SuiteRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].tenant_id == "default"
        assert (await dream(db))["scheduled"] == 0


@pytest.mark.asyncio
async def test_dreaming_never_clusters_matching_signatures_across_tenants(monkeypatch):
    monkeypatch.setattr("llm_user_suite.dreamer.configured", lambda role: False)
    old = datetime.now(UTC) - timedelta(hours=2)
    async with SessionLocal() as db:
        for tenant in ("tenant-a", "tenant-b"):
            for index in range(3):
                await ingest_event(db, BehaviorEventIn(
                    eventId=f"{tenant}-{index}", tenantId=tenant,
                    sessionId=f"{tenant}-session-{index}", kind="qa.stream.started",
                    userId=f"user-{index}", method="POST", path="/api/qa/answer/stream",
                    occurredAt=old,
                    payload={"query": "相同问题", "request": {"query": "相同问题"}},
                ))
        await close_idle_sessions(db)
        result = await dream(db)
        scenarios = (await db.execute(select(Scenario))).scalars().all()

    assert result["created"] == 2
    assert {row.tenant_id for row in scenarios} == {"tenant-a", "tenant-b"}
    assert len({row.signature for row in scenarios}) == 1


@pytest.mark.asyncio
async def test_any_disliked_session_makes_small_cluster_interesting(monkeypatch):
    monkeypatch.setattr("llm_user_suite.dreamer.configured", lambda role: False)
    now = datetime.now(UTC).replace(tzinfo=None)
    async with SessionLocal() as db:
        db.add_all([
            BehaviorSession(
                tenant_id="tenant-a", user_hash="u1", status="closed",
                signature="shared", has_dislike=True,
                last_event_at=now - timedelta(minutes=1), closed_at=now - timedelta(minutes=1),
            ),
            BehaviorSession(
                tenant_id="tenant-a", user_hash="u2", status="closed",
                signature="shared", has_dislike=False,
                last_event_at=now, closed_at=now,
            ),
        ])
        await db.commit()
        result = await dream(db)

    assert result["created"] == 1
