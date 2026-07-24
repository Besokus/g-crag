"""置信度/CRAG 标签体系细化（confidence refinement）测试。

T1: evidence_strength 统一函数（v1/v2/rerank降级三路）。
后续 T2/T3... 测试追加于本文件。
"""
from app.rag.crag import evidence_strength


# ===== T1 · evidence_strength =====

def test_evidence_strength_v2_detail_mixed():
    # 2 relevant + 1 partial / 5 → (2 + 0.5) / 5 = 0.5
    assert evidence_strength(detail={"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}) == 0.5


def test_evidence_strength_v2_all_relevant():
    assert evidence_strength(detail={"relevant": 5, "partial": 0, "irrelevant": 0, "n": 5}) == 1.0


def test_evidence_strength_v2_all_irrelevant():
    assert evidence_strength(detail={"relevant": 0, "partial": 0, "irrelevant": 5, "n": 5}) == 0.0


def test_evidence_strength_v2_partial_only():
    # 0 relevant + 2 partial / 4 → (0 + 1.0) / 4 = 0.25
    assert evidence_strength(detail={"relevant": 0, "partial": 2, "irrelevant": 2, "n": 4}) == 0.25


def test_evidence_strength_v1_top1():
    assert evidence_strength(top1=0.8) == 0.8


def test_evidence_strength_v1_clamp():
    assert evidence_strength(top1=1.5) == 1.0
    assert evidence_strength(top1=-0.2) == 0.0


def test_evidence_strength_rerank_degraded():
    # rerank 未启用/失败 → None（评估器降级，不参与分桶）
    assert evidence_strength(top1=0.9, rerank_ok=False) is None
    assert evidence_strength(detail={"relevant": 5, "partial": 0, "irrelevant": 0, "n": 5},
                             rerank_ok=False) is None


def test_evidence_strength_detail_preferred_over_top1():
    # 同时给 detail 和 top1：detail（v2 逐条）优先
    assert evidence_strength(top1=0.9,
                             detail={"relevant": 0, "partial": 0, "irrelevant": 5, "n": 5}) == 0.0


def test_evidence_strength_n_zero_fallback_top1():
    # detail n=0（异常）→ 回退 top1
    assert evidence_strength(top1=0.7,
                             detail={"relevant": 0, "partial": 0, "irrelevant": 0, "n": 0}) == 0.7


def test_evidence_strength_no_input_none():
    # 啥都没给 → None
    assert evidence_strength() is None
