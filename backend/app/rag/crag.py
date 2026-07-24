"""Corrective RAG (CRAG)：检索结果相关性分级 + 纠错闭环（实时护栏，降幻觉）。

检索（rerank 已给相关性分）后分级：
  correct    top1 分 >= HIGH  → 证据充分，正常生成
  ambiguous  中间            → 证据有限，答案标注不确定性
  incorrect  top1 分 < LOW 或空 → 触发纠错（query 改写重检索）；仍低分 → refused（拒答/保守）

信号源复用 rerank 的 relevance_score（不额外调评估 LLM，省钱 + 低延迟）。
rerank 未启用/失败时 score 语义不可靠 → 降级 ambiguous（保守，不误触发纠错）。

对应 2026 RAG 趋势的 Self-RAG / CRAG / Adaptive RAG：自纠错闭环。
与 rag/judge.py 互补：judge 是离线事后验尸，CRAG 是在线实时前置护栏。
"""
from app.config import settings

GRADE_CORRECT = "correct"
GRADE_AMBIGUOUS = "ambiguous"
GRADE_INCORRECT = "incorrect"


def grade(
    top1_score: float, n_contexts: int, rerank_ok: bool = True,
    es: float | None = None,
) -> tuple[str, str]:
    """检索结果分级。返回 (grade, reason)。

    rerank_ok=False（rerank 关闭/失败，score 不可靠）→ 降级 ambiguous，不误触发纠错。
    T4（断点 C）：es 非 None 时用 es 分桶（统一 v1 绝对阈值 / v2 相对计数口径），
    否则老 top1 逻辑（关 V3 现状）。
    """
    if not rerank_ok:
        return GRADE_AMBIGUOUS, "rerank 未启用，无法可靠分级（保守降级）"
    if n_contexts == 0:
        return GRADE_INCORRECT, "检索无结果"
    if es is not None:
        if es >= settings.CRAG_HIGH:
            return GRADE_CORRECT, f"es {es:.2f} ≥ {settings.CRAG_HIGH}"
        if es < settings.CRAG_LOW:
            return GRADE_INCORRECT, f"es {es:.2f} < {settings.CRAG_LOW}"
        return GRADE_AMBIGUOUS, f"es {es:.2f} 介于阈值间"
    if top1_score >= settings.CRAG_HIGH:
        return GRADE_CORRECT, f"top1 相关性 {top1_score:.2f} ≥ {settings.CRAG_HIGH}"
    if top1_score < settings.CRAG_LOW:
        return GRADE_INCORRECT, f"top1 相关性 {top1_score:.2f} < {settings.CRAG_LOW}"
    return GRADE_AMBIGUOUS, f"top1 相关性 {top1_score:.2f} 介于阈值间"


def confidence_of(grade: str, rewritten: bool) -> str:
    """把分级 + 是否改写重检索映射为对外置信度 high/medium/low/refused。

    refused = 改写重检索后仍 incorrect（强相关证据缺失）→ 建议拒答/保守作答。
    """
    if grade == GRADE_CORRECT and not rewritten:
        return "high"
    if grade == GRADE_INCORRECT and rewritten:
        return "refused"
    return "medium"


def _clamp01(x: float) -> float:
    """夹到 [0,1] 并保留 3 位小数。"""
    return round(max(0.0, min(1.0, float(x))), 3)


def evidence_strength(
    *, top1: float | None = None, detail: dict | None = None,
    rerank_ok: bool = True,
) -> float | None:
    """统一证据强度 ∈ [0,1]，拉通 v1/v2 分级口径（confidence refinement T1）。

    优先级：detail（v2 逐条标签聚合）> top1（v1 rerank top1 分）> None。

    - detail: (relevant + 0.5·partial) / n（relevant 全权重、partial 半权重、irrelevant 零）。
    - rerank_ok=False（rerank 未启用/失败，top1 语义不可靠）→ 返回 None，
      由 caller 标 evaluatorDegraded，不参与 grade/confidence 分桶（断点 D）。
    - detail.n=0（异常）→ 回退 top1。

    v1/v2 两路统一为同一 [0,1] 信号，消除"绝对阈值 vs 相对计数"口径漂移（断点 C）。
    """
    if not rerank_ok:
        return None
    if detail:
        rel = int(detail.get("relevant", 0))
        partial = int(detail.get("partial", 0))
        irr = int(detail.get("irrelevant", 0))
        n = int(detail.get("n", rel + partial + irr))
        if n <= 0:
            return _clamp01(top1) if top1 is not None else None
        return round(min(1.0, (rel + 0.5 * partial) / n), 3)
    if top1 is not None:
        return _clamp01(top1)
    return None


# confidence refinement T3：5 档细分阈值（由 evidence_strength 分桶）
_CONF_HIGH = 0.7
_CONF_MEDIUM_HIGH = 0.5
_CONF_MEDIUM_LOW = 0.35
_CONF_LOW = 0.2
_CONF_DEGRADED_SCORE = 0.3  # rerank 降级封顶 low 的标定分


def confidence_score(
    es: float | None, action: str, degraded: bool = False,
) -> tuple[float, str]:
    """连续置信度 + 5 档标签（confidence refinement T3）。

    es: evidence_strength ∈ [0,1]（None=评估器降级）；action: normal/rewritten/refused。
    返回 (score, label)，label ∈ high/medium_high/medium_low/low/refused。

    - action=refused（改写后仍 incorrect）：强 refused（score 用 es，通常极低）。
    - degraded（rerank 未启用/失败，es=None）：封顶 low（不进 high/medium_high，断点 D）。
    - 否则按 es 分桶；es<_CONF_LOW → refused（证据极弱）。
    """
    if action == "refused":
        return (round(float(es or 0.0), 3), "refused")
    if es is None or degraded:
        return (_CONF_DEGRADED_SCORE, "low")
    es = max(0.0, min(1.0, float(es)))
    if es >= _CONF_HIGH:
        lbl = "high"
    elif es >= _CONF_MEDIUM_HIGH:
        lbl = "medium_high"
    elif es >= _CONF_MEDIUM_LOW:
        lbl = "medium_low"
    elif es >= _CONF_LOW:
        lbl = "low"
    else:
        lbl = "refused"
    return (round(es, 3), lbl)


def label_to_confidence(label: str) -> str:
    """5 档细分标签 → 老对外置信度 high/medium/refused（前端 confLabel 零改动）。

    low 映射 medium（老字段无 low；low 仅在 confidenceLabel 新字段可见）。
    """
    return {"high": "high", "medium_high": "medium", "medium_low": "medium",
            "low": "medium", "refused": "refused"}.get(label, "medium")
