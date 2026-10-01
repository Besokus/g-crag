"""G-CRAG 纠错编排（设计文档 §6、§16.4）：检索分级 → 图谱定位证据 → LLM 改写 → 拒答。

三种模式共用同一套检索、分级阈值与生成，唯一差别是纠错：

| 模式   | ambiguous      | incorrect                                   |
|--------|----------------|---------------------------------------------|
| none   | 生成           | 生成                                        |
| legacy | 生成（标不确定）| LLM 改写重检索 → 仍 incorrect 拒答           |
| gcrag  | 图谱定位证据    | 图谱定位证据 → LLM 改写 → 仍 incorrect 拒答  |
| gcrag_v2 | 同 gcrag，仅图谱证据的采纳方式不同（见下）                    |

- 打分失败（unscored）时任何模式都不纠错、不拒答，置信度 medium（M3）。
- 图谱定位证据：原上下文 ∪ 图谱候选 → 去重 → 打分器统一重打分一次 → 取 topk → 再分级；
  重打分失败时丢弃图谱候选、本轮记 failed，保持原分级进入下一步。
- gcrag_v2（2026-10-01，test2 事后迭代）：分级与 gcrag 完全相同；采纳时原检索条目原样保留（顺序、分数不变），
  只把进入重打分 topk 的图谱条目追加在后面。gcrag 的"取 topk"会让图谱条目挤掉有用的检索条目（test2 t30）。
- 最多两轮纠错。每一步写 span（随 spans_json 落库，始终记录）。
"""
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

from app.config import settings
from app.core.obs import degraded
from app.rag import grading

CorrectionMode = Literal["none", "legacy", "gcrag", "gcrag_v2"]
MODES = ("none", "legacy", "gcrag", "gcrag_v2")
GRAPH_MODES = ("gcrag", "gcrag_v2")
_RANK = {"incorrect": 0, "ambiguous": 1, "correct": 2}


@dataclass
class RoundRecord:
    round: int
    action: str                     # graph_evidence | rewrite
    status: str                     # done | skipped_no_entity | skipped_no_path | unavailable | failed | no_improvement
    grade_before: str
    grade_after: str
    top1_before: float | None
    top1_after: float | None
    admitted: int = 0               # 本轮带入最终上下文的条目数
    detail: dict = field(default_factory=dict)


@dataclass
class CorrectionResult:
    mode: str
    contexts: list[dict]
    initial_grade: str
    final_grade: str
    action: str
    rounds: list[RoundRecord] = field(default_factory=list)
    evidence_paths: list = field(default_factory=list)     # graph_evidence.EvidencePath
    linking: object = None                                  # entity_linking.LinkingResult | None
    scoring_ok: bool = True
    scoring_reason: str = "ok"
    refused_reason: str = ""

    @property
    def confidence(self) -> str:
        if self.action == "refused":
            return "refused"
        if self.final_grade == "correct" and not self.rounds:
            return "high"
        return "medium"

    def to_response(self) -> dict:
        """对外字段（设计文档 §16.8，camelCase）。"""
        def camel(d: dict) -> dict:
            return {_camel(k): v for k, v in d.items()}
        return {
            "correctionMode": self.mode,
            "scoringOk": self.scoring_ok,
            "scoringReason": self.scoring_reason,
            "initialGrade": self.initial_grade,
            "cragGrade": self.final_grade,
            "cragAction": self.action,
            "correctionRounds": [camel(asdict(r)) for r in self.rounds],
            "evidencePaths": [{"id": p.id, "nodes": p.nodes, "hops": p.hops, "text": p.render(),
                               "edges": [{"source": e.source, "target": e.target, "type": e.type,
                                          "chunkIds": e.chunk_ids} for e in p.edges]}
                              for p in self.evidence_paths],
            "linking": None if self.linking is None else {
                "strategy": self.linking.strategy, "degraded": self.linking.degraded,
                "entities": [asdict(e) for e in self.linking.entities]},
            "refusedReason": self.refused_reason,
        }


def _camel(k: str) -> str:
    head, *rest = k.split("_")
    return head + "".join(w.title() for w in rest)


def resolve_mode(mode: str | None) -> str:
    m = mode or settings.CORRECTION_MODE
    if m not in MODES:
        raise ValueError(f"未知纠错模式: {m}（支持: none | legacy | gcrag | gcrag_v2）")
    return m


def _top1(items: list[dict]) -> float | None:
    return round(float(items[0].get("score", 0.0) or 0.0), 4) if items else None


