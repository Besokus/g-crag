# 云端 LLM/Embedding 熔断降级 + 本地兜底 设计文档

日期：2026-08-12

## 1. 背景与问题

调研发现项目已有相对完整的云端 provider 熔断/fallback 骨架，但存在三个实质缺口，直接对应用户的原始问题「云 API 欠费或挂掉时，有没有前端提醒？会不会切到本地模型？」：

1. **LLM fallback 链无本地兜底**：`LLM_FALLBACK_CHAIN` 默认 `"qwen,deepseek,doubao"`，三者皆为云端 API。三者同时不可用（如共用账户欠费、区域网络故障）时无路可退。
2. **全链路耗尽时异常未接住**：`qa_service.answer()` 的 `raw = await _llm_prov.chat(...)` 与 `stream_answer()` 的流式调用均未包 try/except，是全链路中唯一未遵循项目「降级不崩」规范（`app/core/obs.py::degraded`）的关键节点。非流式会变成裸 500，流式会导致 SSE 连接硬断。
3. **前端对降级零感知**：`qa_service.py` 返回给前端的 `modelType` 字段取值是 `model_type or settings.LLM_PROVIDER`（请求参数/默认配置），而不是 `FallbackLLMProvider.last_used_name`（实际命中的 provider）。即使 fallback 已生效，用户看到的模型徽章仍是错的，完全不知道发生过降级。

此外，检索层今天的 commit（`073d91a`）已经给核心 dense 检索加了云端+本地 bge 双路兼底，但前端同样看不到"本次检索云端向量已降级，仅 bge+BM25 命中"这个状态。

## 2. 目标 / 非目标

**目标**
- LLM fallback 链末位新增本地 Ollama 模型，云端全灭时仍能作答（应急，非常规质量保证）
- 前端能看到「本次答案由备用/本地模型生成」「本次检索云端向量降级」两类状态，及触发原因
- 云端 embedding 挂时，语义缓存/改写评估等旁路功能也回退本地 bge，而不是直接跳过
- LLM 全链路（含本地模型）耗尽时优雅拒答，不裸抛异常
- 本地模型可通过管理后台热开关整体禁用（不重启、不影响进行中会话），应对内存不足的部署环境

**非目标**
- 不改造文档入库向量化（`document_service.py`）和 Agent 记忆检索（`agent_memory_service.py`）的 embedding 调用，这两处与"用户日常问答链路可用性"关系较弱，本次不纳入
- 不追求本地模型答案质量对齐云端大模型（7B 量化模型定位是"应急兜底"，前端会明确标注质量降级）
- 不新建通用的运行时配置编辑 UI 框架，只在现有"系统配置"页模式上顺势加一张卡片

## 3. 本地模型：新增 Ollama Provider

- 新增 `backend/app/providers/llm/ollama_llm.py`，结构对齐 `deepseek_llm.py`（Ollama 原生兼容 OpenAI `/v1/chat/completions`，复用 `AsyncOpenAI` SDK，`api_key` 用占位字符串）
- `base_url = f"{settings.OLLAMA_BASE_URL}/v1"`，`model = settings.OLLAMA_MODEL`
- `factory.py::_get_raw_provider` 新增分支 `if p == "ollama": return OllamaLLM()`
- 独立超时 `LLM_LOCAL_TIMEOUT`（CPU 推理慢于云端 API，需要比 `LLM_TIMEOUT` 更宽松，默认 60s）

**模型选型**：`qwen2.5:7b-instruct-q4_K_M`（中文能力强，7B 量化后 CPU 可推理；运行时约占 6-8GB 内存，推理会吃满可用 CPU 核心）。

**健康探活**：`ollama` **不**加入 `_refresh_llm_health_loop` 的周期主动探活（避免平时空转的应急模型每 30s 被迫做一次 CPU 推理）。它的健康状态完全依赖现有的被动熔断机制——`FallbackLLMProvider` 真实调用失败达到 `LLM_CIRCUIT_FAIL_N` 次后走 `record_fail`，进入 `LLM_CIRCUIT_COOLDOWN` 冷却。

## 4. 本地 fallback 开关（热配置，非重启）

`config_service.py` 已有「Redis 持久化 + 内存热读」机制（`rt_llm_fallback_chain()`/`update_llm_router_config()`），改配置即时生效、不影响进行中的请求。本次复用该机制而非新增静态 `settings.py` 字段：

