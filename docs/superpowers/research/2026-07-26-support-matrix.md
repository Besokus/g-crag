# 三线方案支撑矩阵

## 目的

本矩阵用于满足“每一步都要有文档支撑”的要求。每个计划步骤必须至少具备一类支撑：文档、代码、测试、指标。缺失项必须在实施前补齐。

## 支撑矩阵

| Step | 名称 | 文档支撑 | 代码支撑 | 测试支撑 | 指标支撑 | 状态 |
|---|---|---|---|---|---|---|
| A1 | 质量事件可观测 | `2026-07-20-data-flywheel.md` | `quality_event_bus.py`、`models/quality_event.py` | 待补 quality_event 查询 API 测试 | `QUALITY_EVENT_TOTAL` | 可实施 |
| A2 | dislike payload 增强 | `2026-07-25-data-loop-breakpoint-fix-design.md` | `qa.py::feedback`、`qa_service.answer` | `test_feedback_fix_rate.py` 可扩展 | `FEEDBACK_FIX_RATE` | 可实施 |
| A3 | evidence_gap 到自进化回流 | `2026-07-17-knowledge-evolution-design.md` | `evidence_gap_service.py`、`knowledge_evolution_service.py` | `test_evidence_gap_*`、`test_knowledge_evolution.py` | `EVOLUTION_LIFT` | 可实施 |
| A4 | 治理传播 dry-run | `2026-07-20-data-flywheel.md` | `governance_propagate_service.py` | `test_semantic_gov_filter.py`，待补 dry-run 测试 | `GOVERNANCE_PROPAGATED` | 需先补 dry-run |
| B1 | 主动运维建议质量评分 | `2026-07-16-active-operations-governance.md` | `realtime_event_service.process_proactive_run`、`ProactiveOpsRun` | `test_active_ops_integration.py` 可扩展 | 待新增 `grid_proactive_quality_score` | 可实施 |
| B2 | OperationsCenter 闭环增强 | `2026-07-16-active-operations-governance.md` | `OperationsCenter.vue`、`frontend/src/api/index.js` | 待补前端构建验证 | 待新增采纳率/转票率 | 可实施 |
| B3 | 两票状态回写 | `2026-07-01-ticket-audit-design.md`、`2026-07-16-active-operations-governance.md` | `ticket_lifecycle_service.py`、`realtime_event_service.run_to_ticket` | `test_active_ops_integration.py` 已有转票幂等，可扩展状态回写 | `grid_ticket_completion_rate` | 可实施 |
| C1 | traceId 贯穿问答反馈 | `2026-07-18-verifiable-citation-design.md` | `qa_service.answer`、`qa_service.stream_answer`、`qa.py::feedback` | 待补 feedback trace 测试 | 待接 trace 指标 | 可实施 |
| C2 | debug trace 可跳转 | `retrieval-methodology.md` | `retrieval_service.debug_search`、`RetrievalDebug.vue` | 待补 debug_search schema 测试 | 可复用检索延迟指标 | 需补测试 |
| C3 | 评测低分进入质量事件 | `2026-07-20-data-flywheel.md` | `online_eval_service.py`、`retrieval_eval_service.py` | `test_retrieval_eval_metrics.py` 可扩展 | `FAITHFULNESS_TREND`、`QUALITY_EVENT_TOTAL` | 可实施 |
| C4 | 生产配置矩阵 | `系统架构.md`、`2026-07-19-qa-optimization.md` | `config.py` | 配置快照测试待补 | 全链路指标 | 需评审 |

## 实施门槛

1. 每个 Step 开始前必须确认支撑矩阵中“文档支撑”和“代码支撑”不为空。
2. 涉及清理、回写、缓存失效的 Step 必须先补测试。
3. 涉及前端的 Step 必须跑 `cd frontend && npm run build`。
4. 涉及后端主链路的 Step 至少跑对应专题测试和 `pytest tests` 的可行子集。
5. 涉及生产开关的 Step 必须给出默认值、灰度顺序和回滚开关。

## 推荐执行顺序

1. A1：先让质量事件可见。
2. A2：增强 dislike 事件上下文。
3. C1：补 traceId，让后续事件可定位。
4. A3：打通 evidence_gap 到自进化回流。
5. B1/B2：补主动运维建议质量和页面展示。
6. B3：补两票状态回写。
7. C2/C3/C4：固化检索可信链路、低分回流和配置矩阵。
8. A4：治理传播 dry-run 通过后，再考虑真实清理。
