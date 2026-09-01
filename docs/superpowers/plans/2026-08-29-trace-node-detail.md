# 链路节点详情（点击看指标/参数/Prompt）实现计划

> **For agentic workers:** 按任务逐个执行，步骤用 checkbox 跟踪。spec 见 `docs/superpowers/specs/2026-08-29-trace-node-detail-design.md`（字段清单/设计理由在 spec，本计划只写怎么做）。

**Goal:** 链路瀑布图（QaTraceChart）每个节点可点击展开：① 运行指标 ② 生效参数 ③ Prompt/输入输出。采集走 span attrs（现有管道自动透传响应体 + qa_trace 落库），总开关 `QA_TRACE_DETAIL_ENABLE=False`（关=现状）。

**Architecture:** 三层——`qa_trace.py` 加 `attach()` + `llm_attrs()` helper → 各打点接线（qa_service 主链路 + retrieval 子链）→ `QaTraceChart.vue` 行点击内嵌展开。零 schema 变更（attrs 随 `spans_json` 自动落库）；纯观测能力，**不进 citation_cache_version**。

**Tech Stack:** FastAPI / pytest（helper 纯单测，遵循"只测抽出 helper、不 mock 编排链路"惯例）/ Vue 3

## Global Constraints

- 后端测试：`venv/Scripts/python.exe -m pytest tests/<file> -v`；CI 兼容用例不碰 Milvus/embedding/LLM
- **后端运行不带 `--reload`**；backend 源码烤在镜像里，容器验证需 `docker compose up -d --build backend`
- trace 任何失败不得影响主链路（沿用 span() 既有铁律：try/except 全包）
- 开关关=现状：`QA_TRACE_DETAIL_ENABLE=False` 时所有 attrs 不采集，瀑布图与今天完全一致
- attrs 值必须 JSON 可序列化（spans_json 落库）；嵌套 dict 允许
- 全量回归：`venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration"`；`ruff check backend tests`（只要求无新告警，存量 lint 债不扩）

## File Structure

- **Modify:** `backend/app/config.py`（开关 + 截断长度）、`.env.example`、`backend/app/core/qa_trace.py`（attach + llm_attrs）
- **Modify:** `backend/app/services/qa_service.py`（主链路约 8 处接线）、`backend/app/services/retrieval_service.py`（_retr_span 扩展 + 子链 attrs + 补 4 个缺失 span）
- **Modify:** `frontend/src/components/QaTraceChart.vue`（点击展开 + TRACE_DETAIL_SCHEMA）
- **Test:** `tests/test_qa_trace.py`（追加）、`tests/test_retrieval_sources.py`（追加）

---

### Task 1: 开关 + `attach()` + `llm_attrs()` 采集 helper

**Files:**
- Modify: `backend/app/config.py`、`backend/app/core/qa_trace.py`
- Test: `tests/test_qa_trace.py`（追加）

**Interfaces:**
- Produces: `settings.QA_TRACE_DETAIL_ENABLE: bool = False`、`settings.QA_TRACE_PROMPT_CHARS: int = 1200`；`TraceCollector.attach(name, **attrs)`（给最后一个同名 span 追加 attrs，解决"数据在 span 内部产生"的场景）；`llm_attrs(messages, temperature=None, max_tokens=None, usage=None, model=None, output=None, output_chars=200) -> dict`（prompt 截断 + PII 脱敏 + 预算控制）

- [x] **Step 1: 写失败测试**

`tests/test_qa_trace.py` 追加：

