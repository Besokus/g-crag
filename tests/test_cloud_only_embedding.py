"""LOCAL_EMBEDDING_ENABLE=False（G-CRAG 默认，ADR 0006）：入库与检索都不走本地 bge。"""
import asyncio

import pytest

from app.services import document_service, retrieval_service


@pytest.fixture(autouse=True)
def _cloud_only(monkeypatch):
    monkeypatch.setattr(retrieval_service.settings, "LOCAL_EMBEDDING_ENABLE", False)
    monkeypatch.setattr(retrieval_service.settings, "EMB_PROVIDER", "qwen")
    monkeypatch.setattr(retrieval_service.settings, "MILVUS_COLLECTION", "cloud")
    monkeypatch.setattr(retrieval_service.settings, "MILVUS_COLLECTION_BGE", "local")


def test_default_is_cloud_only():
    from app.config import Settings

    assert Settings.model_fields["LOCAL_EMBEDDING_ENABLE"].default is False


def test_small_document_still_routes_to_cloud(monkeypatch):
    monkeypatch.setattr(document_service.settings, "DOC_SIZE_THRESHOLD", 5000)
    assert document_service._embedding_target(100) == ("qwen", "cloud", "cloud")


def test_dense_dual_only_queries_cloud(monkeypatch):
    calls = []

    async def fake_embed_query(text, provider=None):
        calls.append(("embed", provider))
        return [0.1] * 8

    def fake_search(collection, vec, topk, ef):
        calls.append(("search", collection))
        return [{"score": 0.9}]

    monkeypatch.setattr(retrieval_service.embedding_service, "embed_query", fake_embed_query)
    monkeypatch.setattr(retrieval_service.milvus_client, "search", fake_search)

    cloud, bge = asyncio.run(retrieval_service._dense_dual("问题", 10, 64))
    assert cloud == [{"score": 0.9}]
    assert bge == []
    assert calls == [("embed", "qwen"), ("search", "cloud")]


def test_explicit_bge_provider_still_uses_bge(monkeypatch):
    """EMB_PROVIDER=bge 是显式的纯本地部署，不受开关影响（保持原版行为）。"""
    monkeypatch.setattr(document_service.settings, "EMB_PROVIDER", "bge")
    assert document_service._embedding_target(10000) == ("bge", "local", "bge")
