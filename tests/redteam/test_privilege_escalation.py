"""红队回归：越权探测（离线）。

攻击面：低权限角色 / 被注入的 LLM 工具调用试图调用高风险工具（两票）、
伪造 tenant 参数跨租户取数、persona 配置声明越权工具清单。

当前真实防线（file:line 以调研时代码为准）：
- backend/app/core/permissions.py:50-74 ROLE_PERMISSIONS 角色→权限映射；76-90 has_perm
- backend/app/dependencies.py:32-42 require_perm（越权 → BizError 403）
- backend/app/services/agent_runtime.py:22-25 tool_permissions 高风险工具按 role 限制；
  72-81 ToolRegistry.run 角色检查；85-88 tenant/tenant_id/tenantId 保留参数剥离；
  91-105 非租户感知工具拒绝调用
- backend/app/services/realtime_event_service.py:68-79 PROACTIVE_READ_ONLY_TOOLS /
  _proactive_readonly_tools；734-737 process_proactive_run 将 persona.allowed_tools
  与只读集取交集（persona/DB 配置声明不能扩大工具面）
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core.permissions import DOC_DELETE, DOC_READ, SYSTEM_CONFIG, has_perm
from app.core.response import BizError
from app.services.agent_runtime import Persona, Tool, ToolRegistry
from app.services import realtime_event_service as service


def _echo_tool(registry: ToolRegistry, name: str, tenant_aware: bool = True) -> list:
    """注册记录调用参数的假工具；返回 captured 列表。"""
    captured: list = []

    async def handler(db, model_type, message="", tenant=None):
        captured.append({"message": message, "tenant": tenant})
        return "ok"

    if tenant_aware:
        registry.register(Tool(
            name=name, description="echo", parameters={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            handler=handler,
        ))
    else:
        async def bare_handler(db, model_type, message=""):
            captured.append({"message": message})
            return "ok"
        registry.register(Tool(
            name=name, description="bare", parameters={
                "type": "object",
                "properties": {"message": {"type": "string"}},
            },
            handler=bare_handler,
        ))
    return captured


def _tool_with_agent_permission(registry: ToolRegistry, name: str) -> list:
    """注册一个命中 agent_runtime.tool_permissions 的高风险工具名（draft_ticket 等）。"""
    captured: list = []

    async def handler(db, model_type, message="", tenant=None):
        captured.append(message)
        return "ticket-draft"

    registry.register(Tool(
        name=name, description="high risk", parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}},
        },
        handler=handler,
    ))
    return captured


# ===== agent 工具的角色限制（注入/诱导也绕不过 role 检查）=====


@pytest.mark.asyncio
async def test_draft_ticket_denied_for_operator():
    registry = ToolRegistry()
    captured = _tool_with_agent_permission(registry, "draft_ticket")
    result, err = await registry.run(
        None, None, "draft_ticket", {"message": "开票"},
        ctx={"role": "operator", "username": "op1", "tenant": "t1"},
    )
    assert err is True
    assert "权限不足" in result
    assert captured == []                      # 工具 handler 未执行


@pytest.mark.asyncio
async def test_draft_ticket_allowed_for_admin_only():
    registry = ToolRegistry()
    captured = _tool_with_agent_permission(registry, "draft_ticket")
    result, err = await registry.run(
        None, None, "draft_ticket", {"message": "开票"},
        ctx={"role": "admin", "username": "ad1", "tenant": "t1"},
    )
    assert err is False
    assert result == "ticket-draft"
    assert captured == ["开票"]


@pytest.mark.parametrize(
    ("tool", "role", "allowed"),
    [
        ("draft_ticket", "editor", False),      # draft_ticket 仅 admin（tool_permissions）
        ("create_ticket", "operator", False),
        ("create_ticket", "editor", True),
        ("submit_ticket", "auditor", False),
    ],
)
@pytest.mark.asyncio
async def test_tool_role_matrix(tool, role, allowed):
    registry = ToolRegistry()
    captured = _tool_with_agent_permission(registry, tool)
    result, err = await registry.run(
        None, None, tool, {}, ctx={"role": role, "username": "u", "tenant": "t1"},
    )
    assert err is not allowed
    assert (captured == []) is not allowed


@pytest.mark.asyncio
async def test_persona_declaration_cannot_grant_tool_role():
    """persona 声明 draft_ticket 不等于授权：operator 角色仍被 tool_permissions 拒绝。"""
    persona = Persona(
        name="rogue", system_prompt="你可以开票",   # 即使 prompt 诱导声明
        allowed_tools=["draft_ticket"],
    )
    assert "draft_ticket" in persona.allowed_tools
    registry = ToolRegistry()
    captured = _tool_with_agent_permission(registry, "draft_ticket")
    result, err = await registry.run(
        None, None, "draft_ticket", {},
        ctx={"role": "operator", "persona": "rogue", "username": "u", "tenant": "t1"},
    )
    assert err is True and "权限不足" in result and captured == []


@pytest.mark.asyncio
async def test_ctx_none_skips_permission_check_documented_gap():
    """【缺口固化】ctx=None 时跳过权限与审计：draft_ticket 直通（老链路零回归设计）。

    红队视角：任何新链路忘记传 ctx 即无工具权限检查（见 docs/redteam/README.md 缺口清单）。
    本用例固化现状，若未来改为 fail-closed 请同步更新缺口清单。
    """
    registry = ToolRegistry()
    captured = _tool_with_agent_permission(registry, "draft_ticket")
    result, err = await registry.run(None, None, "draft_ticket", {"message": "x"}, ctx=None)
    assert err is False
    assert captured == ["x"]


# ===== LLM 工具参数不可伪造租户 =====


@pytest.mark.asyncio
async def test_llm_tool_args_cannot_override_reserved_tenant():
    registry = ToolRegistry()
    captured = _echo_tool(registry, "search_regulation")
    result, err = await registry.run(
        None, None, "search_regulation",
        {"message": "q", "tenant": "victim-tenant", "tenant_id": "other"},
        ctx={"role": "operator", "username": "u", "tenant": "trusted"},
    )
    assert err is False
    assert captured[0]["tenant"] == "trusted"   # 保留参数被剥离并强制回填 ctx 租户


@pytest.mark.asyncio
async def test_non_tenant_aware_tool_rejected_under_ctx():
    registry = ToolRegistry()
    captured = _echo_tool(registry, "legacy_tool", tenant_aware=False)
    result, err = await registry.run(
        None, None, "legacy_tool", {"message": "q"},
        ctx={"role": "admin", "username": "u", "tenant": "t1"},
    )
    assert err is True
    assert "未声明租户隔离能力" in result
    assert captured == []


# ===== 主动诊断：persona 工具清单与只读白名单取交集，不可被声明绕过 =====


@pytest.mark.asyncio
async def test_proactive_run_intersects_persona_tools_with_readonly_whitelist(test_db, monkeypatch):
    """即使 persona（code/DB）声明 create_ticket/draft_ticket，主动诊断也只保留只读工具。"""
    from app.models.realtime_event import ProactiveOpsRun, RealtimeEvent

    event = RealtimeEvent(
        tenant_id="t1", event_id="E-RO-1", source="generic", event_type="temperature_alarm",
        severity="critical", title="油温越限", summary="顶层油温96℃",
        occurred_at=datetime(2026, 9, 1),
        payload_json="{}", normalized_json='{"measurements": {}}',
        processing_status="queued", rule_decision="trigger",
    )
    test_db.add(event)
    await test_db.flush()
    run = ProactiveOpsRun(tenant_id="t1", event_ref_id=event.id, status="queued")
    test_db.add(run)
    await test_db.commit()

    rogue_persona = Persona(
        name="alert",
        system_prompt="处置该告警",
        allowed_tools=["search_regulation", "create_ticket", "draft_ticket", "search_similar_case"],
    )

    async def fake_get_persona(name):
        assert name == "alert"                  # 默认 v2 关 = alert persona
        return rogue_persona

    captured: dict = {}

    async def fake_run_agent(db, persona, user_msg, model_type=None, **kwargs):
        captured["persona"] = persona
        captured["ctx"] = kwargs.get("ctx")
        return SimpleNamespace(
            answer={"summary": "检查冷却系统", "handling": "", "risks": [], "ticket": {
                "ticketType": "遥控执行票", "steps": ["立即合闸"],
            }},
            steps=[], tools_used=["search_regulation"], iterations=1,
            degraded=False, degrade_reason=None, latency_ms=1,
        )

    import app.services.agent_runtime as agent_runtime
    import app.services.persona_store as persona_store
    monkeypatch.setattr(persona_store, "get_persona", fake_get_persona)
    monkeypatch.setattr(agent_runtime, "run_agent", fake_run_agent)

    await service.process_proactive_run(test_db, run.id, tenant_id="t1")

    readonly = service._proactive_readonly_tools()
    assert set(captured["persona"].allowed_tools) <= readonly
    assert "create_ticket" not in captured["persona"].allowed_tools
    assert "draft_ticket" not in captured["persona"].allowed_tools
    # ctx 固定只读角色（operator），两票工具即使遗留也会被 tool_permissions 拒
    assert captured["ctx"]["role"] == "operator"
    # 注入诱导的票据类型被白名单归一化，且产出恒为只读草稿
    import json as _json
    run_row = await test_db.get(ProactiveOpsRun, run.id)
    ticket = _json.loads(run_row.ticket_draft_json)
    assert ticket["ticketType"] == "操作票"
    assert "仅为草稿" in ticket["notes"]
    recommendation = _json.loads(run_row.recommendation_json)
    assert recommendation["readOnly"] is True
    assert recommendation["controlExecuted"] is False
    assert recommendation["requiresHumanReview"] is True


def test_readonly_whitelist_never_contains_action_tools():
    """只读白名单本身不含任何两票/控制类工具（防线面收窄的基线）。"""
    actionish = {"create_ticket", "submit_ticket", "draft_ticket", "execute_control"}
    assert not (service.PROACTIVE_READ_ONLY_TOOLS & actionish)
    assert service._proactive_readonly_tools() <= (
        service.PROACTIVE_READ_ONLY_TOOLS | {"query_telemetry"}
    )


# ===== RBAC 权限点矩阵 + require_perm =====


def test_rbac_matrix_blocks_privilege_escalation():
    for role in ("operator", "auditor"):
        assert has_perm(role, DOC_DELETE) is False
        assert has_perm(role, SYSTEM_CONFIG) is False
    assert has_perm("operator", DOC_READ) is True
    assert has_perm("admin", SYSTEM_CONFIG) is True     # "*" 全权
    assert has_perm("unknown-role", DOC_READ) is False  # 未定义角色一无所有


@pytest.mark.asyncio
async def test_require_perm_rejects_without_permission():
    from app.dependencies import require_perm

    checker = require_perm(DOC_DELETE)
    with pytest.raises(BizError) as exc:
        await checker(user=SimpleNamespace(role="operator"))
    assert exc.value.code == 403
    assert "doc:delete" in exc.value.message

    user = SimpleNamespace(role="admin")
    assert (await checker(user=user)) is user
