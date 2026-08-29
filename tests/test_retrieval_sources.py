"""检索来源归因 + _to_item 扩字段单测。"""
from app.services import retrieval_service


def test_aggregate_srcs_merges_dense_and_bm25():
    dense = [
        {"key": ("d1", 0), "srcs": ["dense_cloud"]},
        {"key": ("d1", 0), "srcs": ["dense_bge"]},
    ]
    sparse = [{"key": ("d1", 0)}, {"key": ("d2", 1)}]
    m = retrieval_service._aggregate_srcs(dense, sparse)
    assert m[("d1", 0)] == ["bm25", "dense_bge", "dense_cloud"]   # 并集去重排序
    assert m[("d2", 1)] == ["bm25"]


def test_aggregate_srcs_empty():
    assert retrieval_service._aggregate_srcs([], []) == {}


def test_to_item_full_fields():
    h = {
        "text": "abc", "score": 0.9, "doc_id": "d1", "doc_name": "规程A",
        "chunk_idx": 3, "doc_type": "运维手册", "srcs": ["dense_cloud", "bm25"],
    }
    item = retrieval_service._to_item(h)
    assert item["chunk"] == "abc"
    assert item["score"] == 0.9
    assert item["docId"] == "d1"
    assert item["docName"] == "规程A"
    assert item["chunkIdx"] == 3
    assert item["docType"] == "运维手册"
    assert item["sources"] == ["dense_cloud", "bm25"]


def test_to_item_missing_fields_default_empty():
    """旧格式 hit（无 doc_type/srcs/chunk_idx）→ 字段安全缺省，不报错。"""
    item = retrieval_service._to_item({"text": "x", "doc_id": "d", "doc_name": "n"})
    assert item["docType"] == ""
    assert item["chunkIdx"] is None
    assert item["sources"] == []


def test_normalize_unbounded_scores_scales_bm25_to_zero_one():
    """BM25 原始分（>1）按 top 相对值归一化到 0-1，保序。"""
    hits = [{"score": 56.58}, {"score": 30.0}, {"score": 10.0}]
    retrieval_service._normalize_unbounded_scores(hits)
    assert hits[0]["score"] == 1.0
    assert abs(hits[1]["score"] - 0.5302) < 1e-4
    assert abs(hits[2]["score"] - 0.1768) < 1e-4
    assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]


def test_normalize_keeps_dense_cosine_scale():
    """0-1 域（dense 余弦/rerank 分）不动。"""
    hits = [{"score": 0.9}, {"score": 0.5}]
    retrieval_service._normalize_unbounded_scores(hits)
    assert hits[0]["score"] == 0.9 and hits[1]["score"] == 0.5


def test_normalize_empty_and_missing_score():
    retrieval_service._normalize_unbounded_scores([])
    hits = [{"text": "no score"}, {"score": None}]
    retrieval_service._normalize_unbounded_scores(hits)  # 不抛即过
