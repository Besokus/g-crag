# 数据链路闭环断点修复设计

- **日期**：2026-07-25
- **状态**：设计已对齐，待 review → 转 writing-plans
- **范围**：5 条数据链路闭环中坐实的 7 个断点（B1~B7）+ 1 项前端可观测增强（F1）
- **风格**：复用项目既有"机器识别 + 人工兜底 + opt-in 开关 + 零回归"范式（同 confidence refinement / data-flywheel）

---

## 1. 背景：5 条闭环全景与断点诊断

当前系统已建成 5 条数据链路闭环：

| 闭环 | 入口 → 出口 | 度量 |
|------|------------|------|
| ① 在线问答 + CRAG 自纠错 | `/qa/answer` → mixed_search → rerank → `_crag_correct`(分级/改写/refused) → 生成 | CRAG_GRADE/ACTION/CONFIDENCE/EVIDENCE_STRENGTH/REWRITE_DELTA |
| ② 反馈数据流 | `/qa/feedback` → dislike 异步 4 路（失效缓存/黑名单/overconfident/事件总线） | FEEDBACK/OVERCONFIDENT |
| ③ 知识自进化 | dislike 聚类 → 盲区 → LLM 草稿 → 审核回流 Milvus | 草稿状态计数 |
| ④ 证据补全 | 无结果/refused/overconfident → evidence_gap → 审核台 → 回流 | egList |
| ⑤ 知识治理 | 时效/冲突扫描 → issue → 人工审核留痕 | coverage/byType/byStatus |

### 1.1 坐实的断点（均带代码证据）

| ID | 断点 | 产品功能 | 证据（file:line） | 分级 |
|----|------|----------|------------------|------|
| B1 | `FEEDBACK_FIX_RATE` 指标断线 | 坏 case 修复率监控 | 定义 `metrics.py:125`；全库仅 `metrics.py:257` init `set(0)`，零业务 set | 🔴 真断点 |
| B2 | `KB_FRESHNESS` 指标断线 | 知识库鲜活度监控 | 定义 `metrics.py:127`；仅 init `set(0)` | 🔴 真断点 |
| B3 | judge 验尸 → 检索/治理断链 | LLM-judge 质检驱动优化 | `judge_hallucination` callers 全部只回填 Feedback 表；rerank 是百炼黑盒无权重入参 | 🔴 半闭环 |
| B4 | CRAG refused 不自动入队 | 证据补全队列 | `collect` 5 caller 不含 CRAG refused 路径（仅无结果兜底/overconfident/manual） | 🟡 半断点 |
| B5 | 草稿回流无效果回测 | 知识自进化 | `run_scan` 无回流后回测；`gap_evidence_json` 有基线但无 after 对比 | 🟡 半断点 |
| B6 | overconfident 覆盖盲区 | 机器过自信检测 | `check_overconfident`(`feedback_optimizer_service.py:354`) 只扫 Redis 现存缓存；默认关 | 🟡 半断点 |
| B7 | stream 无结果不入队 | 证据补全队列 | `stream_answer`(`qa_service.py:1143-1145`) 无结果分支无 collect | 🟡 半断点（B4 调研中顺手发现） |

### 1.2 关键正向事实（不投入精力）
- `FAITHFULNESS_TREND` 已接线（`online_eval_service.py:117`）。
- evidence_gap 三路入口已通（无结果/overconfident/manual）。
- Grafana `data-flywheel.json` 已有 `grid_feedback_fix_rate`(:56)、`grid_kb_freshness`(:104) 面板——**展示层就绪，只差后端写数**。

---

## 2. 目标与非目标

### 目标
- 把 7 个断点全部补成闭环，按风险/收益分 3 Phase 独立交付。
- 复用既有基础设施（evidence_gap.collect / governance issue / 后台 cron loop / metrics 预注册），零新底座。
- 全程 opt-in 开关 + 默认安全值，可灰度、可回滚。
- 前端除 F1 外零改动；老字段语义不变（同 confidence refinement 风格）。

