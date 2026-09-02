"""Focused governance tests for Agent memory and the MCP tool bus."""

import asyncio
import json
import types

import pytest
from httpx import Response

from app.config import settings
from app.mcp.client import McpClient
from app.mcp.registry import McpRegistry, McpServerConfig
from app.schemas.system import AgentRunRequest
from app.services import agent_memory_service, agent_runtime, agent_tool_audit_service
from app.services.agent_memory_service import agent_memory
from app.services.agent_runtime import AgentResult, Persona, Tool, ToolRegistry, run_agent
from app.services.capability_context import CapabilityContext


class _Result:
    def __init__(self, rows=None):
        self.rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, query):
        self.queries.append(query)
        if len(self.queries) == 1:
            return _Result(self.rows)
        return _Result()

    async def commit(self):
        self.committed = True


class _AnswerProvider:
    async def chat_with_tools(self, _messages, _tools, **_kwargs):
        return {"content": "answer", "tool_calls": None}


def test_memory_write_consent_is_explicit():
    context = CapabilityContext.from_mapping(
        {"username": "alice", "tenant": "tenant-a"},
        trusted_agent_id="qa",
    )
    assert context.memory_write_enabled({}) is False
    assert context.memory_write_enabled({"memoryWrite": "false"}) is False
    assert context.memory_write_enabled({"memory_write": "yes"}) is True
    assert context.memory_read_enabled({}) is False
    assert context.memory_read_enabled({"memoryRead": True}) is True
    assert context.memory_read_enabled({"memoryWrite": True}) is True


def test_memory_service_stops_before_extraction_without_opt_in(monkeypatch):
    extracted = []

    async def fake_extract(*_args, **_kwargs):
        extracted.append(True)
        return []

    monkeypatch.setattr(agent_memory, "extract_facts", fake_extract)
    asyncio.run(agent_memory.extract_and_consolidate(
        "question",
        "answer",
        "alice",
        opt_in=False,
    ))
    assert extracted == []


