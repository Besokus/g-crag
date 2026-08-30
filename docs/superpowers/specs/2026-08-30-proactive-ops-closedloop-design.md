# 主动运维闭环补全（结构化根因 + 遥测证据 + 闭环回填）— 设计 spec

> 日期：2026-08-30 ｜ 状态：**已实现并 Docker e2e 验证通过**（plan 见 `docs/superpowers/plans/2026-08-30-proactive-ops-closedloop.md`）
> 来源：`docs/新方向调研报告.md` 方向 9（P1）+ 2026-08-30 三路代码调研（告警链 / Agent 后台机制 / 前端通知）
> 关联：`services/realtime_event_service.py`（事件→诊断主链）、`services/agent_personas.py`（alert persona）、`services/quality_event_bus.py`、`views/OperationsCenter.vue`、`routers/system.py`（webhook）

## 背景与目标

调研报告方向 9 提出"告警→根因→处置预案→确认派发"。**三路调研证实该骨架已存在约 80%**：`ingest_event()` 幂等入库+五级严重度+规则门禁（`realtime_event_service.py:462-618`）、`proactive_ops_run` 全状态机（queued/running/proposed/confirmed/rejected/ticketed/failed，`models/realtime_event.py:90-132`）、后台 Agent 只读诊断（`process_proactive_run`，:663-816）、前端主动运维中心（WS 推送+确认/驳回/转两票/重试按钮，`OperationsCenter.vue`）。

**本 spec 补齐剩余 4 块，把链路从"能跑"升级为"可演示、可沉淀"：**

1. **打通告警入口**（配置修复）：Grafana contactpoint 已带 `?token=grid-alert-token-2026`（`grafana/provisioning/alerting/contactpoints.yml:13`），但后端 `.env` 未配 `ALERT_WEBHOOK_TOKEN` → `system.py:284-285` fail-closed 503。纯配置缺失，代码无 bug（`tests/test_system_alert_webhook.py:12-17` 明确验证该行为）。
2. **结构化根因分析**：现有诊断是 alert persona 单次 LLM 自由 JSON（塞在 `diagnosis_json`/`recommendation_json`），前端只能整段展示。升级为 schema v2（根因列表/可能性/证据/置信度/依据），前端结构化渲染。
3. **遥测证据**：`PROACTIVE_READ_ONLY_TOOLS = {search_regulation, query_equipment_graph, search_similar_case}`（`realtime_event_service.py:67-71`）不含 `query_telemetry`（mock_scada MCP 已注册进 ToolRegistry，compose `MCP_SERVERS` 已配）。开闸后 Agent 诊断可拉实时遥测作证据。
4. **闭环回填**：confirm/reject/to-ticket 无质量事件（调研:告警→工单链路不进 quality_event_bus）。补 emit，让处置结果可被飞轮/统计消费。

## 现状盘点（都是现成的，缺的是串起来）

| 已有 | 位置 | 缺口 |
|---|---|---|
| Grafana→webhook→ingest 转换 | `routers/system.py:270-368`（alertmanager payload→`RealtimeEventIn`） | `.env` 缺 token 配置，链路 503 |
| 事件入库+门禁+设备映射 | `ingest_event()`，`TRIGGER_SEVERITIES={warning,major,critical}` | 无（本期不动） |
| 后台诊断 worker | `process_proactive_run`（任务队列 `proactive_ops.process` + 无队列降级 `_track_background`） | persona 用 alert（自由 JSON）；工具集无遥测 |
| 确认/驳回/转两票路由 | `POST /realtime/runs/{id}/confirm\|reject\|to-ticket`（`routers/realtime_event.py:169-243`） | 不 emit 质量事件 |
| 老处置链（S3） | `alert_disposal_service`（手动触发，独立状态机+to_ticket） | **本期不动**（兼容保留，共用 alert persona 不受影响） |
| 前端 OperationsCenter | WS `proactive_proposal`→toast+刷新、10s 轮询兜底、`can('alert:manage')` 按钮门控 | run 详情无结构化渲染 |
| 质量事件总线 | `quality_event_bus.emit/subscribe`（fnmatch 派发，`QUALITY_BUS_ENABLE` 控派发、入库恒在） | 无 `proactive-ops` 源 |

## 设计总览（三个开关，全部默认关=现状）

```
Grafana webhook ──(Task1 配置修复)──> ingest_event ──> proactive_ops_run(queued)
                                                            │
                    Task2: proactive_diagnosis persona（schema v2，开关控 persona 选择）
                    Task3: 只读工具集 + query_telemetry（开关控）
                                                            ▼
                                          recommendation_json(schema v2) ──> proposed
                                                            │ WS proactive_proposal
                              OperationsCenter 详情展开（Task5: v2 结构化渲染, v1 兼容回退）
                                                            ▼
                              confirm / reject / to-ticket ──(Task4: emit quality_event_bus)
```

