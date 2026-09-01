# 问答→行动闭环（工单工作流打通）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通"问答/诊断 → 工单自动起草 → 分级审批 → 流转事件"行动闭环：Agent 新增 `create_ticket`/`submit_ticket` 落库工具 + `ops_planner` 检修计划 persona；工单签发/完成时发质量事件；审批/签发等敏感操作拆出 `TICKET_MANAGE` 权限。总开关 `TICKET_ACTION_LOOP_ENABLE=False`（关=现状）。

**Architecture:** 全部复用既有骨架——`ticket_lifecycle_service` 状态机、`agent_tools.build_default_registry` 工具注册、`agent_runtime.Persona`、`quality_event_bus.emit`、`tool_permissions` 高风险工具 role 限制。唯一新概念是权限常量 `TICKET_MANAGE`。spec 见 `docs/superpowers/specs/2026-08-29-qa-action-loop-ticket-design.md`。

**Tech Stack:** FastAPI / Pydantic v2 / SQLAlchemy async / Vue 3 / pytest（同步测试包 `asyncio.run`，sqlite `test_db` fixture）

## Global Constraints

- 后端测试：`venv/Scripts/python.exe -m pytest tests/<file> -v`（conftest 已把 backend 加 sys.path）；CI 兼容用例不碰 Milvus/embedding/LLM（mock 下游 service）
- **后端运行不带 `--reload`**，改完手动重启；backend 源码烤在镜像里，容器化验证需 `docker compose up -d --build backend`
- 复用既有模式：`degraded(tag,e)` / `success` / `write_log` / `@limiter.limit` / `require_perm`；BizError → HTTP 200
- 开关关=现状：工具不注册、persona 不注册、事件不 emit。**权限改造不受开关**（安全边界不能开关化），上线说明需明示 operator 失去审批权
- `create_ticket`/`submit_ticket` 是高风险工具：注册进 `agent_runtime.tool_permissions`（限 admin/editor）
- 工具 handler 签名统一 `(db, model_type, ..., tenant=None) -> str`；失败返回错误串不抛（Registry 已做 per-tool 异常隔离）
- 全量回归：`venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration"`；lint `ruff check backend tests`

## File Structure

- **Modify:** `backend/app/config.py` — `TICKET_ACTION_LOOP_ENABLE=False`
- **Modify:** `.env.example` — 同步开关
- **Modify:** `backend/app/core/permissions.py` — `TICKET_MANAGE` + editor 角色映射
- **Modify:** `backend/app/services/agent_runtime.py` — `tool_permissions` 加两条
- **Modify:** `backend/app/services/agent_tools.py` — `_t_create_ticket`/`_t_submit_ticket` + 条件注册
- **Modify:** `backend/app/services/agent_personas.py` — `OPS_PLANNER_PERSONA` + 条件注册
- **Modify:** `backend/app/services/ticket_lifecycle_service.py` — issue/complete emit 事件
- **Modify:** `backend/app/routers/domain.py` — review/issue/start/complete/archive 改 `require_perm(TICKET_MANAGE)`
- **Modify:** `frontend/src/utils/perm.js` — 镜像 `ticket:manage`
- **Modify:** `frontend/src/views/TicketLifecycle.vue` — 审批/签发/执行按钮按 `ticket:manage` 显隐
- **Modify:** `frontend/src/views/Diagnose.vue` — 诊断卡片「⚙ 生成工单」按钮 + 预填弹窗（Chat.vue 同款逻辑留 Task 6 可选）
- **Test:** `tests/test_permissions.py`（追加）、`tests/test_agent_tools.py`（新建）、`tests/test_agent_personas.py`（新建/追加）、`tests/test_ticket_lifecycle.py`（追加）

---

### Task 1: 配置开关 + 权限常量 + 工具 role 限制

**Files:**
- Modify: `backend/app/config.py`、`.env.example`、`backend/app/core/permissions.py`、`backend/app/services/agent_runtime.py`
- Test: `tests/test_permissions.py`（追加）

