"""ops_planner persona 单测。

注册真身是 persona_store._CODE_PERSONAS（模块级求值，读 settings.TICKET_ACTION_LOOP_ENABLE）。
验证方式：patch 共享 settings 实例的属性（reload 后仍在同一对象上生效）+ importlib.reload。
注意：开关测试须放在文件末尾，最后一个用例以关态 reload 收尾，让模块回到默认状态。
"""
import importlib


def test_ops_planner_defined():
    from app.services.agent_personas import OPS_PLANNER_PERSONA
    assert OPS_PLANNER_PERSONA.name == "ops_planner"
    assert OPS_PLANNER_PERSONA.output_format == "json"
    assert "create_ticket" in OPS_PLANNER_PERSONA.allowed_tools
    assert "submit_ticket" in OPS_PLANNER_PERSONA.allowed_tools
    assert "search_regulation" in OPS_PLANNER_PERSONA.allowed_tools
    assert OPS_PLANNER_PERSONA.fallback is not None


def test_ops_planner_registered_when_enabled(monkeypatch):
    import app.services.persona_store as store
    monkeypatch.setattr(store.settings, "TICKET_ACTION_LOOP_ENABLE", True)
    importlib.reload(store)
    assert "ops_planner" in store._CODE_PERSONAS


def test_ops_planner_not_registered_when_disabled(monkeypatch):
    """开关关不注册（关=现状）；收尾恢复模块默认态。"""
    import app.services.persona_store as store
    monkeypatch.setattr(store.settings, "TICKET_ACTION_LOOP_ENABLE", False)
    importlib.reload(store)
    assert "ops_planner" not in store._CODE_PERSONAS
    assert "diagnose" in store._CODE_PERSONAS
