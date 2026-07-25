"""B4/B7: CRAG refused 与 stream 无结果 自动入 evidence_gap。"""
import pytest

from app.services import qa_service


@pytest.mark.asyncio
async def test_refused_collects_to_gap(monkeypatch):
    """confidence=refused → collect 被调用，source=auto_crag。"""
    called = {}

    async def fake_collect(query, answer, confidence, grade, action, source="auto", tenant="default"):
        called["args"] = dict(query=query, confidence=confidence,
                              grade=grade, action=action, source=source, tenant=tenant)
        return 1

    # helper 内部是函数内 `from app.services import evidence_gap_service` 局部 import，
    # 因此 patch 真实模块 app.services.evidence_gap_service.collect（而非 qa_service 模块级属性）。
    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", True, raising=False)

    await qa_service._maybe_collect_refused(
        nq="主变异常", answer="", confidence="refused",
        grade="incorrect", action="rewritten_failed", tenant="default")
    assert called["args"]["source"] == "auto_crag"
    assert called["args"]["confidence"] == "refused"


@pytest.mark.asyncio
async def test_non_refused_no_collect(monkeypatch):
    """confidence=high → 不调 collect。"""
    called = []

    async def fake_collect(*a, **kw):
        called.append(1)
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", True, raising=False)

    await qa_service._maybe_collect_refused(
        nq="x", answer="", confidence="high",
        grade="correct", action="normal", tenant="default")
    assert called == []


@pytest.mark.asyncio
async def test_no_recall_action_empty_uses_auto_no_recall(monkeypatch):
    """B7 stream 无结果路径：action='' + grade='incorrect' → source=auto_no_recall。"""
    called = {}

    async def fake_collect(query, answer, confidence, grade, action, source="auto", tenant="default"):
        called["args"] = dict(source=source, action=action, grade=grade)
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", True, raising=False)

    # stream 无结果收口点传入 confidence='refused', grade='incorrect', action=''
    # helper 触发条件：action in {rewritten_failed, refused} 不含 '' → 需 confidence='refused' 触发
    await qa_service._maybe_collect_refused(
        nq="无结果query", answer="", confidence="refused",
        grade="incorrect", action="", tenant="default")
    assert called["args"]["source"] == "auto_no_recall"


@pytest.mark.asyncio
async def test_switch_off_skips_collect(monkeypatch):
    """CRAG_REFUSED_TO_GAP_ENABLE=False → 整体跳过，collect 不被调用。"""
    called = []

    async def fake_collect(*a, **kw):
        called.append(1)
        return 1

    monkeypatch.setattr("app.services.evidence_gap_service.collect", fake_collect, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", False, raising=False)

    await qa_service._maybe_collect_refused(
        nq="x", answer="", confidence="refused",
        grade="incorrect", action="rewritten_failed", tenant="default")
    assert called == []


@pytest.mark.asyncio
async def test_collect_exception_swallowed(monkeypatch):
    """collect 抛异常 → degraded 吞掉，helper 不抛（fire-and-forget 安全）。"""
    async def boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.services.evidence_gap_service.collect", boom, raising=False)
    monkeypatch.setattr("app.services.qa_service.settings.CRAG_REFUSED_TO_GAP_ENABLE", True, raising=False)

    # 不应抛
    await qa_service._maybe_collect_refused(
        nq="x", answer="", confidence="refused",
        grade="incorrect", action="refused", tenant="default")