### 非目标
- **不动在线 rerank/检索权重**（B3 走治理 issue 人工兜底，不冒误惩罚好文档的风险）。
- 不重构现有 5 条闭环的主链路代码，只在断点处接线/收口。
- 不在 Vue 端重复造 Grafana 已有的指标面板。

---

## 3. 总体方案：3 Phase 分组

```
Phase 1（飞轮可观测 + 补全不漏料，低风险纯接线/收口）
  B1 FIX_RATE 接线 │ B2 KB_FRESHNESS 接线 │ B4 CRAG refused 入队
  B7 stream 无结果入队 │ F1 前端 source 筛选

Phase 2（半闭环补全，中等）
  B5 草稿回流回测 │ B6 overconfident 默认开 + 基线持久化

Phase 3（judge 驱动治理，中等，新增链路）
  B3 judge 聚合差评文档 → KnowledgeGovernanceIssue(quality_low)
```

每个 Phase 独立可交付、可回归。Phase 1 完全不动在线回答内容（零回归）；Phase 2/3 各自开关 opt-in。

---

## 4. Phase 1 详细设计

### 4.1 B1 — FIX_RATE 接线（周期聚合）

**口径定义**（已对齐）：
- **分母**：近 `FIX_RATE_WINDOW_DAYS`(默认 30) 天内被 dislike 的 unique 归一化 query（nq）数。
- **分子**：这些 nq 中，已经过「evidence_gap.status=synced」**或**「evolution draft.status=indexed」回流，**且**后续再被问时收到 like 的数量。
- **修复率** = 分子 / 分母，`metrics.FEEDBACK_FIX_RATE.set(rate)`。

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/services/feedback_fix_rate_service.py`（新增） | `recompute_fix_rate(tenant) -> float`：纯查询算率 |
| `backend/app/main.py` | lifespan 起后台 loop `fix_rate_cron_loop(tenant)`（仿 `evolution_cron_loop`） |
| `backend/app/config.py` | `FIX_RATE_ENABLE=True` / `FIX_RATE_WINDOW_DAYS=30` / `FIX_RATE_CRON_MINUTES=30` |

**数据流**：
```
Feedback(feedback=dislike, created_at >= now-N天, tenant) → normalize → S1(distinct nq)
EvidenceGap(query∈S1, status=synced) ∪ KnowledgeEvolutionDraft(member_queries∋S1, status=indexed) → S2(已补全)
Feedback(feedback=like, normalize(query)∈S2) → S3
rate = |S3| / |S1|   →   metrics.FEEDBACK_FIX_RATE.set(rate)
```

**关键细节**：`Feedback.query` 是原始 query，`EvidenceGap.query` 是 `term_service.normalize` 后的 nq。匹配前必须对 Feedback.query 做 normalize（复用 `term_service.normalize`）。

**错误处理**：查询异常 `degraded("fix_rate_recompute", e)`，rate 保持上次值（不崩、不假报 0）。

**测试** `tests/test_feedback_fix_rate.py`：
- 造 dislike → synced → like 完整 fixture，assert rate 正确；
- 只 dislike 未补全 → rate=0；
- 空数据 → rate=0 不报错；
- window 外的 dislike 不计入。

---

### 4.2 B2 — KB_FRESHNESS 接线（scan 末尾 set）

**口径定义**：active 文档占比 = `version_status='active' 且 (is_permanent 或 expires_at is null 或 expires_at > now)` 的文档数 / Document 表总文档数。

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/services/knowledge_governance_service.py` | `run_scan` 末尾新增 `_set_freshness_metric(db, tenant)`：算占比 → `metrics.KB_FRESHNESS.set()` |

**为什么挂 run_scan**：治理扫描本就是周期任务（cron + 手动），无需新建 loop。

**错误处理**：异常 `degraded("kb_freshness_set", e)`，不阻塞 scan 主流程。

**测试** `tests/test_kb_freshness.py`：
- active+未过期 / expired / draft / superseded 混合 fixture，assert 占比正确；
- 无 metadata 文档计入分母不计入分子。

---

### 4.3 B4 + B7 — CRAG refused / stream 无结果 自动入 evidence_gap

