# 链路节点详情（点击看指标/参数/Prompt）— 设计 spec

> 日期：2026-08-29 ｜ 状态：待用户审阅（本文档只出方案，未实现）
> 关联：`core/qa_trace.py`（采集器）、`QaTraceChart.vue`（瀑布图）、`models/qa_trace.py`（落库）、`otel_genai.py`（Langfuse 双写）

## 背景与目标

链路耗时瀑布图（Chat「📊 链路耗时」）目前每个节点只有 **耗时/占比/状态**，出问题时（如"卡在哪个节点"）无法就地回答"这个节点当时用了什么参数、检索了什么、发给 LLM 什么 prompt"。

**目标**：瀑布图每个节点可点击，展开显示该节点的 ① 运行指标 ② 生效参数 ③ Prompt/输入输出细节。历史 trace（`GET /api/qa/trace/{id}`）同样可看（数据落库即带）。## 现状盘点（都是现成的，缺的是串起来）

| 已有 | 位置 | 缺口 |
|---|---|---|
| span 级 `attrs` 任意 JSON | `qa_trace.TraceCollector.span(name, group, **attrs)`，`record()` 同 | **所有打点都没传 attrs**——只记了 dur/status |
| 落库自动带 attrs | `qa_trace.spans_json` 存 `to_dict()` 全量 | 无需改表 |
| 历史 API | `GET /api/qa/trace`、`/api/qa/trace/{id}`（`qa_trace_service.list_traces/get_trace`，租户隔离已有） | 前端无页面消费（仅 api wrapper） |
| LLM 真实 token usage | `provider.chat_with_usage()`（同一次调用顺带返回，**零额外成本**；`LLM_USAGE_TRACK_ENABLE` 默认关） | 未挂到 trace |
| Prompt 全文 | 各 LLM 调用点的 `messages` 变量就在现场（如 `qa_service.py` 主答 `build_messages_with_history(...)`） | 未捕获 |
| Langfuse 全链路归档 | otel 双写，`qa_trace.trace_id == otel trace_id` | 无前端深链入口 |
| Agent 模式步骤详情 | `AgentTrace.vue`（tool/args/result 已有） | 不在本期范围（已够用） |

## 设计总览（三层，全部零 schema 变更）

```
采集层  各节点打点处补 attrs（新 helper 统一做 prompt 截断/脱敏）
   ↓        span attrs 随 to_dict() 自动进响应体 + spans_json 落库
数据层  qa_trace 表不动；响应体 trace.spans[].attrs 前端直接可用
   ↓
展示层  QaTraceChart 行点击 → 行下内嵌展开详情区（指标/参数/Prompt 分区）
```

## 节点详情字段清单（核心交付物）

### 1. `llm` LLM 生成（主答，`qa_service.py` 非流式 :945 / 流式 :1567）

| 分区 | 字段 | 来源（现场变量） |
|---|---|---|
| 指标 | dur、pct、provider/model、**tokens in/out**、tokens/s | `_llm_usage`（开 `LLM_USAGE_TRACK_ENABLE` 即有，同调用免费）、`_llm_fields` |
| 参数 | temperature（`config_service.rt_temperature()`）、max_tokens（`settings.LLM_MAX_TOKENS`）、llm_tier + 路由原因（已有 marks）、structured_output 开关 | 现场变量 |
| Prompt | system prompt、user prompt（含检索上下文）、消息数 | `messages`（`prompt_templates.build_messages_with_history` 产物） |
| 输出 | 答案前 200 字、是否结构化 JSON | `raw` |

### 2. `retrieval` 混合检索 + 检索子链（`retrieval_service.py`）

