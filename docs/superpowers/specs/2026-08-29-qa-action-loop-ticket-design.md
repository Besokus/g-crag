# 问答→行动闭环（工单工作流打通）— 设计 spec

> 日期：2026-08-29 ｜ 来源：《docs/新方向调研报告.md》P0 方向 1（问答→行动闭环）
> 状态：已实现（2026-08-29，commits 3b5ff75…9bdffed，实现记录见文末）｜ 关联：`2026-07-02-agentic-diagnose`（工具注册表）、`2026-07-01-rbac-acl-design`（权限）

## 背景与目标

调研报告结论：本项目"问"的纵轴已非常深，最大空白是**"问"到"办"的行动闭环**。行业参照（国网青海"检修计划一键生成"、DiagAgent）表明"诊断结论 → 自动起草工单 → 人工审批 → 派发 → 处置回填"是 2025-2026 央企标配。

**现状盘点（都是现成的，缺的是把它们串起来）**：

| 已有 | 位置 | 缺口 |
|---|---|---|
| 两票状态机 draft→pending_review→reviewed→issued→in_execution→completed→archived | `ticket_lifecycle_service.py` + `/domain/ticket/*` 路由 + `TicketLifecycle.vue` | 与问答/Agent **无入口打通**，全靠手工在工单页创建 |
| Agent `draft_ticket` 工具 | `agent_tools.py:95` | 只调 `generate_ticket` 返回**文本内容**，**不落库**，不能进流转 |
| 质量事件总线 emit/subscribe | `quality_event_bus.py` | 工单流转完全不发事件，完成执行后处置经验**不回流数据飞轮** |
| 权限 | 全部 `/domain/ticket/*` 用 `DOMAIN_USE` | **审核/签发/执行/归档与普通使用同权限**——operator 既能起草又能批自己的票，不符合两票"三级审核"安全惯例 |

**目标（本期做 4 件事）**：

1. **Agent 工具 `create_ticket` / `submit_ticket`**：Agent 诊断/规划产出的处置方案可**一键落库成票**（`source_ref` 幂等关联来源会话），并直接提交审核；
2. **`ops_planner` persona**：检修计划规划 Agent——输入故障/任务，多轮调工具（规程/图谱/案例/操作票草案）产出结构化检修计划，证据充分后可 `create_ticket` 落库；
3. **闭环事件**：签发/完成执行时 emit `quality_event_bus`（`ticket.issued` / `ticket.completed`），供知识自进化等订阅者消费（本期只发不收，消费者已有订阅框架）；
4. **权限分层**：新增 `TICKET_MANAGE = "ticket:manage"`，审核/签发/执行/归档端点要求它；起草/创建/提交保留 `DOMAIN_USE`。默认映射 admin/editor 有，operator 无（一线只能起草提交，值班长/editor 审批）——符合两票分级审批。

**总开关**：`TICKET_ACTION_LOOP_ENABLE=False`（关=现状，零破坏）。控制：两个新 agent 工具注册、ops_planner persona 注册、工单事件 emit。

## 非目标（YAGNI）

- **不做**真实外部系统派发（PMS/MIS 对接，等真实数据接入 spec）；
- **不做**多级会签/会签流（现有单级 review 够用）;
- **不做**事件消费侧（工单→案例自动回填知识库——留给 `knowledge_evolution_service` 订阅 `ticket.completed`，另行 spec）;
- **不做**移动端/语音。

## 架构

```
用户问"1号主变油温高怎么办"
  ├─ Chat/Diagnose 页 ──(已有诊断/QA 答案)──▶ [⚙ 生成工单] 按钮
  │      └─▶ POST /domain/ticket/create（预填 task/device/steps/safety/risks + sourceRef=会话/trace id）
  │              └─▶ ticket_lifecycle_service.create_ticket（现有，幂等）→ draft
  └─ POST /system/agent/run {persona:"ops_planner"}
         └─▶ run_agent：search_regulation → query_equipment_graph → search_similar_case
              → draft_ticket → create_ticket(落库) → submit_ticket(提交审核)
              → answer = 检修计划 JSON + ticketId

工单流转事件：
  issue_ticket / complete_execution 内
  TICKET_ACTION_LOOP_ENABLE=True 时 → quality_event_bus.emit(
      source="ticket-lifecycle", type="ticket.issued"|"ticket.completed",
      payload={ticketId, task, device, steps, executionLog, deviation, tenant})
```

