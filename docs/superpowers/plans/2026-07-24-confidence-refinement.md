# 置信度/CRAG 标签体系细化 · 实现计划（bite-sized TDD）

- **spec**：`docs/superpowers/specs/2026-07-24-confidence-refinement-design.md`（commit 待填）
- **起点 BASE**：待填（spec 评审通过后）
- **硬约束**：全 opt-in 默认关；**前端零改动**（老字段不动，新字段附加）；每 task 独立 commit；测试先行；显式 git add；main 分支
- **复用范式**：`crag`/`crag_v2`/`_crag_correct`/`config_service.rt_*`/`citation_cache_version`，零新底座
- **落地顺序**：Batch 1(P1 核心,evidence_strength 基础先行) → Batch 2(P2 稳健) → Batch 3(P3 拉通+收口)

---

## Batch 1 · P1 核心（断点 A+B，最高 ROI）

### Task 1 · evidence_strength 统一函数（基础）
- **测试** `tests/test_confidence_refinement.py`：① v2 路径 `(relevant+0.5·partial)/n` 计算正确；② v1 路径返回 top1 分；③ rerank 降级返 `None`。
- **文件**：`rag/crag.py`（新增 `evidence_strength(*, top1=None, detail=None, rerank_ok=True) -> float|None`）
- **验证**：pytest 绿；纯函数无 IO。
- **commit**：`feat(confidence): T1 evidence_strength统一函数(v1/v2/rerank降级三路)`

### Task 2 · 捡回 v2 detail + 组装归因（断点 A）
- **测试**：`_crag_correct` v2 路径返回 `cragDetail={relevant,partial,irrelevant,n}` 计数正确 + `cragReason` 含计数文本；`CRAG_V3_ENABLE=False` 不带新字段（现状）。
- **文件**：`services/qa_service.py::_crag_correct`（`grade, _detail = crag_v2.grade_with_llm(...)` 不再丢；CRAG_V3_ENABLE 开时组装 `cragDetail`/`cragReason`）+ `rag/crag_v2.py::grade_with_llm`（确认 detail 上抛，labels_to_grade 已返 detail）
- **验证**：pytest 绿；关开关 done 事件字段 diff=0。
- **commit**：`feat(confidence): T2 捡回v2 detail+cragReason归因(CRAG_V3_ENABLE,opt-in)`

### Task 3 · confidence_of 重构·连续 score + 5 档 + 修 low（断点 B，依赖 T1）
- **测试**：① 5 档边界（0.7/0.5/0.35/0.2）；② `low` 档真产出（ambiguous+degraded 场景）；③ 老字段 `confidence` 由 label 首段映射（high/medium/refused）兼容；④ `CRAG_V3_ENABLE=False` 走老 `confidence_of` 3 档。
- **文件**：`rag/crag.py`（`confidence_of` → `confidence_score(es, matrix_cell, degraded) -> (score, label)`；保留老 `confidence_of` 关开关时用）+ `qa_service.py`（调新签名，下发 `confidenceScore`/`confidenceLabel`/`evidenceStrength`）
- **验证**：pytest 绿；`tests/test_crag.py` 老断言不破（关开关）。
- **commit**：`feat(confidence): T3 连续置信度+5档细分+修死标签low(CRAG_V3_ENABLE,opt-in)`

---

## Batch 2 · P2 稳健（断点 C+D+E+F）

### Task 4 · v1/v2 口径统一·grade 吃 es（断点 C，依赖 T1）
- **测试**：同批 contexts，`CRAG_PERDOC_ENABLE` 开/关，`grade` 一致（口径稳定回归）。
- **文件**：`rag/crag.py::grade`（CRAG_V3_ENABLE 开时改吃 `evidence_strength` 分桶，CRAG_HIGH/LOW 退化为 es 阈值）+ `rag/crag_v2.py`（labels→es）
- **验证**：pytest 绿。
- **commit**：`feat(confidence): T4 v1/v2口径统一grade吃es(断点C,CRAG_V3_ENABLE)`

### Task 5 · rerank 降级精细化 + action 矩阵 + refused 归因（断点 D+E）
- **测试**：① `evaluatorDegraded=True` confidence 封顶 low + 独立计；② action×grade 矩阵 9 格全覆盖（`normal/rewritten_recovered/rewritten_partial/rewritten_failed/boosted`）；③ `rewriteDelta` 符号正确；④ refusedReason 4 类各触发（no_recall/rewrite_exhausted/out_of_domain/evidence_contradict）。
- **文件**：`services/qa_service.py::_crag_correct`（矩阵化 action + refused 归因 + rewriteDelta）+ `rag/crag.py`（降级标记）
- **验证**：pytest 绿；`gen_traffic.py` OFF query 落 `refused + refusedReason=out_of_domain/no_recall`。
- **commit**：`feat(confidence): T5 降级精细+action矩阵+refused归因(断点D+E,CRAG_V3_ENABLE)`

