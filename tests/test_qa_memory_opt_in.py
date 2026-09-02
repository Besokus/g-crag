"""QA Agent memory consent and cache-isolation contracts."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.qa import QaAnswerRequest
from app.services import qa_service


def test_qa_memory_scope_is_schema_restricted():
    assert QaAnswerRequest(query="q", memoryScope="device").memoryScope == "device"
    with pytest.raises(ValidationError):
        QaAnswerRequest(query="q", memoryScope="tenant")


@pytest.mark.asyncio
async def test_agent_memory_consent_bypasses_shared_cache(monkeypatch):
    cache_calls = []
    captured_ctx = {}

    async def fail_cache_get(_key):
        cache_calls.append("get")
        raise AssertionError("personalized memory runs must not read shared cache")

    async def fail_cache_set(*_args, **_kwargs):
        cache_calls.append("set")
        raise AssertionError("personalized memory runs must not write shared cache")

    async def create_conversation(_db, _username, _query):
        return SimpleNamespace(id="conversation-memory")

    async def save_message(*_args, **_kwargs):
        return None

    async def get_persona(_name):
        return SimpleNamespace(name="qa")

    async def run_agent(_db, _persona, _query, _model_type, *, ctx, on_step):
        captured_ctx.update(ctx)
        on_step({"tool": "search_regulation"})
        return SimpleNamespace(
            answer="结合长期记忆生成的答案",
            degraded=False,
            iterations=1,
            tools_used=["search_regulation"],
        )

    from app.services import agent_runtime, persona_store

    monkeypatch.setattr(qa_service.redis_client, "cache_get_json", fail_cache_get)
    monkeypatch.setattr(qa_service.redis_client, "cache_set_json", fail_cache_set)
    monkeypatch.setattr(
        qa_service.conversation_service,
        "create_conversation",
        create_conversation,
    )
    monkeypatch.setattr(
        qa_service.conversation_service,
        "save_message",
        save_message,
    )
    monkeypatch.setattr(persona_store, "get_persona", get_persona)
    monkeypatch.setattr(agent_runtime, "run_agent", run_agent)

    events = [
        event
        async for event in qa_service._stream_agent(
            None,
            "主变油温异常",
            None,
            None,
            "operator-a",
            "tenant-a",
            0.0,
            memory_read=True,
            memory_write=True,
            memory_scope="user",
            user_role="operator",
            trace_id="trace-memory-1",
        )
    ]

    assert cache_calls == []
    assert captured_ctx["memoryRead"] is True
    assert captured_ctx["memoryWrite"] is True
    assert captured_ctx["memoryScope"] == "user"
    assert captured_ctx["tenant"] == "tenant-a"
    assert captured_ctx["trace_id"] == "trace-memory-1"
    assert [event["type"] for event in events] == [
        "meta",
        "tool_step",
        "token",
        "done",
    ]