## 核心设计

### 1. 配置修复（无代码改动）

- `.env.example:120` / `.env.template:135` 的 `ALERT_WEBHOOK_TOKEN=` **键已存在但为空值**——补值 `grid-alert-token-2026` + 注释（**必须与 `grafana/provisioning/alerting/contactpoints.yml:13` 的 token 一致**）。
- 本地 `.env` 加同键（gitignored，不入库）；compose 走 `env_file: .env` 自动注入，无需改 compose。
- 验收：重启后端后 `curl -X POST "http://localhost:8001/api/system/alerts/webhook?token=grid-alert-token-2026" -d @alertmanager_payload.json` 返回 200，`realtime_event` 表新增记录。

### 2. 结构化根因：新 persona `proactive_diagnosis`（不动 alert）

**决策**：新建 code persona，不复用/不改 alert persona——S3 老链（`alert_disposal_service`）共用 alert，改 prompt 会回归老链。与 `ops_planner` 同模式：开关条件注册（**注册真身是 `persona_store._CODE_PERSONAS`，persona_store.py:20-22**，不在 agent_personas.py）。

**新开关** `PROACTIVE_SCHEMA_V2_ENABLE: bool = False`（关=现状用 alert persona）。

**schema v2**（persona `output_format="json"`，prompt 内严格约定）：

```json
{
  "schema": "proactive-recommendation/v2",
  "summary": "一句话结论",
  "rootCauses": [
    {"name": "冷却风机故障", "likelihood": "high|medium|low",
     "evidence": ["规程:DL/T 572 §5.3 油温限值", "遥测:油温85℃持续上升"],
     "handling": "检查风机电源与叶轮"}
  ],
  "steps": ["停电", "验电", "..."], "safety": ["..."], "risks": ["..."],
  "confidence": "high|medium|low",
  "basis": ["依据来源：规程名/案例名"]
}
```

- `process_proactive_run` 里 persona 选择：`get_persona("proactive_diagnosis") if settings.PROACTIVE_SCHEMA_V2_ENABLE else get_persona("alert")`。
- **落库（现场核对修正）**：answer 带 `schema` 字段时**整个 answer 存入 `recommendation_json`**（叠加 readOnly 安全标记）——现有组装逻辑会丢 `rootCauses/steps/confidence/basis`；v1 组装逻辑保持不变。
- **转票草案回退（现场核对修正）**：`run_to_ticket` 走 `normalize_ticket_draft`（读 `answer["ticket"]`），v2 无该子对象 → v2 路径需用顶层 `steps`/`safety`/`risks` 兜底组装草案，否则转票空步骤；v1 行为不变。
- 只读工具过滤逻辑保持：v2 persona 的 `allowed_tools` 与只读集取交集（现行为不变）。
- 兼容：v1（alert 输出 `{summary,diagnosis,handling,ticket,risks}`）与 v2 按 `schema` 字段区分，前端双兼容。

### 3. 遥测证据：`PROACTIVE_TELEMETRY_ENABLE: bool = False`

- 开时诊断工具集 = `PROACTIVE_READ_ONLY_TOOLS | {"query_telemetry"}`（mock_scada 注册名，`mcp/client.py` 原名注册）。工具集合并写成纯函数 `_proactive_readonly_tools() -> set[str]` 便于单测。
- 诊断 user prompt 附设备上下文：**`source_device_id` 与 `canonical_device_id` 都要给（现场核对修正）**——mock_scada 的 `query_telemetry(device_id)` 键是源系统 ID（如 `T1_main_transformer`），而 canonical 是映射后 ID（`SUB-A:T1`/`unmapped:` 前缀），两者都可能用到；提示 LLM "可调用 query_telemetry 获取该设备实时遥测"。mock 未收录的设备返回"无数据"，Agent 按证据不足处理（persona prompt 已有"证据不足如实说明"约定）。
- `evidence_json` 继续存 `steps`（工具调用记录自动含 query_telemetry 的 args/result），零新字段。

### 4. 闭环回填：`PROACTIVE_FEEDBACK_ENABLE: bool = False`

- **（现场核对修正）**流转逻辑已在 service 层：`confirm_run`/`reject_run`/`run_to_ticket`（`realtime_event_service.py:861-940`），路由是薄壳 → emit 直接挂三函数 commit 成功后，无需抽层。
- emit（失败/异常 `degraded("proactive_feedback_emit", e)` 不阻塞流转，沿用 ticket_lifecycle 的 `_emit_ticket_event` 模式）：
  - `("proactive-ops", "proposal.confirmed", {runId, eventRefId, reviewer})`
  - `("proactive-ops", "proposal.rejected", {runId, eventRefId, reviewer, note})`
  - `("proactive-ops", "proposal.ticketed", {runId, eventRefId, ticketId})`