**收口点**（覆盖非流式 + 流式两条路径）：

| 路径 | 位置 | 条件 | source |
|------|------|------|--------|
| 非流式 | `qa_service.answer()` 在 `_crag_correct` 返回后 | `confidence=='refused'` 或 `action in {rewritten_failed, refused}` | `auto_crag` |
| 流式 CRAG | `stream_answer` 在 `_crag_correct`(:1148) 返回后、yield meta 前 | 同上 | `auto_crag` |
| 流式无结果（B7） | `stream_answer` 无结果分支(:1143) | `not contexts` | `auto_no_recall` |

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/services/qa_service.py` | 3 处收口点 fire-and-forget `_bg_tasks.add(asyncio.create_task(evidence_gap_service.collect(...)))` |
| `backend/app/config.py` | `CRAG_REFUSED_TO_GAP_ENABLE=True` |

**时序**（已坐实）：`_crag_correct` 在 LLM 生成前调用，此时 confidence/action/grade 已定，answer 可能未生成 → `original_answer` 传空（`EvidenceGap.original_answer` 允许空）。补全队列看 query 不看原答案，够用。

**去重安全**：`collect` 本身"同 query pending 跳过"（`evidence_gap_service.py:23-27`），与无结果兜底/overconfident/manual 互不冲突。

**测试** `tests/test_crag_refused_to_gap.py`：
- refused 场景 → evidence_gap 多一条 `source=auto_crag`；
- 非 refused（correct/ambiguous）→ 不入队；
- stream 无结果 → 多一条 `source=auto_no_recall`；
- 同 query 重复 → 去重生效。

---

### 4.4 F1 — 前端：证据补全列表加 source 筛选

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/services/evidence_gap_service.py` | `list_gaps` 增 `source: str \| None` 参数，where 过滤 |
| `backend/app/routers/system.py` | `evidence_gap_list` 透传 `source` query 参数 |
| `frontend/src/views/Admin.vue` | 证据补全列表头加 `<select>` source 下拉：`全部/auto/auto_crag/auto_no_recall/overconfident/manual`，绑 `loadEvidenceGaps` 的 params |

**测试**：`list_gaps(status='pending', source='auto_crag')` 只返回对应来源。

---

## 5. Phase 2 详细设计

### 5.1 B5 — 草稿回流回测（run_scan 加回测阶段）

**调研坐实**：`_identify_blind_spot`(`knowledge_evolution_service.py:98`) 用 `_retrieve_top1` 测 representative_query 的 top1_score 判盲区；`gap_evidence_json` 存**回流前基线** `{top1_score, hit_doc_ids, confidence}`；`member_queries_json` 存簇内样本；`indexed_at` 有时间戳。回测的"尺子"现成。

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/services/knowledge_evolution_service.py` | `run_scan` 入口加 `_retest_indexed_drafts(db, tenant)` |
| `backend/app/core/metrics.py` | 新增 `EVOLUTION_LIFT = Histogram("grid_evolution_lift", "自进化回流增益", buckets=(-1.0,-0.2,0,0.2,0.5,1.0))` |
| `backend/app/config.py` | `EVOLUTION_RETEST_ENABLE=True` / `EVOLUTION_RETEST_AFTER_DAYS=7` |

**回测逻辑**：
```
对 status=indexed 且 indexed_at < now-AFTER_DAYS 的草稿:
  before = gap_evidence_json.top1_score
  after  = avg(_retrieve_top1(q) for q in member_queries)
  lift   = after - before
  写回 gap_evidence_json.{after_score, lift, retested_at}
  metrics.EVOLUTION_LIFT.observe(lift)
  if lift <= 0:   # 回流无效
    quality_score 下调（×0.5）或生成治理 issue 提示人工复审
