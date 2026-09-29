"""检索分级（设计文档 §5.2、§16.3）：只认本次打分是否成功，不认配置开关。

原实现分级时读 settings.RERANK_ENABLE（恒为 True），rerank 超时/无 key 时拿 RRF 分数
（约 0.016~0.03）或全 0 分去比阈值 → 全部 incorrect → 全部拒答。现在打分失败时分级为
unscored：不纠错、对外置信度 medium，并在响应中带 scoringOk=false。

三种纠错模式共用同一个 get_thresholds()，保证对照公平。
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from app.config import settings
from app.core.obs import degraded

Grade = Literal["correct", "ambiguous", "incorrect", "unscored"]
GRADE_UNSCORED = "unscored"

_THRESHOLDS_DIR = Path(__file__).resolve().parents[2] / "scorer_thresholds"


@dataclass(frozen=True)
class Thresholds:
    high: float
    low: float
    source: str = ""   # 每个阈值取自哪一层，如 "high=pack,low=settings"


@lru_cache
def _calibrated(scorer_name: str) -> dict:
    p = _THRESHOLDS_DIR / f"{scorer_name}.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def get_thresholds() -> Thresholds:
    """优先级（每个阈值独立回落）：领域包 scorer.high/low > scorer_thresholds/<scorer>.yaml
    > 运行时热配置 config:crag > CRAG_HIGH/CRAG_LOW。结果不满足 low < high 时整体回落到 settings。"""
    from app.core.domain_pack import get_pack
    from app.services import config_service
    pack_sc = get_pack().scorer
    cal = _calibrated(settings.SCORER)
    layers = [
        ("pack", {"high": pack_sc.high, "low": pack_sc.low}),
        ("calibrated", {"high": cal.get("high"), "low": cal.get("low")}),
        ("runtime", {"high": config_service._RUNTIME.get("crag_high"),   # 仅管理后台改过时生效
                     "low": config_service._RUNTIME.get("crag_low")}),
        ("settings", {"high": settings.CRAG_HIGH, "low": settings.CRAG_LOW}),
    ]
    picked: dict[str, tuple[float, str]] = {}
    for key in ("high", "low"):
        for name, vals in layers:
            if vals[key] is not None:
                picked[key] = (float(vals[key]), name)
                break
    (high, hs), (low, ls) = picked["high"], picked["low"]
    if not low < high:
        degraded("grading_thresholds", ValueError(f"low={low}({ls}) >= high={high}({hs})"),
                 "阈值来源冲突 → 回落 CRAG_HIGH/CRAG_LOW")
        return Thresholds(settings.CRAG_HIGH, settings.CRAG_LOW, "settings(fallback)")
    return Thresholds(high, low, f"high={hs},low={ls}")


def grade(items: list[dict], scoring, th: Thresholds) -> tuple[str, str]:
    """返回 (grade, reason)。scoring 为 retrieval_service.ScoringStatus。"""
    if not scoring.ok:
        return GRADE_UNSCORED, scoring.reason
    if not items:
        return "incorrect", "no_items"
    top1 = float(items[0].get("score", 0.0) or 0.0)
    if top1 >= th.high:
        return "correct", f"top1 {top1:.3f} ≥ {th.high}"
    if top1 < th.low:
        return "incorrect", f"top1 {top1:.3f} < {th.low}"
    return "ambiguous", f"top1 {top1:.3f} 介于 [{th.low}, {th.high})"