- emit 统一走 service 层辅助函数 `_emit_run_event(event_type, run, tenant)`；租户透传；`QUALITY_BUS_ENABLE` 只控制派发不入库，本开关控制是否 emit，两层独立。

### 5. 前端：OperationsCenter run 详情结构化展开

- 运行表行点击内嵌展开（交互模式复用 QaTraceChart 的"行点击展开、单开互斥"）：
  - **v2**：summary + confidence 徽章；根因表格（根因/可能性/证据/处置建议）；steps/safety/risks 三组 chips；basis 依据列表。
  - **v1**：回退现状展示（diagnosis/handling 文本段）。
- 确认/驳回/转两票按钮不动（已有 `can('alert:manage')` 门控）。

## 存储与性能影响

- 零 schema 变更（`recommendation_json`/`evidence_json` 自由 JSON 承载 v2）。
- 诊断 LLM 调用不变（仍是单次 run_agent 多轮工具），遥测工具每次调用 +1 次 HTTP（mock_scada 本地 <10ms）。
- 全部新逻辑在开关后，关=现状逐字节一致。

## 实现拆分（预估：后端 0.5 天 + 前端 0.5 天 + e2e 0.5 天）

1. **Task 1**：配置修复（`.env.example`/`.env.template`/本地 `.env`）+ webhook 200 验证；
2. **Task 2**：`PROACTIVE_SCHEMA_V2_ENABLE` + `proactive_diagnosis` persona（开关条件注册）+ `process_proactive_run` persona 选择；
3. **Task 3**：`PROACTIVE_TELEMETRY_ENABLE` + 工具集开关合并 + 设备上下文注入 prompt；
4. **Task 4**：`PROACTIVE_FEEDBACK_ENABLE` + `_emit_run_event` + 三流转点 emit；
5. **Task 5**：前端 run 详情展开（v2 结构化 + v1 回退）；
6. **Task 6**：全量回归 + Docker e2e（webhook→run→诊断→确认→转票→quality_events 落库）+ 文档。

## 非目标（YAGNI）

- 不做真实 SCADA 协议适配（IEC 104/Modbus/OPC-UA）——方向 2 单独立项，mock_scada 保持回退；
- 不做预案版本管理/模板库/会签审批；
- 不做 `fault_prediction` 风险分接入触发条件（等真实数据，避免纯统计触发噪音告警）；
- 不做全局通知铃铛（OperationsCenter 已有 WS+轮询，全局铃铛另立小 spec）；
- 不做控制指令下发（`control_executed` 保持 False，`execution_mode=read_only` 语义不变）；
- 不动 S3 老处置链（`alert_disposal_service`）与 Admin 告警 Tab。

## 实现与验证记录（2026-08-30，已实现并 Docker e2e 通过）

- **提交**：Task 1 配置修复（`368071c`）→ Task 2 schema v2 persona（`e43e1c3`）→ Task 3 遥测证据（`31e53f5`）→ Task 4 闭环回填（`065b3da`）→ Task 5 前端展开（`d258471`）+ 渲染作用域修复。
- **回归**：全量 688 passed / 8 failed（与基线完全相同的 8 个既有环境性失败，零回归）；改动 Python 文件 ruff 无新告警；`npm run build` ✓。
- **Docker e2e（真实 LLM）**：
  - **关态=现状**：compose 无三开关 → 新告警诊断后 recommendation 无 `schema` 字段（v1 形状）、工具仅三只读无 `query_telemetry`、confirm 后 `quality_events` 无 `proactive-ops` 行 ✅
  - **开态全链**：webhook(critical) → ingest(trigger) → 后台 Agent 诊断（queued→running→proposed）→ `recommendation.schema="proactive-recommendation/v2"`，rootCauses 含 遥测/规程/图谱/案例 四源证据；**Agent 调 `query_telemetry` 实测油温 42.3℃，与告警值 99℃ 交叉比对后判定"告警为数据源误告"**（遥测证据价值的真实案例）；ticketDraft.steps 非空（顶层 steps 回退生效）→ confirm → to-ticket → `quality_events` 落 `proposal.confirmed`+`proposal.ticketed`（tenant=default），tickets 落 `source_ref=proactive:<runId>` 草稿；另发告警走 reject → `proposal.rejected` ✅
  - **前端**：admin 登录运维中心 → run 行点击展开 v2 结构化渲染（置信徽章/根因表/chips/依据）实测通过。
- **验证中发现并修复**：Task 5 初版把展开行 `<tr v-if="...">` 写在 `v-for` 作用域外，`r` 未定义致渲染抛错、整页被 Vue 卸载为空注释（Vue 渲染错误不进 window.onerror，浏览器错误钩子抓不到）→ 改为 `<template v-for>` 包裹主行+展开行；验证中一例 401 为脚本漏传 token，非系统问题。
