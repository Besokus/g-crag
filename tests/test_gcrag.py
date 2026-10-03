"""G-CRAG 纠错编排（设计文档 §6、§14、§16.4、§16.12）：三种模式 × 各分级分支 × 轮次上限 + 降级。

分数约定（阈值 high=0.7 / low=0.3）：0.9 correct、0.5 ambiguous、0.1 incorrect。
打分器按文本打分：正文里写着分数（如 "证据@0.9"），便于构造合并重打分后的排序。
"""
import asyncio

import pytest

from app.rag import gcrag, grading
from app.rag.entity_linking import LinkedEntity, LinkingResult
from app.rag.graph_evidence import EvidenceEdge, EvidencePath, GraphEvidenceResult
from app.rag.scorer import ScoreResult
from app.services.retrieval_service import ScoringStatus, SearchResult

TH = grading.Thresholds(high=0.7, low=0.3)


def _item(cid, score, origin="retrieval", paths=None):
    it = {"chunkId": cid, "docId": f"d_{cid}", "chunk": f"{cid}@{score}", "score": score, "origin": origin}
    if paths is not None:
        it["pathIds"] = paths
    return it


def _search(*scores, ok=True, reason="ok"):
    items = [_item(f"r{i}", s) for i, s in enumerate(scores)]
    return SearchResult(items, ScoringStatus("stub", ok, reason, len(items)))


class _TextScorer:
    name, cache_id = "stub", "stub@1"

    def __init__(self):
        self.ok, self.calls = True, []

    async def score(self, query, texts, *, timeout):
        self.calls.append(list(texts))
        if not self.ok:
            return ScoreResult(ok=False, reason="timeout")
        return ScoreResult(ok=True, scores=[float(t.rsplit("@", 1)[1]) for t in texts])


@pytest.fixture
def env(monkeypatch):
    box = {"entities": ["碧水源"], "graph": None, "rewrite": "改写后的问题", "retry": None,
           "calls": {"link": 0, "graph": 0, "rewrite": 0}}
    scorer = _TextScorer()
    box["scorer"] = scorer
    monkeypatch.setattr(grading, "get_thresholds", lambda: TH)
    monkeypatch.setattr("app.rag.scorer.get_scorer", lambda: scorer)

    async def link(q):
        box["calls"]["link"] += 1
        ents = [LinkedEntity(n, n, 1.0) for n in box["entities"]]
        return LinkingResult("dictionary" if ents else "none", ents)

    async def find(db, entities, *, cfg, exclude_chunk_ids):
        box["calls"]["graph"] += 1
        box["exclude"] = exclude_chunk_ids
        return box["graph"] or GraphEvidenceResult("no_path")

    async def rewrite(q, mt=None, force=False):
        box["calls"]["rewrite"] += 1
        return box["rewrite"]

    async def search_ex(db, q, topk, **kw):
        assert kw.get("force_score") is True
        return box["retry"] or _search(0.1)

    monkeypatch.setattr("app.rag.entity_linking.link", link)
    monkeypatch.setattr("app.rag.graph_evidence.find", find)
    monkeypatch.setattr("app.services.query_rewrite.rewrite_query", rewrite)
    monkeypatch.setattr("app.services.retrieval_service.mixed_search_ex", search_ex)
    return box


def _graph(*cand_scores):
    path = EvidencePath(0, ["碧水源", "疏勒河项目"], [EvidenceEdge("碧水源", "疏勒河项目", "参与", ["g0"])], 1)
    other = EvidencePath(1, ["碧水源", "久安"], [EvidenceEdge("碧水源", "久安", "合作", ["g9"])], 1)
    cands = [_item(f"g{i}", s, origin="graph", paths=[0]) for i, s in enumerate(cand_scores)]
    return GraphEvidenceResult("done", [path, other], cands)


def _run(search, mode, topk=3):
    return asyncio.run(gcrag.correct(None, "问题", search, mode=mode, topk=topk))


