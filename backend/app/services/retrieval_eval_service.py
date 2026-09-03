"""检索评测 service：服务化 eval_retrieval，直接调 mixed_search（不绕 HTTP），算 recall/MRR/nDCG。

只建议模式的评测基座：扫描引擎（retrieval_tune_service）用本服务跑 golden 集，
对比不同 overrides 的 recall/MRR/nDCG/无结果率，产出调参建议。
"""
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.obs import degraded
from app.services import quality_event_bus, retrieval_service

_GOLDEN = Path(__file__).resolve().parent.parent.parent / "data" / "golden_qa.json"

# golden 回归 recall 门禁（低于此值 emit eval_low，供 retrieval_tune 订阅调参）
_RECALL_GATE = 0.92


def _load_golden() -> list[dict]:
    """加载 golden 问答集（backend/data/golden_qa.json）。"""
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _doc_relevance_binary(expect: list[str], doc_name: str) -> int:
    """二元相关性：任一 expect 关键词是 docName 子串 → 1（对齐 scripts/eval_retrieval.py 口径）。

    golden 的 expect 是内容关键词（如"主变压器"），docName 是文档标题（如
    "主变压器运行规程.txt"）——精确相等永远 False，必须用子串匹配。
    """
    return 1 if any(kw and kw in doc_name for kw in expect) else 0


def _doc_relevance_graded(relevant_docs: dict, doc_name: str, fallback_expect: list[str]) -> int:
    """分级相关性：优先 relevant_docs{文档关键词→grade}（key 是 docName 子串），否则二元兜底。"""
    if relevant_docs:
        for key, grade in relevant_docs.items():
            if key and key in doc_name:
                return grade
        return 0
    return _doc_relevance_binary(fallback_expect, doc_name)


def _recall_at_k(expect: list[str], got: list[str], relevant_docs: dict | None = None) -> float:
    """query 级召回：top-k 中命中任一相关文档 → 1，否则 0（与 eval_retrieval 报告口径一致）。"""
    if not expect and not relevant_docs:
        return 0.0
    rd = relevant_docs or {}
    return 1.0 if any(_doc_relevance_graded(rd, d, expect) > 0 for d in got) else 0.0


def _mrr(expect: list[str], got: list[str], relevant_docs: dict | None = None) -> float:
    """MRR：第一个相关文档（分级>0）的倒数排名。"""
    rd = relevant_docs or {}
    for i, d in enumerate(got, 1):
        if _doc_relevance_graded(rd, d, expect) > 0:
            return 1.0 / i
    return 0.0


def _ndcg(relevant_docs: dict, got: list[str], expect: list[str] | None = None,
          k: int | None = None) -> float:
    """分级 nDCG：relevant_docs key 按 docName 子串匹配取等级；无 relevant_docs 退化二元。

    口径对齐 scripts/eval_retrieval.py::ndcg_at_k（线性折损、ideal 取等级降序）。
    """
    import math

    rd = relevant_docs or {}
    exp = expect or []
    if k is None:
        k = len(got)
    rels = [_doc_relevance_graded(rd, d, exp) for d in got[:k]]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels) if r)
    if rd:
        ideal = sorted(rd.values(), reverse=True)[:k]
    else:
        max_rel = min(len(exp), k)
        ideal = [1] * max_rel + [0] * (k - max_rel)
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal) if r)
    return dcg / idcg if idcg > 0 else 0.0


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


async def evaluate_over_golden(db: AsyncSession, overrides: dict | None = None, topk: int = 5) -> dict:
    """跑 golden 集，返回 {recall, mrr, ndcg, noResultRate, sampleSize, validSample, perQuery}。

    overrides: 调参扫描时传 {RRF_K:40,...}；None=走 settings（baseline）。
    """
    golden = _load_golden()
    recalls, mrrs, ndcgs, n_empty = [], [], [], 0
    per_query = []
    for item in golden:
        ctx = await retrieval_service.mixed_search(db, item["query"], topk, overrides=overrides)
        got = [c["docName"] for c in ctx] if ctx else []
        if not ctx:
            n_empty += 1
            per_query.append({"query": item["query"], "recall": 0.0, "mrr": 0.0, "empty": True})
            continue
        rd = item.get("relevant_docs") or {}
        r = _recall_at_k(item.get("expect", []), got, rd)
        m = _mrr(item.get("expect", []), got, rd)
        recalls.append(r)
        mrrs.append(m)
        # 无分级标注时 nDCG 退化为二元（对齐 eval_retrieval 口径，全集均值）
        n = _ndcg(rd, got, item.get("expect", []), topk)
        ndcgs.append(n)
        per_query.append({"query": item["query"], "recall": r, "mrr": m, "ndcg": n})
    result = {
        "recall": _mean(recalls), "mrr": _mean(mrrs), "ndcg": _mean(ndcgs),
        "noResultRate": round(n_empty / len(golden), 4) if golden else 0.0,
        "sampleSize": len(golden), "validSample": len(recalls), "perQuery": per_query,
    }
    # B3：baseline run（无 overrides）+ recall 低 → emit retrieval_eval.eval_low
    if overrides is None:
        await _maybe_emit_eval_low(result)
    return result


async def _maybe_emit_eval_low(result: dict) -> None:
    """B3 数据飞轮：baseline recall < _RECALL_GATE → emit retrieval_eval.eval_low。

    只在 baseline（overrides=None）emit，扫描时的 overrides 不 emit（避免每次扫都刷屏）。
    EVAL_EMIT_ENABLE 默认关；QUALITY_BUS_ENABLE 决定是否派发订阅者。
    """
    if not getattr(settings, "EVAL_EMIT_ENABLE", False):
        return
    if result.get("recall", 1.0) >= _RECALL_GATE:
        return
    try:
        await quality_event_bus.emit(
            "retrieval_eval", "eval_low",
            {"recall": result.get("recall"), "gate": _RECALL_GATE,
             "validSample": result.get("validSample")},
            tenant="default",
        )
    except Exception as e:
        degraded("retrieval_eval_emit", e)