### Task 6 · 阈值热调·config_service.rt_*（断点 F）
- **测试**：Redis 覆盖 `rt_crag_high/low` 生效；无覆盖回退 settings 默认。
- **文件**：`services/config_service.py`（`rt_crag_high()/rt_crag_low()`，对齐 `rt_temperature/rt_ef` Redis 热读）+ `rag/crag.py`（读 rt_*）
- **验证**：pytest 绿；热改 Redis 后分级阈值即时变。
- **commit**：`feat(confidence): T6 CRAG阈值热调rt_crag_high/low(断点F)`

### Task 7 · 新指标 + init_metric_series 预注册（可观测）
- **测试**：5 新指标注册 + 触发路径埋点（`grid_crag_evidence_strength` / `grid_crag_confidence_total` 扩 5 档 / `grid_crag_refused_reason_total` / `grid_crag_rewrite_delta` / `grid_overconfident_total`）。
- **文件**：`core/metrics.py`（5 指标）+ `_crag_correct`/`feedback` 埋点 + `init_metric_series` 预注册 0 值（对齐"指标未触碰前 /metrics 隐身"坑）
- **验证**：`/metrics` 含新指标；Grafana confidence 面板细分档渲染。
- **commit**：`feat(confidence): T7 度量5指标+预注册(es/confidence细分/refused/delta/overconfident)`

---

## Batch 3 · P3 拉通 + 收口（断点 G）

### Task 8 · over_confident 机器人工对齐仲裁（断点 G）
- **测试**：query 历史 confidence=high + 本次 dislike → 打 `over_confident` + 该 query L1/L2 缓存失效 + `evidence_gap.collect` 新行 + `FEEDBACK_FIX_RATE` 喂数。
- **文件**：`services/feedback_service.py::record_feedback`（dislike 时查历史 confidence 冲突检测 + 联动）+ 复用 `cache_persist`/`redis_client`/`evidence_gap_service` + `config.CONFIDENCE_OVERCONFIDENT_ENABLE`(默认关)
- **验证**：pytest 绿；端到端 high+dislike → 缓存 miss + evidence_gap 新行。
- **commit**：`feat(confidence): T8 over_confident机器人工对齐仲裁(断点G,CONFIDENCE_OVERCONFIDENT_ENABLE,opt-in)`

### Task 9 · cv 加 R 段 + 集成回归 + 文档
- **测试**：① `citation_cache_version()` 含 R 段（confidence 模型代际），CRAG_V3_ENABLE 切换 R 变 → 老 qa 缓存 key miss；② 集成：OFF query refused 归因 + high+dislike over_confident + 矩阵落格全链路。
- **文件**：`config.citation_cache_version`（加 R 段）+ `tests/test_confidence_integration.py`（集成）+ 更新 `docs/系统架构.md`（confidence 模型图）+ 本 plan 标记进度
- **验证**：集成测试绿；全量回归（test_crag/test_crag_v2/test_citation/golden）。
- **commit**：`test(confidence): T9 cv加R段+集成回归+文档同步(收口)`

---

## 风险与回退

- **最高风险**：T3（confidence_of 重构）+ T5（矩阵化）动核心置信度链路。缓解：全 `CRAG_V3_ENABLE` opt-in，关=现状 diff 0；每 task 独立 commit 可单点回退。
- **缓存兼容**：T9 cv 加 R 段，模型代际变更自动失效老缓存（对齐既有 cv 模式）。
- **前端零改动**：老字段语义不变，新字段附加；`confTitle` 读 `cragReason` 为可选增强（非阻塞）。

---

## 完成状态（2026-07-24，全 9 task 落地）

| Batch | Task | commit |
|---|---|---|
| 1 P1核心 | T1 evidence_strength 统一函数 | fcbe118 |
| | T2 捡回 v2 detail + 归因 | 1646029 |
| | T3 连续置信度 + 5档 + 修 low | b1d3b63 |
| 2 P2稳健 | T4 v1/v2 口径统一 grade 吃 es | 38be6fe |
| | T5 action×grade 矩阵 + refused归因 | 4eba616 |
| | T6 CRAG 阈值热调 rt_crag_high/low | ae7a15d |
| | T7 度量 5 指标 + 预注册 | af5cd6a |
| 3 P3拉通 | T8 over_confident 机器人工对齐 | 134c6e3 |
| | T9 cv R段 + 集成回归 + 收口 | （本提交） |

累计测试：test_confidence_refinement 35 + test_confidence_integration 2 + test_crag 6 + test_crag_v2 12 = **55 绿**。全 opt-in（`CRAG_V3_ENABLE` / `CONFIDENCE_OVERCONFIDENT_ENABLE` 默认关），前端零改动（老字段语义不变）。