```python
def test_attach_updates_last_matching_span():
    c = TraceCollector("q")
    with c.span("crag"):
        pass
    c.attach("crag", grade="correct", action="normal", extras={"es": 0.8})
    s = c.to_dict()["spans"][0]
    assert s["attrs"]["grade"] == "correct"
    assert s["attrs"]["extras"]["es"] == 0.8


def test_attach_missing_span_is_silent_noop():
    c = TraceCollector("q")
    c.attach("nonexistent", a=1)      # 不抛即过（铁律：trace 失败不影响主链路）
    assert c.to_dict()["spans"] == []


def test_llm_attrs_truncates_and_counts():
    from app.core import qa_trace
    long_sys = "sys" * 1000           # 3000 字符 > 1200
    msgs = [{"role": "system", "content": long_sys},
            {"role": "user", "content": "主变油温高怎么办"}]
    a = qa_trace.llm_attrs(msgs, temperature=0.2, max_tokens=2048,
                           usage={"input": 100, "output": 50}, model="deepseek",
                           output="答案" * 200)
    assert a["nMessages"] == 2
    assert a["temperature"] == 0.2 and a["maxTokens"] == 2048
    assert a["model"] == "deepseek"
    assert a["tokenUsage"] == {"input": 100, "output": 50}
    assert len(a["promptSystem"]) == 1200 and a["promptSystemTruncated"] is True
    assert a["promptUser"] == "主变油温高怎么办"
    assert a["outputTruncated"] is True and len(a["output"]) == 200


def test_llm_attrs_minimal_and_none_usage():
    from app.core import qa_trace
    a = qa_trace.llm_attrs([{"role": "user", "content": "q"}])
    assert a["nMessages"] == 1 and "tokenUsage" not in a and "model" not in a


def test_llm_attrs_size_budget_drops_prompts():
    """超预算：丢 prompt 只留参数，标记 promptOmitted。"""
    import app.core.qa_trace as qt
    orig = qt._ATTRS_BUDGET
    qt._ATTRS_BUDGET = 500            # 测试用小预算
    try:
        a = qt.llm_attrs([{"role": "user", "content": "x" * 2000}], temperature=0.2)
        assert a.get("promptOmitted") is True
        assert "promptUser" not in a and a["temperature"] == 0.2
    finally:
        qt._ATTRS_BUDGET = orig


def test_detail_flag_defaults_off():
    from app.config import settings
    assert settings.QA_TRACE_DETAIL_ENABLE is False
    assert settings.QA_TRACE_PROMPT_CHARS == 1200
```

- [x] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_qa_trace.py -v`
Expected: FAIL（`AttributeError: ... has no attribute 'attach'` / `no attribute 'llm_attrs'`）

- [x] **Step 3: 实现**

1. `backend/app/config.py` QA trace 区（`QA_TRACE_SAMPLE_RATE` 之后）追加：

```python
    QA_TRACE_DETAIL_ENABLE: bool = False   # 节点详情采集(参数/prompt attrs, 纯观测不进缓存版本; 关=现状只记耗时)
    QA_TRACE_PROMPT_CHARS: int = 1200      # 单段 prompt/输出截断长度（attrs 大小预算的前置）
```

2. `backend/app/core/qa_trace.py`：

```python
_ATTRS_BUDGET = 8 * 1024      # 单 span attrs 序列化后预算（字节），超限丢 prompt 留参数


def _snap(text, limit):
    """截断 + PII 脱敏（PII_MASK_ENABLE 开时）；非 str 安全。"""
    if not isinstance(text, str) or not text:
        return ""
    try:
        from app.core import safety
        text = safety.mask_pii(text)
    except Exception:
        pass
    return text[:limit]


def llm_attrs(messages, temperature=None, max_tokens=None, usage=None,
              model=None, output=None, output_chars=200) -> dict:
    """LLM 调用详情 → span attrs dict（json 安全；QA_TRACE_DETAIL_ENABLE 由调用点判）。"""
    from app.config import settings
    limit = int(getattr(settings, "QA_TRACE_PROMPT_CHARS", 1200) or 1200)
    a: dict = {}
    msgs = messages if isinstance(messages, list) else []
    a["nMessages"] = len(msgs)
    for m in msgs:
        role = (m or {}).get("role", "")
        content = (m or {}).get("content", "")
        if role == "system":
            a["promptSystem"] = _snap(content, limit)
            if len(content) > limit:
                a["promptSystemTruncated"] = True
        elif role == "user" and "promptUser" not in a:
            a["promptUser"] = _snap(content, limit)
            if len(content) > limit:
                a["promptUserTruncated"] = True
    if temperature is not None:
        a["temperature"] = temperature
    if max_tokens is not None:
        a["maxTokens"] = max_tokens
    if usage:
        a["tokenUsage"] = {"input": usage.get("input"), "output": usage.get("output")}
    if model:
        a["model"] = model
    if output:
        a["output"] = _snap(output, output_chars)
        if len(output) > output_chars:
            a["outputTruncated"] = True
    try:
        import json as _json
        if len(_json.dumps(a, ensure_ascii=False).encode()) > _ATTRS_BUDGET:
            return {k: v for k, v in a.items() if not k.startswith(("prompt", "output"))} \
                | {"promptOmitted": True}
    except Exception:
        pass
    return a