**Interfaces:**
- Produces: `settings.TICKET_ACTION_LOOP_ENABLE: bool = False`；`permissions.TICKET_MANAGE = "ticket:manage"`（editor 默认持有）；`agent_runtime.tool_permissions["create_ticket"|"submit_ticket"] = ["admin","editor"]`

- [x] **Step 1: 写失败测试**

`tests/test_permissions.py` 追加：

```python
def test_ticket_manage_role_mapping():
    from app.core.permissions import ROLE_PERMISSIONS, TICKET_MANAGE
    assert TICKET_MANAGE == "ticket:manage"
    assert TICKET_MANAGE in ROLE_PERMISSIONS["editor"]
    assert TICKET_MANAGE not in ROLE_PERMISSIONS["operator"]
    assert TICKET_MANAGE not in ROLE_PERMISSIONS["auditor"]


def test_ticket_tools_role_restricted():
    from app.services.agent_runtime import tool_permissions
    assert tool_permissions.get("create_ticket") == ["admin", "editor"]
    assert tool_permissions.get("submit_ticket") == ["admin", "editor"]
```

- [x] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_permissions.py -v`
Expected: FAIL（`ImportError/AttributeError: TICKET_MANAGE`）

- [x] **Step 3: 实现**

1. `backend/app/config.py` 在其他 `*_ENABLE` 开关区追加（保持"关=现状"注释风格）：
```python
    # 问答→行动闭环：Agent 工具 create_ticket/submit_ticket + ops_planner persona + 工单流转事件（关=现状/开=工单闭环打通）
    TICKET_ACTION_LOOP_ENABLE: bool = False
```
2. `.env.example` 同步：`TICKET_ACTION_LOOP_ENABLE=False`
3. `backend/app/core/permissions.py` 权限常量区（`DOMAIN_USE` 之后）追加：
```python
TICKET_MANAGE = "ticket:manage"      # 两票审批/签发/执行/归档（分级审批：operator 只能起草提交）
```
并在 `ROLE_PERMISSIONS["editor"]` 集合中追加 `TICKET_MANAGE`（operator/auditor 不加）。
4. `backend/app/services/agent_runtime.py` 的 `tool_permissions` 追加：
```python
    "create_ticket": ["admin", "editor"],
    "submit_ticket": ["admin", "editor"],
```

- [x] **Step 4: 运行确认通过 + lint**

Run: `venv/Scripts/python.exe -m pytest tests/test_permissions.py -v` → PASS；`ruff check backend tests` 无新告警

- [x] **Step 5: Commit**

```bash
git add backend/app/config.py .env.example backend/app/core/permissions.py backend/app/services/agent_runtime.py tests/test_permissions.py
git commit -m "feat(ticket): TICKET_ACTION_LOOP_ENABLE 开关 + TICKET_MANAGE 权限 + 高风险工单工具 role 限制"
```

---

### Task 2: `create_ticket` / `submit_ticket` agent 工具（条件注册）

**Files:**
- Modify: `backend/app/services/agent_tools.py`
- Test: `tests/test_agent_tools.py`（新建）

**Interfaces:**
- Consumes: `ticket_lifecycle_service.create_ticket(db, ticket_type=, task=, device=, steps=, safety=, risks=, source_ref=, creator=, tenant=)` / `ticket_lifecycle_service.submit_for_review(db, ticket_id, tenant=)`
- Produces: 工具 `create_ticket`（schema：task/device/ticketType/steps/safety/risks/sourceRef）、`submit_ticket`（schema：ticketId）；开关关时不在 `DEFAULT_REGISTRY`

- [x] **Step 1: 写失败测试**

`tests/test_agent_tools.py`：

```python
"""行动闭环工具单测：create_ticket / submit_ticket + 开关条件注册。"""
import asyncio
from types import SimpleNamespace