def _span(name: str, dur: float = 0.0, **attrs) -> None:
    from app.core.qa_trace import get_collector
    tc = get_collector()
    if tc:
        tc.record(name, dur, "crag", **attrs)


def _grade(items, scoring, th, rnd: int) -> str:
    g, reason = grading.grade(items, scoring, th)
    _span("gcrag_grade", round=rnd, grade=g, top1=_top1(items), scoringOk=scoring.ok, reason=reason)
    return g


def _dedupe_key(it: dict) -> tuple:
    return (it.get("docId"), it.get("chunk"))


async def _graph_round(db, query: str, contexts: list[dict], grade_before: str, rnd: int,
                       *, topk: int, th, result: CorrectionResult,
                       append: bool = False) -> tuple[list[dict], str, RoundRecord]:
    """一轮图谱定位证据。返回 (新上下文, 新分级, 本轮记录)；未改变时返回原上下文与原分级。

    append=True（gcrag_v2）：新上下文 = 原上下文 + 重打分 topk 中的图谱条目，原条目不被挤掉。
    """
    from app.core.domain_pack import get_pack
    from app.rag import entity_linking, graph_evidence
    from app.rag.scorer import get_scorer
    from app.services.retrieval_service import ScoringStatus

    rec = RoundRecord(rnd, "graph_evidence", "done", grade_before, grade_before,
                      _top1(contexts), _top1(contexts))
    t0 = time.time()
    link = result.linking or await entity_linking.link(query)
    result.linking = link
    _span("gcrag_link", time.time() - t0, strategy=link.strategy, degraded=link.degraded,
          entities=[{"name": e.name, "score": e.score} for e in link.entities])
    rec.detail = {"linking": link.strategy, "entities": [e.name for e in link.entities]}
    if not link.entities:
        rec.status = "skipped_no_entity"
        return contexts, grade_before, rec

    t0 = time.time()
    ev = await graph_evidence.find(db, [e.name for e in link.entities], cfg=get_pack().graph_evidence,
                                   exclude_chunk_ids={c.get("chunkId") for c in contexts if c.get("chunkId")})
    _span("gcrag_graph", time.time() - t0, status=ev.status, pathCount=len(ev.paths),
          candidateCount=len(ev.candidates), truncated=ev.truncated,
          paths=[{"nodes": p.nodes, "edgeTypes": [e.type for e in p.edges]} for p in ev.paths[:10]])
    rec.detail.update(pathCount=len(ev.paths), candidateCount=len(ev.candidates), truncated=ev.truncated)
    if ev.status == "unavailable":
        rec.status = "unavailable"
        return contexts, grade_before, rec
    if ev.status == "no_path" or not ev.candidates:
        rec.status = "skipped_no_path"
        return contexts, grade_before, rec

    merged, seen = [], set()
    for it in [*contexts, *ev.candidates]:
        k = _dedupe_key(it)
        if k not in seen:
            seen.add(k)
            merged.append(it)
    t0 = time.time()
    res = await get_scorer().score(query, [m.get("chunk", "") for m in merged],
                                   timeout=getattr(settings, "RERANK_TIMEOUT", 2.0))
    _span("gcrag_rescore", time.time() - t0, scored=len(merged), ok=res.ok, reason=res.reason)
    if not res.ok:
        degraded("gcrag_rescore", RuntimeError(res.reason), "合并后重打分失败 → 丢弃图谱候选，保持原分级")
        rec.status = "failed"
        rec.detail["rescoreReason"] = res.reason
        return contexts, grade_before, rec
    rescored = sorted(({**m, "score": s} for m, s in zip(merged, res.scores)), key=lambda m: -m["score"])[:topk]
    grade_after = _grade(rescored, ScoringStatus(get_scorer().name, True, "ok", len(merged)), th, rnd)
    rec.grade_after, rec.top1_after = grade_after, _top1(rescored)
    if _RANK[grade_after] < _RANK[grade_before]:
        # 合并只会增加候选；变差只可能来自重打分的查询与初次检索内部改写后的查询不同。
        # 此时不采纳本轮结果，避免把原本可作答的问题推向拒答。
        rec.status = "no_improvement"
        return contexts, grade_before, rec
    admitted = [m for m in rescored if m.get("origin") == "graph"]
    rec.admitted = len(admitted)
    used = {pid for m in admitted for pid in (m.get("pathIds") or [])}
    result.evidence_paths = [p for p in ev.paths if p.id in used]
    return ([*contexts, *admitted] if append else rescored), grade_after, rec