# ---- 初始分级：三种模式共同行为 ----

@pytest.mark.parametrize("mode", [m for m in gcrag.MODES if m != "gcrag_path_all"])   # _all 见文末
def test_correct_generates_without_correction(env, mode):
    r = _run(_search(0.9, 0.5), mode)
    assert (r.initial_grade, r.final_grade, r.action, r.confidence) == ("correct", "correct", "normal", "high")
    assert r.rounds == [] and env["calls"] == {"link": 0, "graph": 0, "rewrite": 0}


@pytest.mark.parametrize("mode", gcrag.MODES)
def test_unscored_never_corrects_or_refuses(env, mode):
    r = _run(_search(0.02, ok=False, reason="timeout"), mode)
    assert (r.final_grade, r.action, r.confidence, r.scoring_ok) == ("unscored", "unscored", "medium", False)
    assert r.rounds == [] and env["calls"]["rewrite"] == 0


@pytest.mark.parametrize("start", [0.5, 0.1])
def test_none_mode_generates_anyway(env, start):
    r = _run(_search(start), "none")
    assert r.action == "normal" and r.rounds == [] and r.confidence == "medium"


# ---- legacy ----

def test_legacy_ambiguous_kept(env):
    r = _run(_search(0.5), "legacy")
    assert (r.action, r.confidence, r.rounds) == ("ambiguous_kept", "medium", [])


def test_legacy_incorrect_rewrite_recovers(env):
    env["retry"] = _search(0.9)
    r = _run(_search(0.1), "legacy")
    assert (r.final_grade, r.action) == ("correct", "rewrite_recovered")
    assert [x.action for x in r.rounds] == ["rewrite"] and env["calls"]["graph"] == 0
    assert r.confidence == "medium"                     # 经过纠错不给 high


def test_legacy_incorrect_refused(env):
    r = _run(_search(0.1), "legacy")
    assert (r.final_grade, r.action, r.confidence) == ("incorrect", "refused", "refused")
    assert r.refused_reason == "correction_exhausted"


# ---- gcrag ----

def test_gcrag_ambiguous_graph_recovers(env):
    env["graph"] = _graph(0.95)
    r = _run(_search(0.5, 0.4), "gcrag")
    assert (r.final_grade, r.action) == ("correct", "graph_recovered")
    assert r.contexts[0]["chunkId"] == "g0" and r.rounds[0].admitted == 1
    assert [p.id for p in r.evidence_paths] == [0]      # 只保留被采纳条目引用的路径
    assert env["exclude"] == {"r0", "r1"} and env["calls"]["rewrite"] == 0
    assert len(env["scorer"].calls[0]) == 3              # 原上下文 ∪ 图谱候选，一次统一重打分


def test_gcrag_v2_appends_graph_keeps_retrieval(env):
    # 同样的分级与采纳判定；差别只在上下文：原检索条目原样保留，图谱条目追加在后
    env["graph"] = _graph(0.95, 0.45)
    v1 = _run(_search(0.5, 0.4, 0.35), "gcrag")
    v2 = _run(_search(0.5, 0.4, 0.35), "gcrag_v2")
    assert (v1.final_grade, v1.action) == (v2.final_grade, v2.action) == ("correct", "graph_recovered")
    assert [c["chunkId"] for c in v1.contexts] == ["g0", "r0", "g1"]          # r1、r2 被挤掉
    assert [c["chunkId"] for c in v2.contexts] == ["r0", "r1", "r2", "g0", "g1"]
    assert [c["score"] for c in v2.contexts[:3]] == [0.5, 0.4, 0.35]          # 原条目分数不变
    assert v2.rounds[0].admitted == v1.rounds[0].admitted == 2
    assert [p.id for p in v2.evidence_paths] == [0]


def test_gcrag_v2_no_improvement_keeps_original(env):
    env["graph"] = _graph(0.2)
    search = _search(0.5)
    search.items[0]["chunk"] = "r0@0.2"                  # 重打分后变差 → 不采纳
    r = _run(search, "gcrag_v2")
    assert r.rounds[0].status == "no_improvement" and r.action == "ambiguous_kept"
    assert [(c["chunkId"], c["score"]) for c in r.contexts] == [("r0", 0.5)]


