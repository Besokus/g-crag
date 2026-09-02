# Track A 调研报告：数据飞轮与知识自进化

## 目标

把用户差评、低置信拒答、在线评测低分、治理状态变更统一纳入质量事件闭环，形成“坏 case 发现 → 证据补全 → 草稿审核 → 回流知识库 → 指标验证”的可追踪链路。

## 文档支撑

| 依据 | 结论 |
|---|---|
| `docs/superpowers/specs/2026-07-20-data-flywheel.md` | 明确 8 个子系统断点：治理只过滤不清理、dislike 不进补全、评测不驱动改进、缺事件总线。 |
| `docs/superpowers/plans/2026-07-25-data-loop-breakpoint-fix-phase1.md` | Phase 1 已围绕数据闭环断点做增量修复。 |
| `.superpowers/sdd/progress.md` | 记录 B1/B2/B3/B4/B5/B6/B3 等任务已按 superpowers SDD 模式执行、review、fix。 |
| `docs/系统架构.md` | 架构总览确认已有 evidence_gap、knowledge_evolution、knowledge_governance、feedback、online_eval 等模块。 |

## 代码支撑

| 能力 | 文件 | 代码事实 |
|---|---|---|
| 质量事件总线 | `backend/app/services/quality_event_bus.py` | `emit(source,type,payload,tenant)` 入库；`subscribe(pattern,handler)` 支持异步派发；`QUALITY_BUS_ENABLE` 控制派发。 |
| dislike 事件入口 | `backend/app/routers/qa.py` | `feedback == dislike` 时可通过 `DISLIKE_TO_GAP_ENABLE` 发送 `feedback.dislike` 事件。 |
| 治理传播 | `backend/app/services/governance_propagate_service.py` | `doc_blocked` 可联动清理 Milvus、Neo4j、qa_cache，并 bump 治理代际。 |
| 指标 | `backend/app/core/metrics.py` | 已有 `QUALITY_EVENT_TOTAL`、`GOVERNANCE_PROPAGATED`、`FEEDBACK_FIX_RATE`、`KB_FRESHNESS`、`EVOLUTION_LIFT`。 |
| 自进化页面 | `frontend/src/views/KnowledgeEvolution.vue` | 支持触发盲区扫描、审核草稿、回流后撤回。 |

## 测试支撑

| 测试 | 支撑点 |
|---|---|
| `tests/test_semantic_gov_filter.py` | 验证 `citation_cache_version` 含治理 G 段，以及语义缓存治理过滤。 |
| `tests/test_evidence_gap_service.py` / `tests/test_evidence_gap_filter.py` | 验证 evidence_gap 收集和筛选。 |
| `tests/test_knowledge_evolution.py` | 验证知识自进化草稿链路。 |
| `tests/test_feedback_fix_rate.py` | 验证坏 case 修复率聚合。 |
| `tests/conftest.py` | teardown 会重置 quality_event_bus 订阅者，说明总线有测试隔离要求。 |

## 已有能力

1. 差评可以进入优化链路：缓存失效、黑名单、过自信检查、质量事件。
2. 质量事件已有统一总线，支持 opt-in 异步派发。
3. 治理状态变化已有传播 handler，具备清理向量、图谱、缓存的基础。
4. 自进化已有页面和服务闭环：扫描、草稿、审核、回流、撤回。
5. 指标名称已经注册，适合直接接 Grafana 面板。

## 缺口

| 缺口 | 影响 | 处理建议 |
|---|---|---|
| dislike payload 较薄 | 后续定位缺少 retrievalSources、confidence、traceId、retrievalQuality | 增强 `FeedbackRequest` 或 feedback 入库后从会话/答案结果补字段。 |
| 质量事件缺统一前端视图 | 事件入库后难以运营和排障 | Admin 增加“质量事件”Tab。 |
| 治理传播真实清理风险高 | 误删 Milvus/Neo4j/cache 影响线上答案 | 先做 dry-run 和候选清理列表，再打开 `GOVERNANCE_PROPAGATE_ENABLE`。 |
| 事件派发状态弱 | 只入库和异步派发，缺少每个 handler 的处理状态 | 增加 handler 处理结果或派发表。 |
| 端到端验收不足 | 单点测试较多，闭环测试不足 | 新增 dislike → quality_event → evidence_gap → draft → approve → retrieval 命中测试。 |

## 方案

### Step A1：质量事件可观测

- 新增 `GET /system/quality-events`。
- 支持 `source/type/status/tenant/time` 过滤。
- Admin 增加事件列表、payload 展开、关联 evidence_gap/draft/doc。

支撑：`quality_event_bus.py` 已有 `QualityEvent` 入库；`Admin.vue` 已有 evidence_gap 和自进化管理区域。

### Step A2：dislike payload 增强

- feedback 事件补齐 `retrievalSources`、`confidence`、`traceId`、`cacheLayer`。
- 差评原因和检索质量作为结构化字段进入 payload。

支撑：`qa.py::feedback` 已接收 `retrievalSources`；`qa_service.answer` 返回 `confidence`、`cacheLayer`、`retrievalSource`。

### Step A3：证据缺口到自进化联动

- `feedback.dislike` 订阅者调用 `evidence_gap.collect`。
- 聚类生成草稿后，在质量事件中记录 `draft_id`。
- 回流后用 `FEEDBACK_FIX_RATE` 和 `EVOLUTION_LIFT` 验收。

支撑：`knowledge_evolution_service.py`、`evidence_gap_service.py`、`metrics.EVOLUTION_LIFT`。

### Step A4：治理传播 dry-run

- `governance.doc_blocked` 先生成候选清理报告。
- 报告确认后再执行真实清理。
- 清理后 bump G 段，让旧 qa/semantic cache 自动失效。

支撑：`governance_propagate_service.py`、`config.citation_cache_version()`、`test_semantic_gov_filter.py`。

## 推荐开关

```env
QUALITY_BUS_ENABLE=true
DISLIKE_TO_GAP_ENABLE=true
SEMANTIC_CACHE_GOV_FILTER_ENABLE=true
GOVERNANCE_PROPAGATE_ENABLE=false
```

## 验收

| 验收项 | 标准 |
|---|---|
| 差评可追踪 | dislike 后可在质量事件列表看到事件和 payload。 |
| 补全可追踪 | 事件能关联到 evidence_gap。 |
| 回流可验证 | 草稿审核回流后，同 query 检索能命中新知识。 |
| 治理安全 | dry-run 展示将清理的向量、图谱、缓存数量。 |
| 指标可见 | `grid_quality_event_total`、`grid_feedback_fix_rate`、`grid_governance_propagated_total` 有值。 |
