# Track B 调研报告：主动运维与两票闭环

## 目标

把实时事件、主动运维建议、人工确认、两票草稿、审核签发执行归档打成可审计闭环，同时确保 Agent 永远只读，不执行设备控制。

## 文档支撑

| 依据 | 结论 |
|---|---|
| `docs/superpowers/specs/2026-07-16-active-operations-governance.md` | 定义实时事件入口、只读 Agent、人工确认、转两票草稿、任务中心和知识治理门禁。 |
| `docs/superpowers/specs/N1-N4-incremental-PRD.md` | N3 数字孪生和 N2 MCP 对主动运维有后续扩展依赖。 |
| `docs/系统架构.md` | 系统总览确认 `OperationsCenter.vue`、`realtime_event`、`task_center`、`ticket_lifecycle` 已纳入主架构。 |
| `.superpowers/sdd/progress.md` | superpowers SDD 记录主动运维相关批次遵循 TDD/review/fix。 |

## 代码支撑

| 能力 | 文件 | 代码事实 |
|---|---|---|
| 实时事件模型 | `backend/app/models/realtime_event.py` | `RealtimeEvent` 支持租户、源事件幂等、设备映射和处理状态；`ProactiveOpsRun` 固定 `execution_mode=read_only`、`control_executed=False`。 |
| 事件入口 | `backend/app/routers/realtime_event.py` | `POST /events` 返回 202；`/runs/{id}/confirm`、`reject`、`to-ticket`、`retry` 已存在。 |
| 主动运维服务 | `backend/app/services/realtime_event_service.py` | 支持规范化、任务入队、Agent 建议生成、建议确认、转两票、重试。 |
| 告警处置兼容链路 | `backend/app/services/alert_disposal_service.py` | 告警处置可生成 proposed，confirmed 后转两票草稿。 |
| 两票生命周期 | `backend/app/services/ticket_lifecycle_service.py` | 支持创建、提交审核、审核、签发、执行、完成、归档、删除。 |
| 前端工作台 | `frontend/src/views/OperationsCenter.vue` | 支持查看 runs/events/tasks/domainEvents、确认、驳回、转两票、重试、终止任务。 |

## 测试支撑

| 测试 | 支撑点 |
|---|---|
| `tests/test_active_ops_integration.py` | 覆盖实时事件入库、主动运维任务、重启接管、只读工具、转票幂等、租户隔离。 |
| `tests/test_realtime_event_service.py` | 覆盖实时事件服务核心行为。 |
| `tests/test_alert_disposal_tenant.py` | 覆盖告警处置租户隔离。 |
| `tests/test_ticket_audit.py` | 覆盖两票解析、规则审核、LLM 审核降级。 |

## 已有能力

1. 实时事件支持 token/HMAC 认证和租户隔离。
2. `eventId + source + tenant` 支持幂等。
3. warning/major/critical 可触发主动运维 run 和持久任务。
4. Agent 运行记录是只读模式，人工确认也只允许转两票草稿。
5. 两票有完整生命周期状态和 `source_ref` 幂等约束。
6. OperationsCenter 已提供运维操作入口。

## 缺口

| 缺口 | 影响 | 处理建议 |
|---|---|---|
| 建议质量缺评分 | 人工只能看文本，难判断建议是否可信 | 增加 evidenceScore、actionabilityScore、riskCompleteness。 |
| run 与 ticket 后续状态弱关联 | 转票后不能直观看到票据审核/执行进展 | 主动运维 run 列表展示 ticket 当前状态，并在票据状态变化时回写。 |
| API 直接覆盖不足 | CodeGraph 标记 `routers/realtime_event.py::to_ticket` 直接 API 测试不足 | 增加路由层 API 测试。 |
| 采纳/驳回原因分析不足 | 难以优化 Agent 建议 | 结构化保存 reject reason、accept note，并做统计。 |
| 外部事件质量治理不足 | 设备未映射、重复事件、低等级事件的质量指标不完整 | 增加事件接入质量看板。 |

## 方案

### Step B1：建议质量评分

在 `process_proactive_run` 生成建议后计算：

- `evidenceScore`：是否有检索来源、图谱证据、历史案例。
- `actionabilityScore`：是否包含明确处置步骤、检查项、转票建议。
- `riskCompleteness`：是否包含风险等级、安全提示、人工确认要求。
- `readOnlyCompliance`：是否明确未执行设备控制。

支撑：`ProactiveOpsRun` 已有 `diagnosis_json`、`recommendation_json`、`evidence_json`、`ticket_draft_json`。

### Step B2：OperationsCenter 闭环增强

- run 卡片展示质量分、采纳/驳回备注、票据状态。
- ticketId 可跳转或定位到 `/ticket`。
- 任务中心展示 runId、eventId、ticketId 关联。

支撑：`OperationsCenter.vue` 已加载 `runs/events/tasks/domainEvents`，已有 `ticketId` 展示。

### Step B3：两票状态回写

- `run_to_ticket` 后记录 `ticketCreatedAt`。
- `ticket_lifecycle_service` 状态变更时，如果 `source_ref=proactive:<run_id>`，回写 run 的 `finalOutcome` 或 `ticketStatus`。
- 完成/归档后 run 标记业务闭环完成。

支撑：`tickets.source_ref` 唯一约束已存在；`test_active_ops_integration.py` 已覆盖 proactive 转票幂等。

### Step B4：指标

新增或补齐：

- `grid_proactive_run_total{status,severity}`
- `grid_proactive_accept_rate`
- `grid_proactive_ticket_conversion_rate`
- `grid_proactive_quality_score`
- `grid_ticket_completion_rate`

支撑：`core/metrics.py` 已有指标注册范式。

## 验收

| 验收项 | 标准 |
|---|---|
| 只读安全 | Agent 工具不能执行控制动作，`control_executed` 始终 false。 |
| 建议评分 | 每个 proposed run 有质量分和评分明细。 |
| 人工确认 | proposed 才能 confirm/reject。 |
| 转票幂等 | 同一 run 重复转票返回同一 ticket。 |
| 状态回写 | ticket 完成/归档后 run 可看到最终结果。 |
| 租户隔离 | tenant-b 不能读取或操作 tenant-a 的 run/ticket。 |