def test_gcrag_incorrect_graph_partial(env):
    env["graph"] = _graph(0.5)
    r = _run(_search(0.1), "gcrag")
    assert (r.final_grade, r.action) == ("ambiguous", "graph_partial") and env["calls"]["rewrite"] == 0


def test_gcrag_incorrect_graph_fails_then_rewrite_recovers(env):
    env["graph"] = _graph(0.2)
    env["retry"] = _search(0.8)
    r = _run(_search(0.1), "gcrag")
    assert [(x.round, x.action, x.grade_after) for x in r.rounds] == [(1, "graph_evidence", "incorrect"),
                                                                      (2, "rewrite", "correct")]
    assert r.action == "rewrite_recovered" and r.evidence_paths == []


def test_gcrag_two_rounds_max_then_refused(env):
    env["graph"] = _graph(0.2)
    r = _run(_search(0.1), "gcrag")
    assert len(r.rounds) == 2 and (r.action, r.confidence) == ("refused", "refused")
    assert env["calls"] == {"link": 1, "graph": 1, "rewrite": 1}


def test_gcrag_ambiguous_graph_no_gain_kept(env):
    env["graph"] = _graph(0.45)
    r = _run(_search(0.5), "gcrag")
    assert (r.final_grade, r.action) == ("ambiguous", "ambiguous_kept") and env["calls"]["rewrite"] == 0


@pytest.mark.parametrize("entities,graph,status", [
    ([], None, "skipped_no_entity"),
    (["碧水源"], None, "skipped_no_path"),
    (["碧水源"], GraphEvidenceResult("unavailable"), "unavailable"),
])
def test_gcrag_graph_skipped_goes_to_rewrite(env, entities, graph, status):
    env["entities"], env["graph"] = entities, graph
    env["retry"] = _search(0.9)
    r = _run(_search(0.1), "gcrag")
    assert r.rounds[0].status == status and r.action == "rewrite_recovered"


def test_gcrag_rescore_failure_discards_graph(env):
    env["graph"] = _graph(0.95)
    env["scorer"].ok = False
    r = _run(_search(0.5), "gcrag")
    assert r.rounds[0].status == "failed" and r.action == "ambiguous_kept"
    assert all(c["origin"] == "retrieval" for c in r.contexts)


def test_gcrag_grade_never_worse_after_merge(env):
    # 重打分的查询与初次检索内部改写后的查询不同 → 原条目分数下降；不采纳，避免推向拒答
    env["graph"] = _graph(0.2)
    search = _search(0.5)
    search.items[0]["chunk"] = "r0@0.2"
    r = _run(search, "gcrag")
    assert r.rounds[0].status == "no_improvement" and r.action == "ambiguous_kept"
    assert r.contexts[0]["score"] == 0.5


def test_gcrag_empty_retrieval_tries_graph(env):
    env["graph"] = _graph(0.9)
    r = _run(_search(), "gcrag")
    assert (r.initial_grade, r.action) == ("incorrect", "graph_recovered")


def test_rewrite_scoring_failure_is_unscored_not_refused(env):
    env["retry"] = _search(0.02, ok=False, reason="no_key")
    r = _run(_search(0.1), "legacy")
    assert (r.final_grade, r.action, r.confidence) == ("unscored", "unscored", "medium")
    assert (r.scoring_ok, r.scoring_reason) == (False, "no_key")


def test_unknown_mode_rejected(env):
    with pytest.raises(ValueError):
        _run(_search(0.9), "magic")