def _settings(monkeypatch, enable: bool):
    import app.services.agent_tools as svc
    monkeypatch.setattr(svc, "settings", SimpleNamespace(TICKET_ACTION_LOOP_ENABLE=enable))


def test_create_ticket_tool(monkeypatch):
    import app.services.agent_tools as svc
    from app.services import ticket_lifecycle_service as tl
    _settings(monkeypatch, True)

    async def fake_create(db, **kw):
        assert kw["source_ref"] == "qa:conv-1"
        return {"id": "t123", "title": kw["task"][:200], "status": "draft"}
    monkeypatch.setattr(tl, "create_ticket", fake_create)
    out = asyncio.run(svc._t_create_ticket(
        None, None, task="1号主变由运行转检修", device="1号主变",
        steps=["停电", "验电"], safety=["戴绝缘手套"], risks=["触电"],
        sourceRef="qa:conv-1", tenant="default", creator="alice"))
    assert "t123" in out and "草稿" in out


def test_create_ticket_requires_task(monkeypatch):
    import app.services.agent_tools as svc
    _settings(monkeypatch, True)
    out = asyncio.run(svc._t_create_ticket(None, None, task="", tenant=None))
    assert "task 不能为空" in out


def test_submit_ticket_tool(monkeypatch):
    import app.services.agent_tools as svc
    from app.services import ticket_lifecycle_service as tl
    _settings(monkeypatch, True)

    async def fake_submit(db, ticket_id, *, tenant="default"):
        return {"id": ticket_id, "status": "reviewed", "reviewScore": 92}
    monkeypatch.setattr(tl, "submit_for_review", fake_submit)
    out = asyncio.run(svc._t_submit_ticket(None, None, ticketId="t1", tenant="default"))
    assert "92" in out and "reviewed" in out


def test_registry_flag_off(monkeypatch):
    """开关关：新工具不注册，老工具不受影响。"""
    import importlib
    import app.services.agent_tools as svc
    _settings(monkeypatch, False)
    importlib.reload(svc)
    assert svc.DEFAULT_REGISTRY.get("create_ticket") is None
    assert svc.DEFAULT_REGISTRY.get("submit_ticket") is None
    assert svc.DEFAULT_REGISTRY.get("draft_ticket") is not None


def test_registry_flag_on(monkeypatch):
    import importlib
    import app.services.agent_tools as svc
    _settings(monkeypatch, True)
    importlib.reload(svc)
    assert svc.DEFAULT_REGISTRY.get("create_ticket") is not None
    assert svc.DEFAULT_REGISTRY.get("submit_ticket") is not None
```

注意：`build_default_registry` 目前用 `DEFAULT_REGISTRY = build_default_registry()` 模块级求值——开关读取必须走模块内 `settings`（`from app.config import settings`），测试用 `monkeypatch.setattr(svc, "settings", ...)` + `importlib.reload` 验证两种状态。

- [x] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_agent_tools.py -v`
Expected: FAIL（`AttributeError: ... has no attribute '_t_create_ticket'`）

- [x] **Step 3: 实现**

`backend/app/services/agent_tools.py`：

