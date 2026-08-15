"""Grid-QA compatibility wrappers for the provider-neutral ``llm_eval_core`` package.

Existing service and HTTP call signatures remain unchanged.  The standalone LLM-as-a-User
suite imports the same evaluator primitives with its own model provider.
"""
from __future__ import annotations

from typing import Optional

from llm_eval_core import (
    judge_answerability as _core_answerability,
    judge_citation as _core_citation,
    judge_completeness as _core_completeness,
    judge_context_relevance as _core_context_relevance,
    judge_hallucination as _core_hallucination,
    verify_claims as _core_verify_claims,
)


async def _chat(model_type: str | None, messages: list[dict], *, temperature: float = 0, max_tokens: int = 800) -> str:
    from app.providers.factory import get_llm_provider

    return await get_llm_provider(model_type).chat(
        messages, temperature=temperature, max_tokens=max_tokens
    )


async def _verify_claims(
    claims: list[str], sources: list[str], model_type: str | None = None,
) -> list[dict]:
    async def chat(messages, temperature=0, max_tokens=800):
        return await _chat(model_type, messages, temperature=temperature, max_tokens=max_tokens)

    return await _core_verify_claims(chat, claims, sources)


async def judge_hallucination(
    answer: str, sources: list[str], model_type: Optional[str] = None,
) -> dict:
    async def chat(messages, temperature=0, max_tokens=800):
        return await _chat(model_type, messages, temperature=temperature, max_tokens=max_tokens)

    return await _core_hallucination(chat, answer, sources)


async def judge_context_relevance(
    query: str, chunks: list[str], model_type: str | None = None,
) -> dict:
    async def chat(messages, temperature=0, max_tokens=800):
        return await _chat(model_type, messages, temperature=temperature, max_tokens=max_tokens)

    return await _core_context_relevance(chat, query, chunks)


async def judge_answerability(
    query: str, chunks: list[str], model_type: str | None = None,
) -> dict:
    async def chat(messages, temperature=0, max_tokens=800):
        return await _chat(model_type, messages, temperature=temperature, max_tokens=max_tokens)

    return await _core_answerability(chat, query, chunks)


async def judge_completeness(
    query: str, answer: str, model_type: str | None = None,
) -> float:
    async def chat(messages, temperature=0, max_tokens=800):
        return await _chat(model_type, messages, temperature=temperature, max_tokens=max_tokens)

    return await _core_completeness(chat, query, answer)


async def judge_citation(
    answer: str, sources: list[str], model_type: str | None = None,
) -> dict:
    async def chat(messages, temperature=0, max_tokens=800):
        return await _chat(model_type, messages, temperature=temperature, max_tokens=max_tokens)

    return await _core_citation(chat, answer, sources)