```

3. `TraceCollector` 加方法（`mark()` 之后）：

```python
    def attach(self, name: str, **attrs) -> None:
        """给最后一个同名 span 追加 attrs（span 内部产生的数据事后补挂）。

        找不到同名 span 时静默忽略——trace 任何失败不影响主链路。
        """
        if not attrs:
            return
        try:
            for s in reversed(self._spans):
                if s.get("name") == name:
                    s.setdefault("attrs", {}).update(attrs)
                    return
        except Exception:
            pass
```

注意：`llm_attrs` 里 `safety.mask_pii` 内部自判 `PII_MASK_ENABLE`（关=原文返回），无需重复判断。

- [x] **Step 4: 运行确认通过 + lint**

Run: `venv/Scripts/python.exe -m pytest tests/test_qa_trace.py -v` → PASS（新 7 + 既有全过）
Run: `ruff check backend/app/core/qa_trace.py backend/app/config.py tests/test_qa_trace.py`

- [x] **Step 5: Commit**

```bash
git add backend/app/config.py backend/app/core/qa_trace.py tests/test_qa_trace.py
git commit -m "feat(trace): QA_TRACE_DETAIL_ENABLE 开关 + span attach/llm_attrs 采集 helper"
```

---

### Task 2: 主链路打点接线（qa_service 非流式 + 流式）

**Files:**
- Modify: `backend/app/services/qa_service.py`

**Interfaces:**
- Consumes: Task 1 的 `attach` / `llm_attrs` / 开关
- 接线原则：`with span(...)` 进入时已知的数据走 span kwargs；**span 内部产生的数据用 `c.attach(...)` 事后补挂**（span kwargs 在进入时求值，内部变量当时还不存在——这是本 Task 最容易踩的坑）
- 本 Task 无新增单测（仓库惯例：只测抽出 helper，不 mock 编排链路）；验证 = py_compile + 既有回归不破 + Task 5 Docker 实测

- [x] **Step 1: 非流式 answer() 接线（行号基于当前文件，改前先 Read 现场）**

统一模式：在 import 区补 `if getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):` 就地判断（`_trace_span` 不动，判断放调用点）。

1. `routing`（:860 附近，span 内产生 routing）— span 块后补：
```python
    _tc = _get_trace()
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False) and routing:
        _feat = getattr(routing, "features", None)
        _tc.attach("routing", route=routing.route, confidence=routing.confidence,
                   reason=(routing.reason or "")[:200],
                   queryType=(getattr(_feat, "query_type", "") or ""))
```
2. `retrieval`（:869 附近）— `contexts = await mixed_search(...)` 后：
```python
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("retrieval", hits=len(contexts),
                   route=(routing.route if routing else "hybrid"),
                   top1=round(float(contexts[0].get("score") or 0), 4) if contexts else None)
```
3. `standalone_rewrite`（:812 附近）— `search_q = await ...` 后：
```python
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("standalone_rewrite", rewritten=search_q[:160], changed=search_q != nq)
```
4. `crag`（:892 附近）— 解构后：
```python
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("crag", grade=crag_grade, action=crag_action, confidence=confidence,
                   **({"extras": crag_extras} if crag_extras else {}))
```
5. `graphrag`（:900 附近）— span 块后：
```python
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("graphrag", lines=len(graph),
                   enabled=getattr(settings, "KG_RAG_ENABLE", False))
```
6. `llm`（:945 附近）— LLM 调用前把 temperature 收进局部变量 `_temperature = config_service.rt_temperature()` 并在 `prov.chat(...)` 处用之；`record("llm", ...)` 后：
```python
    if getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        from app.core.qa_trace import llm_attrs
        _tc.attach("llm", **llm_attrs(messages, temperature=_temperature,
                                      max_tokens=settings.LLM_MAX_TOKENS,
                                      usage=_llm_usage, model=_llm_fields["modelType"],
                                      output=raw))
