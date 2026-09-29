"""打分器接口与录制回放（设计文档 §16.2）。rerank HTTP 用桩替换。"""
import asyncio
import json

import pytest

from app.rag import scorer as sc
from app.services import rerank_service


class _FakeReranker:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    async def rerank(self, query, documents, top_n=5):
        self.calls.append(list(documents))
        return await self.fn(query, documents, top_n)


@pytest.fixture
def fake_rerank(monkeypatch):
    monkeypatch.setattr(sc.settings, "DASHSCOPE_API_KEY", "k")

    def install(fn):
        r = _FakeReranker(fn)
        monkeypatch.setattr(rerank_service, "get_reranker", lambda: r)
        return r
    return install


async def _by_length(query, docs, top_n):
    """按长度打分，按分数降序返回 (原索引, 分数)——与真实 API 一样打乱了输入顺序。"""
    ranked = sorted(((i, len(d) / 10) for i, d in enumerate(docs)), key=lambda x: -x[1])
    return ranked[:top_n]


def _score(s, texts, timeout=2.0):
    return asyncio.run(s.score("问题", texts, timeout=timeout))


def test_ok_scores_restored_to_input_order(fake_rerank):
    r = fake_rerank(_by_length)
    res = _score(sc.DashscopeRerankScorer(), ["a", "aaa", "aa"])
    assert res.ok and res.reason == "ok"
    assert res.scores == [0.1, 0.3, 0.2]
    assert r.calls == [["a", "aaa", "aa"]]   # top_n = 全部文本，一次调用


def test_no_key_is_failure_not_zero_scores(monkeypatch):
    monkeypatch.setattr(sc.settings, "DASHSCOPE_API_KEY", "")
    res = _score(sc.DashscopeRerankScorer(), ["a", "b"])
    assert (res.ok, res.reason, res.scores) == (False, "no_key", [])


def test_timeout(fake_rerank):
    async def slow(q, d, n):
        await asyncio.sleep(1)
    fake_rerank(slow)
    res = _score(sc.DashscopeRerankScorer(), ["a"], timeout=0.01)
    assert (res.ok, res.reason) == (False, "timeout")


def test_error_and_partial_response(fake_rerank):
    async def boom(q, d, n):
        raise RuntimeError("HTTP 500")
    fake_rerank(boom)
    assert _score(sc.DashscopeRerankScorer(), ["a"]).reason == "error"

    async def partial(q, d, n):
        return [(0, 0.9)]   # 少返回了一条
    fake_rerank(partial)
    res = _score(sc.DashscopeRerankScorer(), ["a", "b"])
    assert (res.ok, res.reason) == (False, "error")


def test_empty_texts_ok_without_call(fake_rerank):
    r = fake_rerank(_by_length)
    assert _score(sc.DashscopeRerankScorer(), []).ok and r.calls == []


def test_record_then_replay_without_calls(fake_rerank, tmp_path):
    r = fake_rerank(_by_length)
    path = tmp_path / "s.jsonl"
    rec = sc.CachedScorer(sc.DashscopeRerankScorer(), "record", path)
    assert _score(rec, ["a", "aa"]).scores == [0.1, 0.2]
    assert len(path.read_text().splitlines()) == 2

    async def must_not_call(q, d, n):
        raise AssertionError("replay 命中时不应调用 rerank")
    fake_rerank(must_not_call)
    rep = sc.CachedScorer(sc.DashscopeRerankScorer(), "replay", path)   # 新实例：从文件加载
    assert _score(rep, ["aa", "a"]).scores == [0.2, 0.1]
    assert (rep.hits, rep.misses) == (2, 0)
    assert r.calls == [["a", "aa"]]


def test_replay_miss_scores_only_missing_and_backfills(fake_rerank, tmp_path):
    path = tmp_path / "s.jsonl"
    fake_rerank(_by_length)
    _score(sc.CachedScorer(sc.DashscopeRerankScorer(), "record", path), ["a"])
    r = fake_rerank(_by_length)
    rep = sc.CachedScorer(sc.DashscopeRerankScorer(), "replay", path)
    assert _score(rep, ["a", "aaa"]).scores == [0.1, 0.3]
    assert r.calls == [["aaa"]]                            # 只补打未命中的
    assert len(path.read_text().splitlines()) == 2        # 并补录


def test_failure_not_cached(fake_rerank, tmp_path):
    path = tmp_path / "s.jsonl"

    async def boom(q, d, n):
        raise RuntimeError("x")
    fake_rerank(boom)
    res = _score(sc.CachedScorer(sc.DashscopeRerankScorer(), "record", path), ["a"])
    assert (res.ok, res.reason) == (False, "error") and not path.exists()


def test_key_includes_model_and_query(fake_rerank, tmp_path, monkeypatch):
    path = tmp_path / "s.jsonl"
    fake_rerank(_by_length)
    _score(sc.CachedScorer(sc.DashscopeRerankScorer(), "record", path), ["a"])
    monkeypatch.setattr(sc.settings, "RERANK_MODEL", "gte-rerank-v3")
    rep = sc.CachedScorer(sc.DashscopeRerankScorer(), "replay", path)
    _score(rep, ["a"])
    assert rep.misses == 1                                  # 换模型不回放旧分数
    keys = [json.loads(x)["k"] for x in path.read_text().splitlines()]
    assert len(set(keys)) == 2


def test_get_scorer_modes(monkeypatch, tmp_path):
    monkeypatch.setattr(sc, "_instances", {})
    monkeypatch.setattr(sc.settings, "SCORER_CACHE", "off")
    assert isinstance(sc.get_scorer(), sc.DashscopeRerankScorer)
    monkeypatch.setattr(sc.settings, "SCORER_CACHE", "replay")
    monkeypatch.setattr(sc.settings, "SCORER_CACHE_PATH", str(tmp_path / "x.jsonl"))
    s = sc.get_scorer()
    assert isinstance(s, sc.CachedScorer) and s is sc.get_scorer()
    monkeypatch.setattr(sc.settings, "SCORER_CACHE", "bogus")
    with pytest.raises(ValueError):
        sc.get_scorer()


def test_rerank_service_no_key_raises(monkeypatch):
    monkeypatch.setattr(rerank_service.settings, "DASHSCOPE_API_KEY", "")
    with pytest.raises(RuntimeError):
        asyncio.run(rerank_service.Reranker().rerank("q", ["a"]))
