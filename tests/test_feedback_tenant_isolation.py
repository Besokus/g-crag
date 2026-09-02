"""Focused tenant-isolation tests for feedback management and background updates."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.db import session as session_module
from app.models.feedback import Feedback
from app.rag import judge
from app.services import feedback_service


@pytest_asyncio.fixture
async def feedback_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Feedback.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _feedback(
    feedback_id: str,
    *,
    tenant_id: str,
    query: str,
    feedback: str,
    judge_halluc: float | None = None,
) -> Feedback:
    return Feedback(
        id=feedback_id,
        tenant_id=tenant_id,
        conversation_id=f"conversation-{feedback_id}",
        query=query,
        answer=f"answer-{feedback_id}",
        feedback=feedback,
        username=f"operator-{tenant_id}",
        judge_halluc=judge_halluc,
    )


@pytest.mark.asyncio
async def test_record_feedback_persists_authenticated_tenant(
    feedback_session_factory,
):
    async with feedback_session_factory() as db:
        await feedback_service.record_feedback(
            db,
            conversation_id="conversation-a",
            query="tenant-a question",
            answer="tenant-a answer",
            feedback="like",
            username="operator-a",
            tenant_id="tenant-a",
        )
        row = (await db.execute(select(Feedback))).scalar_one()

    assert row.tenant_id == "tenant-a"


@pytest.mark.asyncio
async def test_list_feedbacks_only_returns_current_tenant(
    feedback_session_factory,
):
    async with feedback_session_factory() as db:
        db.add_all([
            _feedback(
                "feedback-a-like",
                tenant_id="tenant-a",
                query="a-like",
                feedback="like",
            ),
            _feedback(
                "feedback-a-dislike",
                tenant_id="tenant-a",
                query="a-dislike",
                feedback="dislike",
            ),
            _feedback(
                "feedback-b-dislike",
                tenant_id="tenant-b",
                query="b-dislike",
                feedback="dislike",
            ),
        ])
        await db.commit()

        tenant_a = await feedback_service.list_feedbacks(
            db, tenant_id="tenant-a",
        )
        tenant_b = await feedback_service.list_feedbacks(
            db, feedback="dislike", tenant_id="tenant-b",
        )

    assert tenant_a["total"] == 2
    assert {row["id"] for row in tenant_a["list"]} == {
        "feedback-a-like",
        "feedback-a-dislike",
    }
    assert tenant_b["total"] == 1
    assert [row["id"] for row in tenant_b["list"]] == [
        "feedback-b-dislike",
    ]


@pytest.mark.asyncio
async def test_feedback_stats_are_tenant_scoped(
    feedback_session_factory, monkeypatch,
):
    from app.services import term_service

    monkeypatch.setattr(term_service, "_load_terms", lambda: {})
    async with feedback_session_factory() as db:
        db.add_all([
            _feedback(
                "feedback-a-like",
                tenant_id="tenant-a",
                query="a-like",
                feedback="like",
            ),
            _feedback(
                "feedback-a-dislike",
                tenant_id="tenant-a",
                query="a-dislike",
                feedback="dislike",
                judge_halluc=0.2,
            ),
            _feedback(
                "feedback-b-dislike-1",
                tenant_id="tenant-b",
                query="b-dislike",
                feedback="dislike",
                judge_halluc=0.9,
            ),
            _feedback(
                "feedback-b-dislike-2",
                tenant_id="tenant-b",
                query="b-dislike",
                feedback="dislike",
                judge_halluc=0.7,
            ),
        ])
        await db.commit()

        tenant_a = await feedback_service.feedback_stats(
            db, tenant_id="tenant-a",
        )
        tenant_b = await feedback_service.feedback_stats(
            db, tenant_id="tenant-b",
        )

    assert tenant_a["total"] == 2
    assert tenant_a["like"] == 1
    assert tenant_a["dislike"] == 1
    assert tenant_a["avgHallucination"] == 0.2
    assert tenant_a["topBadCases"] == [{"query": "a-dislike", "count": 1}]

    assert tenant_b["total"] == 2
    assert tenant_b["like"] == 0
    assert tenant_b["dislike"] == 2
    assert tenant_b["avgHallucination"] == 0.8
    assert tenant_b["topBadCases"] == [{"query": "b-dislike", "count": 2}]


@pytest.mark.asyncio
async def test_feedback_detail_hides_cross_tenant_primary_key(
    feedback_session_factory,
):
    async with feedback_session_factory() as db:
        db.add_all([
            _feedback(
                "feedback-a",
                tenant_id="tenant-a",
                query="tenant-a private question",
                feedback="dislike",
            ),
            _feedback(
                "feedback-b",
                tenant_id="tenant-b",
                query="tenant-b private question",
                feedback="dislike",
            ),
        ])
        await db.commit()

        own = await feedback_service.get_feedback(
            db, "feedback-a", tenant_id="tenant-a",
        )
        cross_tenant = await feedback_service.get_feedback(
            db, "feedback-a", tenant_id="tenant-b",
        )

    assert own is not None
    assert own["query"] == "tenant-a private question"
    assert cross_tenant is None


@pytest.mark.asyncio
async def test_golden_backflow_cannot_read_or_write_cross_tenant_feedback(
    feedback_session_factory, monkeypatch, tmp_path,
):
    golden_path = tmp_path / "golden_qa.json"
    monkeypatch.setattr(feedback_service, "_GOLDEN_PATH", golden_path)

    async with feedback_session_factory() as db:
        db.add_all([
            _feedback(
                "feedback-a",
                tenant_id="tenant-a",
                query="tenant-a private question",
                feedback="dislike",
            ),
            _feedback(
                "feedback-b",
                tenant_id="tenant-b",
                query="tenant-b private question",
                feedback="dislike",
            ),
        ])
        await db.commit()

        denied = await feedback_service.mark_golden(
            db, "feedback-a", tenant_id="tenant-b",
        )
        assert denied == {"added": False, "reason": "反馈不存在"}
        assert not golden_path.exists()

        added = await feedback_service.mark_golden(
            db, "feedback-b", tenant_id="tenant-b",
        )
        denied_again = await feedback_service.mark_golden(
            db, "feedback-a", tenant_id="tenant-b",
        )

    items = json.loads(golden_path.read_text(encoding="utf-8"))
    assert added["added"] is True
    assert denied_again == {"added": False, "reason": "反馈不存在"}
    assert [item["query"] for item in items] == ["tenant-b private question"]


@pytest.mark.asyncio
async def test_background_judge_update_is_tenant_scoped(
    feedback_session_factory, monkeypatch,
):
    async def fake_hallucination(*_args, **_kwargs):
        return {"supported_ratio": 0.75, "hallucination": 0.25}

    monkeypatch.setattr(session_module, "AsyncSessionLocal", feedback_session_factory)
    monkeypatch.setattr(judge, "judge_hallucination", fake_hallucination)

    async with feedback_session_factory() as db:
        row = _feedback(
            "feedback-a",
            tenant_id="tenant-a",
            query="tenant-a private question",
            feedback="dislike",
        )
        db.add(row)
        await db.commit()

    await feedback_service._judge_bg(
        "feedback-a",
        "tenant-a private question",
        "answer",
        tenant_id="tenant-b",
    )
    async with feedback_session_factory() as db:
        unchanged = await db.get(Feedback, "feedback-a")
        assert unchanged.judge_supported is None
        assert unchanged.judge_halluc is None

    await feedback_service._judge_bg(
        "feedback-a",
        "tenant-a private question",
        "answer",
        tenant_id="tenant-a",
    )
    async with feedback_session_factory() as db:
        updated = await db.get(Feedback, "feedback-a")
        assert updated.judge_supported == 0.75
        assert updated.judge_halluc == 0.25


@pytest.mark.asyncio
async def test_qa_feedback_routes_forward_authenticated_tenant(monkeypatch):
    from app.routers import qa

    captured: dict[str, str] = {}

    async def fake_record(_db, **kwargs):
        captured["record"] = kwargs["tenant_id"]

    async def fake_list(_db, *_args, **kwargs):
        captured["list"] = kwargs["tenant_id"]
        return {"total": 0, "list": []}

    async def fake_detail(_db, _feedback_id, **kwargs):
        captured["detail"] = kwargs["tenant_id"]
        return {"id": "feedback-a"}

    async def fake_stats(_db, **kwargs):
        captured["stats"] = kwargs["tenant_id"]
        return {"total": 0}

    async def fake_golden(_db, _feedback_id, **kwargs):
        captured["golden"] = kwargs["tenant_id"]
        return {"added": False, "reason": "already covered"}

    monkeypatch.setattr(qa.feedback_service, "record_feedback", fake_record)
    monkeypatch.setattr(qa.feedback_service, "list_feedbacks", fake_list)
    monkeypatch.setattr(qa.feedback_service, "get_feedback", fake_detail)
    monkeypatch.setattr(qa.feedback_service, "feedback_stats", fake_stats)
    monkeypatch.setattr(qa.feedback_service, "mark_golden", fake_golden)

    user = SimpleNamespace(
        id="user-a",
        username="operator-a",
        tenant_id="tenant-a",
        dept="ops",
        role="admin",
    )
    body = SimpleNamespace(
        traceId="trace-router-tenant-a",
        sources=[],
        retrievalSources="",
        conversationId="conversation-a",
        query="tenant-a question",
        answer="tenant-a answer",
        feedback="noop",
        reason="",
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/qa/feedback",
        "headers": [],
        "client": ("127.0.0.1", 12345),
    })

    await qa.feedback(request=request, body=body, db=object(), user=user)
    await qa.list_feedbacks(
        feedback="", page=1, size=20, db=object(), user=user,
    )
    await qa.get_feedback(
        feedback_id="feedback-a", db=object(), user=user,
    )
    await qa.feedback_stats(db=object(), user=user)
    await qa.mark_golden(
        feedback_id="feedback-a", db=object(), user=user,
    )

    assert captured == {
        "record": "tenant-a",
        "list": "tenant-a",
        "detail": "tenant-a",
        "stats": "tenant-a",
        "golden": "tenant-a",
    }