def test_runtime_requires_global_and_request_memory_opt_in(monkeypatch):
    recalls = []
    writes = []

    async def fake_recall(*_args, **kwargs):
        recalls.append(kwargs)
        return ""

    async def fake_extract(*_args, **kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(agent_memory, "recall", fake_recall)
    monkeypatch.setattr(agent_memory, "extract_and_consolidate", fake_extract)
    monkeypatch.setattr(agent_runtime, "get_llm_provider", lambda _mt: _AnswerProvider())
    persona = Persona(name="qa", system_prompt="system", allowed_tools=[])

    async def scenario():
        monkeypatch.setattr(settings, "MEMORY_AUTO_SAVE_ENABLED", True)
        await run_agent(
            None,
            persona,
            "question",
            registry=ToolRegistry(),
            ctx={"username": "alice", "tenant": "tenant-a"},
        )
        await asyncio.sleep(0)
        assert recalls == []
        assert writes[-1]["opt_in"] is False

        await run_agent(
            None,
            persona,
            "question",
            registry=ToolRegistry(),
            ctx={
                "username": "alice",
                "tenant": "tenant-a",
                "memoryRead": True,
            },
        )
        await asyncio.sleep(0)
        assert len(recalls) == 1
        assert writes[-1]["opt_in"] is False

        monkeypatch.setattr(settings, "MEMORY_AUTO_SAVE_ENABLED", False)
        await run_agent(
            None,
            persona,
            "question",
            registry=ToolRegistry(),
            ctx={
                "username": "alice",
                "tenant": "tenant-a",
                "memoryWrite": True,
            },
        )
        await asyncio.sleep(0)
        assert len(recalls) == 2
        assert writes[-1]["opt_in"] is False

        monkeypatch.setattr(settings, "MEMORY_AUTO_SAVE_ENABLED", True)
        await run_agent(
            None,
            persona,
            "question",
            registry=ToolRegistry(),
            ctx={
                "username": "alice",
                "tenant": "tenant-a",
                "memoryWrite": True,
            },
        )
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert len(recalls) == 3
    assert len(writes) == 4
    assert [item["opt_in"] for item in writes] == [False, False, False, True]
    assert writes[-1]["tenant_id"] == "tenant-a"
    assert writes[-1]["agent_id"] == "qa"


def test_agent_run_request_defaults_and_scope_validation():
    body = AgentRunRequest(persona="qa", query="question")
    assert body.memoryRead is False
    assert body.memoryWrite is False
    assert body.memoryScope == "user"
    with pytest.raises(ValueError):
        AgentRunRequest(
            persona="qa",
            query="question",
            memoryScope="tenant",
        )


def test_agent_run_passes_only_validated_memory_context(monkeypatch):
    from app.routers import system as system_router

    captured = {}
    persona = Persona(name="qa", system_prompt="system", allowed_tools=[])

    async def fake_get_persona(_name):
        return persona

    async def fake_run_agent(_db, _persona, _query, _model_type, ctx):
        captured.update(ctx)
        return AgentResult(
            answer="answer",
            steps=[],
            iterations=1,
            degraded=False,
            degrade_reason=None,
            latency_ms=1,
            persona="qa",
            tools_used=[],
        )

    monkeypatch.setattr("app.services.persona_store.get_persona", fake_get_persona)
    monkeypatch.setattr("app.services.agent_runtime.run_agent", fake_run_agent)
    body = AgentRunRequest(
        persona="qa",
        query="question",
        memoryWrite=True,
        memoryScope="device",
    )
    user = types.SimpleNamespace(
        username="alice",
        tenant_id="tenant-a",
        role="operator",
    )

    asyncio.run(system_router.agent_run(body, None, user))
    assert captured == {
        "username": "alice",
        "tenant": "tenant-a",
        "role": "operator",
        "memoryRead": False,
        "memoryWrite": True,
        "memoryScope": "device",
    }


def test_tool_audit_route_forwards_authenticated_tenant(monkeypatch):
    from app.routers import system as system_router

    captured = {}

    async def fake_query(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"total": 0, "list": []}

    monkeypatch.setattr(system_router, "query_tool_calls", fake_query)
    user = types.SimpleNamespace(tenant_id="tenant-a")
    asyncio.run(system_router.agent_tool_calls(
        page=1,
        size=20,
        persona=None,
        tool=None,
        username=None,
        user=user,
    ))
    assert captured["kwargs"]["tenant"] == "tenant-a"


def test_memory_management_routes_forward_authenticated_tenant(monkeypatch):
    from app.routers import memory as memory_router

    captured = {}

    async def fake_list(**kwargs):
        captured["list"] = kwargs
        return {"total": 0, "list": []}

    async def fake_forget(memory_id, **kwargs):
        captured["forget"] = {"memory_id": memory_id, **kwargs}
        return True

    async def fake_stats(**kwargs):
        captured["stats"] = kwargs
        return {}

    monkeypatch.setattr(memory_router.agent_memory, "list_memories", fake_list)
    monkeypatch.setattr(memory_router.agent_memory, "forget", fake_forget)
    monkeypatch.setattr(memory_router.agent_memory, "get_stats", fake_stats)
    user = types.SimpleNamespace(tenant_id="tenant-a")

    async def scenario():
        await memory_router.list_memories(
            userId="alice",
            agentId="qa",
            scope="user",
            page=1,
            size=20,
            user=user,
        )
        await memory_router.delete_memory("memory-1", user=user)
        await memory_router.memory_stats(
            agentId="qa",
            scope="user",
            user=user,
        )

    asyncio.run(scenario())
    assert captured["list"]["tenant_id"] == "tenant-a"
    assert captured["forget"]["tenant_id"] == "tenant-a"
    assert captured["stats"]["tenant_id"] == "tenant-a"


def test_legacy_memory_is_not_recallable_even_with_matching_owner(monkeypatch):
    class _Redis:
        async def zrevrange(self, *_args):
            return ["legacy-id", "explicit-id"]

    rows = [
        types.SimpleNamespace(
            fact_id="legacy-id",
            fact_text="legacy must stay hidden",
            category="preference",
            write_mode="legacy",
        ),
        types.SimpleNamespace(
            fact_id="explicit-id",
            fact_text="explicit may be recalled",
            category="preference",
            write_mode="explicit",
        ),
    ]

    class _FilteringSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, query):
            sql = str(query.compile(compile_kwargs={"literal_binds": True}))
            assert "write_mode IN" in sql
            assert "'explicit'" in sql
            assert "'explicit_opt_in'" in sql
            return _Result([
                row
                for row in rows
                if row.write_mode in {"explicit", "explicit_opt_in"}
            ])

    async def fake_embed(_query):
        return [0.1, 0.2]

    monkeypatch.setattr(
        agent_memory_service,
        "AsyncSessionLocal",
        lambda: _FilteringSession(),
    )
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: _Redis())
    monkeypatch.setattr("app.services.embedding_service.embed_query", fake_embed)
    monkeypatch.setattr(
        "app.clients.milvus_client.search_memory",
        lambda *_args, **_kwargs: [],
    )

    text = asyncio.run(agent_memory.recall(
        "question",
        "alice",
        tenant_id="tenant-a",
        agent_id="qa",
        scope="user",
    ))
    assert "explicit may be recalled" in text
    assert "legacy must stay hidden" not in text


