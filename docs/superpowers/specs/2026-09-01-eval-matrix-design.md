# 开关对照评测矩阵与能力收益矩阵 设计（capability eval matrix）

> **版本:** v1.0（已实现，2026-09-01；实跑验证待 Milvus/LLM 环境可用后按 plan Task 5 Step 2/3 执行）| **日期:** 2026-09-01 | **依据:** 纯代码审计（`config.py` 全部开关、`services/`/`rag/` 生效层、`scripts/` 评测脚手架、`tests/redteam/` 实跑）+ 市场主流方案桌面调研（来源见 §3）。**现状结论全部以代码为准，未采信任何 docs/*.md。**

---

## 1. 背景与问题（代码事实）

1. **开关多、对照缺。** `backend/app/config.py` 实有 13 个"默认关=现状"的 QA 主链路 opt-in 开关（`RRF_ROUTE_AWARE_ENABLE:156`、`RAPTOR_ENABLE:157`、`SEMANTIC_CACHE_ENABLE:158`、`CRAG_NEIGHBOR_EXPAND_ENABLE:171`、`CRAG_PERDOC_ENABLE:175`、`CRAG_V3_ENABLE:177`、`DEBATE_ON_LOW_CONFIDENCE_ENABLE:179`、`SUFFICIENCY_GATE_ENABLE:180`、`MULTI_TURN_CACHE_ENABLE:216`、`HYDE/MULTI_QUERY/SELF_RAG_ENABLE:225-227`、`CITATION_VERIFIER/NLI_ENABLE:255-256`）。**没有任何一条管线产出"开 vs 关"的对照数据**——开关要么永远不敢开，要么凭感觉开。
2. **现有评测脚手架只覆盖"固定三维"。** `scripts/eval_suite.py` 串行调 `eval_retrieval / eval_generation / eval_citation` 三维度（recall 门禁 0.85、faithfulness 门禁 0.85、citation 关联率门禁 0.8），全部针对**当前 env 下的单一配置**跑一遍，无变体概念。`retrieval_eval_service.evaluate_over_golden(db, overrides, topk)`（`retrieval_eval_service.py:62`）虽支持 overrides，但 `mixed_search` 的 `_ov` 只覆盖**参数级**（RERANK/MMR/SMALL_TO_BIG/RRF 权重/ef）；`HYDE/MULTI_QUERY/RAPTOR/RRF_ROUTE_AWARE` 等在 `retrieval_service.py:187/325/335/532` 直接读 `settings`，`CRAG_V3/DEBATE/SUFFICIENCY/SEMANTIC_CACHE/CITATION_*` 在 `qa_service.py:335/388/785...` 读 `settings`——**进程内 overrides 触达不到功能开关**。
3. **golden 集薄。** `backend/data/` 实测 `golden_qa.json` 32 条、`golden_citation.json` 4 条、`golden_tickets.json` 10 条；对照结论必须带样本量噪声提示，且扩集是后续独立工作。
4. **红队已有离线种子（另一工作线维护中）。** `tests/redteam/` 4 文件 52 用例（注入/越权/幻觉/缓存污染）当前实跑全绿、尚未入库；本 spec 只做定位引用，**不改动该目录**。
5. `scripts/eval_generation.py` 的 `BASE="http://127.0.0.1:8001"` 写死在模块级且逻辑都在 `main()` 里不可复用；`scripts/eval_citation.py` 已有可导入的 `evaluate()`（`:33`）。

## 2. 目标 / 非目标

**目标**
- 一条命令产出：**每个开关"开 vs 关（现状）"在 golden 集上的多维指标对照**（检索维 recall/MRR/nDCG/空结果率；生成维 faithfulness/幻觉率/平均时延），汇总成单份"能力收益矩阵"报告（markdown + JSON）。
- 对照结论带**建议列**（建议常开候选 / 维持关闭 / 建议回收），人拍板——对齐仓库既有"评测先行、retrieval_tune 只建议"纪律。
- 业务代码零侵入：不改任何 service 行为；矩阵本身**不新增运行时开关**、不进 `citation_cache_version()`。

**非目标（YAGNI，本期不做）**
- 不扩 golden 集（独立工作项，本报告只做噪声标注与扩集建议）。
- 不做 agent 轨迹级评测（tool-call/trajectory 打分）——市场已验证方向，留待矩阵底座就绪后立项。
- 不动 `tests/redteam/`（另一工作线维护）；不做 LLM-in-the-loop 自适应攻击。
- 不做"依据矩阵自动改 `.env`/常开开关"的自动化——只建议。
- 不做前端/管理页展示——产物是 `reports/` 下文件。

## 3. 市场主流方案调研（2026-09 检索）与对标

### 3.1 RAG/LLM 评测框架
主流格局是三件套分工：**RAGAS**（实验期指标库）、**DeepEval**（pytest 式 CI 门禁）、**TruLens**（生产追踪+反馈），外加 **Promptfoo**（配置矩阵×断言、红队插件）、**Langfuse/Arize Phoenix/LangSmith/MLflow**（观测+评测分层）。第三方横评的共识结论："RAGAS 做快速实验、DeepEval 做 CI 门禁、TruLens 看生产"（[Particula 对比](https://particula.tech/blog/deepeval-vs-ragas-vs-trulens-rag-evaluation-stack)、[AIMultiple 六工具横评](https://aimultiple.com/rag-evaluation-tools)、[DeepEval Top5](https://deepeval.com/blog/top-5-llm-evaluation-frameworks)、[Medium 全景对比（含 Promptfoo/微软 SDK）](https://medium.com/@mirzasamaddanat/ai-evaluation-frameworks-compared-ragas-deepeval-microsoft-ai-evaluation-sdk-trulens-91a4a4e6fa6b)）。

**对标**：本仓库已有 DeepEval 式"门禁层"（golden 三维门禁 + LLM-judge faithfulness）与 Langfuse 式"观测层"（OTel 全链路），**缺的恰是 Promptfoo 式"配置矩阵一次跑 + 横向对比报告"层**——这正是本 spec 补的位，而非重造指标。

### 3.2 LLM 红队
主流：**Garak**（NVIDIA，静态探针库）、**PyRIT**（微软，自适应多轮攻击编排）、**Promptfoo redteam / DeepTeam / Giskard**（应用侧 OWASP 插件化），方法论锚点是 **OWASP LLM Top 10 (2025)**（LLM01 注入 / LLM02 敏感信息 / LLM06 过度代理 / LLM09 错误信息…）（[Garak vs PyRIT 对比](https://aisecurityandsafety.org/en/compare/garak-vs-pyrit/)、[OWASP GenAI 官方](https://genai.owasp.org/llm-top-10/)、[2026 三方对比](https://beyondscale.tech/blog/ai-red-teaming-tools-comparison-2026)）。本仓库 `tests/redteam/` 52 离线结构断言 ≈ Garak 式静态探针的最小自研版，演进方向（自适应攻击、CI 常备）由该工作线承接，不在本 spec 范围。

### 3.3 Agent 评测
2026 共识是**三层评测**：final-answer / trajectory（工具调用轨迹）/ per-step（[Morphllm 综述](https://www.morphllm.com/ai-agent-evaluation)、[MLflow Top5](https://mlflow.org/top-5-agent-evaluation-frameworks/)、[ACL 2026 Findings Survey](https://aclanthology.org/2026.findings-acl.1330/)）。本仓库 `agent_tool_audit_service` + `qa_trace` 已有底座，轨迹评测是矩阵之后的自然延伸（非目标本期不做）。

### 3.4 AIOps 闭环
市场从"相关度聚合"转向 **Agentic ITOps**：BigPanda（检测→分诊→解决的 agentic 闭环）、Keep（开源告警编排 + YAML workflow）、Shoreline（runbook 自动修复，被 NVIDIA 收购），研究侧 LLM RCA agent（[arXiv 2403.04123](https://arxiv.org/abs/2403.04123)）与 L5 闭环成熟度模型（[Keep 官网](https://www.keephq.dev/)、[BigPanda](https://www.bigpanda.io/)）。本仓库已实现的"告警→只读诊断→人工确认→转票→质量事件回填"（`realtime_event_service` 三流转 + `PROACTIVE_FEEDBACK_ENABLE`）对位"人在环的 L3~L4"，深化依赖真实数据与安全评审，本期不动。

### 3.5 国内电网行业
国网"光明"大模型（千亿级多模态，覆盖 27 家省公司、600+ 业务场景）、南网"大瓦特"（100% 自主可控，输电缺陷识别/山火评估），叠加四部门"推动 5 个以上能源专业大模型深度应用"政策（[新华网 2026-08](https://www.news.cn/energy/20260803/824c3bd8bab34f03a3a932debc21f7fd/c.html)、[南网数研院 2026-05 投关记录](https://www.sgpjbg.com/)）。行业路径的共同点是**"每个上线能力都有评测背书"的工程纪律**——本 spec 是该纪律在本仓库的落点。

**调研结论**：主流方案收敛于"评测驱动 + 矩阵化对照 + 红队常备 + 人在环闭环"。本仓库纵轴能力齐备，唯独缺"开关级对照矩阵"这一层；且它是后续一切新开关（LC/RAG 混合路由、本地裁决器、微调收益对比）的**前置度量底座**。

## 4. 方案设计

### 4.1 总体
新增一个**评测工具层**（无新运行时开关）：

```
scripts/eval_matrix.py (CLI 编排/探针)          backend/app/services/eval_matrix_service.py (纯核心)
├─ variants 注册表（引用 service 常量）          ├─ VARIANTS: 13+1 开关注册表（name/env/dims）
├─ 检索探针：子进程 + env 覆盖                   ├─ build_env_overlay(variant)
│    └─ retrieval_eval_service.evaluate_over_golden  ├─ compute_delta / build_verdict
├─ 生成探针：每 variant 起 uvicorn 子进程        ├─ aggregate(probe_jsons) → 矩阵
│    └─ eval_generation.run_generation_eval      └─ render_markdown → reports/eval_matrix_<ts>.md
└─ 聚合落盘 reports/eval_matrix_<ts>.{json,md}
```

**采集机制（关键决策）**：统一走"**子进程 + 环境变量覆盖**"——每个 variant 由父进程以 `{...os.environ, **variant.env}` 启动独立子进程，pydantic-settings（`case_sensitive=False`）自动从 env 读开关。这样**不改动任何业务代码**即可对照"直接读 `settings`"的功能开关（§1.2 的进程内 overrides 触达不到的那些）。此模式复用 `eval_suite.run_dim`（`eval_suite.py:31-50`，已含 Windows GBK→utf-8 强制）的既有先例。

### 4.2 变体注册表（全部为 config.py 实有键，关=现状）

| variant | env 覆盖 | 维度 | 备注 |
|---|---|---|---|
| `baseline` | {}（全关） | retrieval+generation | 现状对照基线 |
| `rrf_route_aware` | `RRF_ROUTE_AWARE_ENABLE=true` | retrieval | `retrieval_service.py:325` |
| `crag_neighbor` | `CRAG_NEIGHBOR_EXPAND_ENABLE=true` | retrieval | 邻域扩展 |
| `raptor` | `RAPTOR_ENABLE=true` | retrieval | `retrieval_service.py:532` |
| `hyde` | `HYDE_ENABLE=true` | retrieval | `retrieval_service.py:187` |
| `multi_query` | `MULTI_QUERY_ENABLE=true` | retrieval | `retrieval_service.py:335` |
| `self_rag` | `SELF_RAG_ENABLE=true` | retrieval | qa 层判据影响检索触发 |
| `crag_v3` | `CRAG_V3_ENABLE=true` | generation | `qa_service` 置信链 |
| `crag_perdoc` | `CRAG_PERDOC_ENABLE=true` | generation | LLM 逐条评估，**token 成本注意** |
| `citation_verifier` | `CITATION_VERIFIER_ENABLE=true` + `CITATION_STRUCTURED_OUTPUT=true` | generation | 校验联动 `CITATION_REWRITE_ON_FAIL`（默认 true） |
| `citation_nli` | verifier 三个键 + `CITATION_NLI_ENABLE=true` | generation | 最重档 |
| `debate` | `DEBATE_ON_LOW_CONFIDENCE_ENABLE=true` | generation | 预期增延迟（成本项，报告列时延） |
| `sufficiency_gate` | `SUFFICIENCY_GATE_ENABLE=true` | generation | 同上 |
| `query_rewrite` | `QUERY_REWRITE_ENABLE=true` | generation | 自带评估闭环，矩阵交叉验证 |
| `semantic_cache` | `SEMANTIC_CACHE_ENABLE=true` | generation | **首跑不命中，收益看时延列，报告带 caveat** |

排除项及理由：`MULTI_TURN_CACHE_ENABLE`（golden 无多轮对，需先建多轮评测集）；citation 独立维度（`eval_citation.evaluate()` 是对 golden_citation 的离线打分，不读这些开关——flag 不敏感，做不了变体对照；运行时 NLI 的行为差异已被 `citation_nli` 变体的生成维覆盖）。

### 4.3 维度与指标
- **retrieval 探针**（子进程直调，需 Milvus+embedding，无需 LLM/后端）：`AsyncSessionLocal` 起会话 → `retrieval_eval_service.evaluate_over_golden(db, None, topk)` → `{recall, mrr, ndcg, noResultRate, sampleSize}`。
- **generation 探针**（每 variant 起后端子进程，需 LLM key）：`eval_generation.run_generation_eval(base_url, limit, gate)` → `{faithfulness, hallucination, avgLatencyMs, sampleSize}`（新增平均往返时延，为 debate/semantic_cache 类变体提供成本视角）。
- **verdict 规则**（只建议）：任一指标比 baseline 恶化 > 0.005（时延除外，仅列示）→"建议回收（存在退化）"；主指标（检索维 recall / 生成维 faithfulness）提升 ≥ 0.01 / ≥ 0.02 且无退化 → "建议常开候选"；其余 → "维持关闭（收益不足）"。

### 4.4 产物
`reports/eval_matrix_<ts>/probe_<variant>_<dim>.json`（逐次原始值）+ `reports/eval_matrix_<ts>.md`（矩阵表 + Δ 列 + 建议列 + **样本量 <50 的噪声警告** + semantic_cache caveat + 运行环境摘要）+ `reports/eval_matrix_<ts>.json`（机器可读汇总）。

### 4.5 兼容与安全
- 业务 service 零改动；唯一既有文件改动是 `eval_generation.py` 抽函数（CLI 行为逐字节不变，仅新增 `--base-url` 可选参数）。
- 不新增任何 `config.py` 字段；`.env.example` 不动；不进 `citation_cache_version()`。
- CI 不跑矩阵（需 Milvus/LLM，与 eval_retrieval/generation 同级的"手动/夜间"定位）；CI 只跑矩阵纯函数与探针编排的单测（全 mock，不碰服务）。
- Windows 兼容：子进程 env 强制 `PYTHONIOENCODING=utf-8`/`PYTHONUTF8=1`（`eval_suite.run_dim` 同款）；后端子进程起停用 `Popen.terminate()`→`kill()` 兜底。

## 5. 实现拆分（↔ plan 任务）

1. `eval_matrix_service.py` 纯核心（VARIANTS/env/delta/verdict/render）+ CI 单测 ↔ Task 1
2. 探针模式与 runner 骨架（`scripts/eval_matrix.py --probe retrieval`）+ 单测 ↔ Task 2
3. `eval_generation.py` 抽 `run_generation_eval` + `--base-url`（行为不变）+ 后端子进程起停 ↔ Task 3
4. runner main 聚合落盘（串 variants×dims、起停后端、写报告）+ 单测 ↔ Task 4
5. 全量回归 + lint + 实跑记录 + AGENTS.md 命令行补充 ↔ Task 5

## 6. 风险与对策

1. **样本噪声**：golden_qa 32 条，Δ<2pp 无统计意义 → 报告强制噪声警告；扩集列为本矩阵后的独立项。
2. **LLM 成本**：生成维全矩阵 ≈ 15 variant × limit(默认5) × (1 次问答 + judge) ≈ 150 次调用 → `--limit`/`--variants` 可裁剪，默认值保守。
3. **后端子进程预热**：bge 预热 ~20s → 健康等待超时默认 240s、逐 variant 串行。
4. **语义缓存等"二跑才见效"的变体**：报告 caveat 明示"收益看时延列、需二次运行验证"，不假装首跑能测出。
5. **结论误用**：verdict 恒为"建议"，不改配置不自动开开关——决策留人。
