# Track C 调研报告：检索质量与可信答案

## 目标

把检索召回、证据筛选、答案生成、引用校验、用户反馈和在线评测串成可追踪链路，做到“答得不好能定位，引用不可信能阻断，低分样本能回流”。

## 文档支撑

| 依据 | 结论 |
|---|---|
| `docs/系统架构.md` | 问答链路已有缓存、指代消解、路由、混合检索、CRAG、GraphRAG、引用校验和后台评测。 |
| `docs/methodology/retrieval-methodology.md` | 作为检索策略和调参方法论支撑。 |
| `docs/superpowers/specs/2026-07-18-verifiable-citation-design.md` | 可核验引用三层校验设计依据。 |
| `docs/superpowers/plans/2026-07-18-verifiable-citation.md` | 引用校验实施计划依据。 |
| `.superpowers/sdd/task-7-report.md` | 结构化 citation schema、prompt 强约束和降级解析已按 SDD 完成。 |
| `.superpowers/sdd/task-10-report.md` | `_apply_citation_verification` 和校验-CRAG 联动已完成并回归。 |

## 代码支撑

| 能力 | 文件 | 代码事实 |
|---|---|---|
| 混合检索 | `backend/app/services/retrieval_service.py` | `mixed_search` 支持 rewrite、multi-query、dense/BM25、RRF、rerank、metadata filter、MMR、small-to-big、RAPTOR、治理过滤。 |
| 检索调试 | `backend/app/services/retrieval_service.py` / `backend/app/routers/retrieval.py` | `debug_search` 给 `/retrieval/debug` 使用，返回全链路 trace。 |
| 前端调试 | `frontend/src/views/RetrievalDebug.vue` | 展示 query rewrite、multi-query、召回、RRF、rerank、MMR 等步骤。 |
| 问答链路 | `backend/app/services/qa_service.py` | `answer` 串联缓存、CRAG、GraphRAG、LLM、auto_cite、citation verification、rewrite-on-fail。 |
| 引用 schema | `backend/app/schemas/citation.py` | `CitationAnswer`、`CitationItem`、`VerifyResult` 已定义。 |
| 改写 | `backend/app/services/query_rewrite.py` | `rewrite_query_v2` 支持分类、缓存、评估、事件记录。 |

## 测试支撑

| 测试 | 支撑点 |
|---|---|
| `tests/test_citation.py` | 覆盖 auto_cite、citation schema、citation verifier、`_apply_citation_verification`。 |
| `tests/test_rewrite_*` | 覆盖 query rewrite、缓存、事件、评估。 |
| `tests/test_route_aware_retrieval.py` | 覆盖 route-aware 检索策略。 |
| `tests/test_mixed_search_overrides.py` | 覆盖 mixed_search 参数覆盖。 |
| `tests/test_retrieval_eval_metrics.py` | 覆盖检索评测指标。 |
| `tests/test_crag*.py` | 覆盖 CRAG 分级、拒答、证据缺口回流。 |

## 已有能力

1. 主链路具备多级缓存和知识有效性核验。
2. 检索链路具备多策略召回、路由感知权重、ACL、治理硬门禁。
3. CRAG 能对低质量检索做纠错、重检索或拒答。
4. 引用校验能校验编号、相似度、NLI，并可触发 rewrite。
5. 前端已有管理员检索调试页面。
6. 在线评测和检索评测已有服务基础。

## 缺口

| 缺口 | 影响 | 处理建议 |
|---|---|---|
| feedback 与 trace 未统一关联 | 用户差评后难直接定位是哪一步出问题 | 问答返回 traceId，feedback 带 traceId。 |
| debug_search 直接测试不足 | 调试页面结构变化容易回归 | 增加 trace schema 测试。 |
| online_eval 低分闭环弱 | 评测低分不一定进入补全/调参 | 低分统一 emit quality_event。 |
| 生产开关组合不明确 | 能力存在但线上可能未生效 | 固化推荐配置矩阵和灰度顺序。 |
| NLI 异步结果可观测不足 | 后台校验失败难感知 | 将 NLI backfill 结果纳入 trace/metrics。 |

## 方案

### Step C1：traceId 贯穿问答和反馈

- `qa_service.answer/stream_answer` 生成 `traceId`。
- 返回结果和 SSE done 事件携带 `traceId`。
- `FeedbackRequest` 增加 `traceId`。
- `quality_event` payload 保存 `traceId`。

支撑：`qa_service.answer` 已统一组装 result；`qa.py::feedback` 已集中处理反馈。

### Step C2：debug trace 可跳转

- Admin 差评列表增加“查看 trace”。
- 通过 traceId 反查 query、retrieval debug 快照或重新执行 debug_search。
- trace 展示低分原因：rewrite、召回、rerank、治理过滤、CRAG、citation。

支撑：`RetrievalDebug.vue` 已可展示 trace；`retrieval.py::debug` 已有接口。

### Step C3：评测低分进入质量事件

- `online_eval_service.eval_quality` 低于阈值时 emit `online_eval.low_faith`。
- `retrieval_eval_service` recall/precision 低于阈值时 emit `retrieval_eval.eval_low`。
- 订阅者分别进入 evidence_gap 和 retrieval_tune 建议。

支撑：`retrieval_eval_service.py` 已 import `quality_event_bus` 并有 emit 使用点。

### Step C4：生产配置矩阵

推荐基础配置：

```env
QUERY_REWRITE_ENABLE=true
REWRITE_ADAPTIVE_ENABLE=true
REWRITE_EVAL_ENABLE=true
ROUTING_ENABLE=true
RRF_ROUTE_AWARE_ENABLE=true
RERANK_ENABLE=true
MMR_ENABLE=true
SMALL_TO_BIG_ENABLE=true
KG_RAG_ENABLE=true
CRAG_ENABLE=true
CRAG_PERDOC_ENABLE=true
CITATION_AUTO_ENABLE=true
CITATION_VERIFIER_ENABLE=true
CITATION_NLI_ASYNC_ENABLE=true
CITATION_REWRITE_ON_FAIL=true
CACHE_PERSIST_ENABLE=true
SEMANTIC_CACHE_ENABLE=true
SEMANTIC_CACHE_GOV_FILTER_ENABLE=true
```

灰度顺序：

1. 先开 trace/事件，不改变答案。
2. 再开 semantic governance filter。
3. 再开 citation verifier。
4. 最后开 rewrite-on-fail 和治理真实清理。

## 验收

| 验收项 | 标准 |
|---|---|
| 差评可定位 | 每条 dislike 能关联 query、traceId、retrievalSources。 |
| 低分可回流 | online_eval/retrieval_eval 低分样本进入 quality_event。 |
| 引用可信 | 越界引用被剔除，矛盾引用被 drop 或触发 rewrite。 |
| 治理有效 | withdrawn/expired/replaced 文档不会进入 LLM 上下文。 |
| 缓存安全 | 低置信拒答不写脏缓存，治理 G 段变化后旧缓存 miss。 |
| 测试可守护 | mixed_search/debug_search/online_eval 有直接测试。 |
