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


# ---------- proactive_diagnosis（主动运维结构化诊断）----------


def test_proactive_diagnosis_defined():
    from app.services.agent_personas import PROACTIVE_DIAGNOSIS_PERSONA
    assert PROACTIVE_DIAGNOSIS_PERSONA.name == "proactive_diagnosis"
    assert PROACTIVE_DIAGNOSIS_PERSONA.output_format == "json"
    assert set(PROACTIVE_DIAGNOSIS_PERSONA.allowed_tools) == {
        "search_regulation", "query_equipment_graph",
        "search_similar_case", "query_telemetry",
    }
    # schema v2 JSON 约定写在 prompt 里；证据不足如实说明 + 高风险标 risks 是硬约束
    assert "proactive-recommendation/v2" in PROACTIVE_DIAGNOSIS_PERSONA.system_prompt
    assert "证据不足" in PROACTIVE_DIAGNOSIS_PERSONA.system_prompt
    assert "risks" in PROACTIVE_DIAGNOSIS_PERSONA.system_prompt
    assert "只读" in PROACTIVE_DIAGNOSIS_PERSONA.system_prompt
    assert PROACTIVE_DIAGNOSIS_PERSONA.fallback is not None


def test_proactive_diagnosis_registered_when_enabled(monkeypatch):
    import app.services.persona_store as store
    monkeypatch.setattr(store.settings, "PROACTIVE_SCHEMA_V2_ENABLE", True)
    # CI 无 .env 时 TICKET_ACTION_LOOP_ENABLE 默认 False；显式开保证 ops_planner 断言环境无关
    monkeypatch.setattr(store.settings, "TICKET_ACTION_LOOP_ENABLE", True)
    importlib.reload(store)
    assert "proactive_diagnosis" in store._CODE_PERSONAS
    assert "ops_planner" in store._CODE_PERSONAS  # 既有开关不受影响


def test_proactive_diagnosis_not_registered_when_disabled(monkeypatch):
    """开关关不注册（关=现状）；本文件最后一个用例以关态 reload 收尾，恢复模块默认态。"""
    import app.services.persona_store as store
    monkeypatch.setattr(store.settings, "PROACTIVE_SCHEMA_V2_ENABLE", False)
    importlib.reload(store)
    assert "proactive_diagnosis" not in store._CODE_PERSONAS
    assert "alert" in store._CODE_PERSONAS