1. import 区追加：
```python
from app.config import settings
from app.services import ticket_lifecycle_service
```
2. `_t_draft_ticket` 之后追加两个 handler：
```python
async def _t_create_ticket(db, model_type, task, device="", ticketType="操作票",
                           steps=None, safety=None, risks=None,
                           sourceRef="", tenant=None, creator="", ctx_user=""):
    """把处置方案落库成两票草稿（source_ref 幂等，重复创建返回既有票）。"""
    task = (task or "").strip()
    if not task:
        return "创建失败：task 不能为空"
    kwargs = {"tenant": tenant} if tenant else {}
    t = await ticket_lifecycle_service.create_ticket(
        db, ticket_type=ticketType or "操作票", task=task, device=device or "",
        steps=steps or [], safety=safety or [], risks=risks or [],
        source_ref=sourceRef or None, creator=creator or ctx_user, **kwargs)
    return f"已创建工单 {t['id']}（状态:{t['status']}），标题:{t['title']}。可调用 submit_ticket 提交审核"


async def _t_submit_ticket(db, model_type, ticketId, tenant=None):
    """提交工单进审核（自动跑审核引擎，高分自动初审通过）。"""
    kwargs = {"tenant": tenant} if tenant else {}
    try:
        t = await ticket_lifecycle_service.submit_for_review(db, ticketId, **kwargs)
    except ValueError as e:
        return f"提交失败：{e}"
    return (f"工单 {t['id']} 已提交审核，状态:{t['status']}，"
            f"审核得分:{t.get('reviewScore', 0)}")
```
3. schema（`_SCHEMA_TASK` 之后追加）：
```python
_SCHEMA_CREATE_TICKET = {"type": "object",
                         "properties": {
                             "task": {"type": "string", "description": "操作任务，如 '1号主变由运行转检修'"},
                             "device": {"type": "string", "description": "设备名"},
                             "ticketType": {"type": "string", "enum": ["操作票", "工作票"], "description": "票据类型，默认操作票"},
                             "steps": {"type": "array", "items": {"type": "string"}, "description": "操作步骤列表"},
                             "safety": {"type": "array", "items": {"type": "string"}, "description": "安全措施列表"},
                             "risks": {"type": "array", "items": {"type": "string"}, "description": "风险/危险点列表"},
                             "sourceRef": {"type": "string", "description": "来源关联（如 qa:会话id），幂等键"}},
                         "required": ["task"]}
_SCHEMA_SUBMIT_TICKET = {"type": "object",
                         "properties": {"ticketId": {"type": "string", "description": "工单 id"}},
                         "required": ["ticketId"]}
```
4. `build_default_registry()` 末尾、`return reg` 前追加（条件注册）：
```python
    if settings.TICKET_ACTION_LOOP_ENABLE:
        reg.register(Tool("create_ticket",
                          "把已确定的处置方案落库成两票草稿。仅当用户明确要求生成/创建工单时调用。",
                          _SCHEMA_CREATE_TICKET, _t_create_ticket))
        reg.register(Tool("submit_ticket",
                          "把工单提交审核（自动跑审核引擎）。create_ticket 成功后按用户要求调用。",
                          _SCHEMA_SUBMIT_TICKET, _t_submit_ticket))
```

- [x] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_agent_tools.py tests/test_agent_runtime.py -v`
Expected: PASS（新 5 个 + 既有 agent runtime 回归不破——注意 `importlib.reload` 不影响其他测试文件各自 import）

- [x] **Step 5: Commit**

```bash
git add backend/app/services/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(ticket): create_ticket/submit_ticket agent 工具（开关条件注册 + source_ref 幂等）"
```

---

### Task 3: 工单流转事件（issue/complete → quality_event_bus）

**Files:**
- Modify: `backend/app/services/ticket_lifecycle_service.py`
- Test: `tests/test_ticket_lifecycle.py`（追加）

**Interfaces:**
- Produces: `issue_ticket` 成功后 emit `("ticket-lifecycle", "ticket.issued", payload)`；`complete_execution` 成功后 emit `("ticket-lifecycle", "ticket.completed", payload)`。payload=`{ticketId, task, device, steps, executionLog?, deviation?, creator}`。仅 `TICKET_ACTION_LOOP_ENABLE=True` 时发；emit 异常 `degraded("ticket_event_emit", e)` 不阻塞流转。

- [x] **Step 1: 写失败测试**

`tests/test_ticket_lifecycle.py` 追加（复用文件内既有 `test_db` fixture 建票辅助；若无则用 `create_ticket` 建票）：

```python
def test_issue_emits_event(test_db, monkeypatch):
    import app.services.ticket_lifecycle_service as tl
    events = []
    async def fake_emit(source, type, payload=None, tenant=None):
        events.append((source, type, payload))
    monkeypatch.setattr(tl, "_emit_ticket_event", fake_emit)
    t = asyncio.run(tl.create_ticket(test_db, task="主变检修", device="1号主变", creator="a"))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True, reviewer="r"))
    asyncio.run(tl.issue_ticket(test_db, t["id"], issuer="i"))
    assert events and events[0][1] == "ticket.issued"
    assert events[0][2]["ticketId"] == t["id"]