- `config:llm_router` 这个 Redis blob 新增字段 `ollamaEnable: bool`（默认 `true`）
- `_RUNTIME` 新增内存缓存位，`config_service.py` 新增 `rt_ollama_enable()` getter
- `_fallback_chain()`（`llm_router.py`）读取该值，为 `false` 时从链里过滤掉 `"ollama"`
- 新增管理接口 `GET/PUT /system/config/llm-router`（对齐现有 `/system/config/milvus`、`/system/config/prompt` 的路由风格），复用 `config_service.update_llm_router_config`
- `Admin.vue`「⚙️ 系统配置」tab 加一张卡片：一个"启用本地应急模型"开关 + 说明文案"关闭后云端全部不可用时将直接拒答，不使用本地模型"

## 5. 部署：docker-compose 新增 ollama 服务

- 镜像 `ollama/ollama`，挂 volume 持久化已拉取的模型
- 启动命令自动拉取模型：`ollama serve & sleep 3 && ollama pull ${OLLAMA_MODEL:-qwen2.5:7b-instruct-q4_K_M} && wait`
- 加资源限制 `mem_limit`（建议 10g，为模型+运行时开销留余量）与 `cpus`（建议 4），防止真正触发兜底、开始推理时把同机的 mysql/milvus/redis/neo4j 等服务挤爆（OOM/CPU 饥饿）。具体数值按部署机器配置可调，本次先给保守默认。
- `backend` 服务 `depends_on: [ollama]`，但非强依赖——Ollama 未就绪时该 provider 在 fallback 链里失败即可，不阻塞其余云端 provider 正常工作

## 6. 降级信号传递（LLM 侧）

`FallbackLLMProvider`（`llm_router.py`）新增两个实例属性：
- `self.degraded: bool` — `last_used_name != names[0]` 时为 `True`（没用上第一顺位模型）
- `self.degrade_reason: str` — 切换前最后一次异常的简述（如「qwen 不可用(APITimeoutError)，已切换至 doubao」）；若最终命中 `ollama`，固定标注「云端模型全部不可用，已使用本地应急模型」

`qa_service.answer()` / `stream_answer()` 调用 LLM 后读取这两个属性，连同真实 `last_used_name` 一起写入返回字典：
- `modelType`：改用 `last_used_name`（修复现有失真 bug）
- `llmDegraded` / `llmDegradedReason`
- 若 `last_used_name == "ollama"`：对 `confidence` 做 `min()` 封顶到 `"medium"`（不改 CRAG 内部分级逻辑，只在最终返回值封顶），避免「绿色 high 徽章」与「本地应急模型警告」语义打架

## 7. 降级信号传递（检索侧）

复用项目里已有的 trace 打点机制（`_get_trace()/mark()`，与现有标记 `provider_used` 是同一套，不改动 `mixed_search`/`_dense_and_sparse`/`debug_search` 的返回签名）：

- `_dense_dual`（`retrieval_service.py`）云路 `except` 分支里加 `tc = _get_trace(); tc and tc.mark("dense_cloud_failed", True)`
- `qa_service.answer()` 在检索之后读该 mark，写入返回字典：`retrievalDegraded` / `retrievalDegradedReason="云端向量检索不可用，已降级为本地embedding+关键词检索"`

## 8. 旁路 embedding 也接 bge 兜底

云端 embedding 调用点排查结果：
- `semantic_cache.py`（2 处，语义缓存的 embed 调用）—— **本次纳入**，云端异常时回退调用 `bge` provider
- `rewrite_evaluator.py`（1 处，多查询改写效果评估）—— **本次纳入**
- HyDE（`_hyde_or_cache`）本身不直接调用 embedding，生成的假设文档文本走下游已双路兼底的 dense 检索，天然覆盖，不用改
- `document_service.py`（文档入库向量化）、`agent_memory_service.py`（Agent 记忆检索）—— **不纳入本次范围**（见「非目标」）

## 9. LLM 全链路耗尽时的优雅降级