| 节点 | 补充 attrs | 现场来源 |
|---|---|---|
| `retrieval`（总） | route、topk、cand、最终命中数、各来源计数（dense_cloud/dense_bge/bm25）、top 分数 | `routing_decision`、`pool` |
| `query_rewrite` | 原始 query → 改写后 query、是否跳过（adaptive） | `rewrite_query_v2` 返回 |
| `dense_search` | ef、cand、双路各命中数 | `_ef`、`dense_cloud/dense_bge` |
| `sparse_search` | topk、top BM25 分（已归一化 0-1） | `all_sparse` |
| `rrf` | k、dense/sparse 权重（route-aware 生效值）、融合数 | `_ov` 参数现场 |
| `rerank` | top_n、超时、重排后 top 分 | `ranked` |
| `mmr` | λ（query_type 生效值）、候选数 | `_default_lambda` |
| `filter_acl` | 过滤前/后条数、命中的过滤条件（tenant/docType/dept） | 过滤块现场 |
| `hyde` / `standalone_rewrite` / `multi_query` | 改写/假设文档产物前 200 字 + 各自 prompt | 各自 LLM 调用点 |
| `small_to_big` / `raptor` | 扩展父块数 / 摘要层命中数 | 现场变量 |

### 3. `crag` CRAG 自纠错（`qa_service.py:892`）

- attrs：grade（correct/ambiguous/incorrect）、action、confidence、es、v1/v2/v3 路径、改写后 query（若触发重检索）、extras（confidenceScore/Label/evidenceStrength）——全部在 `_crag_correct` 返回值里。

### 4. `graphrag`（:900）/ `citation`（:965）

- graphrag：实体数、返回因果链条数、top 实体。
- citation：引用率、auto_cite 补标数、verifier 分层结果（开启时）。

### 5. `cache_lookup` / `hotqa` / `standalone_rewrite`（缓存族）

- attrs：查询的 key 前缀、各层命中结果（L1/L1.5/L2）、黑名单命中、最终 cacheLayer（已有 mark）、hotqa 命中的问答对 id。

### 6. `routing`（:860）

- attrs：route、confidence、query_type、特征摘要（长度/疑问词/设备词）、路由理由——`routing_decision.features/reason` 现成。

## 采集实现要点

1. **新 helper `qa_trace.llm_attrs(messages, temperature, max_tokens, usage, model, output)`**：把 prompt 细节扁平化成 attrs dict，统一做——
   - 截断：system/user 各取前 `QA_TRACE_PROMPT_CHARS`（默认 1200）字符，`truncated: true` 标记；
   - 脱敏：出站文本过既有 `safety.mask_pii`（`PII_MASK_ENABLE` 开时生效，与答案同一管道）；
   - 大小预算：单 span attrs 总量 ≤ 8KB，超限丢弃 prompt 只留参数（`prompt_omitted: true`）。
2. **打点改造**：约 10 个调用点从 `with _trace_span("xxx"):` → `with _trace_span("xxx", **attrs)`（attrs 变量在行上组装，不膨胀调用行）；LLM 用 `record("llm", dur, **llm_attrs(...))`。
3. **开关**：`QA_TRACE_DETAIL_ENABLE: bool = False`（**关=现状**，只记耗时；开=补 attrs）。纯观测能力，**不进 citation_cache_version**（不影响答案语义/缓存 key）。
4. **usage**：建议同时把 `LLM_USAGE_TRACK_ENABLE` 打开说明写进 `.env.example`（`chat_with_usage` 是同一次调用顺带读 usage，零额外成本），不开则 tokens 字段显示"未采集"。
5. **Langfuse 深链**（可选件）：span 详情面板放「在 Langfuse 查看完整链路」→ `${VITE_LANGFUSE_HOST}/trace/{traceId}`；全文 prompt 完整性依赖 Langfuse 侧采集，本期不做入库全文。

## 前端交互（`QaTraceChart.vue` 改造）

- 行点击 → 行下方**内嵌展开**详情区（不用抽屉/弹窗，与消息流内嵌场景匹配；再点收起；同一时间只展开一行）。
- 详情区按 `span.name` 查前端映射表 `TRACE_DETAIL_SCHEMA` 渲染三个分区：
  - **指标**：key-value 网格（dur/pct/status/tokens/tps…），error 红显 `err`；
  - **参数**：key-value 网格（中文名映射同现有 `markLabel` 风格）；
  - **Prompt/IO**：折叠预览（默认 4 行，点击展开全文）+ 复制按钮；`truncated` 标注「已截断，全文见 Langfuse」。