def test_complete_emits_event(test_db, monkeypatch):
    import app.services.ticket_lifecycle_service as tl
    events = []
    async def fake_emit(source, type, payload=None, tenant=None):
        events.append(type)
    monkeypatch.setattr(tl, "_emit_ticket_event", fake_emit)
    t = asyncio.run(tl.create_ticket(test_db, task="x"))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True))
    asyncio.run(tl.issue_ticket(test_db, t["id"]))
    asyncio.run(tl.start_execution(test_db, t["id"]))
    asyncio.run(tl.complete_execution(test_db, t["id"], log="完成", deviation="无"))
    assert "ticket.completed" in events


def test_emit_disabled_by_flag(test_db, monkeypatch):
    import app.services.ticket_lifecycle_service as tl
    called = []
    monkeypatch.setattr(tl.settings, "TICKET_ACTION_LOOP_ENABLE", False)
    async def fake_bus_emit(*a, **kw): called.append(a)
    monkeypatch.setattr(tl.quality_event_bus, "emit", fake_bus_emit)
    t = asyncio.run(tl.create_ticket(test_db, task="y"))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True))
    asyncio.run(tl.issue_ticket(test_db, t["id"]))
    assert called == []
```

- [x] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_lifecycle.py -v`
Expected: FAIL（`AttributeError: ... '_emit_ticket_event'`）

- [x] **Step 3: 实现**

`backend/app/services/ticket_lifecycle_service.py`：

1. import 区追加：
```python
from app.config import settings
from app.services import quality_event_bus
```
2. 内部辅助区（`_build_ticket_text` 前）追加：
```python
async def _emit_ticket_event(event_type: str, t: Ticket, tenant: str = "default"):
    """流转事件：开关开才发；失败 degraded 不阻塞流转。"""
    if not settings.TICKET_ACTION_LOOP_ENABLE:
        return
    try:
        await quality_event_bus.emit(
            source="ticket-lifecycle", type=event_type,
            payload={"ticketId": t.id, "task": t.task, "device": t.device,
                     "steps": _parse_json(t.steps, []),
                     "executionLog": t.execution_log or "",
                     "deviation": t.deviation or "",
                     "creator": t.creator or ""},
            tenant=tenant)
    except Exception as e:
        degraded("ticket_event_emit", e)
```
3. `issue_ticket` 的 `await db.refresh(t)` 前追加：`await _emit_ticket_event("ticket.issued", t, tenant)`；
   `complete_execution` 的 `await db.refresh(t)` 前追加：`await _emit_ticket_event("ticket.completed", t, tenant)`。

- [x] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_ticket_lifecycle.py -v` → PASS（新 3 + 既有全过）

- [x] **Step 5: Commit**

```bash
git add backend/app/services/ticket_lifecycle_service.py tests/test_ticket_lifecycle.py
git commit -m "feat(ticket): 签发/完成 emit 质量事件（开关控制，失败降级不阻塞）"
```

---

### Task 4: `ops_planner` persona（检修计划规划）

**Files:**
- Modify: `backend/app/services/agent_personas.py`
- Test: `tests/test_agent_personas.py`（新建或追加）

**Interfaces:**
- Consumes: Task 2 工具 + 既有 4 工具名
- Produces: `OPS_PLANNER_PERSONA`（name=`ops_planner`，output_format=json，工具集 6 个），开关关时不进 persona 注册表。经 `persona_store.get_persona("ops_planner")` → `POST /system/agent/run` 自动可用。

先 **Read `backend/app/services/persona_store.py`** 确认 code persona 注册方式（静态 dict 或注册函数），按实际结构调整注册点；若 `get_persona` 只查 DB 则需在该文件补 code-persona 合并逻辑（预期已支持——alert/diagnose persona 同机制）。

- [x] **Step 1: 写失败测试**

`tests/test_agent_personas.py`：

```python
"""ops_planner persona 单测。"""
import asyncio
from types import SimpleNamespace