```
7. `citation`（:965 附近）— `_trace = citation.evidence_trace(ans)` / auto_cite 后：
```python
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("citation", refs=len(contexts), annotated=len(_trace or []))
```
8. `hotqa`（:725 附近）— `hot = await _hit_hotqa(...)` 后：
```python
    if _tc and getattr(settings, "QA_TRACE_DETAIL_ENABLE", False):
        _tc.attach("hotqa", hit=bool(hot))
```

注意：`_tc = _get_trace()` 在各段就地取（answer() 入口已由 qa.py new_collector 绑定）；`_tc` 为 None 时短路。**缓存族不加新 span**：三级缓存块（:736-805）return 出口多、包 with 需大段缩进重构，收益低——cacheLayer 已有 mark 承载，spec 已记录该取舍。

- [x] **Step 2: 流式 stream_answer() 链同步接线（:1438/:1470/:1479/:1494/:1567 附近）**

与 Step 1 同模式，差异两处：
- `llm` 节点：流式无 usage（`stream()` 不返回 usage）→ `usage=None`；`output` 在 `full = "".join(parts)` 之后 attach（`record("llm")` 在 parts 合并前，attach 顺序无妨）；
- `_tc` 同样就地 `_get_trace()`。

- [x] **Step 3: 编译 + 回归**

Run: `venv/Scripts/python.exe -m py_compile backend/app/services/qa_service.py`
Run: `venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration" 2>&1 | tail -3` → 与基线一致（661 passed / 8 个既有环境性失败）

- [x] **Step 4: Commit**

```bash
git add backend/app/services/qa_service.py
git commit -m "feat(trace): 主链路节点 attrs 接线(routing/retrieval/crag/llm/citation/graphrag/hotqa, 开关控制)"
```

---

### Task 3: 检索子链 attrs + 补缺失 span（retrieval_service）

**Files:**
- Modify: `backend/app/services/retrieval_service.py`
- Test: `tests/test_retrieval_sources.py`（追加）

**Interfaces:**
- `_retr_span(name, **attrs)` 透传 attrs 给 `c.span`
- 新增 4 个缺失 span：`sparse_search`（sparse 路由分支 + `_dense_and_sparse` 的 BM25 段）、`rrf`（hybrid 融合段）、`mmr`（step 5）、`filter_acl`（step 4，多 return 列表过滤段用 `record()` 事后记，避免大段缩进重构）
- waterfall 变化：新增行让 BM25/融合/过滤耗时可见（加行不破坏既有行）

- [x] **Step 1: 写失败测试**

`tests/test_retrieval_sources.py` 追加：

```python
def test_retr_span_passes_attrs(monkeypatch):
    from app.core.qa_trace import new_collector
    c = new_collector("q")
    with retrieval_service._retr_span("dense_search", ef=64, cand=20):
        pass
    s = c.to_dict()["spans"][0]
    assert s["name"] == "dense_search" and s["group"] == "retrieval"
    assert s["attrs"] == {"ef": 64, "cand": 20}


def test_retr_span_no_collector_noop():
    # 不绑 collector（默认 contextvar None）→ 不抛即过
    with retrieval_service._retr_span("rrf", k=60):
        pass
```

- [x] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_retrieval_sources.py -v`
Expected: FAIL（`_retr_span() got an unexpected keyword argument 'ef'`）

- [x] **Step 3: 实现**

1. `_retr_span`（:28）扩展：
```python
def _retr_span(name: str, **attrs):
    c = get_collector()
    return c.span(name, "retrieval", **attrs) if c else nullcontext()
