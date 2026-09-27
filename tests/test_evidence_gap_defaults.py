"""G-CRAG 默认关闭证据补全的自动深度补全入库（LLM 生成 FAQ 直接入库会污染知识库与评测语料）。"""
import asyncio

from app.config import Settings


def test_auto_deep_sync_off_by_default():
    assert Settings.model_fields["EVIDENCE_GAP_DEEP_INTERVAL"].default == 0
    assert Settings.model_fields["EVIDENCE_GAP_AUTO_COLLECT"].default is True   # 缺口仍自动收集


def test_deep_cron_returns_immediately_when_off(monkeypatch):
    from app.services import evidence_gap_service as eg
    monkeypatch.setattr(eg, "batch_deep_and_sync", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应运行")))
    from app.config import settings
    monkeypatch.setattr(settings, "EVIDENCE_GAP_DEEP_INTERVAL", 0)
    asyncio.run(asyncio.wait_for(eg.deep_cron_loop(), timeout=1))