def test_ops_planner_defined():
    from app.services.agent_personas import OPS_PLANNER_PERSONA
    assert OPS_PLANNER_PERSONA.name == "ops_planner"
    assert OPS_PLANNER_PERSONA.output_format == "json"
    assert "create_ticket" in OPS_PLANNER_PERSONA.allowed_tools
    assert "search_regulation" in OPS_PLANNER_PERSONA.allowed_tools


def test_ops_planner_registered_when_enabled(monkeypatch):
    import importlib
    import app.services.agent_personas as mod
    monkeypatch.setattr(mod, "settings", SimpleNamespace(TICKET_ACTION_LOOP_ENABLE=True))
    importlib.reload(mod)
    assert "ops_planner" in mod.CODE_PERSONAS  # 以 persona_store 实际注册结构为准


def test_ops_planner_not_registered_when_disabled(monkeypatch):
    import importlib
    import app.services.agent_personas as mod
    monkeypatch.setattr(mod, "settings", SimpleNamespace(TICKET_ACTION_LOOP_ENABLE=False))
    importlib.reload(mod)
    assert "ops_planner" not in mod.CODE_PERSONAS
```

- [x] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_agent_personas.py -v`
Expected: FAIL（`ImportError: OPS_PLANNER_PERSONA`）

- [x] **Step 3: 实现**

`backend/app/services/agent_personas.py`：

1. import 区追加 `from app.config import settings`。
2. `ALERT_PERSONA` 之后追加：

```python
_OPS_PLANNER_SYSTEM = """你是电网检修计划专家。收到故障描述或检修任务后，通过调用工具自主收集证据（规程限值/设备因果链/历史案例/操作票草案），产出可直接执行的检修计划。
规则：
1) 每次可调用 0 个或多个工具；证据充分后停止调用工具，给出最终计划。
2) 最终输出严格 JSON：{"task":"检修任务","device":"设备","steps":["步骤1","步骤2"],"safety":["安全措施"],"risks":["风险点"],"basis":["依据来源：规程/案例名"],"summary":"计划概述"}
3) 只有用户明确要求"生成工单/创建工单/开票"时才调用 create_ticket，否则只输出计划；开票后按需 submit_ticket 提交审核。
4) 高风险操作（停电/接地/倒闸）必须在 risks 标注；步骤需含"验电/挂接地线"等规程动作。"""


async def _ops_planner_fallback(db, user_msg, model_type):
    """降级：直接生成操作票草案文本（不落库）。"""
    res = await domain_service.generate_ticket(db, user_msg, model_type, 5)
    return res.get("ticket", {}) or {"summary": "检修计划生成失败，请人工编制"}


OPS_PLANNER_PERSONA = Persona(
    name="ops_planner",
    system_prompt=_OPS_PLANNER_SYSTEM,
    allowed_tools=["search_regulation", "query_equipment_graph",
                   "search_similar_case", "draft_ticket",
                   "create_ticket", "submit_ticket"],
    max_iter=8,
    temperature=0.2,
    max_tokens=2000,
    output_format="json",
    fallback=_ops_planner_fallback,
    config_source="code",
)
```
3. 按 persona_store 的实际注册结构，在模块尾部条件注册：
```python
CODE_PERSONAS = {
    "diagnose": DIAGNOSE_PERSONA,
    "qa": QA_PERSONA,
    "evidence_gap": EVIDENCE_PERSONA,
    "alert": ALERT_PERSONA,
}
if settings.TICKET_ACTION_LOOP_ENABLE:
    CODE_PERSONAS["ops_planner"] = OPS_PLANNER_PERSONA
```
（若该文件已有等价注册 dict，就地追加 ops_planner 条目 + 开关判断，勿重复造 dict。）

