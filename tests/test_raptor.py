"""RAPTOR 层次化摘要检索单测（回归 + 检索路径纯逻辑）。

背景：raptor.py 曾同时存在两处致命错误——从 routing_service 导入不存在的
RouteDecision（ImportError），以及调用不存在的 embedding_service.get_embeddings
（AttributeError）。由于调用点 retrieval_service 用 except Exception 静默降级，
RAPTOR_ENABLE 打开后每次检索都 100% 走空，本文件防止回归。
"""
import pytest

from app.rag import raptor


def test_raptor_module_import():
    """回归：模块必须可导入（RouteDecision 导入错误曾致惰性 import 恒失败）。"""
    from app.rag import raptor as _r
    assert hasattr(_r, "retrieve_with_raptor")
    assert hasattr(_r, "generate_and_cache_summaries")


def test_routing_decision_is_real_export():
    """RoutingDecision 是 routing_service 的真实导出名，raptor 的引用必须可用。"""
    from app.routing.routing_service import RoutingDecision  # noqa: F401


def test_embedding_service_apis_exist():
    """回归：raptor 依赖的 embed_texts/embed_query 必须真实存在。"""
    from app.services import embedding_service
    assert callable(embedding_service.embed_texts)
    assert callable(embedding_service.embed_query)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """retrieve_with_raptor 只用 db.execute(select(...)).all() 取文档清单。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


@pytest.mark.asyncio
async def test_retrieve_with_raptor_scores_summaries(monkeypatch):
    """query 与摘要向量同向时应命中摘要层，返回格式兼容 mixed_search。"""
    from app.services import embedding_service

    summary = {
        "level": 1, "docId": "d1", "docName": "运行规程",
        "section": "故障处置", "summary": "主变差动保护动作处置步骤",
        "embedding": [1.0, 0.0], "chunkIndices": [0, 1],
    }
    monkeypatch.setattr(raptor, "load_summaries", lambda doc_id: [summary])

    async def fake_embed_query(text, provider=None):
        return [1.0, 0.0]

    monkeypatch.setattr(embedding_service, "embed_query", fake_embed_query)

    hits = await raptor.retrieve_with_raptor(_FakeDB([("d1", "运行规程")]), "主变差动", topk=5)
    assert len(hits) == 1
    hit = hits[0]
    assert hit["docId"] == "d1"
    assert hit["source"] == "raptor_l1"
    assert hit["level"] == 1
    assert hit["chunkIndices"] == [0, 1]
    assert hit["score"] > 0.3


@pytest.mark.asyncio
async def test_retrieve_with_raptor_empty_returns_before_embed(monkeypatch):
    """无摘要可用时直接返回空，不触发 query embedding。"""
    from app.services import embedding_service

    monkeypatch.setattr(raptor, "load_summaries", lambda doc_id: [])

    async def _fail(*_a, **_k):
        raise AssertionError("无摘要时不应调用 embed_query")

    monkeypatch.setattr(embedding_service, "embed_query", _fail)

    hits = await raptor.retrieve_with_raptor(_FakeDB([]), "任意查询")
    assert hits == []


@pytest.mark.asyncio
async def test_generate_chunk_summary_sets_embeddings(monkeypatch):
    """摘要生成走 embed_texts 批量回填向量（原 get_embeddings AttributeError 回归）。"""
    from app.services import embedding_service

    class _FakeProvider:
        async def chat(self, messages, temperature=0.2, max_tokens=500):
            return "摘要：设备主变差动保护动作，需检查二次回路并按规程执行操作步骤，确保安全措施到位后方可送电。"

    monkeypatch.setattr(raptor, "get_llm_provider", lambda model_type=None: _FakeProvider())

    captured = {}

    async def fake_embed_texts(texts, chunk_ids=None):
        captured["texts"] = texts
        return [[0.5, 0.5] for _ in texts]

    monkeypatch.setattr(embedding_service, "embed_texts", fake_embed_texts)

    chunks = [
        {"docName": "规程A", "section": "处置", "chunk": "主变差动保护动作时，应先检查保护装置与二次回路，" * 5},
        {"docName": "规程A", "section": "处置", "chunk": "确认无内部故障后方可申请试送电，并做好记录。" * 5},
    ]
    summaries = await raptor.generate_chunk_summary(None, "doc-1", chunks)
    levels = sorted(s.level for s in summaries)
    assert levels == [1, 2]  # 段落摘要 + 全文摘要各一条
    assert captured["texts"] == [s.summary_text for s in summaries]
    assert all(s.embedding == [0.5, 0.5] for s in summaries)
