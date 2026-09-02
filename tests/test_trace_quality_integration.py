"""Trace ID, structured feedback, and quality event center contracts."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.trace_id import resolve_trace_id, valid_trace_id
from app.db.base import Base
from app.models.quality_event import QualityEvent
from app.schemas.quality_event import normalize_quality_payload
from app.services import feedback_service, quality_event_service


class _CaptureSession:
    def __init__(self) -> None:
        self.added = []
        self.commits = 0

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1


@pytest_asyncio.fixture
async def quality_event_db():
    """Create only the quality_events table; no external services are involved."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[QualityEvent.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


def _quality_event(
    event_id: str,
    *,
    tenant: str,
    source: str = "feedback",
    event_type: str = "dislike",
    status: str = "pending",
) -> QualityEvent:
    return QualityEvent(
        id=event_id,
        tenant=tenant,
        source=source,
        type=event_type,
        status=status,
        payload={
            "trace_id": f"trace-{event_id}",
            "conversation_id": f"conversation-{event_id}",
            "query": f"query-{event_id}",
        },
    )


def test_trace_id_reuses_first_valid_candidate():
    caller_trace_id = "trace-20260727-valid"

    assert resolve_trace_id("bad value", f"  {caller_trace_id}  ") == caller_trace_id
    assert valid_trace_id(caller_trace_id) == caller_trace_id


@pytest.mark.parametrize(
    "invalid_value",
    [
        "short",
        "trace id with spaces",
        "x" * 65,
        None,
    ],
)
def test_invalid_trace_id_is_rebuilt_as_a_valid_id(invalid_value):
    rebuilt = resolve_trace_id(invalid_value)

    assert rebuilt != invalid_value
    assert valid_trace_id(rebuilt) == rebuilt


@pytest.mark.asyncio
async def test_feedback_persists_structured_sources_and_legacy_projection():
    db = _CaptureSession()

    await feedback_service.record_feedback(
        db,
        conversation_id="conversation-1",
        query="主变油温异常如何处理",
        answer="先核对负荷与冷却系统。",
        feedback="like",
        username="operator-a",
        trace_id="trace-feedback-1",
        sources=[
            {
                "docId": "doc-1",
                "docName": "主变运行规程",
                "chunkId": "chunk-2",
                "chunkIdx": 0,
                "score": 0.92,
                "text": "检查风机、油泵及散热器。",
            }
        ],
    )

    assert db.commits == 1
    assert len(db.added) == 1
    feedback = db.added[0]
    assert feedback.trace_id == "trace-feedback-1"
    assert feedback.retrieval_sources == "主变运行规程"
    assert json.loads(feedback.sources_json) == [
        {
            "doc_id": "doc-1",
            "doc_name": "主变运行规程",
            "chunk_id": "chunk-2",
            "chunk_idx": 0,
            "score": 0.92,
            "chunk": "检查风机、油泵及散热器。",
        }
    ]


@pytest.mark.asyncio
async def test_feedback_upgrades_legacy_source_names_to_structured_json():
    db = _CaptureSession()

    await feedback_service.record_feedback(
        db,
        conversation_id="conversation-legacy",
        query="旧客户端问题",
        answer="旧客户端答案",
        feedback="like",
        username="operator-a",
        retrieval_sources="规程A, 规程B",
    )

    feedback = db.added[0]
    assert feedback.retrieval_sources == "规程A, 规程B"
    assert json.loads(feedback.sources_json) == [
        {"doc_name": "规程A"},
        {"doc_name": "规程B"},
    ]
    assert feedback_service.load_sources_json(feedback.sources_json) == [
        {"doc_name": "规程A"},
        {"doc_name": "规程B"},
    ]


@pytest.mark.parametrize(
    ("payload", "fallback_tenant", "expected"),
    [
        (
            {
                "trace_id": "trace-snake",
                "conversation_id": "conversation-snake",
                "query": "snake query",
                "answer": "snake answer",
                "feedback_reason": "snake reason",
                "retrieval_sources": [
                    {
                        "doc_name": "snake doc",
                        "chunk_text": "snake chunk",
                        "rerank_score": 0.81,
                    }
                ],
                "tenant_id": "tenant-snake",
                "user_id": "user-snake",
                "username": "snake-user",
            },
            "fallback",
            {
                "trace_id": "trace-snake",
                "conversation_id": "conversation-snake",
                "query": "snake query",
                "answer": "snake answer",
                "reason": "snake reason",
                "tenant": "tenant-snake",
                "user_id": "user-snake",
                "username": "snake-user",
                "doc": "snake doc",
                "chunk": "snake chunk",
                "score": 0.81,
            },
        ),
        (
            {
                "traceId": "trace-camel",
                "conversationId": "conversation-camel",
                "question": "camel query",
                "response": "camel answer",
                "feedbackReason": "camel reason",
                "retrievalSources": [
                    {
                        "docName": "camel doc",
                        "chunkText": "camel chunk",
                        "rerankScore": 0.82,
                    }
                ],
                "tenantId": "tenant-camel",
                "user": {"userId": "user-camel", "name": "camel-user"},
            },
            "fallback",
            {
                "trace_id": "trace-camel",
                "conversation_id": "conversation-camel",
                "query": "camel query",
                "answer": "camel answer",
                "reason": "camel reason",
                "tenant": "tenant-camel",
                "user_id": "user-camel",
                "username": "camel-user",
                "doc": "camel doc",
                "chunk": "camel chunk",
                "score": 0.82,
            },
        ),
        (
            {
                "trace": "trace-flat",
                "q": "flat query",
                "answer": "flat answer",
                "reason": "flat reason",
                "retrievalSources": "flat doc A, flat doc B",
                "userId": "user-flat",
                "userName": "flat-user",
            },
            "tenant-fallback",
            {
                "trace_id": "trace-flat",
                "conversation_id": "",
                "query": "flat query",
                "answer": "flat answer",
                "reason": "flat reason",
                "tenant": "tenant-fallback",
                "user_id": "user-flat",
                "username": "flat-user",
                "doc": "flat doc A",
                "chunk": "",
                "score": None,
            },
        ),
    ],
    ids=["snake_case", "camelCase", "flat_legacy"],
)
def test_normalize_quality_payload_accepts_field_variants(
    payload,
    fallback_tenant,
    expected,
):
    normalized = normalize_quality_payload(payload, fallback_tenant)

    assert normalized["trace_id"] == expected["trace_id"]
    assert normalized["conversation_id"] == expected["conversation_id"]
    assert normalized["query"] == expected["query"]
    assert normalized["answer"] == expected["answer"]
    assert normalized["reason"] == expected["reason"]
    assert normalized["tenant"] == expected["tenant"]
    assert normalized["user"]["id"] == expected["user_id"]
    assert normalized["user"]["username"] == expected["username"]
    assert normalized["sources"][0]["doc"] == expected["doc"]
    assert normalized["sources"][0]["chunk"] == expected["chunk"]
    assert normalized["sources"][0]["score"] == expected["score"]


