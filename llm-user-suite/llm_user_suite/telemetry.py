from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf.json_format import MessageToDict
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import metrics
from .config import settings
from .models import BehaviorEvent, BehaviorSession
from .privacy import hash_user, redact
from .schemas import BehaviorEventIn


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _source_id(event: BehaviorEventIn) -> str:
    if event.eventId:
        return event.eventId[:128]
    raw = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _resolve_session(db: AsyncSession, event: BehaviorEventIn, occurred_at: datetime) -> BehaviorSession:
    user_hash = (event.userHash or hash_user(event.userId))[:64]
    tenant_id = (event.tenantId or "default")[:64]
    conversation_id = (event.conversationId or "")[:64]
    trace_id = (event.traceId or "")[:64]
    if event.sessionId:
        raw_id = f"{tenant_id}|{user_hash}|{event.sessionId}"
        session_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()
        row = await db.get(BehaviorSession, session_id)
        if row:
            return row
        row = BehaviorSession(
            id=session_id, tenant_id=tenant_id,
            user_hash=user_hash, conversation_id=conversation_id,
            trace_id=trace_id,
            started_at=occurred_at, last_event_at=occurred_at,
        )
        db.add(row)
        await db.flush()
        return row

    cutoff = occurred_at - timedelta(seconds=settings.SESSION_IDLE_SECONDS)
    base_filters = [
        BehaviorSession.tenant_id == tenant_id,
        BehaviorSession.user_hash == user_hash,
        BehaviorSession.status == "open",
        BehaviorSession.last_event_at >= cutoff,
    ]

    async def find_session(*extra_filters) -> BehaviorSession | None:
        return (await db.execute(
            select(BehaviorSession).where(
                *base_filters, *extra_filters,
            ).order_by(desc(BehaviorSession.last_event_at)).limit(1)
        )).scalar_one_or_none()

    if conversation_id:
        row = await find_session(BehaviorSession.conversation_id == conversation_id)
        # 首次问答通常在 started 事件中还没有 conversationId；completed 事件
        # 必须通过同一 HTTP trace 回填到原会话，不能被切成两个行为会话。
        if not row and trace_id:
            row = await find_session(BehaviorSession.trace_id == trace_id)
    elif trace_id:
        row = await find_session(BehaviorSession.trace_id == trace_id)
    else:
        row = await find_session()
    if row:
        return row
    row = BehaviorSession(
        tenant_id=tenant_id, user_hash=user_hash,
        conversation_id=conversation_id, started_at=occurred_at,
        trace_id=trace_id,
        last_event_at=occurred_at,
    )
    db.add(row)
    await db.flush()
    return row


