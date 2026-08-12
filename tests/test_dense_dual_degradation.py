"""_dense_dual 云路降级可见性单测：云端 embedding/检索异常时打 trace mark，
供 qa_service 透出 retrievalDegraded 给前端（不依赖真实 Milvus/DashScope）。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.core.qa_trace import TraceCollector
from app.services import retrieval_service


def test_cloud_failure_marks_trace():
    async def go():
        tc = TraceCollector("q")

        async def fake_embed(text, provider=None):
            if provider == "bge":
                return [0.1] * 8
            raise RuntimeError("DashScope 欠费")

        with patch.object(retrieval_service, "_get_trace", return_value=tc), \
             patch.object(retrieval_service.embedding_service, "embed_query",
                          AsyncMock(side_effect=fake_embed)), \
             patch.object(retrieval_service.milvus_client, "search", return_value=[{"score": 0.5}]):
            cloud_hits, bge_hits = await retrieval_service._dense_dual("query", 5, 64)

        assert cloud_hits == []
        assert bge_hits == [{"score": 0.5}]
        assert tc.marks.get("dense_cloud_failed") is True
    asyncio.run(go())


def test_both_paths_ok_no_mark():
    async def go():
        tc = TraceCollector("q")

        with patch.object(retrieval_service, "_get_trace", return_value=tc), \
             patch.object(retrieval_service.embedding_service, "embed_query",
                          AsyncMock(return_value=[0.1] * 8)), \
             patch.object(retrieval_service.milvus_client, "search", return_value=[{"score": 0.9}]):
            cloud_hits, bge_hits = await retrieval_service._dense_dual("query", 5, 64)

        assert cloud_hits and bge_hits
        assert "dense_cloud_failed" not in tc.marks
    asyncio.run(go())


def test_bge_failure_alone_does_not_mark_retrieval_degraded():
    """bge 路单独挂不算"云端降级"（bge 本来就是本地兜底路，它挂了是另一类问题）。"""
    async def go():
        tc = TraceCollector("q")

        async def fake_embed(text, provider=None):
            if provider == "bge":
                raise RuntimeError("bge 模型加载失败")
            return [0.1] * 8

        with patch.object(retrieval_service, "_get_trace", return_value=tc), \
             patch.object(retrieval_service.embedding_service, "embed_query",
                          AsyncMock(side_effect=fake_embed)), \
             patch.object(retrieval_service.milvus_client, "search", return_value=[{"score": 0.9}]):
            cloud_hits, bge_hits = await retrieval_service._dense_dual("query", 5, 64)

        assert bge_hits == []
        assert "dense_cloud_failed" not in tc.marks
    asyncio.run(go())