```
（import 处 `from app.core.qa_trace import get_collector` 若为 `get_collector as _get_trace` 风格，按现场对齐。）

2. 既有 4 处调用点补 attrs（均为 span 内产生的数据 → 进出后 `attach`；`get_collector()` 已在 `_retr_span` 内使用，文件内补一个 `_tc = get_collector()` 就地取用）：
   - `query_rewrite`（:309）：`q = (await rewrite_query_v2(...))["query"]` 后 `attach("query_rewrite", rewritten=q[:160], changed=q != query)`；
   - `dense_search`（:245/:371）：命中后 `attach("dense_search", ef=_ef, cand=cand, cloud=len(dense_cloud or []), bge=len(dense_bge or []))`；
   - `rerank`（:425）：成功分支 `attach("rerank", topN=min(_pool_cap, len(pool)), top1=round(float(ranked[0][1]), 4) if ranked else None)`；degraded 分支 `attach("rerank", degraded=True)`。
3. 新增 span（`QA_TRACE_DETAIL_ENABLE` **关时也要计耗时吗？**——要。span 计时是现有能力（瀑布行），attrs 才受开关控；新 span 无 kwargs、纯计时，不影响"关=现状"的耗时语义）：
   - sparse 路由分支（:330 附近）BM25 收集段包 `with _retr_span("sparse_search"):`；
   - `_dense_and_sparse` 的 BM25 段（:238-252）同理；
   - hybrid RRF 融合段（:404-424 附近）包 `with _retr_span("rrf"):`；
   - step 5 MMR（:455 附近）包 `with _retr_span("mmr"):`；
   - step 4 过滤段（:430-454）：段前 `_f0 = time.time()`，段后 `_tc and _tc.record("filter_acl", time.time() - _f0, "retrieval", before=<过滤前 pool 长度>, after=len(pool))`（before 变量在过滤前先存）。
4. attrs 版补充（开关内）：`attach("sparse_search", topk=cand)`；`attach("rrf", k=..., dw=..., sw=...)`（用现场 `_ov` 结果）；`attach("mmr", lamda=_default_lambda, candidates=len(pool))`。

- [x] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_retrieval_sources.py tests/test_qa_trace.py tests/test_mmr.py -v` → PASS
Run: `venv/Scripts/python.exe -m py_compile backend/app/services/retrieval_service.py`

- [x] **Step 5: Commit**

```bash
git add backend/app/services/retrieval_service.py tests/test_retrieval_sources.py
git commit -m "feat(trace): 检索子链 attrs(rewrite/dense/rerank) + 补 sparse_search/rrf/mmr/filter_acl 计时 span"
```

---

### Task 4: 前端节点点击展开（QaTraceChart.vue）

**Files:**
- Modify: `frontend/src/components/QaTraceChart.vue`

**Interfaces:**
- 数据源不变：`trace.spans[].attrs`（后端已透传）；`attrs` 为空 → 行保持原样不可点
- 交互：行点击 toggle 内嵌展开区；同一时间仅展开一行（重复点击收起）；展开区三分区

- [x] **Step 1: 实现模板与逻辑**

1. `bar-row` 加 `@click="toggle(i)"` + 可点态 class `:class="{ clickable: hasDetail(s), open: openIdx === i }"`；
2. script 补：
```js
const openIdx = ref(-1)
function toggle(i) { openIdx.value = openIdx.value === i ? -1 : i }
function hasDetail(s) { return !!(s.attrs && Object.keys(s.attrs).length) }

// 节点 → 详情分区字段映射（key 对齐后端 attrs 命名）
const DETAIL_META = {
  grade: 'CRAG 分级', action: 'CRAG 动作', confidence: '置信', extras: 'CRAG 明细',
  route: '路由', queryType: '问题类型', reason: '理由', hits: '命中数',
  top1: 'top 分数', ef: 'HNSW ef', cand: '候选数', cloud: '云端向量命中',
  bge: 'bge 命中', rewritten: '改写后查询', changed: '是否改写',
  temperature: 'temperature', maxTokens: 'max_tokens', model: '模型',
  tokenUsage: 'tokens(in/out)', nMessages: '消息数', lines: '图谱链条数',
  annotated: '补标引用数', refs: '证据数', hit: 'hotqa 命中',
  topN: '重排输出数', degraded: '降级', k: 'RRF k', dw: 'dense 权重', sw: 'sparse 权重',
  lamda: 'MMR λ', candidates: '候选数', before: '过滤前', after: '过滤后',
}
const PROMPT_KEYS = ['promptSystem', 'promptUser', 'output']
```
3. 展开区（`bar-row` 之后 `v-if="openIdx === i"`）三分区：
   - **指标/参数**：遍历 `s.attrs`，`PROMPT_KEYS` 之外的字段渲染 `DETAIL_META[k] || k : v`（对象值 JSON.stringify，长文本 `v.length > 80` 截断 + title 全文）；
   - **Prompt/IO**：`PROMPT_KEYS` 中存在的 key，折叠显示（默认 max-height 4 行，点击展开全部）+ 「复制」按钮（`navigator.clipboard.writeText`）；`promptOmitted` 显示「内容超预算未采集」；`*Truncated` 显示「已截断，全文见 Langfuse」。
