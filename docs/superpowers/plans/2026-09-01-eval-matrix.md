# 开关对照评测矩阵（capability eval matrix）实现计划

> **For agentic workers:** 按任务逐个执行，步骤用 checkbox 跟踪。spec 见 `docs/superpowers/specs/2026-09-01-eval-matrix-design.md`（背景/市场对标/设计理由在 spec，本计划只写怎么做）。**所有行号基于当前文件，动手前先 Read 现场核对。**

**Goal:** 一条命令产出 13 个"默认关=现状"开关（`config.py:156-258`）各自的"开 vs 关"golden 对照矩阵：检索维 recall/MRR/nDCG/空结果率（子进程+env 覆盖直调 `evaluate_over_golden`），生成维 faithfulness/幻觉率/平均时延（每变体起 uvicorn 子进程 + `run_generation_eval`），汇总 `reports/eval_matrix_<ts>.md/.json`，verdict 只出建议（人拍板）。**不新增任何运行时开关，业务 service 零改动。**

**Architecture:** 新增工具层两件：`backend/app/services/eval_matrix_service.py`（纯核心：VARIANTS 注册表 / env 覆盖 / delta / verdict / markdown 渲染）+ `scripts/eval_matrix.py`（CLI 编排与探针子进程）。采集统一"子进程 + env 覆盖"（pydantic-settings `case_sensitive=False` 自动读 env），绕开"功能开关直接读 `settings`、进程内 overrides 触达不到"的限制（`retrieval_service.py:187/325/335/532`、`qa_service.py:335/388/785...`）；子进程编码强制 utf-8 复用 `eval_suite.run_dim`（`eval_suite.py:38`）既有先例。唯一既有文件改动：`scripts/eval_generation.py` 抽可导入函数（CLI 行为逐字节不变，新增 `--base-url` 可选参数）。**不碰 `tests/redteam/`（另一工作线维护中）。**

**Tech Stack:** 纯 Python（subprocess/asyncio/httpx）+ pytest（纯函数与注入 fake，不碰 Milvus/LLM/网络/真子进程）。

## Global Constraints

- 单测全 CI 兼容：`venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v`（conftest 自动加 `backend/` 进 sys.path）；**scripts 导入**在测试内 `sys.path.insert(0, "scripts")`（conftest 不含 scripts）。
- 矩阵实跑定位**手动/夜间**（需 Milvus+embedding+seed 数据 / LLM key），同 `eval_retrieval/eval_generation` 的既有口径；CI 不跑矩阵。
- 无新 config 字段：`.env.example`/`.env.template` 不动；不进 `citation_cache_version()`；不改任何 `services/` 业务行为。
- verdict 恒为"建议"，绝不自动改 `.env`/开关（评测先行、人拍板，对齐 `retrieval_tune_service` 只建议纪律）。
- Windows 兼容：子进程 env 强制 `PYTHONIOENCODING=utf-8`、`PYTHONUTF8=1`；后端子进程 `terminate()`→`kill()` 兜底。
- 排除变体：`MULTI_TURN_CACHE_ENABLE`（golden 无多轮对）、citation 独立维度（`eval_citation.evaluate()` 不读这些开关，flag 不敏感；运行时 NLI 已被 `citation_nli` 变体的生成维覆盖）。
- lint：`ruff check backend tests scripts`（只要求无新告警）。

## File Structure

- **Add:** `backend/app/services/eval_matrix_service.py`、`scripts/eval_matrix.py`、`tests/test_eval_matrix.py`
- **Modify:** `scripts/eval_generation.py`（抽 `run_generation_eval` + `--base-url`，行为不变）、`AGENTS.md`（Commands 补一行矩阵用法）
- **产物（运行时生成，不入库）:** `reports/eval_matrix_<ts>/probe_*.json`、`reports/eval_matrix_<ts>.md`、`reports/eval_matrix_<ts>.json`

---

### Task 1: 纯核心 `eval_matrix_service.py` + 单测

**Files:**
- Add: `backend/app/services/eval_matrix_service.py`
- Test: `tests/test_eval_matrix.py`

**Interfaces:**
- Produces: `VARIANTS`（baseline 恒首位、env 全空）、`get_variant(name)`、`select_variants(names, dims) -> list[dict]`（未知名抛 `ValueError`；baseline 恒含；dims 求交）、`build_env_overlay(variant, base_env)`、`compute_delta(base, cur)`（跳过 `sampleSize`，`direction=higher|lower`）、`build_verdict(dim, delta)`、`aggregate(probe_results)`、`render_markdown(agg, meta)`
- verdict 规则：恶化 >0.005（`avgLatencyMs` 仅列示不判）→"建议回收（存在退化: …）"；主指标（retrieval=recall ≥+0.01 / generation=faithfulness ≥+0.02）且无退化 →"建议常开候选"；否则"维持关闭（收益不足）"

- [x] **Step 1: 写失败测试**

`tests/test_eval_matrix.py` 新建：