```

**错误处理**：单条回测异常 `degraded("evo_retest_one", e)`，不阻塞整批。

**测试** `tests/test_evolution_retest.py`：
- indexed 草稿 + mock 检索分数提升 → lift>0，写回正确；
- 分数无变化 → lift=0，quality_score 下调；
- indexed_at 在 AFTER_DAYS 内 → 跳过不回测。

---

### 5.2 B6 — overconfident 默认开 + 高置信基线持久化

**调研坐实**：`check_overconfident`(`feedback_optimizer_service.py:337`) 只扫 Redis `qa:*:{nq}`，问答缓存过期就丢；Feedback 表无 confidence 字段，只能靠问答时记录的 high 基线。

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/config.py` | `CONFIDENCE_OVERCONFIDENT_ENABLE` 默认改 `True` |
| `backend/app/services/qa_service.py` | answer/stream 返回 `confidence=='high'` 时，写 Redis `qa:highbase:{nq}` = `{confidence, answer, ts}`，TTL `OVERCONFIDENT_BASELINE_TTL_DAYS`(默认 30) |
| `backend/app/services/feedback_optimizer_service.py` | `check_overconfident` 改为扫 `qa:highbase:{nq}`（不依赖问答缓存是否存活） |

**数据流**：
```
answer→high → SET qa:highbase:{nq} (TTL 30天)
dislike → check_overconfident → SCAN qa:highbase:{nq} → 命中则 OVERCONFIDENT.inc() + evidence_gap 复核
```

**错误处理**：Redis 异常 `degraded("overconfident_baseline", e)`，降级为不检出（安全侧）。

**测试** `tests/test_overconfident_baseline.py`：
- high 答案 → highbase key 写入；
- 30 天后 key 过期；
- 缓存过期后 dislike 仍能从 highbase 检出冲突。

---

## 6. Phase 3 详细设计

### 6.1 B3 — judge 聚合差评文档 → 治理 issue（路径3，不动在线权重）

**决策**：B3 走**路径3**（已对齐）——judge 聚合"高频被差评文档"自动生成 `KnowledgeGovernanceIssue(issue_type=quality_low)`，进治理审核台人工兜底。**不动在线 rerank/检索权重**，零回归风险。

**改动点**：
| 文件 | 改动 |
|------|------|
| `backend/app/services/doc_quality_service.py`（新增） | `aggregate_doc_quality(tenant)`：按 `Feedback.retrieval_sources` 分组，算每个文档的 dislike 命中率 = dislike 次数 / 被引用总次数 |
| `backend/app/services/doc_quality_service.py` | dislike 率 ≥ `DOC_QUALITY_DISLIKE_THRESHOLD`(默认 0.5) 且 count ≥ `DOC_QUALITY_MIN_COUNT`(默认 3) → 生成 `KnowledgeGovernanceIssue(issue_type='quality_low', doc_id, evidence_json={dislike_count,total,rate})` |
| `backend/app/services/knowledge_governance_service.py` | `ISSUE_TYPES`(:39) 加 `'quality_low'`；`run_scan` 末尾调 `aggregate_doc_quality` |
| `backend/app/main.py` | 可选：周期 loop `doc_quality_cron_loop`（或直接挂 governance scan） |
| `backend/app/config.py` | `DOC_QUALITY_ISSUE_ENABLE=True` |
| `frontend/src/views/KnowledgeGovernance.vue` | `issueTypes`(:408) 加 `'quality_low'` + 中文标签 |

**去重**：复用 `KnowledgeGovernanceIssue` 的 `(tenant_id, fingerprint)` 唯一约束（fingerprint = `quality_low:{doc_id}`）。

**错误处理**：异常 `degraded("doc_quality_aggregate", e)`，不阻塞 scan。

**测试** `tests/test_doc_quality_issue.py`：
- 某文档被多次 dislike 命中 → 生成 quality_low issue；
- 重复扫描 → 去重不重复生成；
- 率低于阈值 → 不生成。

---

## 7. 指标清单

| 指标 | 类型 | 状态 | 断点 |
|------|------|------|------|
| `grid_feedback_fix_rate` | Gauge | 已定义，**本 spec 接线** | B1 |
| `grid_kb_freshness` | Gauge | 已定义，**本 spec 接线** | B2 |
| `grid_evolution_lift` | Histogram | **本 spec 新增** | B5 |
| `grid_overconfident_total` | Counter | 已有 | B6（扩大覆盖） |
| quality_low issue | 数据 | 走 governance issue 表 | B3 |