- 无 attrs 的老 trace（开关未开时期的历史数据）行不显示可点击态，零破坏。
- 历史诊断：`/api/qa/trace/{id}` 返回同构数据，本期先在 Chat 内嵌入口满足；独立"链路诊断页"留待后续（API 已就绪，纯前端页面工作）。

## 存储与性能影响

- spans_json 现 ~2-4KB/条 → 开详情后 ~8-16KB/条（截断后），写库仍走 fire-and-forget 独立 session + `QA_TRACE_SAMPLE_RATE` 采样，无同步路径影响；
- 响应体增大 ~4-10KB（实时瀑布必带），相对答案体可忽略；
- 采集全部在 try/except 隔离内（沿用 span() 既有铁律：trace 失败绝不影响主链路）。

## 实现拆分（预估：后端 1 天 + 前端 1 天 + 验证 0.5 天）

1. **Task 1**：`qa_trace.py` 加 `llm_attrs` helper + `QA_TRACE_DETAIL_ENABLE` 开关 + 单测（截断/脱敏/预算）；
2. **Task 2**：主链路打点补 attrs（llm/crag/graphrag/citation/cache/routing/standalone）+ 流式路径同步；
3. **Task 3**：`retrieval_service` 子链 attrs（query_rewrite/dense/sparse/rrf/rerank/mmr/filter_acl）；
4. **Task 4**：`QaTraceChart.vue` 点击展开 + `TRACE_DETAIL_SCHEMA` 渲染 + 截断展开/复制；
5. **Task 5**：`.env.example` 同步、Docker 验证（开开关跑一条真实问答，逐节点核对详情面板）。

## 非目标（YAGNI）

- 不做嵌套时间轴/火焰图（平铺行 + 内嵌详情够定位问题）；
- 不做 prompt 全文入库（Langfuse 承担全文归档，应用内只存截断预览）;
- 不做跨请求聚合分析（慢节点排行等，等有需求另立 spec）;
- 不做 Agent 链路改造（AgentTrace 已有 args/result）；
- 流式 first-token 延迟细分（stream 路径 LLM 只记总耗时，现状保留）。

## 实现与验证记录（2026-08-30，已实现并 Docker 实测通过）

- **提交**：开关+helper（`09552a7`）→ 主链路接线（`ff5a318`）→ 检索子链+补 span（`83f814e`）→ 前端展开（`71344fd`）。
- **回归**：`pytest tests/ -q --ignore=tests/test_api.py -m "not integration"` → 675 passed / 8 failed（stash 基线比对：同样 8 个既有失败，667 passed 基线 + 8 个新增测试，零回归）；改动 Python 文件 ruff 无新告警（qa_service 17 条均为存量）；`npm run build` ✓。
- **Docker e2e（compose 开 `QA_TRACE_DETAIL_ENABLE: "true"`，真实 LLM）**：
  - 非流式：normalize→…→citation 全链 16 span，attrs 全部符合字段清单——`llm`(temperature/maxTokens/model/nMessages/promptSystem/promptUser/output+截断标记)、`crag`(grade/action/confidence/extras)、`routing`(route/confidence/reason/queryType)、`retrieval`(hits/route/top1)；新增计时 span `sparse_search`/`rrf`/`mmr`/`filter_acl` 均出现且带 attrs（BM25 段 ~700ms、RRF 融合段耗时首次可见）。
  - 流式：done 事件 trace 同构——`llm` attrs 齐全（usage=None 符合预期，`LLM_USAGE_TRACK_ENABLE` 未开）；CRAG 触发重检索时两轮检索子链 attrs 完整；流式新增 `citation` record 行（refs/annotated）。
- **验证中发现并修复**：citation attrs 初版接线放到了 `with _trace_span("citation")` 开启之前（attach 找不到同名 span 静默 no-op）→ 非流式把 attach 挪到 span 关闭后；流式路径本无 citation span → 改用 `record()` 带计时+attrs（开关内，关=无此行）。
- **flag 语义**：关（默认）时所有 attach/record 短路，瀑布图与改造前一致（attrs 不存在）；compose 已显式置 true。
