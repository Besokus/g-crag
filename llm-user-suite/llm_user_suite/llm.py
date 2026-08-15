from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass

from openai import AsyncOpenAI

from .config import settings


@dataclass(frozen=True)
class ModelConfig:
    role: str
    base_url: str
    api_key: str
    model: str


_USED_CONFIGS: ContextVar[dict[str, ModelConfig]] = ContextVar("llm_user_used_configs", default={})


def role_config(role: str) -> ModelConfig:
    prefix = role.upper()
    base_url = getattr(settings, f"{prefix}_BASE_URL", "")
    model = getattr(settings, f"{prefix}_MODEL", "")
    if base_url and model:
        return ModelConfig(role, base_url, getattr(settings, f"{prefix}_API_KEY", "") or "not-set", model)
    return ModelConfig(role, settings.OLLAMA_BASE_URL, "ollama", settings.OLLAMA_MODEL)


def used_config(role: str) -> ModelConfig:
    return _USED_CONFIGS.get().get(role, role_config(role))


def configured(role: str) -> bool:
    cfg = role_config(role)
    return bool(cfg.base_url and cfg.model)


async def chat(role: str, messages: list[dict], *, temperature: float = 0, max_tokens: int = 1200) -> str:
    cfg = role_config(role)

    async def call(target: ModelConfig) -> str:
        client = AsyncOpenAI(
            base_url=target.base_url, api_key=target.api_key,
            timeout=settings.MODEL_TIMEOUT_SECONDS, max_retries=1,
        )
        result = await client.chat.completions.create(
            model=target.model, messages=messages, temperature=temperature, max_tokens=max_tokens
        )
        current = dict(_USED_CONFIGS.get())
        current[role] = target
        _USED_CONFIGS.set(current)
        return result.choices[0].message.content or ""

    try:
        return await call(cfg)
    except Exception:
        fallback = ModelConfig(role, settings.OLLAMA_BASE_URL, "ollama", settings.OLLAMA_MODEL)
        if not fallback.base_url or not fallback.model or (
            cfg.base_url.rstrip("/") == fallback.base_url.rstrip("/") and cfg.model == fallback.model
        ):
            raise
        return await call(fallback)


async def chat_json(role: str, messages: list[dict], *, temperature: float = 0, max_tokens: int = 1200) -> dict:
    text = await chat(role, messages, temperature=temperature, max_tokens=max_tokens)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model output has no JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("model output is not a JSON object")
    return value