```python
"""开关对照评测矩阵纯核心单测（CI 兼容：纯函数，不碰 Milvus/LLM/网络/子进程）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from app.config import settings  # noqa: E402
from app.services import eval_matrix_service as svc  # noqa: E402


# ---------- 注册表 ----------


def test_variants_reference_real_config_keys():
    """注册表引用的每个 env 键必须真实存在于 settings（防 config 漂移后矩阵空转）。"""
    for v in svc.VARIANTS:
        for key in v["env"]:
            assert hasattr(settings, key), f"{v['name']} 引用了不存在的开关 {key}"
            assert v["env"][key] == "true"


def test_baseline_first_unique_names():
    assert svc.VARIANTS[0]["name"] == "baseline"
    assert svc.VARIANTS[0]["env"] == {}
    names = [v["name"] for v in svc.VARIANTS]
    assert len(names) == len(set(names))
    assert {"retrieval", "generation"} <= {d for v in svc.VARIANTS for d in v["dims"]}


def test_select_variants_all_keeps_baseline_and_intersects_dims():
    picked = svc.select_variants("all", {"retrieval"})
    assert picked[0]["name"] == "baseline"
    assert all(v["dims"] and set(v["dims"]) <= {"retrieval"} for v in picked)


def test_select_variants_explicit_list_always_has_baseline():
    picked = svc.select_variants("hyde,crag_v3", {"retrieval", "generation"})
    assert [v["name"] for v in picked][:2] == ["baseline", "hyde"]


def test_select_variants_unknown_name_rejected():
    with pytest.raises(ValueError):
        svc.select_variants("baseline,no_such_flag", {"retrieval"})


def test_build_env_overlay_applies_variant_and_utf8():
    env = svc.build_env_overlay(svc.get_variant("crag_v3"), base_env={"PATH": "x"})
    assert env["PATH"] == "x"
    assert env["CRAG_V3_ENABLE"] == "true"
    assert env["PYTHONUTF8"] == "1" and env["PYTHONIOENCODING"] == "utf-8"


# ---------- delta / verdict ----------


def test_compute_delta_directions_and_skips_sample_size():
    base = {"recall": 0.87, "noResultRate": 0.03, "sampleSize": 32, "avgLatencyMs": 8000.0}
    cur = {"recall": 0.90, "noResultRate": 0.0, "sampleSize": 32, "avgLatencyMs": 9000.0}
    d = svc.compute_delta(base, cur)
    assert d["recall"]["delta"] == pytest.approx(0.03)
    assert d["recall"]["direction"] == "higher"
    assert d["noResultRate"]["direction"] == "lower"
    assert d["noResultRate"]["delta"] == pytest.approx(-0.03)
    assert "sampleSize" not in d
    assert "avgLatencyMs" in d  # 成本项保留（渲染列示），verdict 不判


def test_verdict_regression_beats_gain():
    d = {"faithfulness": {"base": 0.8, "cur": 0.99, "delta": 0.19, "direction": "higher"},
         "recall": {"base": 0.9, "cur": 0.5, "delta": -0.4, "direction": "higher"}}
    assert "回收" in svc.build_verdict("generation", d)


def test_verdict_adopt_and_insufficient():
    up = {"recall": {"base": 0.86, "cur": 0.88, "delta": 0.02, "direction": "higher"}}
    assert svc.build_verdict("retrieval", up) == "建议常开候选"
    small = {"recall": {"base": 0.86, "cur": 0.865, "delta": 0.005, "direction": "higher"}}
    assert svc.build_verdict("retrieval", small) == "维持关闭（收益不足）"


def test_verdict_lower_better_regression_and_cost_ignored():
    d = {"faithfulness": {"base": 0.9, "cur": 0.9, "delta": 0.0, "direction": "higher"},
         "hallucination": {"base": 0.05, "cur": 0.10, "delta": 0.05, "direction": "lower"},
         "avgLatencyMs": {"base": 8000.0, "cur": 60000.0, "delta": 52000.0, "direction": "lower"}}
    assert "回收" in svc.build_verdict("generation", d)  # 幻觉率恶化判回收
    d2 = {"faithfulness": {"base": 0.9, "cur": 0.9, "delta": 0.0, "direction": "higher"},
          "avgLatencyMs": {"base": 8000.0, "cur": 60000.0, "delta": 52000.0, "direction": "lower"}}
    assert svc.build_verdict("generation", d2) == "维持关闭（收益不足）"  # 时延不判退化


# ---------- 聚合 / 渲染 ----------


def _probes():
    return [
        {"variant": "baseline", "dim": "retrieval",
         "metrics": {"recall": 0.87, "mrr": 0.6, "ndcg": 0.5, "noResultRate": 0.03, "sampleSize": 32}},
        {"variant": "hyde", "dim": "retrieval",
         "metrics": {"recall": 0.90, "mrr": 0.6, "ndcg": 0.5, "noResultRate": 0.03, "sampleSize": 32}},
    ]


def test_aggregate_pairs_baseline_and_orders_by_registry():
    agg = svc.aggregate(_probes())
    rows = agg["retrieval"]["rows"]
    assert [r["variant"] for r in rows] == ["baseline", "hyde"]
    assert rows[0]["verdict"] == "—"
    assert rows[1]["delta"]["recall"]["delta"] == pytest.approx(0.03)
    assert rows[1]["verdict"] == "建议常开候选"
    assert agg["retrieval"]["sampleSize"] == 32


def test_render_markdown_columns_warnings_and_meta():
    md = svc.render_markdown(svc.aggregate(_probes()),
                             {"goldenSize": 32, "topk": 5, "limit": 5, "envSummary": "deepseek"})
    assert "| hyde |" in md and "Δ召回" in md and "建议常开候选" in md
    assert "噪声警告" in md and "32" in md
    assert "semantic_cache" in md and "verdict 仅为建议" in md
```

- [x] **Step 2: 运行确认失败**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
# 预期 FAIL: ModuleNotFoundError: No module named 'app.services.eval_matrix_service'
```

- [x] **Step 3: 实现**

`backend/app/services/eval_matrix_service.py` 新建（全文）：

```python
"""开关对照评测矩阵纯核心：variants 注册表 + env 覆盖 + delta/verdict + 报告渲染。

spec: docs/superpowers/specs/2026-09-01-eval-matrix-design.md
- VARIANTS 全部引用 config.py 实有开关（关=现状）；baseline=全关（现状）。
- 采集走"子进程 + env 覆盖"（pydantic-settings case_sensitive=False 读 env），业务代码零改动。
- verdict 只出建议（评测先行、人拍板）；本模块是评测工具，无运行时开关、
  不影响问答语义、不进 citation_cache_version()。
"""
from __future__ import annotations

import time