- [x] **Step 4: 运行确认通过 + 全量回归**

Run: `venv/Scripts/python.exe -m pytest tests/test_agent_personas.py tests/test_agent_tools.py -v` → PASS；
Run: `venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration"` → 全过

- [x] **Step 5: Commit**

```bash
git add backend/app/services/agent_personas.py tests/test_agent_personas.py
git commit -m "feat(agent): ops_planner 检修计划 persona（开关条件注册，工具集含落库开票）"
```

---

### Task 5: 路由权限分层 + 前端

**Files:**
- Modify: `backend/app/routers/domain.py`、`frontend/src/utils/perm.js`、`frontend/src/views/TicketLifecycle.vue`、`frontend/src/views/Diagnose.vue`、`frontend/src/api/index.js`（如需）

**Interfaces:**
- `/domain/ticket/{id}/review|issue|start|complete|archive` → `require_perm(TICKET_MANAGE)`；`create|list|{id}|submit` 保持 `DOMAIN_USE`
- 前端：`perm.js` 加 `TICKET_MANAGE`；TicketLifecycle 审批/签发/执行按钮按权限显隐；Diagnose 诊断卡片「⚙ 生成工单」预填创建

- [x] **Step 1: 路由权限改造**

`backend/app/routers/domain.py`：import 行加 `TICKET_MANAGE`；把 5 个端点（review/issue/start/complete/archive，先 Read 确认端点名）的 `Depends(require_perm(DOMAIN_USE))` 改为 `Depends(require_perm(TICKET_MANAGE))`。

- [x] **Step 2: 权限回归测试**

`tests/test_permissions.py` 追加（或确认既有路由测试覆盖）：

```python
def test_editor_can_manage_ticket_but_operator_cannot():
    from app.core.permissions import has_perm, TICKET_MANAGE
    assert has_perm("editor", TICKET_MANAGE) is True
    assert has_perm("operator", TICKET_MANAGE) is False
    assert has_perm("admin", TICKET_MANAGE) is True
```

Run: `venv/Scripts/python.exe -m pytest tests/test_permissions.py -v` → PASS

- [x] **Step 3: 前端 perm.js + TicketLifecycle.vue**

1. `frontend/src/utils/perm.js`：权限常量表加 `TICKET_MANAGE: 'ticket:manage'`（先 Read 确认现有结构再改）；
2. `frontend/src/views/TicketLifecycle.vue`：审核/签发/开始执行/完成/归档按钮外层加 `v-if="hasPerm('ticket:manage')"`（用文件内既有 perm 工具函数；若无则从 `utils/perm.js` import）；「创建工单」「提交审核」按钮保持所有人可见。

- [x] **Step 4: Diagnose.vue「⚙ 生成工单」**

1. `frontend/src/api/index.js`：确认 `createTicket(data)` 存在（已存在，`POST /domain/ticket/create`）；如请求体缺 `sourceRef` 字段，检查后端 `TicketCreateRequest` schema——**Read `backend/app/schemas/` 中 TicketCreate 相关 model**，缺 `sourceRef` 则补 `Optional[str]` 字段并透传给 `create_ticket(source_ref=...)`（`routers/domain.py` ticket_create 端点同步透传）；
2. `Diagnose.vue` 诊断卡片（`diag` 结果区）按钮行追加：
```html
        <button class="btn" @click="toTicket" :disabled="ticketCreating">
          {{ ticketCreating ? '创建中…' : '⚙ 生成工单' }}
        </button>
```
script：
```js
const ticketCreating = ref(false)
async function toTicket() {
  if (!diag.value?.diagnosis) return
  ticketCreating.value = true
  try {
    const d = diag.value.diagnosis
    const ticket = (await createTicket({
      task: (d.summary || '').slice(0, 180),
      device: d.causes?.[0]?.name || '',
      steps: (d.causes || []).map(c => c.handling).filter(Boolean),
      risks: d.risks || [],
      sourceRef: 'diag:' + (traceId.value || Date.now()),
    })).data
    show(`工单已创建（草稿）：${ticket.data?.id || ''}，请到两票管理页流转`)
  } catch (e) { show('工单创建失败') } finally { ticketCreating.value = false }
}
```
（先 **Read `Diagnose.vue`** 确认 `diag` 结构 / `show` / `traceId` 实际命名，按现实调整；无 traceId 就用 `Date.now()`。）

