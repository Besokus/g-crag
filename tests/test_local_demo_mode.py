"""单 Pod 本地演示模式的 provider/collection 路由测试。"""
import asyncio


def test_dense_local_mode_only_queries_bge(monkeypatch):
    from app.services import retrieval_service

    calls = []

    async def fake_embed_query(text, provider=None):
        calls.append(("embed", provider))
        return [0.1, 0.2]

    def fake_search(collection, vector, cand, ef):
        calls.append(("search", collection))
        return []

    monkeypatch.setattr(retrieval_service.settings, "EMB_PROVIDER", "bge")
    monkeypatch.setattr(retrieval_service.settings, "MILVUS_COLLECTION", "cloud")
    monkeypatch.setattr(retrieval_service.settings, "MILVUS_COLLECTION_BGE", "local")
    monkeypatch.setattr(retrieval_service.embedding_service, "embed_query", fake_embed_query)
    monkeypatch.setattr(retrieval_service.milvus_client, "search", fake_search)

    cloud, bge = asyncio.run(retrieval_service._dense_dual("问题", 10, 64))
    assert cloud == []
    assert bge == []
    assert calls == [("embed", "bge"), ("search", "local")]


def test_document_local_mode_routes_large_documents_to_bge(monkeypatch):
    from app.services import document_service

    monkeypatch.setattr(document_service.settings, "EMB_PROVIDER", "bge")
    monkeypatch.setattr(document_service.settings, "DOC_SIZE_THRESHOLD", 1)
    monkeypatch.setattr(document_service.settings, "MILVUS_COLLECTION_BGE", "local")
    assert document_service._embedding_target(10000) == ("bge", "local", "bge")


def test_milvus_local_mode_only_uses_bge_collection(monkeypatch):
    from app.clients import milvus_client

    monkeypatch.setattr(milvus_client.settings, "EMB_PROVIDER", "bge")
    monkeypatch.setattr(milvus_client.settings, "MILVUS_COLLECTION", "cloud")
    monkeypatch.setattr(milvus_client.settings, "MILVUS_COLLECTION_BGE", "local")
    monkeypatch.setattr(milvus_client.settings, "BGE_DIM", 512)
    assert milvus_client.document_collections() == (("local", 512),)
    assert milvus_client.primary_document_collection() == "local"


def test_health_marks_ollama_configured(monkeypatch):
    from app import main

    async def fake_probe():
        return {"mysql": "ok", "minio": "ok", "milvus": "ok", "redis": "ok"}

    monkeypatch.setattr(main, "_probe_components", fake_probe)
    monkeypatch.setattr(main.settings, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(main.settings, "EMB_PROVIDER", "bge")
    response = asyncio.run(main.health())
    assert response.data["providers"]["llm"]["keyConfigured"] is True
    assert response.data["providers"]["embedding"]["keyConfigured"] is True


def test_seed_action_is_idempotent_for_vectorized_document():
    from kb_seed.seed_kb import action_for

    assert action_for(None) == (True, True, True)
    assert action_for({"status": "pending"}) == (False, True, True)
    assert action_for({"status": "parsed"}) == (False, False, True)
    assert action_for({"status": "vectorized"}) == (False, False, False)


def test_startup_dependency_wait_is_disabled_by_default(monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "STARTUP_DEPENDENCY_RETRIES", 0)
    asyncio.run(main._wait_for_startup_dependencies())
