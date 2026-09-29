"""Claude provider：OpenAI 格式 ↔ Anthropic 格式转换，以及请求构造（HTTP 用假传输层）。"""
import asyncio
import json

import httpx
import pytest

from app.providers.llm import claude_llm as cl


def test_system_merged_and_roles_alternate():
    system, msgs = cl.to_anthropic_messages([
        {"role": "system", "content": "你是助手"},
        {"role": "system", "content": "用中文"},
        {"role": "user", "content": "问题一"},
        {"role": "user", "content": [{"type": "text", "text": "补充"}]},
        {"role": "assistant", "content": "回答"},
    ])
    assert system == "你是助手\n\n用中文"
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert [b["text"] for b in msgs[0]["content"]] == ["问题一", "补充"]


def test_tool_history_converted():
    _, msgs = cl.to_anthropic_messages([
        {"role": "user", "content": "北京天气"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "weather", "arguments": '{"city": "北京"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "晴"},
    ])
    assert msgs[1] == {"role": "assistant",
                       "content": [{"type": "tool_use", "id": "t1", "name": "weather", "input": {"city": "北京"}}]}
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "晴"}


def test_tools_and_choice_converted():
    tools = [{"type": "function", "function": {"name": "f", "description": "d",
                                                "parameters": {"type": "object", "properties": {}}}}]
    assert cl.to_anthropic_tools(tools) == [{"name": "f", "description": "d",
                                             "input_schema": {"type": "object", "properties": {}}}]
    assert cl._tool_choice("auto") == {"type": "auto"}
    assert cl._tool_choice("required") == {"type": "any"}
    assert cl._tool_choice("none") is None


@pytest.fixture
def fake_http(monkeypatch):
    """拦截 httpx.AsyncClient，记录请求并返回预置响应。"""
    seen: dict = {"responses": []}
    real = httpx.AsyncClient

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(req.url), req.headers, json.loads(req.content)
        status, data = seen["responses"].pop(0)
        return httpx.Response(status, json=data)

    monkeypatch.setattr(cl.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(cl.settings, "CLAUDE_BASE_URL", "https://proxy.example/")
    monkeypatch.setattr(cl.settings, "CLAUDE_API_KEY", "k")
    monkeypatch.setattr(cl.settings, "CLAUDE_MODEL", "claude-x")
    monkeypatch.setattr(cl.settings, "CLAUDE_PROTOCOL", "anthropic-proxy")
    monkeypatch.setattr(cl.settings, "CLAUDE_SEND_TEMPERATURE", False)
    return seen


_OK = {"content": [{"type": "text", "text": "你好"}], "usage": {"input_tokens": 3, "output_tokens": 2}}


def test_chat_request_shape_and_usage(fake_http):
    fake_http["responses"] = [(200, _OK)]
    content, usage = asyncio.run(cl.ClaudeLLM().chat_with_usage(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], temperature=0.1, max_tokens=50))
    assert content == "你好" and usage == {"input": 3, "output": 2}
    assert fake_http["url"] == "https://proxy.example/anthropic/deployments/claude-x/chat/completions"
    assert fake_http["headers"]["x-api-key"] == "k"
    body = fake_http["body"]
    assert body["system"] == "s" and body["max_tokens"] == 50 and "temperature" not in body


def test_standard_protocol_url(fake_http, monkeypatch):
    monkeypatch.setattr(cl.settings, "CLAUDE_PROTOCOL", "anthropic")
    fake_http["responses"] = [(200, _OK)]
    asyncio.run(cl.ClaudeLLM().chat([{"role": "user", "content": "hi"}]))
    assert fake_http["url"] == "https://proxy.example/v1/messages"


def test_retry_on_5xx_but_not_on_4xx(fake_http, monkeypatch):
    monkeypatch.setattr(cl.settings, "LLM_MAX_RETRIES", 1)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(cl.asyncio, "sleep", lambda *_: real_sleep(0))
    fake_http["responses"] = [(503, {}), (200, _OK)]
    assert asyncio.run(cl.ClaudeLLM().chat([{"role": "user", "content": "hi"}])) == "你好"
    fake_http["responses"] = [(400, {"error": "bad"}), (200, _OK)]
    with pytest.raises(RuntimeError, match="HTTP 400"):
        asyncio.run(cl.ClaudeLLM().chat([{"role": "user", "content": "hi"}]))


def test_chat_with_tools_parses_tool_use(fake_http):
    fake_http["responses"] = [(200, {"content": [
        {"type": "text", "text": "查一下"},
        {"type": "tool_use", "id": "t9", "name": "weather", "input": {"city": "北京"}}]})]
    res = asyncio.run(cl.ClaudeLLM().chat_with_tools(
        [{"role": "user", "content": "天气"}],
        [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object"}}}]))
    assert res == {"content": "查一下", "tool_calls": [{"id": "t9", "name": "weather", "arguments": {"city": "北京"}}]}
    assert fake_http["body"]["tool_choice"] == {"type": "auto"}


def test_cache_tag_follows_configured_model(monkeypatch):
    from app.providers import factory
    monkeypatch.setattr(factory.settings, "LLM_PROVIDER", "claude")
    monkeypatch.setattr(factory.settings, "CLAUDE_MODEL", "claude-opus-4-8")
    assert factory.llm_cache_tag(None) == "claude@claude-opus-4-8"
    assert factory.llm_cache_tag("default") == "claude@claude-opus-4-8"
    assert factory.llm_cache_tag("claude@claude-opus-4-8") == "claude@claude-opus-4-8"   # 幂等
    monkeypatch.setattr(factory.settings, "LLM_PROVIDER", "qwen")
    assert factory.llm_cache_tag(None).startswith("qwen@")   # 换 provider → 换缓存键，不再命中旧答案