# 变体注册表：name 对应 .env 键的 snake_case；env 值恒为 "true"。
# dims: retrieval=子进程直调 evaluate_over_golden（需 Milvus+embedding，无需 LLM/后端）
#       generation=每变体起独立后端子进程 + run_generation_eval（需 LLM key）
VARIANTS: list[dict] = [
    {"name": "baseline", "title": "现状（对照基线）", "env": {},
     "dims": ("retrieval", "generation")},
    # ---- 检索维（retrieval_service 直读 settings，子进程 env 覆盖生效）----
    {"name": "rrf_route_aware", "title": "路由感知调参（A2/A3/A5/B6）",
     "env": {"RRF_ROUTE_AWARE_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "crag_neighbor", "title": "CRAG ambiguous 邻域扩展",
     "env": {"CRAG_NEIGHBOR_EXPAND_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "raptor", "title": "RAPTOR 层次化摘要检索",
     "env": {"RAPTOR_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "hyde", "title": "HyDE 假设答案检索",
     "env": {"HYDE_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "multi_query", "title": "多查询分解",
     "env": {"MULTI_QUERY_ENABLE": "true"}, "dims": ("retrieval",)},
    {"name": "self_rag", "title": "Self-RAG 检索自判",
     "env": {"SELF_RAG_ENABLE": "true"}, "dims": ("retrieval",)},
    # ---- 生成维（qa_service 直读 settings，需变体后端）----
    {"name": "crag_v3", "title": "CRAG v3 连续置信度+归因",
     "env": {"CRAG_V3_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "crag_perdoc", "title": "CRAG v2 LLM 逐条证据评估（token 成本注意）",
     "env": {"CRAG_PERDOC_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "citation_verifier", "title": "引用校验引擎（格式+向量+结构化输出）",
     "env": {"CITATION_VERIFIER_ENABLE": "true", "CITATION_STRUCTURED_OUTPUT": "true"},
     "dims": ("generation",)},
    {"name": "citation_nli", "title": "引用校验3 NLI 精准核验（最重档）",
     "env": {"CITATION_VERIFIER_ENABLE": "true", "CITATION_STRUCTURED_OUTPUT": "true",
             "CITATION_NLI_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "debate", "title": "低置信 debate 三专家裁决（预期增延迟）",
     "env": {"DEBATE_ON_LOW_CONFIDENCE_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "sufficiency_gate", "title": "证据不足置信降档",
     "env": {"SUFFICIENCY_GATE_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "query_rewrite", "title": "LLM query 改写",
     "env": {"QUERY_REWRITE_ENABLE": "true"}, "dims": ("generation",)},
    {"name": "semantic_cache", "title": "语义缓存（首跑不命中，收益看时延列）",
     "env": {"SEMANTIC_CACHE_ENABLE": "true"}, "dims": ("generation",)},
]

# 越高越好的指标；其余（noResultRate/hallucination）越低越好；avgLatencyMs 仅列示不判
_HIGHER_BETTER = frozenset({"recall", "mrr", "ndcg", "faithfulness"})
_PRIMARY = {"retrieval": "recall", "generation": "faithfulness"}
_ADOPT_GAIN = {"retrieval": 0.01, "generation": 0.02}
_REGRESS_EPS = 0.005
_COST_KEYS = frozenset({"avgLatencyMs"})
_SKIP_DELTA = frozenset({"sampleSize"})

_COLS = {
    "retrieval": ("recall", "mrr", "ndcg", "noResultRate"),
    "generation": ("faithfulness", "hallucination", "avgLatencyMs"),
}
_LABEL = {"recall": "召回", "mrr": "MRR", "ndcg": "nDCG", "noResultRate": "空结果率",
          "faithfulness": "支撑率", "hallucination": "幻觉率", "avgLatencyMs": "平均时延ms"}


def get_variant(name: str) -> dict:
    for v in VARIANTS:
        if v["name"] == name:
            return v
    raise ValueError(f"未知变体: {name}（可选: {','.join(v['name'] for v in VARIANTS)}）")


def select_variants(names: str, dims: set[str]) -> list[dict]:
    """按逗号名单挑变体（all=全部）；baseline 恒含且居首；每变体 dims 与所选维度求交。"""
    if names.strip() == "all":
        picked = list(VARIANTS)
    else:
        picked = [get_variant(n.strip()) for n in names.split(",") if n.strip()]
    if not any(v["name"] == "baseline" for v in picked):
        picked.insert(0, get_variant("baseline"))
    out = []
    for v in picked:
        dims_v = tuple(d for d in v["dims"] if d in dims)
        if dims_v:
            out.append({**v, "dims": dims_v})
    return out


def build_env_overlay(variant: dict, base_env: dict | None = None) -> dict:
    """子进程 env = 父 env + 变体覆盖；编码强制 utf-8 兜 Windows GBK（同 eval_suite.run_dim）。"""
    env = {**(base_env or {}), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    env.update(variant.get("env") or {})
    return env


def compute_delta(base: dict, cur: dict) -> dict:
    """逐指标 Δ=cur-base；非数值与 _SKIP_DELTA 跳过；direction 标注好坏方向。"""
    out = {}
    for key, cur_v in cur.items():
        if key in _SKIP_DELTA or not isinstance(cur_v, (int, float)) or isinstance(cur_v, bool):
            continue
        base_v = base.get(key)
        if not isinstance(base_v, (int, float)) or isinstance(base_v, bool):
            continue
        out[key] = {"base": base_v, "cur": cur_v, "delta": round(cur_v - base_v, 4),
                    "direction": "higher" if key in _HIGHER_BETTER else "lower"}
    return out


def build_verdict(dim: str, delta: dict) -> str:
    """verdict 只建议：恶化>eps→建议回收；主指标升≥阈值且无退化→建议常开候选；否则维持关闭。"""
    if not delta:
        return "—"
    regressed = sorted(
        k for k, d in delta.items()
        if k not in _COST_KEYS and (
            (d["direction"] == "higher" and d["delta"] < -_REGRESS_EPS)
            or (d["direction"] == "lower" and d["delta"] > _REGRESS_EPS)
        )
    )
    if regressed:
        return f"建议回收（存在退化: {','.join(regressed)}）"
    primary = delta.get(_PRIMARY[dim])
    if primary and primary["delta"] >= _ADOPT_GAIN[dim]:
        return "建议常开候选"
    return "维持关闭（收益不足）"


def aggregate(probe_results: list[dict]) -> dict:
    """probe_results=[{variant,dim,metrics}] → 按维度分组、对 baseline 算 Δ 与 verdict。

    行序按 VARIANTS 注册表（baseline 恒首）；缺探针的变体不出行。
    """
    by_dim: dict[str, dict] = {}
    for r in probe_results:
        by_dim.setdefault(r["dim"], {}).setdefault("raw", {})[r["variant"]] = r["metrics"]
    out = {}
    for dim, info in by_dim.items():
        base = info["raw"].get("baseline", {})
        rows = []
        for v in VARIANTS:
            metrics = info["raw"].get(v["name"])
            if metrics is None:
                continue
            is_base = v["name"] == "baseline"
            delta = {} if is_base else compute_delta(base, metrics)
            rows.append({
                "variant": v["name"], "title": v["title"], "metrics": metrics,
                "delta": delta, "verdict": "—" if is_base else build_verdict(dim, delta),
            })
        out[dim] = {"baseline": base, "rows": rows, "sampleSize": base.get("sampleSize")}
    return out


def _fmt(v) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if 0 <= v <= 1.5:
        return f"{v * 100:.1f}%"
    return f"{v:,.0f}"


def _fmt_delta(key: str, d: dict) -> str:
    v = d["delta"]
    if key == "avgLatencyMs":
        return f"{v:+,.0f}ms"
    return f"{v * 100:+.1f}pp"


def render_markdown(agg: dict, meta: dict) -> str:
    """矩阵 md：逐维度 Δ 表 + 建议列 + 噪声/语义缓存/只建议三条警示。"""
    lines = [
        f"# 开关对照评测矩阵（{time.strftime('%Y-%m-%d %H:%M:%S')}）", "",
        f"- 环境：{meta.get('envSummary', '')}",
        f"- 参数：topk={meta.get('topk')} limit={meta.get('limit')} "
        f"评测集条数={meta.get('goldenSize', '?')}", "",
    ]
    for dim in ("retrieval", "generation"):
        block = agg.get(dim)
        if not block:
            continue
        cols = _COLS[dim]
        header = (["变体", "说明"]
                  + [c for k in cols for c in (_LABEL[k], f"Δ{_LABEL[k]}")] + ["建议"])
        lines += [f"## {dim} 维", "",
                  "| " + " | ".join(header) + " |",
                  "|" + "---|" * len(header)]
        for row in block["rows"]:
            cells = [row["variant"], row["title"]]
            for k in cols:
                m = row["metrics"].get(k)
                cells.append(_fmt(m) if m is not None else "—")
                d = row["delta"].get(k)
                cells.append(_fmt_delta(k, d) if d else "—")
            cells.append(row["verdict"])
            lines.append("| " + " | ".join(str(c) for c in cells) + " |")
        lines.append("")
    n = int(meta.get("goldenSize") or 0)
    if 0 < n < 50:
        lines.append(f"> ⚠️ 噪声警告：评测集仅 {n} 条，Δ<2pp 不具备统计意义；先扩集再下常开/回收决策。")
    lines += [
        "> ⚠️ semantic_cache 首跑不命中：收益看时延列，需二次运行验证。",
        "> verdict 仅为建议：开关变更需人工评审后改 .env（关=现状语义见 config.py 注释）。", "",
    ]
    return "\n".join(lines) + "\n"
```

- [x] **Step 4: 运行确认通过 + lint**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
ruff check backend/app/services/eval_matrix_service.py tests/test_eval_matrix.py
```
Expected: 14 PASS；ruff 无告警。

- [x] **Step 5: Commit**

```bash
git add backend/app/services/eval_matrix_service.py tests/test_eval_matrix.py
git commit -m "feat(eval): 开关对照评测矩阵纯核心（VARIANTS 注册表/env 覆盖/delta/verdict/渲染）"
```

---

### Task 2: 探针模式骨架 `scripts/eval_matrix.py`（retrieval 探针 + 编排入口）

**Files:**
- Add: `scripts/eval_matrix.py`

**Interfaces:**
- 探针模式（内部）：`python scripts/eval_matrix.py --probe retrieval --json-out <path> [--topk 5]`——**本进程 env 已被父进程覆盖**（变体配置即 `settings`），直调 `retrieval_eval_service.evaluate_over_golden(db, None, topk)` 落 JSON；变体名从 env `EVAL_MATRIX_VARIANT` 读取
- 编排模式：`--dims/--variants/--topk/--out-dir`；Task 4 补 generation 分支与聚合
- Produces（Task 2 版）：检索维矩阵 md/json 已可出（`--dims retrieval`）

- [x] **Step 1: 实现**

`scripts/eval_matrix.py` 新建（Task 2 版全文；Task 3/4 在标注处追加）：

```python
"""开关对照评测矩阵 runner：variants × dims 子进程采集 + 聚合报告。

spec: docs/superpowers/specs/2026-09-01-eval-matrix-design.md
两种用法：
1. 编排模式（默认）：
   python scripts/eval_matrix.py --dims retrieval            # 快速档（需 Milvus+embedding+seed 数据）
   python scripts/eval_matrix.py                             # 全量（生成维另需 LLM key）
   python scripts/eval_matrix.py --variants baseline,hyde,crag_v3 --limit 3
2. 探针模式（内部，由编排模式以 env 覆盖后的子进程调起，勿手工调）：
   python scripts/eval_matrix.py --probe retrieval --json-out reports/x.json [--topk 5]
   python scripts/eval_matrix.py --probe generation --base-url http://127.0.0.1:8011 --json-out ... [--limit 5]

产物：reports/eval_matrix_<ts>/probe_*.json + reports/eval_matrix_<ts>.md + .json
前置：docker compose 起数据服务 + scripts/seed_demo.py 建库（同 eval_retrieval 口径）；
     矩阵实跑需真实服务，定位手动/夜间；CI 只跑其纯函数单测（tests/test_eval_matrix.py）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.services import eval_matrix_service as svc  # noqa: E402


def _write_probe(json_out: Path, dim: str, metrics: dict) -> None:
    """探针结果落盘；env 摘要只留 _ENABLE=true 键做溯源。"""
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps({
        "variant": os.environ.get("EVAL_MATRIX_VARIANT", "unknown"),
        "dim": dim,
        "env": {k: v for k, v in os.environ.items()
                if k.endswith("_ENABLE") and v.lower() == "true"},
        "metrics": metrics,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def run_probe_retrieval(json_out: Path, topk: int) -> int:
    """检索探针：本进程 env 已被父进程覆盖 → settings 即变体配置；直调服务化评测。"""
    from app.db.session import AsyncSessionLocal
    from app.services import retrieval_eval_service

    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            return await retrieval_eval_service.evaluate_over_golden(db, None, topk=topk)

    metrics = asyncio.run(_run())
    _write_probe(json_out, "retrieval", metrics)
    print(f"[probe:retrieval variant={os.environ.get('EVAL_MATRIX_VARIANT', '?')}] "
          f"recall={metrics.get('recall')} sampleSize={metrics.get('sampleSize')}")
    return 0


# ---- Task 3 在此追加：run_probe_generation / start_backend / wait_backend_ready / stop_backend ----


def _run_probe_sync(cli_args: list[str], env: dict) -> bool:
    """以 env 覆盖后的子进程跑探针；utf-8 强制与超时兜底同 eval_suite.run_dim。"""
    cmd = [sys.executable, str(ROOT / "scripts" / "eval_matrix.py")] + cli_args
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired:
        print("    探针超时（1800s）")
        return False
    if r.returncode != 0:
        print((r.stdout or "")[-800:])
        print((r.stderr or "")[-300:])
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="开关对照评测矩阵（variants × dims → 能力收益矩阵）")
    ap.add_argument("--dims", default="retrieval,generation")
    ap.add_argument("--variants", default="all", help="all 或逗号名单（baseline 恒含）")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--limit", type=int, default=5, help="生成维每变体评测条数（控 LLM 成本）")
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    ap.add_argument("--backend-port-base", type=int, default=8010)
    ap.add_argument("--health-timeout", type=float, default=240.0,
                    help="后端子进程就绪等待秒（bge 预热 ~20s）")
    ap.add_argument("--probe", choices=["retrieval", "generation"], default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--base-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--json-out", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.probe:
        if not args.json_out:
            print("探针模式必须给 --json-out")
            return 2
        if args.probe == "retrieval":
            return run_probe_retrieval(Path(args.json_out), args.topk)
        # ---- Task 3 在此追加 generation 探针分支 ----
        print("generation 探针未实现（Task 3）")
        return 2

    dims = {d.strip() for d in args.dims.split(",") if d.strip()}
    unknown = dims - {"retrieval", "generation"}
    if unknown:
        print(f"不支持维度: {','.join(sorted(unknown))}（可选 retrieval,generation）")
        return 2
    try:
        variants = svc.select_variants(args.variants, dims)
    except ValueError as e:
        print(str(e))
        return 2

    out_base = Path(args.out_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = out_base / f"eval_matrix_{ts}"
    probes: list[dict] = []
    for v in variants:
        env = svc.build_env_overlay(v, dict(os.environ))
        env["EVAL_MATRIX_VARIANT"] = v["name"]
        for dim in v["dims"]:
            json_out = outdir / f"probe_{v['name']}_{dim}.json"
            if dim == "retrieval":
                print(f">>> 探针 {v['name']}/{dim}")
                ok = _run_probe_sync(
                    ["--probe", "retrieval", "--json-out", str(json_out),
                     "--topk", str(args.topk)], env)
            else:
                # ---- Task 4 在此追加：_run_generation_with_backend 分支 ----
                print(f">>> 跳过 {v['name']}/{dim}（generation 编排在 Task 4 接线）")
                ok = False
            if ok:
                probes.append(json.loads(json_out.read_text(encoding="utf-8")))
            else:
                print(f"    {v['name']}/{dim}: 探针失败（详见上方输出）")

    # ---- Task 4 在此追加：聚合落盘 ----
    print(f"探针成功 {len(probes)} 个（聚合在 Task 4 接线）")
    return 0 if probes else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 2: 编译与冒烟（不需要服务）**

```bash
venv/Scripts/python.exe -m py_compile scripts/eval_matrix.py
venv/Scripts/python.exe scripts/eval_matrix.py --dims retrieval --variants baseline,no_such 2>&1 | tail -2
# 预期: 未知变体: no_such（可选: baseline,rrf_route_aware,...） 退出码 2
venv/Scripts/python.exe scripts/eval_matrix.py --dims bad_dim 2>&1 | tail -1
# 预期: 不支持维度: bad_dim（可选 retrieval,generation）
ruff check scripts/eval_matrix.py
```

- [x] **Step 3: 探针单测（mock 服务层，CI 兼容）**

`tests/test_eval_matrix.py` 末尾追加：

```python
# ---------- scripts/eval_matrix.py 探针（mock 服务层，不碰 Milvus/网络/子进程）----------
import json  # noqa: E402


def test_probe_retrieval_writes_json(tmp_path, monkeypatch):
    import eval_matrix as em

    async def fake_eval(db, overrides, topk=5):
        return {"recall": 0.9, "mrr": 0.7, "ndcg": 0.6, "noResultRate": 0.0, "sampleSize": 32}

    from app.services import retrieval_eval_service
    monkeypatch.setattr(retrieval_eval_service, "evaluate_over_golden", fake_eval)
    # run_probe_retrieval 的 from-import 在调用时才解析 → patch 源模块属性生效（见步骤内注）

    class _FakeSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    import app.db.session as db_session
    monkeypatch.setattr(db_session, "AsyncSessionLocal", lambda: _FakeSession())

    out = tmp_path / "probe.json"
    monkeypatch.setenv("EVAL_MATRIX_VARIANT", "hyde")
    rc = em.run_probe_retrieval(out, topk=5)
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["variant"] == "hyde" and data["dim"] == "retrieval"
    assert data["metrics"]["recall"] == 0.9
    assert data["env"].get("EVAL_MATRIX_VARIANT") is None  # env 摘要只含 *_ENABLE 键
```

（注：`run_probe_retrieval` 内部是 `from app.db.session import AsyncSessionLocal` 函数内导入——**函数内 from-import 在调用时才解析**，故 patch `db_session.AsyncSessionLocal` 生效；`retrieval_eval_service.evaluate_over_golden` 同理 patch 源模块属性。若现场验证 from-import 缓存导致 patch 不生效，把 `run_probe_retrieval` 改为模块级 `from app.db.session import AsyncSessionLocal` 之上再 `db_session.AsyncSessionLocal()` 调用形式——以实测为准，两种写法都给出在实现时二选一。）

- [x] **Step 4: 运行确认通过**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
```
Expected: PASS（Task 1 的 14 个 + 本步 1 个）。若 `eval_matrix` 导入触发 `app.config` 读 `.env` 报错，确认工作目录为仓库根（conftest 已保证）。

- [x] **Step 5: Commit**

```bash
git add scripts/eval_matrix.py tests/test_eval_matrix.py
git commit -m "feat(eval): eval_matrix runner 骨架 + retrieval 探针（env 覆盖子进程直调 evaluate_over_golden）"
```

---

### Task 3: `eval_generation.py` 抽 `run_generation_eval`（行为不变）+ generation 探针与后端子进程起停

**Files:**
- Modify: `scripts/eval_generation.py`（BASE 原为模块级 `"http://127.0.0.1:8001"`，主逻辑在 `main()` 内）
- Modify: `scripts/eval_matrix.py`（追加 generation 探针 + 后端起停）
- Test: `tests/test_eval_matrix.py`（追加）

**Interfaces:**
- Produces: `eval_generation.run_generation_eval(base_url=BASE, limit=10, gate=0.85) -> dict`，键 `{faithfulness, hallucination, avgLatencyMs, sampleSize, rows, gate, pass}`；CLI 行为逐字节兼容（`--base-url` 为新增可选参数，默认值不变）
- `eval_matrix.start_backend(port, env) -> Popen`（uvicorn `--app-dir backend`，同 AGENTS 开发口径）、`wait_backend_ready(base_url, timeout_s, prober=None, interval_s=2.0)`（POST `/api/system/login` HTTP 200 即就绪——BizError 也走 200 body，可作存活探针；bge 预热 ~20s，默认等 240s）、`stop_backend(proc)`（terminate→wait(10)→kill）

- [x] **Step 1: 写失败测试**

`tests/test_eval_matrix.py` 末尾追加：

```python
# ---------- eval_generation 抽函数 + 后端起停 ----------


def test_run_generation_eval_importable_and_defaults():
    """可导入 + 默认值保持原 CLI 口径（base 8001 / gate 0.85），不发请求。"""
    import inspect

    import eval_generation as eg

    sig = inspect.signature(eg.run_generation_eval)
    assert sig.parameters["base_url"].default == eg.BASE
    assert sig.parameters["gate"].default == 0.85
    assert eg.BASE == "http://127.0.0.1:8001"  # 原 CLI 行为不变


class _FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.polled = None

    def poll(self):
        return self.polled

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def test_stop_backend_terminate_graceful_and_kill_fallback():
    import subprocess as sp

    import eval_matrix as em

    p = _FakeProc()
    em.stop_backend(p)
    assert p.terminated and not p.killed

    p2 = _FakeProc()

    def timeout_wait(timeout=None):
        raise sp.TimeoutExpired(cmd="uvicorn", timeout=timeout or 10)

    p2.wait = timeout_wait
    em.stop_backend(p2)
    assert p2.terminated and p2.killed  # terminate 超时 → kill 兜底


def test_stop_backend_skips_dead_process():
    import eval_matrix as em

    p = _FakeProc()
    p.polled = 0  # 已退出
    em.stop_backend(p)
    assert not p.terminated and not p.killed


def test_wait_backend_ready_injected_prober_and_timeout():
    import eval_matrix as em

    assert em.wait_backend_ready("http://x", timeout_s=1.0,
                                 prober=lambda url: True, interval_s=0.01) is True
    assert em.wait_backend_ready("http://x", timeout_s=0.05,
                                 prober=lambda url: False, interval_s=0.01) is False
```

- [x] **Step 2: 运行确认失败**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
# 预期 FAIL: AttributeError: module 'eval_generation' has no attribute 'run_generation_eval'
#           与 AttributeError: module 'eval_matrix' has no attribute 'stop_backend'
```

- [x] **Step 3: 实现**

1. `scripts/eval_generation.py`：把 `main()` 内的评测主体抽为模块级 `run_generation_eval`（**保持原打印与口径**，行号基于当前文件，改前先 Read 现场）。将现有：

```python
async def main():
    ...
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[: args.limit]
    ...
    rows, sup_sum = [], 0.0
    for it in golden:
        ...
    n = len(rows)
    avg_sup = sup_sum / n if n else 0.0
    avg_halluc = sum(r[2] for r in rows) / n if n else 0.0
    passed = avg_sup >= args.gate
    ...
```

改为（新全文替换 `main`，新增 `run_generation_eval`；`import time` 加进模块头）：

```python
async def run_generation_eval(base_url: str = BASE, limit: int = 10, gate: float = 0.85) -> dict:
    """可导入评测核心：POST /api/qa/answer × golden 前 limit 条 + LLM-judge 支撑率。

    返回 {faithfulness, hallucination, avgLatencyMs, sampleSize, rows, gate, pass}；
    CLI main() 薄壳打印并按 pass 退出。矩阵探针复用本函数（base_url 指向变体后端）。
    """
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[:limit]
    from app.config import settings
    from app.rag.judge import judge_hallucination

    rows, sup_sum, hall_sum, lat_sum = [], 0.0, 0.0, 0.0
    async with httpx.AsyncClient(timeout=120) as c:
        token = (
            await c.post(f"{base_url}/api/system/login",
                         json={"username": "admin", "password": "admin123"})
        ).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}
        for it in golden:
            q = it["query"]
            t0 = time.perf_counter()
            r = (
                await c.post(f"{base_url}/api/qa/answer", headers=H,
                             json={"query": q, "modelType": "deepseek"})
            ).json()["data"]
            latency_ms = (time.perf_counter() - t0) * 1000
            sources = [s.get("text", "") for s in r.get("retrievalSource", [])]
            j = await judge_hallucination(r["answer"], sources, settings.LLM_PROVIDER)
            sup_sum += j["supported_ratio"]
            hall_sum += j["hallucination"]
            lat_sum += latency_ms
            rows.append({"query": q, "support": j["supported_ratio"],
                         "hallucination": j["hallucination"], "latencyMs": round(latency_ms)})
            print(f'  支撑={j["supported_ratio"]:.2f} 幻觉={j["hallucination"]:.2f} | {q[:24]}')

    n = len(rows)
    avg_sup = sup_sum / n if n else 0.0
    avg_halluc = hall_sum / n if n else 0.0
    return {
        "faithfulness": round(avg_sup, 4), "hallucination": round(avg_halluc, 4),
        "avgLatencyMs": round(lat_sum / n if n else 0.0), "sampleSize": n, "rows": rows,
        "gate": gate, "pass": avg_sup >= gate,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="评测条数（每条 1 次 LLM，控制成本）")
    ap.add_argument("--gate", type=float, default=0.85, help="平均支撑率门禁（低于 exit 1）")
    ap.add_argument("--base-url", default=BASE, help="后端地址（矩阵探针指向变体后端）")
    args = ap.parse_args()
    r = await run_generation_eval(base_url=args.base_url, limit=args.limit, gate=args.gate)
    print(f"\n平均支撑率 = {r['faithfulness']:.2%} | 平均幻觉率 = {r['hallucination']:.2%}"
          f" | 平均时延 = {r['avgLatencyMs']:.0f}ms")
    print(f"门禁 {args.gate:.0%} → {'✓ PASS' if r['pass'] else '✗ FAIL'}")
    sys.exit(0 if r["pass"] else 1)
```

2. `scripts/eval_matrix.py`：把 Task 2 标注的"Task 3 追加区"填充（追加在 `run_probe_retrieval` 之后）：

```python
def run_probe_generation(json_out: Path, base_url: str, limit: int) -> int:
    """生成探针：base_url 指向**父进程已按变体 env 起好的后端**；gate 传 1.01（矩阵只比数值不卡门禁）。"""
    from eval_generation import run_generation_eval

    metrics = asyncio.run(run_generation_eval(base_url=base_url, limit=limit, gate=1.01))
    slim = {k: v for k, v in metrics.items() if k != "rows"}
    _write_probe(json_out, "generation", slim)
    print(f"[probe:generation variant={os.environ.get('EVAL_MATRIX_VARIANT', '?')}] "
          f"faithfulness={metrics.get('faithfulness')}")
    return 0


def start_backend(port: int, env: dict) -> subprocess.Popen:
    """起变体后端子进程（uvicorn --app-dir backend，同 AGENTS 开发口径）。"""
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--app-dir", "backend"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _login_probe(base_url: str) -> bool:
    import httpx

    try:
        return httpx.post(f"{base_url}/api/system/login", json={}, timeout=5).status_code == 200
    except Exception:
        return False


def wait_backend_ready(base_url: str, timeout_s: float, prober=None,
                       interval_s: float = 2.0) -> bool:
    """轮询到就绪；prober 可注入供单测。bge 预热 ~20s，编排默认等 240s。"""
    deadline = time.time() + timeout_s
    prober = prober or _login_probe
    while time.time() < deadline:
        if prober(base_url):
            return True
        time.sleep(interval_s)
    return False


def stop_backend(proc: subprocess.Popen) -> None:
    """terminate→wait(10)→kill 兜底；已退出进程跳过。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
```

并把 `main()` 探针分支的占位替换：

```python
        if args.probe == "retrieval":
            return run_probe_retrieval(Path(args.json_out), args.topk)
        if not args.base_url:
            print("generation 探针必须给 --base-url")
            return 2
        return run_probe_generation(Path(args.json_out), args.base_url, args.limit)
```

- [x] **Step 4: 运行确认通过 + lint + CLI 兼容冒烟**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
ruff check scripts/eval_generation.py scripts/eval_matrix.py tests/test_eval_matrix.py
# CLI 行为兼容冒烟（可选，需后端在 8001）：python scripts/eval_generation.py --limit 1
```
Expected: 全 PASS；ruff 无新告警。

- [x] **Step 5: Commit**

```bash
git add scripts/eval_generation.py scripts/eval_matrix.py tests/test_eval_matrix.py
git commit -m "feat(eval): run_generation_eval 抽函数(--base-url 行为不变) + generation 探针与变体后端起停"
```

---

### Task 4: 编排 main 聚合落盘（矩阵报告）

**Files:**
- Modify: `scripts/eval_matrix.py`（main 的 generation 分支与聚合落盘）
- Test: `tests/test_eval_matrix.py`（追加）

**Interfaces:**
- main 编排：逐变体（baseline 恒首）→ 检索维子进程探针 / 生成维"起后端→等就绪→探针→停后端"→ 收集 probe JSON → `svc.aggregate` + `svc.render_markdown` → 落 `<out-dir>/eval_matrix_<ts>.md`、`.json`
- 生成维后端端口从 `--backend-port-base` 起逐变体递增；单变体后端未就绪 → 跳过该变体该维度并继续（不中断整场）
- 全部探针失败 → 退出码 1

- [x] **Step 1: 写失败测试**

`tests/test_eval_matrix.py` 末尾追加：

```python
# ---------- 编排 main（fake 探针/后端，CI 兼容）----------


def _fake_probe_writer(metrics_by_variant: dict):
    """伪造 _run_probe_sync：按 env 变体名与 --json-out 落 canned 探针 JSON。"""

    def fake(cli_args, env):
        variant = env["EVAL_MATRIX_VARIANT"]
        out = Path(cli_args[cli_args.index("--json-out") + 1])
        dim = "retrieval" if "retrieval" in cli_args else "generation"
        metrics = metrics_by_variant.get((variant, dim))
        if metrics is None:
            return False
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"variant": variant, "dim": dim, "metrics": metrics},
                                  ensure_ascii=False), encoding="utf-8")
        return True

    return fake


def test_main_orchestrates_and_writes_matrix(tmp_path, monkeypatch):
    import eval_matrix as em

    metrics = {
        ("baseline", "retrieval"): {"recall": 0.87, "mrr": 0.6, "ndcg": 0.5,
                                    "noResultRate": 0.03, "sampleSize": 32},
        ("hyde", "retrieval"): {"recall": 0.90, "mrr": 0.6, "ndcg": 0.5,
                                "noResultRate": 0.03, "sampleSize": 32},
        ("baseline", "generation"): {"faithfulness": 0.86, "hallucination": 0.05,
                                     "avgLatencyMs": 8000, "sampleSize": 5},
        ("crag_v3", "generation"): {"faithfulness": 0.89, "hallucination": 0.04,
                                    "avgLatencyMs": 8200, "sampleSize": 5},
    }
    monkeypatch.setattr(em, "_run_probe_sync", _fake_probe_writer(metrics))

    async def fake_gen(v, json_out, env, port, args):
        return em._run_probe_sync(
            ["--probe", "generation", "--json-out", str(json_out), "--limit", "5"], env)

    monkeypatch.setattr(em, "_run_generation_with_backend", fake_gen)

    rc = em.main_with_args(
        ["--dims", "retrieval,generation", "--variants", "baseline,hyde,crag_v3",
         "--out-dir", str(tmp_path)])
    assert rc == 0
    mds = list(tmp_path.glob("eval_matrix_*.md"))
    assert mds and "hyde" in mds[0].read_text(encoding="utf-8")
    assert "建议常开候选" in mds[0].read_text(encoding="utf-8")
    js = list(tmp_path.glob("eval_matrix_*.json"))
    assert js and json.loads(js[0].read_text(encoding="utf-8"))["matrix"]["generation"]["rows"]


def test_main_all_probes_failed_exit_1(tmp_path, monkeypatch):
    import eval_matrix as em

    monkeypatch.setattr(em, "_run_probe_sync", lambda a, e: False)
    monkeypatch.setattr(em, "_run_generation_with_backend", lambda *a, **k: False)
    assert em.main_with_args(["--dims", "retrieval", "--out-dir", str(tmp_path)]) == 1


def test_main_unknown_dim_exit_2(tmp_path):
    import eval_matrix as em

    assert em.main_with_args(["--dims", "bad", "--out-dir", str(tmp_path)]) == 2
```

- [x] **Step 2: 运行确认失败**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
# 预期 FAIL: AttributeError: module 'eval_matrix' has no attribute 'main_with_args'
```

- [x] **Step 3: 实现**

`scripts/eval_matrix.py`：

1. 把 Task 2 的 `main() -> int` 拆为 `main_with_args(argv: list[str] | None = None) -> int`（`argv` 传给 `ap.parse_args(argv)`；`if __name__` 块改 `sys.exit(main_with_args())`）。
2. Task 2 的"Task 4 占位"替换为真实分支：

```python
            else:
                ok = _run_generation_with_backend(v, json_out, env, port, args)
                port += 1
```

（`port = args.backend_port_base` 在变体循环前初始化。）

3. 循环后追加聚合落盘（替换"Task 4 占位"打印）：

```python
    if not probes:
        print("无成功探针，无法聚合")
        return 1

    agg = svc.aggregate(probes)
    meta = {
        "envSummary": f"provider={os.environ.get('LLM_PROVIDER', '')}",
        "topk": args.topk, "limit": args.limit,
        "goldenSize": next((p["metrics"].get("sampleSize") for p in probes
                            if p["dim"] == "retrieval"), None),
    }
    md_path = out_base / f"eval_matrix_{ts}.md"
    json_path = out_base / f"eval_matrix_{ts}.json"
    md_path.write_text(svc.render_markdown(agg, meta), encoding="utf-8")
    json_path.write_text(json.dumps({"meta": meta, "matrix": agg},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== 矩阵完成：报告 {md_path} | JSON {json_path} ===")
    return 0
```

4. 生成维后端编排（追加在 `_run_probe_sync` 之后）：

```python
def _run_generation_with_backend(v: dict, json_out: Path, env: dict, port: int, args) -> bool:
    """起变体后端 → 等就绪 → generation 探针 → 停后端；未就绪跳过不中断整场。"""
    base_url = f"http://127.0.0.1:{port}"
    print(f">>> 起变体后端 {v['name']} @:{port} ...")
    proc = start_backend(port, env)
    try:
        if not wait_backend_ready(base_url, args.health_timeout):
            print(f"    后端未就绪（>{args.health_timeout}s），跳过 {v['name']}")
            return False
        return _run_probe_sync(
            ["--probe", "generation", "--json-out", str(json_out),
             "--base-url", base_url, "--limit", str(args.limit)], env)
    finally:
        stop_backend(proc)
```

- [x] **Step 4: 运行确认通过 + lint**

```bash
venv/Scripts/python.exe -m pytest tests/test_eval_matrix.py -v
ruff check scripts/eval_matrix.py tests/test_eval_matrix.py
```
Expected: 全 PASS（Task 1 的 14 + Task 2 的 1 + Task 3 的 4 + 本步 3 = 22）。

- [x] **Step 5: Commit**

```bash
git add scripts/eval_matrix.py tests/test_eval_matrix.py
git commit -m "feat(eval): eval_matrix 编排聚合（生成维变体后端起停 + 矩阵 md/json 落盘）"
```

---

### Task 5: 全量回归 + 实跑验证 + 文档

- [x] **Step 1: 全量回归 + lint**

```bash
ruff check backend tests scripts
venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/test_api.py -m "not integration"
```
Expected: ruff 无新告警；测试全过（新增仅 `tests/test_eval_matrix.py`；`tests/redteam/` 为另一工作线的未跟踪目录，结果不计入本计划验收）。既有环境性失败不新增。

- [ ] **Step 2: 检索维实跑（快速档）**

前置：`docker compose up -d` 数据服务健康 + `scripts/seed_demo.py` 建过库（golden 文档在 Milvus）。

```bash
venv/Scripts/python.exe scripts/eval_matrix.py --dims retrieval
```
逐项核对：
1. `reports/eval_matrix_*/` 有 7 个 `probe_*_retrieval.json`（baseline + 6 检索变体）；
2. `reports/eval_matrix_*.md` 检索维表行序 = 注册表序（baseline 首行，verdict 列非空）；
3. baseline recall 与 `python scripts/eval_retrieval.py --topk 5` 同量级（同一引擎两种入口，允许采样口径差异）；
4. JSON 里 `env` 字段能溯源每个变体开启的 `_ENABLE` 键。

- [ ] **Step 3: 生成维实跑（小样控成本）**

前置：`.env` 有 `DEEPSEEK_API_KEY`（或改 `LLM_PROVIDER`）；宿主 venv 可起后端（MySQL/Milvus/Redis 走 compose 映射端口）。

```bash
venv/Scripts/python.exe scripts/eval_matrix.py --dims generation --limit 3
```
逐项核对：
1. 每 generation 变体后端起停日志完整、端口从 8010 递增、无残留进程（`netstat -ano | findstr 801` 应只剩常驻 8001）；
2. md 生成维表含支撑率/幻觉率/平均时延三列与建议列；
3. `semantic_cache` 行时延列异常偏大属预期（首跑不命中），md 尾部 caveat 已说明；
4. 总 LLM 调用量 ≈ 9 变体 × 3 条 × 2（问答+judge）≈ 54 次，成本可接受。

- [x] **Step 4: 关态回归核对（零破坏）**

```bash
git stash && docker compose up -d --build backend && sleep 25 \
  && curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8001/api/system/login -H "Content-Type: application/json" -d '{}' ; git stash pop
```
Expected: 本计划全程未改任何 `backend/app/**` 业务文件（仅 `scripts/` + 新 service），后端行为与改造前一致；`config.py`/`.env.example` 无 diff。

- [x] **Step 5: 文档提交**

1. `AGENTS.md` 的 Commands 段（Gates 行之后）补一行：

```markdown
- `scripts/eval_matrix.py`（开关对照矩阵，手动/夜间；检索维需 Milvus+seed，生成维另需 LLM）: `python scripts/eval_matrix.py --dims retrieval` 快速档，全量 `python scripts/eval_matrix.py`；报告落 `reports/eval_matrix_*.md`，verdict 仅建议。
```

2. spec 头部标注"已实现（2026-09-01）"；勾选本 plan 各 checkbox。

```bash
git add docs/superpowers/plans/2026-09-01-eval-matrix.md docs/superpowers/specs/2026-09-01-eval-matrix-design.md AGENTS.md
git commit -m "docs(eval): 开关对照评测矩阵 spec/plan 入库 + AGENTS 命令补充"
```

---

## Self-Review（已自检）

- **spec 覆盖**：spec §5 五项拆分 ↔ Task 1（纯核心）/ Task 2（retrieval 探针+骨架）/ Task 3（generation 抽函数+后端起停）/ Task 4（编排聚合）/ Task 5（回归+实跑+文档）一一对应 ✅；非目标（扩 golden 集、agent 轨迹评测、红队扩展、自动改配置、前端页）均未引入 ✅
- **零业务侵入**：唯一既有文件改动是 `eval_generation.py` 抽函数（CLI 默认行为不变，有 `test_run_generation_eval_importable_and_defaults` 锁 BASE/默认 gate）；`config.py`/`.env.example`/`services/` 业务逻辑零 diff；不进 `citation_cache_version()` ✅
- **开关语义**：矩阵不改开关，只以子进程 env 覆盖采集；`test_variants_reference_real_config_keys` 防 config 漂移；baseline 恒首位且 env 全空有测试锁定 ✅
- **verdict 纪律**：只建议、人拍板；`avgLatencyMs` 仅列示不判退化（debate/semantic_cache 类成本项）；lower-better 指标方向有专测 ✅
- **复用不新造**：`evaluate_over_golden` / `run_generation_eval` / `eval_suite.run_dim` 的子进程 utf-8 先例 / `AsyncSessionLocal`；唯一新抽象是 VARIANTS 注册表与后端起停 helper（后者 prober 可注入可单测）✅
- **测试可行性**：22 例全部纯函数/monkeypatch/注入 fake，不碰 Milvus/embedding/LLM/真子进程/网络，CI 兼容；scripts 导入用 `sys.path.insert` 显式声明 ✅
- **Windows 兼容**：子进程 env 强制 utf-8（`eval_suite.run_dim` 同款）；`stop_backend` terminate→kill 兜底有测试；端口递增避让 8001 ✅
- **成本护栏**：生成维默认 limit=5、实跑验收用 limit=3，LLM 调用量在报告中可估；探针 1800s 超时兜底 ✅
- **边界**：不碰 `tests/redteam/`（另一工作线维护中，Task 5 明确不计入验收）；不改 `docs/*.md` 既有内容（仅 spec/plan 新文件与 AGENTS.md 一行）✅
- **无占位符**：service 全文、script 全文（Task 2 骨架 + Task 3/4 精确替换块）、测试全量代码、改造前后 diff 片段、命令与预期输出均已给全；"行号基于当前文件，改前先 Read 现场"属防漂移检查 ✅