- [x] **Step 5: 构建验证**

```bash
cd frontend && npm run build    # 预期 ✓ built
venv/Scripts/python.exe -m py_compile backend/app/routers/domain.py backend/app/schemas/*.py
```

- [x] **Step 6: Commit**

```bash
git add backend/app/routers/domain.py backend/app/schemas/ frontend/src/utils/perm.js frontend/src/views/TicketLifecycle.vue frontend/src/views/Diagnose.vue frontend/src/api/index.js
git commit -m "feat(ticket): 审批操作拆 TICKET_MANAGE 权限 + 前端按钮显隐 + 诊断页一键生成工单"
```

---

### Task 6: 端到端手动验证 + 文档

- [x] **Step 1: 全量回归**

```bash
ruff check backend tests
venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration"
```
Expected: ruff 无新告警；测试全过。

- [x] **Step 2: 本地起服务手动验证（开关开）**

`.env` 加 `TICKET_ACTION_LOOP_ENABLE=True` → 不带 `--reload` 起后端 + `npm run dev`：
1. admin 登录 → `/system/agent/run`（或 Agent 页）跑 `ops_planner`，输入"1号主变油温高，安排检修"→ 返回计划 JSON；明确说"生成工单并提交审核"→ 返回含 ticketId + 审核得分；
2. 用 operator 账号登录 → 两票管理页：能创建/提交，**看不到**审核/签发按钮；直接 curl review 端点 → 403（BizError body）；
3. editor 账号审批通过 → 签发 → Grafana/Prometheus 查 `grid_quality_event_total{source="ticket-lifecycle"}` 增长；
4. `.env` 关开关重启 → agent/run 跑 ops_planner → persona 不存在报错（符合预期）；诊断页「生成工单」仍可用（走路由，不受开关控）。

- [x] **Step 3: 更新 `.env.example` 确认同步 + Commit**

```bash
git add .env.example docs/
git commit -m "docs(ticket): 行动闭环验证记录与开关说明"
```

---

## Self-Review（已自检）

- **Spec 覆盖**：4 目标 ↔ Task 1（开关+权限）/ Task 2（agent 工具）/ Task 3（事件）/ Task 4（persona）/ Task 5（路由权限+前端）✅；YAGNI 项（外部派发/会签/消费侧/移动端）未引入 ✅
- **开关语义**：工具注册、persona 注册、事件 emit 三处全部 `TICKET_ACTION_LOOP_ENABLE` 控制；权限与「诊断页生成工单」路由不受开关（安全边界与既有路由能力）✅
- **复用而非新造**：状态机/幂等 source_ref/ToolRegistry/tool_permissions/persona_store/quality_event_bus 全部既有骨架 ✅
- **降级不崩**：事件 emit 失败 `degraded("ticket_event_emit")`；工具失败返回错误串不抛 ✅
- **租户隔离**：工具与事件都透传 `tenant`；`create_ticket` 既有 `source_ref` 查重按 tenant 过滤 ✅
- **破坏性变更**：operator 失去审批权——权限不能开关化，PR 说明必须明示 ✅
- **测试可行**：全部 sqlite + mock，不依赖 Milvus/embedding/LLM，CI 兼容 ✅
- **无占位符**：核心代码已给全；Task 4/5 标注"先 Read 确认 persona_store 注册结构与 Diagnose.vue 现状"属于防漂移检查而非留白 ✅