async def ingest_event(db: AsyncSession, incoming: BehaviorEventIn) -> BehaviorEvent | None:
    occurred_at = incoming.occurredAt or utcnow()
    if occurred_at.tzinfo is not None:
        occurred_at = occurred_at.astimezone(UTC).replace(tzinfo=None)
    session = await _resolve_session(db, incoming, occurred_at)
    payload = redact(incoming.payload)
    user_hash = (incoming.userHash or hash_user(incoming.userId))[:64]
    row = BehaviorEvent(
        source_event_id=_source_id(incoming), session_id=session.id,
        tenant_id=(incoming.tenantId or "default")[:64], user_hash=user_hash,
        conversation_id=(incoming.conversationId or "")[:64],
        trace_id=(incoming.traceId or "")[:64], qa_trace_id=(incoming.qaTraceId or "")[:64],
        kind=incoming.kind[:96], method=incoming.method[:12].upper(), path=incoming.path[:512],
        status_code=incoming.statusCode, duration_ms=incoming.durationMs,
        payload=payload, occurred_at=occurred_at,
    )
    try:
        if incoming.kind in {"http.request", "qa.started", "qa.stream.started", "qa.websocket.started"}:
            previous = (await db.execute(select(BehaviorEvent).where(
                BehaviorEvent.session_id == session.id,
                BehaviorEvent.kind == incoming.kind,
                BehaviorEvent.method == incoming.method[:12].upper(),
                BehaviorEvent.path == incoming.path[:512],
            ).order_by(desc(BehaviorEvent.occurred_at)).limit(1))).scalar_one_or_none()
            current_query = str(
                payload.get("query")
                or (payload.get("request") or {}).get("query", "")
            ).strip()
            previous_payload = previous.payload if previous else {}
            previous_query = str(
                previous_payload.get("query")
                or (previous_payload.get("request") or {}).get("query", "")
            ).strip()
            retry_after_failure = bool(
                previous
                and (session.has_failure or session.has_degradation)
                and (occurred_at - previous.occurred_at).total_seconds() <= 300
            )
            if previous and (
                current_query and previous_query and current_query == previous_query
                or retry_after_failure
            ):
                session.retry_count += 1
        db.add(row)
        session.event_count += 1
        session.last_event_at = max(session.last_event_at or occurred_at, occurred_at)
        session.conversation_id = session.conversation_id or (incoming.conversationId or "")[:64]
        session.trace_id = session.trace_id or (incoming.traceId or "")[:64]
        session.has_dislike = session.has_dislike or incoming.kind == "feedback.submitted" and payload.get("feedback") == "dislike"
        session.has_failure = session.has_failure or bool(
            incoming.statusCode and incoming.statusCode >= 500
        ) or incoming.kind in {"qa.stream.aborted", "qa.stream.error", "qa.websocket.aborted", "qa.websocket.error", "http.error", "timeout"}
        session.has_degradation = session.has_degradation or bool(
            incoming.statusCode in {408, 429}
            or any(token in incoming.kind for token in ("fallback", "degraded", "timeout"))
            or payload.get("degraded")
        )
        faith = payload.get("faithfulness")
        if isinstance(faith, (int, float)):
            session.min_faithfulness = faith if session.min_faithfulness is None else min(session.min_faithfulness, faith)
        await db.commit()
        await db.refresh(row)
        metrics.EVENTS_INGESTED.labels(incoming.kind[:96]).inc()
        return row
    except IntegrityError:
        await db.rollback()
        metrics.EVENTS_DROPPED.labels("duplicate").inc()
        return None