def test_vector_hits_use_database_owned_content_only(monkeypatch):
    class _Redis:
        async def zrevrange(self, *_args):
            return []

        async def zadd(self, *_args):
            return None

    owned_row = types.SimpleNamespace(
        fact_id="owned-id",
        fact_text="DB owned fact",
        category="preference",
    )
    session = _Session([owned_row])
    searched_owner_keys = []

    async def fake_embed(_query):
        return [0.1, 0.2]

    def fake_search(_vector, owner_key, topk=5):
        searched_owner_keys.append(owner_key)
        return [
            {
                "pk": "foreign-id",
                "text": "MALICIOUS cross-tenant text",
                "category": "diagnosis",
                "score": 0.99,
            },
            {
                "pk": "owned-id",
                "text": "stale vector text",
                "category": "preference",
                "score": 0.98,
            },
        ]

    monkeypatch.setattr(agent_memory_service, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: _Redis())
    monkeypatch.setattr("app.services.embedding_service.embed_query", fake_embed)
    monkeypatch.setattr("app.clients.milvus_client.search_memory", fake_search)

    text = asyncio.run(agent_memory.recall(
        "question",
        "same-user",
        tenant_id="tenant-a",
        agent_id="qa",
        scope="user",
    ))

    assert "DB owned fact" in text
    assert "MALICIOUS" not in text
    assert "stale vector text" not in text
    assert searched_owner_keys and searched_owner_keys[0] != "same-user"
    query_text = str(session.queries[0])
    for field in ("tenant_id", "user_id", "agent_id", "scope"):
        assert field in query_text


def test_mcp_registry_enforces_global_gate_and_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "MCP_SERVERS", json.dumps([
        {"name": "scada", "url": "http://scada"},
        {"name": "tickets", "url": "http://tickets"},
    ]))
    monkeypatch.setattr(settings, "MCP_PROVIDER_ALLOWLIST", "scada")
    registry = McpRegistry()
    assert asyncio.run(registry.load_from_config()) == 2

    monkeypatch.setattr(settings, "MCP_EXTERNAL_ENABLED", False)
    assert registry.list_enabled() == []

    monkeypatch.setattr(settings, "MCP_EXTERNAL_ENABLED", True)
    assert [server.name for server in registry.list_enabled()] == ["scada"]
    assert registry.is_provider_allowed("tickets") is False

    monkeypatch.setattr(settings, "MCP_SERVERS", "[]")
    assert asyncio.run(registry.load_from_config()) == 0
    assert registry.is_provider_allowed("scada") is False


def test_mcp_client_uses_configured_connect_and_operation_timeouts(monkeypatch):
    captured_timeouts = []

    class _HttpClient:
        def __init__(self, **kwargs):
            captured_timeouts.append(kwargs["timeout"])

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **_kwargs):
            if url.endswith("/list"):
                return Response(200, json={"tools": []})
            return Response(200, json={"result": "ok"})

    monkeypatch.setattr("app.mcp.client.httpx.AsyncClient", _HttpClient)
    monkeypatch.setattr(settings, "MCP_CONNECT_TIMEOUT_SECONDS", 1.25)
    monkeypatch.setattr(settings, "MCP_DISCOVERY_TIMEOUT_SECONDS", 4.5)
    monkeypatch.setattr(settings, "MCP_CALL_TIMEOUT_SECONDS", 9.5)
    client = McpClient()

    asyncio.run(client.discover([McpServerConfig(name="scada", url="http://scada")]))
    asyncio.run(client.call_tool("http://scada", "read", {}))

    assert captured_timeouts[0].connect == 1.25
    assert captured_timeouts[0].read == 4.5
    assert captured_timeouts[1].connect == 1.25
    assert captured_timeouts[1].read == 9.5


def test_denied_tool_call_is_audited_with_governance_fields(monkeypatch):
    audits = []

    async def fake_audit(**kwargs):
        audits.append(kwargs)

    async def handler(_db, _model_type, tenant=None):
        return f"unexpected:{tenant}"

    monkeypatch.setattr(
        "app.services.agent_tool_audit_service.log_tool_call",
        fake_audit,
    )
    registry = ToolRegistry()
    registry.register(Tool(
        "draft_ticket",
        "draft",
        {"type": "object"},
        handler,
        provider="builtin",
    ))

    async def scenario():
        result, error = await registry.run(
            None,
            None,
            "draft_ticket",
            {},
            ctx={
                "username": "bob",
                "tenant": "tenant-a",
                "role": "operator",
                "persona": "ops",
                "iter": 2,
            },
        )
        await asyncio.sleep(0)
        return result, error

    result, error = asyncio.run(scenario())
    assert error is True
    assert "权限不足" in result
    assert len(audits) == 1
    assert audits[0]["provider"] == "builtin"
    assert audits[0]["action_type"] == "write"
    assert audits[0]["denied_reason"] == "role_not_allowed"
    assert audits[0]["duration_ms"] >= 0


def test_audit_service_persists_governance_fields(monkeypatch):
    added = []

    class _AuditSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def add(self, row):
            added.append(row)

        async def commit(self):
            return None

    monkeypatch.setattr(
        agent_tool_audit_service,
        "AsyncSessionLocal",
        lambda: _AuditSession(),
    )
    asyncio.run(agent_tool_audit_service.log_tool_call(
        persona="ops",
        tool="draft_ticket",
        iter=2,
        args={},
        result="denied",
        error=True,
        username="alice",
        tenant="tenant-a",
        role="operator",
        provider="mcp:tickets",
        action_type="write",
        duration_ms=17,
        denied_reason="role_not_allowed",
    ))

    assert len(added) == 1
    assert added[0].provider == "mcp:tickets"
    assert added[0].action_type == "write"
    assert added[0].duration_ms == 17
    assert added[0].denied_reason == "role_not_allowed"
