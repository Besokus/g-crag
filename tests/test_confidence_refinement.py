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


# ===== T2 · 捡回 v2 detail + 归因（断点 A）=====

import pytest
from app.rag import crag as crag_mod


def test_format_crag_reason_with_detail():
    from app.services.qa_service import _format_crag_reason
    r = _format_crag_reason({"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}, "ambiguous")
    assert "证据有限" in r
    assert "2 relevant" in r and "1 partial" in r and "5 条" in r


def test_format_crag_reason_empty():
    from app.services.qa_service import _format_crag_reason
    assert _format_crag_reason({}, "correct") == ""
    assert _format_crag_reason(None, "correct") == ""


@pytest.mark.asyncio
async def test_crag_correct_v3_picks_up_detail(monkeypatch):
    """CRAG_V3_ENABLE + PERDOC 开 → extras 含 cragDetail/cragReason（断点 A）。"""
    from app.services import qa_service
    from app.rag import crag_v2

    monkeypatch.setattr(qa_service.settings, "CRAG_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_PERDOC_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_V3_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "RERANK_ENABLE", True)

    detail = {"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}

    async def _fake_grade(*a, **k):
        return crag_mod.GRADE_CORRECT, detail

    monkeypatch.setattr(crag_v2, "grade_with_llm", _fake_grade)

    _ctxs, conf, _action, grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.9}], "deepseek", 5
    )
    assert grade == "correct"
    # T3: es=(2+0.5)/5=0.5 → medium_high → 老字段 confidence 映射 medium
    assert conf == "medium"
    assert extras["confidenceLabel"] == "medium_high"
    assert extras["confidenceScore"] == 0.5
    assert extras["cragDetail"] == detail
    assert "2 relevant" in extras["cragReason"]


# ===== T3 · 连续置信度 + 5档 + 修 low（断点 B）=====

from app.rag.crag import confidence_score, label_to_confidence


def test_confidence_score_buckets():
    assert confidence_score(0.9, "normal") == (0.9, "high")
    assert confidence_score(0.6, "normal") == (0.6, "medium_high")
    assert confidence_score(0.4, "normal") == (0.4, "medium_low")
    assert confidence_score(0.25, "normal") == (0.25, "low")        # low 真产出
    assert confidence_score(0.1, "normal") == (0.1, "refused")


def test_confidence_score_refused_action():
    # action=refused（改写后仍 incorrect）→ 强 refused（不论 es）
    assert confidence_score(0.5, "refused") == (0.5, "refused")


def test_confidence_score_degraded_caps_low():
    # rerank 降级（es=None/degraded）→ 封顶 low，绝不进 high
    assert confidence_score(None, "normal", degraded=True) == (0.3, "low")
    assert confidence_score(None, "normal") == (0.3, "low")


def test_confidence_score_boundaries():
    assert confidence_score(0.7, "normal")[1] == "high"
    assert confidence_score(0.5, "normal")[1] == "medium_high"
    assert confidence_score(0.35, "normal")[1] == "medium_low"
    assert confidence_score(0.2, "normal")[1] == "low"


def test_label_to_confidence_legacy_mapping():
    # 老字段兼容：high/refused 保留，medium_*/low → medium（前端零改动）
    assert label_to_confidence("high") == "high"
    assert label_to_confidence("medium_high") == "medium"
    assert label_to_confidence("medium_low") == "medium"
    assert label_to_confidence("low") == "medium"     # low 仅 confidenceLabel 新字段可见
    assert label_to_confidence("refused") == "refused"


@pytest.mark.asyncio
async def test_crag_correct_v3_v1_path_confidence(monkeypatch):
    """V3 开 + v1 路径（无 detail）→ extras 含 confidenceScore/Label/evidenceStrength。"""
    from app.services import qa_service

    monkeypatch.setattr(qa_service.settings, "CRAG_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_PERDOC_ENABLE", False)  # 走 v1
    monkeypatch.setattr(qa_service.settings, "CRAG_V3_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "RERANK_ENABLE", True)

    # top1=0.8 → grade=correct, es=0.8 → high
    _ctxs, conf, _action, grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.8}], "deepseek", 5
    )
    assert grade == "correct"
    assert conf == "high"
    assert extras["confidenceLabel"] == "high"
    assert extras["confidenceScore"] == 0.8
    assert extras["evidenceStrength"] == 0.8
    assert "cragDetail" not in extras  # v1 路径无逐条 detail


@pytest.mark.asyncio
async def test_crag_correct_v3_off_no_extras(monkeypatch):
    """CRAG_V3_ENABLE 关 → extras={} 现状（前端零改动）。"""
    from app.services import qa_service
    from app.rag import crag_v2

    monkeypatch.setattr(qa_service.settings, "CRAG_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_PERDOC_ENABLE", True)
    monkeypatch.setattr(qa_service.settings, "CRAG_V3_ENABLE", False)
    monkeypatch.setattr(qa_service.settings, "RERANK_ENABLE", True)

    detail = {"relevant": 2, "partial": 1, "irrelevant": 2, "n": 5}

    async def _fake_grade(*a, **k):
        return crag_mod.GRADE_CORRECT, detail

    monkeypatch.setattr(crag_v2, "grade_with_llm", _fake_grade)

    _ctxs, _conf, _action, _grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.9}], "deepseek", 5
    )
    assert extras == {}
