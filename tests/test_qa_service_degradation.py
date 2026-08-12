"""云端LLM/Embedding熔断降级：qa_service 里新增的降级信号透出逻辑单测。

只测被抽出的独立 helper（不 mock 整条 answer()/stream_answer() 编排链路，
理由见 2026-08-12-llm-degradation-resilience.md Task 6 说明）。
"""
from app.core.qa_trace import new_collector
from app.services import qa_service


def test_llm_degradation_fields_no_switch():
    class Prov:
        last_used_name = "qwen"
        degraded = False
        degrade_reason = ""
    fields = qa_service._llm_degradation_fields(Prov(), "qwen")
    assert fields == {"modelType": "qwen", "llmDegraded": False, "llmDegradedReason": ""}


def test_llm_degradation_fields_switched_to_ollama():
    class Prov:
        last_used_name = "ollama"
        degraded = True
        degrade_reason = "云端模型全部不可用，已使用本地应急模型"
    fields = qa_service._llm_degradation_fields(Prov(), "qwen")
    assert fields["modelType"] == "ollama"
    assert fields["llmDegraded"] is True
    assert "本地应急模型" in fields["llmDegradedReason"]


def test_llm_degradation_fields_missing_attrs_falls_back_to_requested():
    """provider 对象缺 last_used_name（防御性分支）→ 回落请求参数。"""
    fields = qa_service._llm_degradation_fields(object(), "deepseek")
    assert fields["modelType"] == "deepseek"
    assert fields["llmDegraded"] is False


def test_cap_confidence_caps_ollama_high_to_medium():
    assert qa_service._cap_confidence_for_local_model("high", "ollama") == "medium"


def test_cap_confidence_leaves_cloud_provider_unchanged():
    assert qa_service._cap_confidence_for_local_model("high", "qwen") == "high"


def test_cap_confidence_leaves_non_high_unchanged():
    assert qa_service._cap_confidence_for_local_model("medium", "ollama") == "medium"


def test_retrieval_degradation_fields_when_marked():
    tc = new_collector("q")
    tc.mark("dense_cloud_failed", True)
    fields = qa_service._retrieval_degradation_fields()
    assert fields["retrievalDegraded"] is True
    assert "云端向量检索" in fields["retrievalDegradedReason"]


def test_retrieval_degradation_fields_when_not_marked():
    new_collector("q")
    fields = qa_service._retrieval_degradation_fields()
    assert fields["retrievalDegraded"] is False
    assert fields["retrievalDegradedReason"] == ""


def test_llm_all_down_response_structure():
    contexts = [{"docId": "d1", "docName": "规程A", "docType": "pdf", "chunkIdx": 0,
                 "chunk": "内容", "score": 0.8, "sources": ["dense_cloud"]}]
    resp = qa_service._llm_all_down_response("主变异常", contexts, 0.0, "conv1")
    assert resp["confidence"] == "refused"
    assert resp["cragAction"] == "llm_all_down"
    assert resp["conversationId"] == "conv1"
    assert len(resp["retrievalSource"]) == 1
    assert resp["retrievalSource"][0]["docId"] == "d1"
    assert "本地应急模型" in resp["answer"]
