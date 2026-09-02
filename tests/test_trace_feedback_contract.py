"""Focused contracts for trace IDs and structured feedback sources."""

from app.core.trace_id import resolve_trace_id, valid_trace_id
from app.services.feedback_service import load_sources_json, normalize_sources


def test_resolve_trace_id_preserves_valid_caller_value():
    trace_id = "trace-20260727-abcd"

    assert resolve_trace_id(trace_id) == trace_id
    assert valid_trace_id(trace_id) == trace_id


def test_resolve_trace_id_replaces_invalid_value():
    trace_id = resolve_trace_id("bad value with spaces")

    assert trace_id != "bad value with spaces"
    assert valid_trace_id(trace_id) == trace_id


def test_normalize_sources_accepts_structured_aliases():
    sources = normalize_sources([
        {
            "docName": "主变运维手册",
            "docType": "运维规程",
            "docId": "doc-1",
            "chunkId": "chunk-2",
            "chunkIdx": 0,
            "score": 0.91,
            "text": "油温异常处置",
            "sources": ["dense_cloud", "bm25"],
        }
    ])

    assert sources == [{
        "doc_id": "doc-1",
        "doc_name": "主变运维手册",
        "doc_type": "运维规程",
        "chunk_id": "chunk-2",
        "chunk_idx": 0,
        "score": 0.91,
        "chunk": "油温异常处置",
        "retrieval_channels": ["dense_cloud", "bm25"],
    }]


def test_normalize_sources_falls_back_to_legacy_names():
    assert normalize_sources([], "规程A, 规程B") == [
        {"doc_name": "规程A"},
        {"doc_name": "规程B"},
    ]


def test_load_sources_json_rejects_invalid_or_non_list_payload():
    assert load_sources_json('{"doc_name": "规程A"}') == []
    assert load_sources_json("not-json") == []
    assert load_sources_json('[{"doc_name": "规程A"}]') == [{"doc_name": "规程A"}]
