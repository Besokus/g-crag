"""质量事件管理与 dislike 载荷共享契约。"""
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field


class QualityEventStatusUpdate(BaseModel):
    status: Literal["open", "processing", "resolved", "ignored"]
    note: str = Field(default="", max_length=1000)


def _pick(data: dict, *keys: str, default=""):
    for key in keys:
        if data.get(key) not in (None, ""):
            return data[key]
    return default


def normalize_sources(value: Any) -> list[dict]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, list):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, str):
            result.append({"doc": item, "chunk": "", "score": None})
            continue
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        row.update({
            "doc": str(_pick(row, "doc", "docName", "doc_name", "title")),
            "chunk": str(_pick(row, "chunk", "chunkText", "chunk_text", "content")),
            "score": _pick(row, "score", "rerankScore", "rerank_score", default=None),
        })
        result.append(row)
    return result


def normalize_quality_payload(payload: dict | None, tenant: str = "default") -> dict:
    """兼容历史字段缺失、camelCase 和旧 retrievalSources 字符串。"""
    raw = dict(payload or {})
    user = raw.get("user") if isinstance(raw.get("user"), Mapping) else {}
    normalized = dict(raw)
    normalized.update({
        "trace_id": str(_pick(raw, "trace_id", "traceId", "trace")),
        "conversation_id": str(_pick(raw, "conversation_id", "conversationId")),
        "query": str(_pick(raw, "query", "q", "question")),
        "answer": str(_pick(raw, "answer", "response")),
        "reason": str(_pick(raw, "reason", "feedbackReason", "feedback_reason")),
        "sources": normalize_sources(_pick(raw, "sources", "retrievalSources", "retrieval_sources", default=[])),
        "tenant": str(_pick(raw, "tenant", "tenantId", "tenant_id", default=tenant)),
        "user": {
            **dict(user),
            "id": str(_pick(dict(user), "id", "userId", "user_id", default=_pick(raw, "userId", "user_id"))),
            "username": str(_pick(dict(user), "username", "name", default=_pick(raw, "username", "userName"))),
        },
    })
    return normalized


def build_dislike_payload(**values: Any) -> dict:
    """生产端 helper：输出 snake_case 契约，额外字段原样保留。"""
    return normalize_quality_payload(values, str(values.get("tenant") or "default"))
