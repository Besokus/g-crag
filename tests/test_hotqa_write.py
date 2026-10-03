"""write_hotqa：点赞写入高频问答对；结构化拒答与空答案不写。"""
import asyncio

from app.core.domain_pack import get_pack
from app.services import feedback_optimizer_service as fo


class _Redis:
    def __init__(self):
        self.store = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v):
        self.store[k] = v


def _write(monkeypatch, answer):
    r = _Redis()
    monkeypatch.setattr("app.clients.redis_client.get_redis", lambda: r)
    asyncio.run(fo.write_hotqa("碧水源中标金额是多少", answer))
    return r.store


def test_normal_answer_written(monkeypatch):
    assert len(_write(monkeypatch, "13.57亿元[1]")) == 1


def test_refusal_and_empty_not_written(monkeypatch):
    assert _write(monkeypatch, get_pack().render_prompt("refuse").strip()) == {}
    assert _write(monkeypatch, "  ") == {}