def test_response_fields_camel_case(env):
    env["graph"] = _graph(0.95)
    out = _run(_search(0.5), "gcrag").to_response()
    assert out["correctionMode"] == "gcrag" and out["initialGrade"] == "ambiguous"
    assert out["cragGrade"] == "correct" and out["cragAction"] == "graph_recovered"
    rnd = out["correctionRounds"][0]
    assert {"round", "action", "status", "gradeBefore", "gradeAfter", "top1Before", "top1After",
            "admitted", "detail"} == set(rnd)
    assert out["evidencePaths"][0]["text"] == "碧水源 -[参与]-> 疏勒河项目"
    assert out["linking"]["strategy"] == "dictionary" and out["refusedReason"] == ""


def test_trace_spans_recorded(env, monkeypatch):
    spans = []

    class TC:
        def record(self, name, dur, group="main", **attrs):
            spans.append((name, group))
    monkeypatch.setattr("app.core.qa_trace.get_collector", lambda: TC())
    env["graph"] = _graph(0.2)
    _run(_search(0.1), "gcrag")
    names = [n for n, _ in spans]
    assert names[:4] == ["gcrag_grade", "gcrag_link", "gcrag_graph", "gcrag_rescore"]
    assert {"gcrag_rewrite", "gcrag_refuse"} <= set(names) and all(g == "crag" for _, g in spans)


# ---- gcrag_path / gcrag_path_all：路径感知打分、只追加、不要求分级提升 ----

def test_path_query_dedupes_paths_in_candidate_order():
    g = _graph(0.9, 0.5)
    g.candidates[1]["pathIds"] = [1, 0]
    q = gcrag.path_query("问题", g.paths, g.candidates)
    assert q == "问题\n相关的知识关系：碧水源 -[参与]-> 疏勒河项目；碧水源 -[合作]-> 久安"
    assert gcrag.path_query("问题", g.paths, g.candidates, limit=1).count("；") == 0
    assert gcrag.path_query("问题", [], []) == "问题"


def test_gcrag_path_ambiguous_appends_graph_without_grade_gain(env):
    env["graph"] = _graph(0.9, 0.5, 0.2)
    r = _run(_search(0.5, 0.4), "gcrag_path")
    assert (r.final_grade, r.action, r.confidence) == ("ambiguous", "graph_supplemented", "medium")
    assert [c["chunkId"] for c in r.contexts] == ["r0", "r1", "g0", "g1"]      # 原条目不动，≥ low 的前 2 条追加
    assert len(env["scorer"].calls[0]) == 3                                   # 只给图谱候选打分
    assert r.rounds[0].admitted == 2 and "相关的知识关系" in r.rounds[0].detail["pathQuery"]


def test_gcrag_path_incorrect_admitted_becomes_ambiguous(env):
    env["graph"] = _graph(0.35)
    r = _run(_search(0.1), "gcrag_path")
    assert (r.final_grade, r.action) == ("ambiguous", "graph_partial") and env["calls"]["rewrite"] == 0


def test_gcrag_path_low_scores_fall_back_to_rewrite(env):
    env["graph"] = _graph(0.2, 0.1)
    env["retry"] = _search(0.9)
    r = _run(_search(0.1), "gcrag_path")
    assert [(x.action, x.status) for x in r.rounds] == [("graph_evidence", "no_improvement"), ("rewrite", "done")]
    assert r.action == "rewrite_recovered"


def test_gcrag_path_all_supplements_correct_keeps_high(env):
    env["graph"] = _graph(0.6)
    r = _run(_search(0.9), "gcrag_path_all")
    assert (r.final_grade, r.action, r.confidence) == ("correct", "graph_supplemented", "high")
    assert r.rounds[0].action == "graph_supplement" and r.contexts[-1]["origin"] == "graph"
    r = _run(_search(0.9), "gcrag_path")
    assert r.rounds == [] and r.action == "normal"                            # 只有 _all 会在 correct 时走图谱


def test_gcrag_path_all_no_entity_is_normal(env):
    env["entities"] = []
    r = _run(_search(0.9), "gcrag_path_all")
    assert (r.action, r.confidence) == ("normal", "high") and r.rounds[0].status == "skipped_no_entity"
