"""LLM 模型路由(L0 fallback + L1 熔断 + stream 切备)单测。

照 test_crag_v2.py 的 mock provider 范式。不依赖真实云 API——用 FakeProv 模拟
异常/空输出/流式中断,验证 FallbackLLMProvider 的切备与熔断状态机。
"""
import pytest

from app.providers.llm_router import (
    FallbackLLMProvider, is_healthy, record_fail, record_ok, resolve_chain,
)


class FakeProv:
    """模拟单个 provider：可控 chat 返回 / stream token / 抛异常。"""

    def __init__(self, name, chat_res="ok", stream_tokens=None, exc=None):
        self.name = name
        self.chat_res = chat_res
        self.stream_tokens = stream_tokens or ["t1", "t2"]
        self.exc = exc

    async def chat(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        if self.exc:
            raise self.exc
        return self.chat_res

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        if self.exc:
            raise self.exc
        return self.chat_res, None

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2,
                              max_tokens=2048, model=None, **kw):
        if self.exc:
            raise self.exc
        return {"content": self.chat_res, "tool_calls": None}

    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        if self.exc:
            raise self.exc
        for t in self.stream_tokens:
            yield t


# ===== L0 fallback =====

@pytest.mark.asyncio
async def test_fallback_on_exception():
    """主 provider 异常 → 切备，返回备结果，last_used_name 更新。"""
    p1 = FakeProv("deepseek", exc=RuntimeError("boom"))
    p2 = FakeProv("qwen", chat_res="from qwen")
    fb = FallbackLLMProvider([p1, p2], ["deepseek", "qwen"])
    res = await fb.chat([{"role": "user", "content": "hi"}])
    assert res == "from qwen"
    assert fb.last_used_name == "qwen"


@pytest.mark.asyncio
async def test_fallback_on_empty():
    """主 provider 空 answer → 切备（LLM_FALLBACK_ON_EMPTY 直击 deepseek 空 answer）。"""
    p1 = FakeProv("deepseek", chat_res="")
    p2 = FakeProv("qwen", chat_res="real answer")
    fb = FallbackLLMProvider([p1, p2], ["deepseek", "qwen"])
    res = await fb.chat([{"role": "user", "content": "hi"}])
    assert res == "real answer"
    assert fb.last_used_name == "qwen"


@pytest.mark.asyncio
async def test_all_fail_raises_last():
    """全部 provider 异常 → 抛最后一个异常（不吞错）。"""
    p1 = FakeProv("deepseek", exc=RuntimeError("e1"))
    p2 = FakeProv("qwen", exc=RuntimeError("e2"))
    fb = FallbackLLMProvider([p1, p2], ["deepseek", "qwen"])
    with pytest.raises(RuntimeError, match="e2"):
        await fb.chat([{"role": "user", "content": "hi"}])


# ===== L0 stream fallback =====

@pytest.mark.asyncio
async def test_stream_fallback_before_first_token():
    """流式首 token 前异常 → 切备重启，拿备的 token。"""

    class MidFail:
        async def stream(self, messages, **kw):
            raise RuntimeError("connect fail")
            yield  # 让 Python 识别为 async generator
        async def chat(self, *a, **kw):
            return ""

    p1 = MidFail()
    p2 = FakeProv("qwen", stream_tokens=["a", "b"])
    fb = FallbackLLMProvider([p1, p2], ["deepseek", "qwen"])
    toks = [t async for t in fb.stream([{"role": "user", "content": "hi"}])]
    assert toks == ["a", "b"]
    assert fb.last_used_name == "qwen"


@pytest.mark.asyncio
async def test_stream_no_switch_after_first_token():
    """首 token 已吐后中途异常 → 不切备（避免重复吐字），抛给上层。"""

    class MidFail:
        async def stream(self, messages, **kw):
            yield "x"
            raise RuntimeError("mid stream break")
        async def chat(self, *a, **kw):
            return ""

    fb = FallbackLLMProvider([MidFail(), FakeProv("qwen")], ["deepseek", "qwen"])
    toks = []
    with pytest.raises(RuntimeError, match="mid stream break"):
        async for t in fb.stream([{"role": "user", "content": "hi"}]):
            toks.append(t)
    assert toks == ["x"]  # 只收到首 token，没切备重跑


# ===== L1 熔断状态机 =====

def test_circuit_breaker_open_and_recover(monkeypatch):
    """连续失败达 N → 熔断；成功 → 恢复。"""
    from app.config import settings
    p = "test_cb_provider_unique"  # 唯一名避免全局状态污染
    record_ok(p)  # 清零起点
    assert is_healthy(p) is True
    for _ in range(settings.LLM_CIRCUIT_FAIL_N):
        record_fail(p)
    assert is_healthy(p) is False  # 进冷却
    record_ok(p)
    assert is_healthy(p) is True  # 恢复


# ===== resolve_chain =====

def test_resolve_chain_user_priority():
    """用户显式选优先（排链头），fallback 链跟后，去重保序。"""
    chain = resolve_chain("deepseek")
    assert chain[0] == "deepseek"          # 用户选在前
    assert "qwen" in chain
    assert len(chain) == len(set(chain))   # 去重


def test_resolve_chain_default_falls_through():
    """default/auto 占位 → 跳过，用 fallback 链头。"""
    chain = resolve_chain("default")
    assert chain[0] != "default"
    assert len(chain) >= 1
