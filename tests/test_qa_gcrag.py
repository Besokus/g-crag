"""qa_service.answer 接入 G-CRAG（设计文档 §9、§16.4、§16.8、§16.9）。外部依赖全部打桩。"""
import asyncio

import pytest

from app.rag import gcrag
from app.services import qa_service
from app.services.retrieval_service import ScoringStatus, SearchResult


@pytest.fixture
def qa(monkeypatch):
    import app.config as cfg
    for k in ("CACHE_PERSIST_ENABLE", "KG_RAG_ENABLE", "ROUTING_ENABLE", "SEMANTIC_CACHE_ENABLE",
              "SELF_RAG_ENABLE", "MULTI_TURN_CACHE_ENABLE", "CITATION_STRUCTURED_OUTPUT",
              "CITATION_AUTO_ENABLE", "CITATION_VERIFIER_ENABLE", "CRAG_REFUSED_TO_GAP_ENABLE"):
        monkeypatch.setattr(cfg.settings, k, False)
    monkeypatch.setattr(cfg.settings, "CRAG_ENABLE", True)
    box = {"cache_get": [], "cache_set": [], "llm": 0, "modes": [], "items": None, "cres": None}

    async def nq_same(db, query, nq, cid, history, mt):
        return nq

    async def not_bl(nq):
        return False

    async def cache_get(key):
        box["cache_get"].append(key)
        return None

    async def cache_set(key, *a, **k):
        box["cache_set"].append(key)

    async def no_hot(*a, **k):
        return None

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(qa_service, "_search_query_for_retrieve", nq_same)
    monkeypatch.setattr(qa_service, "_is_blacklisted", not_bl)
    monkeypatch.setattr(qa_service, "_hit_hotqa", no_hot)
    monkeypatch.setattr(qa_service.redis_client, "cache_get_json", cache_get)
    monkeypatch.setattr(qa_service.redis_client, "cache_set_json", cache_set)
    monkeypatch.setattr(qa_service.conversation_service, "save_message", noop)

    async def new_conv(*a, **k):
        return type("Conv", (), {"id": "conv1"})()
    monkeypatch.setattr(qa_service.conversation_service, "create_conversation", new_conv)

    async def search_ex(db, q, topk, **kw):
        assert kw.get("force_score") is True
        items = box["items"] if box["items"] is not None else [
            {"chunkId": "c1", "docId": "d1", "docName": "a.txt", "chunk": "正文", "score": 0.9,
             "origin": "graph", "pathIds": [0]}]
        return SearchResult(items, ScoringStatus("stub", True, "ok", len(items)))
    monkeypatch.setattr(qa_service.retrieval_service, "mixed_search_ex", search_ex)

    async def fake_correct(db, query, search, *, mode, topk, tenant="default", model_type=None):
        box["modes"].append(mode)
        if box["cres"]:
            return box["cres"](search, mode)
        return gcrag.CorrectionResult(mode=mode, contexts=search.items, initial_grade="correct",
                                      final_grade="correct", action="normal")
    monkeypatch.setattr(gcrag, "correct", fake_correct)

    class _LLM:
        last_used_name = "claude"

        async def chat(self, messages, **kw):
            box["llm"] += 1
            return "碧水源中标金额为13.57亿元[1]。"
    monkeypatch.setattr(qa_service, "get_llm_provider", lambda *a, **k: _LLM())
    return box


def _answer(**kw):
    return asyncio.run(qa_service.answer(None, "碧水源中标金额是多少", **kw))


def test_mode_passed_through_and_in_cache_key(qa):
    out = _answer(correction_mode="legacy")
    assert qa["modes"] == ["legacy"] and out["correctionMode"] == "legacy"
    assert qa["cache_get"] and all("#legacy:" in k for k in qa["cache_get"])
    assert qa["cache_set"] and all("#legacy:" in k for k in qa["cache_set"])   # high 置信写缓存


def test_default_mode_from_settings(qa, monkeypatch):
    monkeypatch.setattr(qa_service.settings, "CORRECTION_MODE", "none")
    assert _answer()["correctionMode"] == "none"


def test_no_cache_skips_all_reads_and_writes(qa):
    out = _answer(correction_mode="gcrag", no_cache=True)
    assert qa["cache_get"] == [] and qa["cache_set"] == [] and out["cached"] is False


def test_response_items_carry_origin_and_paths(qa):
    src = _answer(correction_mode="gcrag", no_cache=True)["retrievalSource"][0]
    assert (src["origin"], src["pathIds"], src["chunkId"]) == ("graph", [0], "c1")


def test_refused_does_not_call_llm(qa):
    def refused(search, mode):
        return gcrag.CorrectionResult(mode=mode, contexts=search.items, initial_grade="incorrect",
                                      final_grade="incorrect", action="refused",
                                      refused_reason="correction_exhausted",
                                      rounds=[gcrag.RoundRecord(1, "rewrite", "done", "incorrect",
                                                                "incorrect", 0.1, 0.1)])
    qa["cres"] = refused
    out = _answer(correction_mode="gcrag", no_cache=True)
    assert qa["llm"] == 0                                   # 不生成（原实现只在 prompt 里提示"优先回答无法确认"）
    assert out["answer"].startswith("根据现有资料无法回答该问题")
    assert (out["confidence"], out["cragAction"], out["refusedReason"]) == ("refused", "refused",
                                                                            "correction_exhausted")
    assert out["correctionRounds"][0]["action"] == "rewrite"


def test_empty_retrieval_enters_correction_only_in_gcrag(qa):
    qa["items"] = []
    out = _answer(correction_mode="none", no_cache=True)
    assert qa["modes"] == [] and out["confidence"] == "refused"   # none/legacy：保持原兜底
    _answer(correction_mode="gcrag", no_cache=True)
    _answer(correction_mode="gcrag_v2", no_cache=True)
    assert qa["modes"] == ["gcrag", "gcrag_v2"]                     # 图谱模式：图谱可能找得到证据


def test_invalid_mode_rejected_by_schema():
    from pydantic import ValidationError

    from app.schemas.qa import QaAnswerRequest
    assert QaAnswerRequest(query="q", correctionMode="gcrag", noCache=True).noCache is True
    with pytest.raises(ValidationError):
        QaAnswerRequest(query="q", correctionMode="magic")