async def _rewrite_round(db, query: str, contexts: list[dict], grade_before: str, rnd: int,
                         *, topk: int, tenant: str, model_type: str | None, th,
                         result: CorrectionResult) -> tuple[list[dict], str, RoundRecord]:
    """一轮 LLM 改写重检索（原有纠错）。改写后的检索打分失败 → 分级 unscored（不据此拒答）。"""
    from app.services import retrieval_service
    from app.services.query_rewrite import rewrite_query

    rec = RoundRecord(rnd, "rewrite", "done", grade_before, grade_before, _top1(contexts), _top1(contexts))
    t0 = time.time()
    try:
        new_q = await rewrite_query(query, model_type, force=True)
        _span("gcrag_rewrite", time.time() - t0, newQuery=(new_q or "")[:200])
        rec.detail = {"newQuery": new_q or ""}
        if not new_q or new_q == query:
            rec.status = "failed"
            return contexts, grade_before, rec
        sr = await retrieval_service.mixed_search_ex(db, new_q, topk, tenant=tenant, force_score=True)
    except Exception as e:
        degraded("gcrag_rewrite", e, "LLM 改写重检索失败 → 视为本轮未改善")
        rec.status = "failed"
        return contexts, grade_before, rec
    if not sr.scoring.ok:
        rec.status, rec.grade_after = "failed", grading.GRADE_UNSCORED
        result.scoring_ok, result.scoring_reason = False, sr.scoring.reason
        return contexts, grading.GRADE_UNSCORED, rec
    if not sr.items:
        rec.status = "failed"
        return contexts, grade_before, rec
    grade_after = _grade(sr.items, sr.scoring, th, rnd)
    rec.grade_after, rec.top1_after, rec.admitted = grade_after, _top1(sr.items), len(sr.items)
    if grade_after == "incorrect":
        return contexts, grade_after, rec          # 未改善：保留原上下文
    return sr.items, grade_after, rec


def _outcome(action_prefix: str, grade: str) -> str:
    return {"correct": f"{action_prefix}_recovered", "ambiguous": f"{action_prefix}_partial"}[grade]


async def correct(db, query: str, search, *, mode: str, topk: int, tenant: str = "default",
                  model_type: str | None = None) -> CorrectionResult:
    """search 为 retrieval_service.SearchResult（初次检索，调用方应传 force_score=True）。"""
    mode = resolve_mode(mode)
    th = grading.get_thresholds()
    items, scoring = search.items, search.scoring
    g0 = _grade(items, scoring, th, 0)
    res = CorrectionResult(mode=mode, contexts=items, initial_grade=g0, final_grade=g0, action="normal",
                           scoring_ok=scoring.ok, scoring_reason=scoring.reason)
    if g0 == grading.GRADE_UNSCORED:
        res.action = "unscored"
        return res
    if mode == "none" or g0 == "correct":
        return res
    if mode == "legacy" and g0 == "ambiguous":
        res.action = "ambiguous_kept"
        return res

    ctx, grade, rnd = items, g0, 0
    if mode in GRAPH_MODES:
        rnd += 1
        ctx, grade, rec = await _graph_round(db, query, ctx, grade, rnd, topk=topk, th=th, result=res,
                                             append=mode == "gcrag_v2")
        res.rounds.append(rec)
        if grade in ("correct", "ambiguous"):
            res.contexts, res.final_grade = ctx, grade
            improved = grade != g0 and rec.status == "done"
            res.action = _outcome("graph", grade) if improved else "ambiguous_kept"
            return res

    # incorrect（legacy 的第 1 轮 / gcrag 图谱后仍 incorrect 的第 2 轮）→ LLM 改写重检索
    rnd += 1
    ctx, grade, rec = await _rewrite_round(db, query, ctx, grade, rnd, topk=topk, tenant=tenant,
                                           model_type=model_type, th=th, result=res)
    res.rounds.append(rec)
    res.contexts, res.final_grade = ctx, grade
    if grade == grading.GRADE_UNSCORED:
        res.action = "unscored"
    elif grade in ("correct", "ambiguous"):
        res.action = _outcome("rewrite", grade)
        res.evidence_paths = []                    # 改写重检索的上下文不含图谱条目
    else:
        res.action = "refused"
        res.refused_reason = "no_items" if not items and not ctx else "correction_exhausted"
        _span("gcrag_refuse", reason=res.refused_reason, rounds=len(res.rounds))
    return res
