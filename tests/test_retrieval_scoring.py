"""检索如实返回打分状态（设计文档 §5.2、§16.1）：成功 / 超时 / 无 key / 跳过 四种情况。"""
import asyncio
from types import SimpleNamespace

import pytest

from app.rag import scorer as sc
from app.rag.scorer import ScoreResult
from app.services import retrieval_service as rs


class _StubScorer:
    name = "stub"
    cache_id = "stub@1"

    def __init__(self, result=None, delay=0.0):
        self.result, self.delay, self.calls = result, delay, []

    async def score(self, query, texts, *, timeout):
        self.calls.append(list(texts))
        if self.delay:
            try:
                await asyncio.wait_for(asyncio.sleep(self.delay), timeout=timeout)
            except asyncio.TimeoutError:
                return ScoreResult(ok=False, reason="timeout")
        return self.result(texts) if callable(self.result) else self.result


def _pool(n):
    return [{"key": ("d", i), "text": "x" * (i + 1), "score": 1.0 / (i + 1)} for i in range(n)]


@pytest.fixture
def use_scorer(monkeypatch):
    def install(s):
        monkeypatch.setattr(sc, "get_scorer", lambda: s)
        return s
    return install


def _run(pool, **kw):
    kw = {"enabled": True, "skip_by_route": False, "force": False, **kw}
    return asyncio.run(rs._score_pool("q", pool, **kw))


def test_success_reorders_by_scorer(use_scorer):
    use_scorer(_StubScorer(lambda t: ScoreResult(ok=True, scores=[len(x) / 10 for x in t])))
    pool, st = _run(_pool(3))
    assert (st.ok, st.reason, st.scored) == (True, "ok", 3)
    assert [h["key"][1] for h in pool] == [2, 1, 0] and pool[0]["score"] == 0.3


def test_timeout_keeps_original_order_and_reports(use_scorer):
    use_scorer(_StubScorer(ScoreResult(ok=True, scores=[0.9, 0.9]), delay=1))
    rs.settings.RERANK_TIMEOUT, old = 0.01, rs.settings.RERANK_TIMEOUT
    try:
        pool, st = _run(_pool(2))
    finally:
        rs.settings.RERANK_TIMEOUT = old
    assert (st.ok, st.reason) == (False, "timeout")
    assert [h["score"] for h in pool] == [1.0, 0.5]   # 原分数不变，但调用方不能拿它分级


def test_no_key_reported(use_scorer):
    use_scorer(_StubScorer(ScoreResult(ok=False, reason="no_key")))
    _, st = _run(_pool(2))
    assert (st.ok, st.reason) == (False, "no_key")


def test_route_skip_reported_unless_forced(use_scorer):
    s = use_scorer(_StubScorer(lambda t: ScoreResult(ok=True, scores=[0.1] * len(t))))
    _, st = _run(_pool(2), skip_by_route=True)
    assert (st.ok, st.reason) == (False, "skipped_by_route") and s.calls == []
    _, st = _run(_pool(2), skip_by_route=True, force=True)
    assert st.ok and len(s.calls) == 1               # 纠错模式不允许跳过


def test_single_candidate_scored_only_when_forced(use_scorer):
    s = use_scorer(_StubScorer(lambda t: ScoreResult(ok=True, scores=[0.2])))
    _, st = _run(_pool(1))
    assert (st.ok, st.reason) == (False, "not_forced") and s.calls == []
    pool, st = _run(_pool(1), force=True)
    assert st.ok and pool[0]["score"] == 0.2          # 不再保留 BM25 归一分 1.0


def test_disabled_and_empty(use_scorer):
    use_scorer(_StubScorer(ScoreResult(ok=True, scores=[])))
    assert _run(_pool(2), enabled=False)[1].reason == "disabled"
    _, st = _run([])
    assert st.ok and st.scored == 0                   # 没有候选：由分级判 incorrect，而不是 unscored


# ---- 端到端：sparse 高置信路由（原缺陷：BM25 归一到 top1=1.0 且跳过 rerank → 恒判 correct）----

class _FakeResult:
    def all(self):
        return []

    def scalars(self):
        return self

    def __iter__(self):
        return iter([])


class _FakeDB:
    async def execute(self, *a, **k):
        return _FakeResult()


@pytest.fixture
def sparse_env(monkeypatch):
    chunks = [{"doc_id": "d1", "doc_name": "a.txt", "chunk_idx": 0, "text": "无关新闻甲"},
              {"doc_id": "d2", "doc_name": "b.txt", "chunk_idx": 0, "text": "无关新闻乙"}]

    async def rewrite(q, mt=None):
        return {"query": q}

    async def ensure_built(db):
        return None

    monkeypatch.setattr(rs.query_rewrite, "rewrite_query_v2", rewrite)
    monkeypatch.setattr(rs.bm25_service, "ensure_built", ensure_built)
    monkeypatch.setattr(rs.bm25_service, "search", lambda q, topk: [{"idx": 0, "score": 12.0},
                                                                     {"idx": 1, "score": 3.0}])
    monkeypatch.setattr(rs.bm25_service, "get_chunk", lambda i: chunks[i])
    monkeypatch.setattr(rs.settings, "MULTI_QUERY_ENABLE", False)
    monkeypatch.setattr(rs.settings, "MMR_ENABLE", False)
    monkeypatch.setattr(rs.settings, "RERANK_ENABLE", True)
    monkeypatch.setattr("app.routing.query_classifier.should_skip_rerank", lambda d: True)
    return SimpleNamespace(route="sparse", confidence=0.99, features=None, skip_rerank=True)


def _search(decision, force):
    return asyncio.run(rs.mixed_search_ex(_FakeDB(), "无关问题", 5, routing_decision=decision,
                                          force_score=force))


def test_sparse_high_confidence_not_graded_unless_forced(sparse_env, use_scorer):
    use_scorer(_StubScorer(lambda t: ScoreResult(ok=True, scores=[0.05] * len(t))))
    res = _search(sparse_env, force=False)
    assert res.items[0]["score"] == 1.0                    # BM25 归一分
    assert (res.scoring.ok, res.scoring.reason) == (False, "skipped_by_route")

    res = _search(sparse_env, force=True)
    assert res.scoring.ok and res.items[0]["score"] == 0.05   # 真实相关度很低 → 分级可判 incorrect


def test_mixed_search_still_returns_list(sparse_env, use_scorer):
    use_scorer(_StubScorer(ScoreResult(ok=False, reason="error")))
    items = asyncio.run(rs.mixed_search(_FakeDB(), "无关问题", 5, routing_decision=sparse_env))
    assert isinstance(items, list) and items