## 组件与改动点

### 1. 配置（`backend/app/config.py`）

- 新增 `TICKET_ACTION_LOOP_ENABLE=False`（注释：关=现状/开=Agent 可落库工单+流转事件+ops_planner persona）。
- `.env.example` 同步。

### 2. 权限（`backend/app/core/permissions.py`）

- 新增 `TICKET_MANAGE = "ticket:manage"`；
- `ROLE_PERMISSIONS`：admin（`*` 天然含）不变；**editor** 加 `TICKET_MANAGE`；operator/auditor 不加；
- 路由改造（`routers/domain.py`）：`/ticket/{id}/review|issue|start|complete|archive` 从 `require_perm(DOMAIN_USE)` → `require_perm(TICKET_MANAGE)`；`/ticket/create|list|{id}|{id}/submit` 维持 `DOMAIN_USE`。**破坏性变更**：operator 角色失去审批权——符合两票分级审批惯例，需在 PR 说明中标注。
- 前端 `utils/perm.js` 镜像该常量（仅按钮隐藏用）。

### 3. Agent 工具（`backend/app/services/agent_tools.py`）

- `create_ticket(db, model_type, task, device, steps[], safety[], risks[], ticketType?, sourceRef?) -> str`：调 `ticket_lifecycle_service.create_ticket`（`source_ref` 幂等），返回 `"已创建工单 {id}（草稿），标题: ..."`；
- `submit_ticket(db, model_type, ticketId) -> str`：调 `submit_for_review`，返回审核得分/状态摘要；
- 两工具在注册表注册处受 `TICKET_ACTION_LOOP_ENABLE` 控制（关=不注册，LLM 看不见也调不到）；
- agent 工具走既有 `agent_tool_audit_service` 审计（ToolRegistry 统一入口，零额外工作）。

### 4. ops_planner persona（`backend/app/services/agent_personas.py`）

- 新增 `OPS_PLANNER_PERSONA = Persona(name="ops_planner", ...)`：
  - 工具集：`search_regulation / query_equipment_graph / search_similar_case / draft_ticket / create_ticket / submit_ticket`（前 4 个已有，后 2 个受开关）；
  - system prompt：电网检修计划专家——先收集证据（规程限值/因果链/历史案例），再产出计划 `{task, device, steps[], safety[], risks[], basis[]}`，明确指示"仅当用户明确要求开票时才调 create_ticket"（防止 Agent 自作主张落库）；
  - fallback：工具调不动时降级调 `domain_service.generate_ticket` 返回草案文本；
- 经 `persona_store` / `POST /system/agent/run` 自动暴露，**无需新路由**；注册受开关控制。

### 5. 闭环事件（`backend/app/services/ticket_lifecycle_service.py`）

- `issue_ticket` 与 `complete_execution` 成功提交后：
  ```python
  if settings.TICKET_ACTION_LOOP_ENABLE:
      await quality_event_bus.emit(source="ticket-lifecycle", type="ticket.issued", payload={...}, tenant=tenant)
  ```
  emit 失败走 `degraded("ticket_event_emit", e)`，不阻塞流转（符合"降级不崩"铁律）；
- payload：`{ticketId, task, device, steps, executionLog?, deviation?, creator}`。

### 6. 前端（最小改动）

- `frontend/src/api/index.js`：`createTicket` 已存在，补 `sourceRef` 透传即可（`createTicket` 的 data 参数直接带）；
- `Chat.vue` / `Diagnose.vue`：答案/诊断卡片下加「⚙ 生成工单」按钮 → 弹预填对话框（task/device/steps/safety/risks 可改）→ `createTicket({..., sourceRef: 会话id或traceId})` → 提示成功并给工单跳转链接；按钮用 `perm.js` 判 `domain:use`；
- `TicketLifecycle.vue`：审核/签发/执行按钮按 `ticket:manage` 显隐。

