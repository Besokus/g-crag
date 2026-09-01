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
