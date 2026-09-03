"""debug_search trace schema 直接测试（Track C 缺口补齐：调试页结构变化容易回归）。

保护 /retrieval/debug 前端（RetrievalDebug.vue）依赖的 trace 结构：
config 快照字段、步骤序列、步骤字段、结果命中分数归因。
检索原语（dense/BM25/rerank/multi-query 改写）打桩，不依赖 Milvus/LLM/Redis。
"""
import pytest

from app.config import settings


def _dense_hit(doc_id: str, chunk_idx: int, doc_name: str, score: float) -> dict:
    return {"doc_id": doc_id, "chunk_idx": chunk_idx, "score": score,
            "text": f"{doc_name} 第{chunk_idx}块内容", "doc_name": doc_name}


def _chunk(doc_id: str, chunk_idx: int, doc_name: str) -> dict:
    return {"doc_id": doc_id, "chunk_idx": chunk_idx,
            "text": f"{doc_name} 第{chunk_idx}块内容", "doc_name": doc_name}


class _FakeReranker:
    async def rerank(self, query, docs, top_n=None):
        return [(i, 0.9 - i * 0.1) for i in range(min(top_n or len(docs), len(docs)))]


@pytest.fixture
def stub_primitives(monkeypatch):
    """打桩检索原语：cloud/bge 各 2 命中 + BM25 1 命中，改写恒等。"""
    from app.services import bm25_service, query_rewrite, rerank_service, retrieval_service

    async def _identity(query, model_type=None, force=False):
        return query

    monkeypatch.setattr(query_rewrite, "rewrite_query", _identity)

    async def _fake_dense(q, cand, ef):
        cloud = [_dense_hit("doc-a", 0, "主变压器运行规程.txt", 0.92),
                 _dense_hit("doc-a", 1, "主变压器运行规程.txt", 0.81),
                 _dense_hit("doc-b", 0, "SF6断路器维护手册.txt", 0.77)]
        bge = [_dense_hit("doc-a", 0, "主变压器运行规程.txt", 0.66)]
        return cloud, bge

    monkeypatch.setattr(retrieval_service, "_dense_dual", _fake_dense)

    async def _noop(db):
        return None

    monkeypatch.setattr(bm25_service, "ensure_built", _noop)
    monkeypatch.setattr(bm25_service, "search", lambda q, topk=20: [{"idx": 7, "score": 3.4}])
    monkeypatch.setattr(bm25_service, "get_chunk", lambda idx: _chunk("doc-b", 1, "SF6断路器维护手册.txt"))
    monkeypatch.setattr(rerank_service, "get_reranker", lambda: _FakeReranker())


@pytest.mark.asyncio
async def test_debug_search_trace_schema_with_rerank(stub_primitives, monkeypatch):
    """rerank 开：步骤序列完整 + 命中带五路分数归因。"""
    from app.services.retrieval_service import debug_search

    monkeypatch.setattr(settings, "RERANK_ENABLE", True)
    monkeypatch.setattr(settings, "MMR_ENABLE", False)
    trace = await debug_search(None, "主变压器温度异常怎么办", topk=3)

    # 顶层结构
    assert {"config", "steps", "result", "diversity"} <= set(trace)

    # config 快照字段（前端展示开关与运行时参数）
    cfg = trace["config"]
    for k in ("topK", "candidate", "queryRewrite", "hyde", "multiQuery", "rerank",
              "mmr", "smallToBig", "embProvider", "milvusCollections", "runtimeEf"):
        assert k in cfg, f"config 缺 {k}"

    # 步骤序列（顺序即流水线）
    names = [s["step"] for s in trace["steps"]]
    assert names == ["query_rewrite", "multi_query", "retrieve",
                     "rrf_fuse", "rerank", "metadata_filter", "mmr"]

    rw = trace["steps"][0]
    assert {"input", "output", "changed"} <= set(rw) and rw["changed"] is False

    mq = trace["steps"][1]
    assert mq["subQueries"] == [] and mq["totalQueries"] == 1

    ret = trace["steps"][2]
    assert ret["denseTotal"] == 4 and ret["bm25Total"] == 1
    assert ret["perQuery"] and {"query", "hyde", "denseHits", "bm25Hits"} <= set(ret["perQuery"][0])

    assert trace["steps"][3]["fusedCount"] >= 1
    assert trace["steps"][4]["ok"] is True and trace["steps"][4]["reranked"] >= 1
    assert trace["steps"][5]["skipped"] is True  # 未传 tenant/docType 等过滤条件
    assert trace["steps"][6]["applied"] is False

    # 结果命中：分数归因五路 + 来源标签 + 文本截断
    res = trace["result"]
    assert res["finalHits"] == len(res["hits"])
    assert isinstance(res["latencyMs"], (int, float))
    for h in res["hits"]:
        assert {"docId", "docName", "chunkIdx", "text", "sources", "scores"} <= set(h)
        assert set(h["scores"]) == {"dense", "bm25", "rrf", "rerank", "final"}
        assert h["scores"]["rerank"] is not None
        assert set(h["sources"]) <= {"dense_cloud", "dense_bge", "bm25"}
        assert len(h["text"]) <= 200
    # rerank 分数降序
    rr = [h["scores"]["rerank"] for h in res["hits"]]
    assert rr == sorted(rr, reverse=True)

    # 多样性指标
    assert {"doc_uniqueness", "source_entropy", "chunk_adjacency_ratio",
            "distinct_docs"} <= set(trace["diversity"])


@pytest.mark.asyncio
async def test_debug_search_trace_schema_rerank_disabled(stub_primitives, monkeypatch):
    """rerank 关：步骤标记 ok=False reason=disabled，rerank 分数缺省 None。"""
    from app.services.retrieval_service import debug_search

    monkeypatch.setattr(settings, "RERANK_ENABLE", False)
    monkeypatch.setattr(settings, "MMR_ENABLE", False)
    trace = await debug_search(None, "SF6压力低闭锁", topk=2)

    names = [s["step"] for s in trace["steps"]]
    assert "rerank" in names
    rk = next(s for s in trace["steps"] if s["step"] == "rerank")
    assert rk["ok"] is False and rk["reason"] == "disabled"
    for h in trace["result"]["hits"]:
        assert h["scores"]["rerank"] is None
        assert h["scores"]["rrf"] is not None


@pytest.mark.asyncio
async def test_debug_search_multi_query_branch(stub_primitives, monkeypatch):
    """multi-query 开：子问题进 perQuery，totalQueries 增长。"""
    from app.services import multi_query
    from app.services.retrieval_service import debug_search

    async def _decompose(query, model_type=None):
        return ["主变油温限值是多少", "主变冷却系统检查"]

    monkeypatch.setattr(multi_query, "decompose", _decompose)
    monkeypatch.setattr(settings, "MULTI_QUERY_ENABLE", True)
    monkeypatch.setattr(settings, "RERANK_ENABLE", False)
    monkeypatch.setattr(settings, "MMR_ENABLE", False)
    trace = await debug_search(None, "主变压器油温异常", topk=2)

    mq = next(s for s in trace["steps"] if s["step"] == "multi_query")
    assert len(mq["subQueries"]) == 2 and mq["totalQueries"] == 3
    ret = next(s for s in trace["steps"] if s["step"] == "retrieve")
    assert len(ret["perQuery"]) == 3
