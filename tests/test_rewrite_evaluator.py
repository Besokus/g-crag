"""RewriteEvaluator 单测：分数对比 + margin + 异常回退。mock _light_dense 避免真检索。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.services import rewrite_evaluator


def test_improved_when_new_higher():
    """new 分数和 > orig*(1+margin) → improved。"""
    async def go():
        def fake(q, mt):
            return [{"score": 0.2}] * 5 if q == "orig" else [{"score": 0.3}] * 5
        with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(side_effect=fake)):
            r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
        assert r["improved"] is True
        assert r["orig_score"] < r["new_score"]
    asyncio.run(go())


def test_reject_when_not_better():
    """分数接近（< margin）→ not improved。"""
    async def go():
        same = [{"score": 0.2}] * 5
        with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(return_value=same)):
            r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
        assert r["improved"] is False
    asyncio.run(go())


def test_exception_returns_not_improved():
    """检索异常 → 回退 not improved（不抛）。"""
    async def go():
        with patch.object(rewrite_evaluator, "_light_dense", AsyncMock(side_effect=RuntimeError("boom"))):
            r = await rewrite_evaluator.evaluate("orig", "rewritten", None)
        assert r["improved"] is False
    asyncio.run(go())


def test_cloud_embed_failure_falls_back_to_bge():
    """云端 embedding 异常 → 回退 bge embedding + bge collection（不能只切 embedding 不切 collection，
    否则用 bge 向量查云端 collection，向量空间不匹配，分数没有意义）。"""
    async def go():
        calls = []

        async def fake_embed_query(text, provider=None):
            calls.append(provider)
            if provider != "bge":
                raise RuntimeError("DashScope 欠费")
            return [0.1] * 8

        def fake_search(collection, vec, cand):
            assert collection == rewrite_evaluator.settings.MILVUS_COLLECTION_BGE
            return [{"score": 0.5}] * 5

        with patch.object(rewrite_evaluator.embedding_service, "embed_query",
                          AsyncMock(side_effect=fake_embed_query)), \
             patch.object(rewrite_evaluator.milvus_client, "search", fake_search):
            hits = await rewrite_evaluator._light_dense("query", None)

        assert hits == [{"score": 0.5}] * 5
        assert "bge" in calls
    asyncio.run(go())


def test_cloud_embed_success_uses_cloud_collection():
    """云端 embedding 正常 → 走云端 collection，不触发 bge 回退。"""
    async def go():
        def fake_search(collection, vec, cand):
            assert collection == rewrite_evaluator.settings.MILVUS_COLLECTION
            return [{"score": 0.7}] * 5

        with patch.object(rewrite_evaluator.embedding_service, "embed_query",
                          AsyncMock(return_value=[0.2] * 8)), \
             patch.object(rewrite_evaluator.milvus_client, "search", fake_search):
            hits = await rewrite_evaluator._light_dense("query", None)

        assert hits == [{"score": 0.7}] * 5
    asyncio.run(go())