@pytest.mark.asyncio
async def test_quality_event_list_and_detail_are_tenant_isolated(quality_event_db):
    quality_event_db.add_all(
        [
            _quality_event("event-a-open", tenant="tenant-a"),
            _quality_event(
                "event-a-resolved",
                tenant="tenant-a",
                source="online_eval",
                event_type="low_faith",
                status="handled",
            ),
            _quality_event(
                "event-b-ignored",
                tenant="tenant-b",
                source="governance",
                event_type="doc_blocked",
                status="ignored",
            ),
        ]
    )
    await quality_event_db.commit()

    total, rows = await quality_event_service.list_events(
        quality_event_db,
        tenant_id="tenant-a",
    )
    assert total == 2
    assert {row.id for row in rows} == {"event-a-open", "event-a-resolved"}

    own = await quality_event_service.get_event(
        quality_event_db,
        "event-a-open",
        tenant_id="tenant-a",
    )
    foreign = await quality_event_service.get_event(
        quality_event_db,
        "event-a-open",
        tenant_id="tenant-b",
    )
    assert own is not None
    assert foreign is None


@pytest.mark.asyncio
async def test_quality_event_stats_are_tenant_isolated(quality_event_db):
    quality_event_db.add_all(
        [
            _quality_event("event-a-open", tenant="tenant-a"),
            _quality_event(
                "event-a-resolved",
                tenant="tenant-a",
                source="online_eval",
                event_type="low_faith",
                status="handled",
            ),
            _quality_event(
                "event-b-ignored",
                tenant="tenant-b",
                source="governance",
                event_type="doc_blocked",
                status="ignored",
            ),
        ]
    )
    await quality_event_db.commit()

    result = await quality_event_service.stats(
        quality_event_db,
        tenant_id="tenant-a",
    )

    assert result["counts"] == {
        "open": 1,
        "processing": 0,
        "resolved": 1,
        "ignored": 0,
        "total": 2,
    }
    assert {item["value"] for item in result["sources"]} == {
        "feedback",
        "online_eval",
    }
    assert {item["value"] for item in result["eventTypes"]} == {
        "dislike",
        "low_faith",
    }


@pytest.mark.asyncio
async def test_quality_event_status_history_and_foreign_tenant_not_found(
    quality_event_db,
):
    row = _quality_event("event-status", tenant="tenant-a")
    quality_event_db.add(row)
    await quality_event_db.commit()

    foreign_update = await quality_event_service.update_status(
        quality_event_db,
        row.id,
        tenant_id="tenant-b",
        status="processing",
        operator="foreign-operator",
        note="must not be visible",
    )
    assert foreign_update is None
    assert row.status == "pending"

    processing = await quality_event_service.update_status(
        quality_event_db,
        row.id,
        tenant_id="tenant-a",
        status="processing",
        operator="operator-a",
        note="开始复核证据",
    )
    assert processing is not None
    assert processing.status == "processing"
    assert processing.handled_at is None
    assert processing.payload["management"]["history"] == [
        {
            "from": "open",
            "to": "processing",
            "operator": "operator-a",
            "note": "开始复核证据",
            "at": processing.payload["management"]["history"][0]["at"],
        }
    ]

    resolved = await quality_event_service.update_status(
        quality_event_db,
        row.id,
        tenant_id="tenant-a",
        status="resolved",
        operator="operator-b",
        note="证据已补齐",
    )
    history = resolved.payload["management"]["history"]
    assert resolved.status == "resolved"
    assert resolved.handled_at is not None
    assert [(item["from"], item["to"]) for item in history] == [
        ("open", "processing"),
        ("processing", "resolved"),
    ]
    assert history[-1]["operator"] == "operator-b"
    assert history[-1]["note"] == "证据已补齐"

    with pytest.raises(
        quality_event_service.InvalidQualityEventTransition,
        match="resolved 不能流转到 processing",
    ):
        await quality_event_service.update_status(
            quality_event_db,
            row.id,
            tenant_id="tenant-a",
            status="processing",
            operator="operator-a",
        )