def _attrs(values: list[dict] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values or []:
        key = item.get("key", "")
        value = item.get("value", {})
        for value_key in ("stringValue", "intValue", "doubleValue", "boolValue", "bytesValue"):
            if value_key in value:
                result[key] = value[value_key]
                break
        if "arrayValue" in value:
            result[key] = [_attrs([{"key": "v", "value": v}]).get("v") for v in value["arrayValue"].get("values", [])]
    return result


def _trace_id(value: str) -> str:
    if not value:
        return ""
    if len(value) in (16, 32) and all(ch in "0123456789abcdefABCDEF" for ch in value):
        return value.lower()
    try:
        return base64.b64decode(value).hex()
    except Exception:
        return value[:64]


def _event_from_attrs(kind: str, attrs: dict[str, Any], *, trace_id: str = "", occurred_at: datetime | None = None) -> BehaviorEventIn:
    payload = {}
    for key, value in attrs.items():
        if key.startswith("grid.event.payload."):
            payload_key = key.removeprefix("grid.event.payload.")
            if isinstance(value, str) and value[:1] in {"{", "["}:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            payload[payload_key] = value
    return BehaviorEventIn(
        eventId=str(attrs.get("grid.event.id", "")), kind=kind,
        tenantId=str(attrs.get("grid.tenant.id", "default")),
        userId=str(attrs.get("enduser.id", "")), userHash=str(attrs.get("grid.user.hash", "")),
        sessionId=str(attrs.get("grid.session.id", "")),
        conversationId=str(attrs.get("grid.conversation.id", "")),
        traceId=trace_id or str(attrs.get("grid.trace.id", "")),
        qaTraceId=str(attrs.get("grid.qa_trace.id", "")),
        method=str(attrs.get("http.request.method", attrs.get("http.method", payload.get("method", "")))),
        path=str(attrs.get("url.path", attrs.get("http.route", payload.get("path", "")))),
        statusCode=int(attrs["http.response.status_code"]) if str(attrs.get("http.response.status_code", "")).isdigit() else None,
        durationMs=float(attrs["grid.duration_ms"]) if attrs.get("grid.duration_ms") is not None else None,
        occurredAt=occurred_at, payload=payload,
    )


def decode_otlp_json(signal: str, document: dict) -> list[BehaviorEventIn]:
    events: list[BehaviorEventIn] = []
    if signal == "traces":
        for resource in document.get("resourceSpans", []):
            resource_attrs = _attrs(resource.get("resource", {}).get("attributes"))
            for scope in resource.get("scopeSpans", resource.get("instrumentationLibrarySpans", [])):
                for span in scope.get("spans", []):
                    attrs = {**resource_attrs, **_attrs(span.get("attributes"))}
                    trace_id = _trace_id(span.get("traceId", ""))
                    kind = attrs.get("grid.event.kind")
                    semantic_events = [
                        event for event in span.get("events", [])
                        if str(event.get("name", "")).startswith("grid.user.")
                    ]
                    if kind and not semantic_events:
                        events.append(_event_from_attrs(str(kind), attrs, trace_id=trace_id))
                    for event in semantic_events:
                        event_attrs = {**attrs, **_attrs(event.get("attributes"))}
                        name = str(event.get("name", ""))
                        if name.startswith("grid.user."):
                            timestamp = None
                            nanos = event.get("timeUnixNano")
                            if nanos and str(nanos).isdigit():
                                timestamp = datetime.fromtimestamp(int(nanos) / 1_000_000_000, UTC)
                            events.append(_event_from_attrs(name.removeprefix("grid.user."), event_attrs, trace_id=trace_id, occurred_at=timestamp))
    elif signal == "logs":
        for resource in document.get("resourceLogs", []):
            resource_attrs = _attrs(resource.get("resource", {}).get("attributes"))
            for scope in resource.get("scopeLogs", resource.get("instrumentationLibraryLogs", [])):
                for record in scope.get("logRecords", []):
                    attrs = {**resource_attrs, **_attrs(record.get("attributes"))}
                    kind = attrs.get("grid.event.kind") or record.get("body", {}).get("stringValue")
                    if kind:
                        events.append(_event_from_attrs(str(kind), attrs, trace_id=_trace_id(record.get("traceId", ""))))
    elif signal == "metrics":
        # Metrics are stored in minute aggregates by metric_store, never as full OTLP documents.
        return []
    return events


def decode_otlp_protobuf(signal: str, raw: bytes) -> list[BehaviorEventIn]:
    if signal == "traces":
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        message = ExportTraceServiceRequest()
    elif signal == "logs":
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceRequest,
        )
        message = ExportLogsServiceRequest()
    else:
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
            ExportMetricsServiceRequest,
        )
        message = ExportMetricsServiceRequest()
    message.ParseFromString(raw)
    return decode_otlp_json(signal, MessageToDict(message, preserving_proto_field_name=False))


async def close_idle_sessions(db: AsyncSession) -> int:
    cutoff = utcnow() - timedelta(seconds=settings.SESSION_IDLE_SECONDS)
    rows = (await db.execute(
        select(BehaviorSession).where(
            BehaviorSession.status == "open", BehaviorSession.last_event_at < cutoff
        )
    )).scalars().all()
    for row in rows:
        events = (await db.execute(
            select(BehaviorEvent).where(BehaviorEvent.session_id == row.id).order_by(BehaviorEvent.occurred_at)
        )).scalars().all()
        sequence = [f"{event.method}:{event.path or event.kind}" for event in events if event.kind != "metric.snapshot"]
        row.signature = hashlib.sha256("|".join(sequence).encode("utf-8")).hexdigest() if sequence else ""
        row.status = "closed"
        row.closed_at = utcnow()
    await db.commit()
    return len(rows)