- **非流式** `qa_service.answer()`：包 try/except，命中时 `degraded("llm_all_exhausted", e)`，返回结构化拒答（`confidence: "refused"`, `cragAction: "llm_all_down"`），保留已检索到的 `retrievalSource`（沿用现有"无检索结果"兜底模式里保留证据的做法），并调用 `evidence_gap_service.collect(..., source="auto_llm_down", ...)` 记录知识缺口（`source` 字段是自由字符串，`Admin.vue` 的展示映射表需要补一条 `auto_llm_down: '服务不可用'`）
- **流式** `stream_answer()`：`async for token in _llm_prov.stream(...)` 包 try/except
  - 还未吐出任何 token 就异常 → yield `{"type": "error", "content": "当前所有 AI 模型（含本地应急模型）暂时不可用，请稍后重试", "confidence": "refused"}`（前端 `Chat.vue` 已原生支持 `ev.type==='error'` 分支，无需改前端逻辑）
  - 已吐出部分 token 才异常 → 保留已生成内容，追加「（后续生成中断：服务异常）」后转成正常 `done` 事件收尾，而非让 SSE 连接硬断导致前端只能显示语焉不详的"流式中断"

## 10. 前端展示（Chat.vue）

在现有 badge 行（`cached`/`confidence`）追加两个条件 badge：
```html
<span v-if="m.llmDegraded" class="badge badge-warn" :title="m.llmDegradedReason">
  {{ m.modelType === 'ollama' ? '🖥️ 本地应急模型' : '⚠️ 备用模型' }}
</span>
<span v-if="m.retrievalDegraded" class="badge badge-warn" :title="m.retrievalDegradedReason">⚠️ 检索降级</span>
```
`onStreamEvent` 的 `done`/`error` 分支透传新字段（`llmDegraded`/`llmDegradedReason`/`retrievalDegraded`/`retrievalDegradedReason`），写法对齐现有 `ev.confidence`/`ev.cacheLayer`。

## 11. 配置项汇总（`backend/app/config.py`）

```python
OLLAMA_BASE_URL: str = "http://ollama:11434"
OLLAMA_MODEL: str = "qwen2.5:7b-instruct-q4_K_M"
LLM_LOCAL_TIMEOUT: int = 60
LLM_FALLBACK_CHAIN: str = "qwen,deepseek,doubao,ollama"   # 原值追加 ollama
```
`ollamaEnable` 不放在 `config.py`（静态需重启），而是走第 4 节的热配置体系。

## 12. 测试计划

- `OllamaLLM` 的 `chat`/`stream` 单测（mock `AsyncOpenAI`，仿照现有 provider 测试写法）
- `FallbackLLMProvider.degraded`/`degrade_reason` 赋值正确性（前几个 provider 抛异常、最终成功场景）
- `qa_service.answer()` mock 全 provider 失败 → 断言返回结构化拒答而非抛异常，且带 `retrievalSource`
- `stream_answer()` mock 中途失败（有/无已吐 token 两种情况）→ 断言 yield 出预期的 `error`/`done` 事件
- `_dense_dual` 云路异常 → 断言 trace mark 被设置，`qa_service.answer()` 透出 `retrievalDegraded=True`
- `rt_ollama_enable()` / `update_llm_router_config` 热切换后 `_fallback_chain()` 剔除/恢复 `"ollama"` 的行为
- `semantic_cache.py`/`rewrite_evaluator.py` 云端 embedding 异常 → 回退 bge 成功路径的单测
- ollama 命中时 confidence 封顶到 medium 的断言

## 13. 涉及文件一览

- 新增：`backend/app/providers/llm/ollama_llm.py`
- 修改：`backend/app/providers/factory.py`、`backend/app/providers/llm_router.py`
- 修改：`backend/app/services/qa_service.py`（`answer`/`stream_answer`）
- 修改：`backend/app/services/retrieval_service.py`（`_dense_dual`）
- 修改：`backend/app/rag/semantic_cache.py`、`backend/app/services/rewrite_evaluator.py`
- 修改：`backend/app/services/config_service.py`（`rt_ollama_enable`/`load_runtime`/`update_llm_router_config`）
- 修改：`backend/app/routers/system.py`（新增 `/system/config/llm-router`）
- 修改：`backend/app/services/evidence_gap_service.py` 调用处（新 `source` 值，非函数签名变更）
- 修改：`backend/app/config.py`（新增配置项）
- 修改：`docker-compose.yml`（新增 `ollama` 服务）
- 修改：`frontend/src/views/Chat.vue`（新增 badge + 事件字段透传）
- 修改：`frontend/src/views/Admin.vue`（本地兜底开关卡片 + evidence gap source 展示映射补一条）