4. 展开区样式：`background: var(--surface-2)`、左边框 2px 对应 group 色、`font-size: 11px`，沿用文件内既有 CSS 变量。

- [x] **Step 2: 构建验证**

```bash
cd frontend && npm run build    # 预期 ✓ built
```

- [x] **Step 3: Commit**

```bash
git add frontend/src/components/QaTraceChart.vue
git commit -m "feat(trace): 瀑布图节点点击展开详情(指标/参数/prompt, 截断标注+复制)"
```

---

### Task 5: 回归 + Docker 端到端验证 + 文档

- [x] **Step 1: 全量回归**

```bash
ruff check backend/app/core/qa_trace.py backend/app/services/qa_service.py backend/app/services/retrieval_service.py tests/test_qa_trace.py tests/test_retrieval_sources.py
venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration"
```
Expected: 与基线一致（661+新增 全过 / 8 个既有环境性失败不新增）

- [x] **Step 2: 开关关=现状验证（容器）**

`.env` / compose 不开 `QA_TRACE_DETAIL_ENABLE` → `docker compose up -d --build backend` → 跑一条问答 → 响应体 `trace.spans[].attrs` 均不存在（或为空）；瀑布图与改造前一致。

- [x] **Step 3: 开关开验证（容器，真实 LLM）**

compose backend 环境加 `QA_TRACE_DETAIL_ENABLE: "true"`（仿 TICKET_ACTION_LOOP_ENABLE 的接线方式）→ 重建 → 跑一条真实问答（admin token + `POST /api/qa/answer`），逐项核对响应体 `trace`：
1. `llm` span：`temperature/maxTokens/model/nMessages/promptSystem/promptUser/output` 齐全，prompt 长度 ≤ 1200；若开 `LLM_USAGE_TRACK_ENABLE` 则 `tokenUsage` 有值；
2. `crag` span：`grade/action/confidence`；
3. `routing/retrieval` span：`route/confidence/hits/top1`；
4. 新 span 行（`sparse_search`/`rrf`/`mmr`/`filter_acl`）按路由出现且带耗时；
5. 前端（`npm run dev` 或 :5173）：Chat 页「📊 链路耗时」点节点展开三分区、复制按钮生效、无 attrs 的行不可点；
6. 关开关重启 → 恢复 Step 2 状态。

- [x] **Step 4: `.env.example` 同步 + Commit**

```bash
# .env.example QA/观测区追加：
# QA_TRACE_DETAIL_ENABLE=false  # 链路节点详情(参数/prompt attrs; 关=现状只记耗时)
# QA_TRACE_PROMPT_CHARS=1200    # 单段 prompt/输出截断长度
git add .env.example docker-compose.yml docs/superpowers/specs/2026-08-29-trace-node-detail-design.md
git commit -m "docs(trace): 节点详情验证记录与开关说明"
```

---

## Self-Review（已自检）

- **spec 覆盖**：spec 字段清单的每一项 ↔ Task 2/3 接线点一一对应 ✅；YAGNI 项（嵌套火焰图/prompt 全文入库/聚合分析/Agent 链路）未引入 ✅
- **span kwargs 求值时机**：span 内部产生的数据全部走 `attach()` 事后补挂，plan 已显式标注这个坑 ✅
- **开关语义**：attrs 采集受 `QA_TRACE_DETAIL_ENABLE`；新 span（sparse/rrf/mmr/filter_acl）是纯计时行（现有能力的补全），关开关也存在但仅多 4 行耗时——Step 2 验证以"attrs 不存在"为准 ✅
- **缓存 key**：纯观测，不进 citation_cache_version ✅
- **铁律**：attach/llm_attrs 全 try/except 静默降级 ✅
- **测试可行**：Task 1/3 纯 helper 单测（CI 兼容）；Task 2 接线按仓库惯例不 mock 编排链路，靠 py_compile + Task 5 容器实测 ✅
- **无占位符**：核心代码已给全；标注"行号基于当前文件，改前先 Read 现场"属防漂移检查 ✅