所有新指标在 `metrics.py` 定义后，按项目惯例在 init_metric_series 预注册 0 值（避免 Prometheus 事件驱动指标未触碰前在 /metrics 隐身，见 memory grafana-monitoring 坑①）。

---

## 8. 配置开关清单（新增/变更）

| 开关 | 默认 | 断点 |
|------|------|------|
| `FIX_RATE_ENABLE` | True | B1 |
| `FIX_RATE_WINDOW_DAYS` | 30 | B1 |
| `FIX_RATE_CRON_MINUTES` | 30 | B1 |
| `CRAG_REFUSED_TO_GAP_ENABLE` | True | B4/B7 |
| `EVOLUTION_RETEST_ENABLE` | True | B5 |
| `EVOLUTION_RETEST_AFTER_DAYS` | 7 | B5 |
| `CONFIDENCE_OVERCONFIDENT_ENABLE` | **True（变更：原默认关）** | B6 |
| `OVERCONFIDENT_BASELINE_TTL_DAYS` | 30 | B6 |
| `DOC_QUALITY_ISSUE_ENABLE` | True | B3 |
| `DOC_QUALITY_DISLIKE_THRESHOLD` | 0.5 | B3 |
| `DOC_QUALITY_MIN_COUNT` | 3 | B3 |

全部加入 `config.py` + `.env` 文档对齐（见 memory P0: .env 字段对齐要求）。

---

## 9. 测试策略

- **单元测试**：每个断点一个测试文件（见各节），共 7 个新测试文件。
- **集成回归**：Phase 1 完成后跑 `test_flywheel_metrics.py`（已有，验证指标预注册 + init 0 值）+ `test_confidence_refinement.py`（确保 CRAG 路径零回归）。
- **前端**：F1 手动验证 source 筛选下拉（项目前端无自动化测试，沿用惯例）。
- **Golden 回归集**：Phase 1/2 各跑一次 golden_qa 回归（memory P2: golden 回归集 + CI 门禁）。

---

## 10. 兼容性与风险

| 维度 | 评估 |
|------|------|
| 在线回答内容 | **零改动**（B4/B7 只新增 collect 调用，不改回答文本） |
| 前端字段 | F1 外零改动；老 confidence/confidenceLabel 语义不变 |
| 检索/rerank 权重 | **不动**（B3 走治理 issue） |
| 数据库 | 无 schema 变更（B5 写回 gap_evidence_json，B3 复用 issue 表） |
| 性能 | B1/B3 周期 loop 离线聚合，不卡请求；B4/B6 bg task 异步 |
| 回滚 | 全部 opt-in 开关，设 False 即回滚 |

**主要风险**：
- B1 修复率口径若与运营预期不符 → 先用默认 30 天窗口灰度，按 Grafana 实测调参。
- B5 回测若误判（检索分数波动）导致 quality_score 误下调 → 设 `EVOLUTION_RETEST_AFTER_DAYS=7` 缓冲 + 只下调不自动撤回（人工复审）。

---

## 11. 交付与验收

| Phase | 内容 | 验收信号 |
|-------|------|----------|
| 1 | B1/B2/B4/B7/F1 | Grafana fix_rate/freshness 面板有真实数值；证据补全列表出现 `auto_crag`/`auto_no_recall` 来源且可筛选 |
| 2 | B5/B6 | `grid_evolution_lift` 有数据；overconfident 默认开且历史 high 也能检出 |
| 3 | B3 | 治理审核台出现 `quality_low` 类型 issue |

---

## 12. 后续（非本 spec 范围）

- B3 若运营验证 quality_low issue 准确率高，后续可考虑路径1/2（在线 rerank 后 `score × doc_penalty`），但需先有足够样本。
- `KnowledgeEvolutionDraft.quality_score=0.6` 在检索层是否真实生效（降权是否接线）未在本轮确认，可作为后续独立排查项。
