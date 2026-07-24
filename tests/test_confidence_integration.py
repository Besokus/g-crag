"""confidence refinement 集成回归（T9 收口）。

验证 V3 全链路端到端 + cv R 段（confidence 模型代际）切换失效老缓存。
"""
import pytest

from app.config import citation_cache_version


def test_cv_includes_r_segment(monkeypatch):
    """cv 含 R 段（CRAG_V3 代际）；V3 开/关 R 不同 → 切换时老 qa 缓存 key 变自动失效。"""
    from app.config import settings
    monkeypatch.setattr(settings, "CRAG_V3_ENABLE", False)
    cv_off = citation_cache_version()
    assert "R0" in cv_off
    monkeypatch.setattr(settings, "CRAG_V3_ENABLE", True)
    cv_on = citation_cache_version()
    assert "R1" in cv_on
    assert cv_off != cv_on  # 切换必然 key 变


@pytest.mark.asyncio
async def test_v3_end_to_end_refused_path(monkeypatch):
    """V3 全链路：incorrect → rewrite → 仍 incorrect → refused + refusedReason + 指标埋点。"""
    from app.services import qa_service
    from prometheus_client import generate_latest

    async def _fake_rw(q, *a, **k):
        return q + "_改写"

    async def _fake_mixed(*a, **k):
        return [{"score": 0.05}]

    from app.services import query_rewrite
    monkeypatch.setattr(query_rewrite, "rewrite_query", _fake_rw)
    monkeypatch.setattr(qa_service.retrieval_service, "mixed_search", _fake_mixed)
    for k, v in {"CRAG_ENABLE": True, "CRAG_PERDOC_ENABLE": False,
                 "CRAG_V3_ENABLE": True, "RERANK_ENABLE": True}.items():
        monkeypatch.setattr(qa_service.settings, k, v)

    _ctxs, conf, action, grade, extras = await qa_service._crag_correct(
        None, "q", [{"score": 0.05}], "deepseek", 5)

    assert action == "rewritten_failed"
    assert grade == "incorrect"
    assert conf == "refused"
    assert extras["confidenceLabel"] == "refused"
    assert extras["refusedReason"] == "rewrite_exhausted"
    assert extras["rewriteDelta"] == 0.0  # 改写前后 es 均 0.05
    text = generate_latest().decode()
    assert "grid_crag_confidence_label_total" in text
