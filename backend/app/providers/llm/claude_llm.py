"""Claude LLM（Anthropic Messages 协议，httpx 直连）。

调用方统一用 OpenAI 格式的 messages / tools / tool_calls（其他 provider 都是 OpenAI 兼容），
本类在边界处转换成 Anthropic 格式，业务代码不感知。

CLAUDE_PROTOCOL：
- anthropic-proxy：{base}/anthropic/deployments/{model}/chat/completions，请求/响应为 Anthropic 格式
- anthropic：标准 {base}/v1/messages
两者都用 x-api-key 鉴权。部分模型（如 claude-opus-4-8）拒绝 temperature 参数，
由 CLAUDE_SEND_TEMPERATURE 控制是否发送。
"""
import asyncio
import json

import httpx

from app.config import settings
from app.providers.base import LLMProvider

_RETRY_STATUS = {429, 500, 502, 503, 504, 529}


def _text_of(content) -> str:
    """OpenAI content 可能是 str 或 [{"type":"text","text":...}, ...]，只取文本部分。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI 消息列表 → (system, Anthropic messages)。

    system 消息合并为顶层 system；assistant 的 tool_calls → tool_use 块；role=tool → user 的
    tool_result 块；相邻同角色消息合并（Anthropic 要求 user/assistant 交替）。
    """
    system_parts: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(_text_of(m.get("content")))
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                           "content": _text_of(m.get("content")) or "(空)"}])
        elif role == "assistant":
            blocks = []
            text = _text_of(m.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": tc.get("id", ""), "name": fn.get("name", ""),
                               "input": args})
            push("assistant", blocks)
        else:
            push("user", [{"type": "text", "text": _text_of(m.get("content")) or " "}])
    return "\n\n".join(p for p in system_parts if p), out


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """OpenAI {"type":"function","function":{name,description,parameters}} → Anthropic 工具定义。"""
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({"name": fn["name"], "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _tool_choice(choice) -> dict | None:
    if choice in (None, "auto"):
        return {"type": "auto"}
    if choice == "required":
        return {"type": "any"}
    if choice == "none":
        return None
    if isinstance(choice, dict) and choice.get("function", {}).get("name"):
        return {"type": "tool", "name": choice["function"]["name"]}
    return {"type": "auto"}


class ClaudeLLM(LLMProvider):
    def __init__(self):
        self.model = settings.CLAUDE_MODEL
        self.base_url = settings.CLAUDE_BASE_URL.rstrip("/")
        self.protocol = settings.CLAUDE_PROTOCOL

    def _url(self, model: str) -> str:
        if self.protocol == "anthropic-proxy":
            return f"{self.base_url}/anthropic/deployments/{model}/chat/completions"
        return f"{self.base_url}/v1/messages"

    def _headers(self) -> dict:
        return {"x-api-key": settings.CLAUDE_API_KEY, "anthropic-version": "2023-06-01",
                "content-type": "application/json"}

    def _body(self, messages, temperature, max_tokens, model) -> dict:
        system, msgs = to_anthropic_messages(messages)
        body = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        if system:
            body["system"] = system
        if settings.CLAUDE_SEND_TEMPERATURE and temperature is not None:
            body["temperature"] = temperature
        return body

    async def _post(self, body: dict, model: str, timeout: float | None) -> dict:
        t = timeout or settings.LLM_TIMEOUT
        last: Exception | None = None
        for attempt in range(settings.LLM_MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=t) as c:
                    r = await c.post(self._url(model), headers=self._headers(), json=body)
                if r.status_code == 200:
                    return r.json()
                last = RuntimeError(f"Claude HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code not in _RETRY_STATUS:
                    raise last
            except httpx.TransportError as e:   # 超时、连接失败
                last = e
            if attempt < settings.LLM_MAX_RETRIES:
                await asyncio.sleep(1.0 * (attempt + 1))
        raise last  # type: ignore[misc]

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None,
                              **kw) -> tuple[str, dict | None]:
        from app.core.otel_genai import record_exception, trace_span
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat"}):
                data = await self._post(self._body(messages, temperature, max_tokens, _model),
                                        _model, kw.get("timeout"))
                content = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                u = data.get("usage") or {}
                usage = {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)} if u else None
                self._record_llm_span(_model, messages, content, temperature, max_tokens)
                return content, usage
        except Exception as exc:
            record_exception(exc)
            raise

    async def chat(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> str:
        content, _ = await self.chat_with_usage(messages, temperature=temperature,
                                                max_tokens=max_tokens, model=model, **kw)
        return content

    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        _model = model or self.model
        body = {**self._body(messages, temperature, max_tokens, _model), "stream": True}
        async with httpx.AsyncClient(timeout=kw.get("timeout") or settings.LLM_TIMEOUT) as c:
            async with c.stream("POST", self._url(_model), headers=self._headers(), json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"Claude HTTP {r.status_code}: {(await r.aread())[:300]!r}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if ev.get("type") == "content_block_delta" and ev["delta"].get("type") == "text_delta":
                        yield ev["delta"]["text"]
                    elif ev.get("type") == "error":
                        raise RuntimeError(f"Claude stream error: {ev.get('error')}")

    async def chat_with_tools(self, messages, tools, tool_choice="auto", temperature=0.2,
                              max_tokens=2048, model=None, **kw):
        from app.core.otel_genai import record_exception, trace_span
        from opentelemetry.trace import SpanKind
        _model = model or self.model
        body = self._body(messages, temperature, max_tokens, _model)
        choice = _tool_choice(tool_choice)
        if choice is not None and tools:
            body["tools"] = to_anthropic_tools(tools)
            body["tool_choice"] = choice
        try:
            with trace_span("llm.generate", kind=SpanKind.CLIENT,
                            attributes={"gen_ai.operation.name": "chat_with_tools"}):
                data = await self._post(body, _model, kw.get("timeout"))
                blocks = data.get("content", [])
                text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
                calls = [{"id": b["id"], "name": b["name"], "arguments": b.get("input") or {}}
                         for b in blocks if b.get("type") == "tool_use"]
                result = {"content": text or None, "tool_calls": calls or None}
                self._record_llm_span(_model, messages, result, temperature, max_tokens)
                return result
        except Exception as exc:
            record_exception(exc)
            raise