## 数据结构

`create_ticket` 复用现有 Ticket 模型，**零 schema 变更**。`source_ref` 字段已存在（幂等键），本期语义扩展为"来源会话/诊断 trace id"（格式 `qa:{conversationId}` / `diag:{traceId}`）。

事件 payload 见上文第 5 节。

## 错误处理 / 安全网

- **开关关=现状**：工具不注册、persona 不注册、事件不 emit、权限改造不受开关（权限是安全边界，不能开关化——operator 失审批权在上线说明中明示）；
- 工单事件 emit 失败 → `degraded` 计数，流转照常；
- `create_ticket` 幂等：同 `source_ref` 重复创建返回既有票（现有逻辑直接复用）；
- Agent 误开票防线：persona prompt 明确"仅用户明确要求才落库" + `agent_tool_audit_service` 全量审计 + 工单草稿态可改可软删。

## 测试（`tests/`）

- `tests/test_permissions.py`（追加）：operator 无 `ticket:manage`、editor 有；
- `tests/test_ticket_lifecycle.py`（追加）：开关开 → issue/complete 发事件；开关关 → 不发；emit 抛错不阻塞流转；
- `tests/test_agent_tools.py`（追加）：`create_ticket` 落库 + source_ref 幂等；开关关时工具不在注册表；
- `tests/test_agent_personas.py`（追加）：ops_planner 注册/受开关控制、fallback。

## 范围与迭代

预计 1 个迭代内完成（后端 3 天 + 前端 1 天 + 测试回归 1 天）。后续衔接：真实数据接入后 ops_planner 加遥测工具；知识自进化订阅 `ticket.completed` 实现处置经验回流。

## 实现记录（2026-08-29）

**Commits**：`3b5ff75` 开关+权限+role 限制 → `4cf0861` agent 工具 → `c98a81c` 流转事件 → `2b371b0` ops_planner persona → `9bdffed` 路由权限+前端。

**⚠ 破坏性变更**：`review` 端点原为 `require_admin`，现为 `TICKET_MANAGE`（admin/editor）；`issue/execute/archive` 从 `DOMAIN_USE` 升级为 `TICKET_MANAGE`。**operator 角色失去审批/签发/执行/归案权**（两票分级审批惯例），上线说明必须明示。

**与计划的差异（按实际代码结构调整）**：
- persona 注册真身在 `persona_store._CODE_PERSONAS`（非 agent_personas 内 dict），条件注册加在 persona_store；`_ops_planner_fallback` 未入 `_FALLBACK_REGISTRY`（YAGNI）。
- registry 开关测试不用 `importlib.reload` + 替换模块 settings（reload 会重执行 import 覆盖 patch）；改为 patch 共享 settings 属性 + 直接调 `build_default_registry()`；persona 开关测试 patch settings 实例属性 + reload persona_store。
- `_t_create_ticket` 去掉 `ctx_user` 死参数（ToolRegistry.run 只注入 tenant，不注入 username）。
- 生命周期测试流补 `submit_for_review`（review_ticket 要求 pending_review+，不能从 draft 直审）；另加 emit 抛错不阻塞流转用例。
- 诊断页 `sourceRef=diag:{Date.now()}`（无现成 traceId）；`TicketCreateRequest` 补 `sourceRef` 透传。
- 顺手清理所改文件内既有死导入（persona_store `func`、domain.py `get_current_user`/`TicketListRequest`）。

**验证**：`pytest tests/ -q --ignore=tests/test_api.py -m "not integration"` → 661 passed，8 failed（经 fad0037 基线 worktree 比对均为既有问题：5 个测试间污染簇 + 2 个本地 `.env` 覆盖所致 + 1 个需特定触发顺序）；`npm run build` ✓；改动文件 ruff 全过。**容器化端到端手动验证（Task 6 Step 2：ops_planner 开票/多角色审批/Grafana 事件指标）未执行——Docker 守护进程未运行，待栈启动后按计划步骤补验。**
