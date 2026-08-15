from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.core.response import success
from app.events.registry import publish_event
from app.schemas.llm_user import LlmUserEvaluationEvent

router = APIRouter(prefix="/integrations/llm-user", tags=["LLM User评测接入"])


def _verify(raw: bytes, timestamp: str, signature: str) -> None:
    secret = settings.LLM_USER_CALLBACK_SECRET
    if not secret:
        raise HTTPException(status_code=503, detail="LLM User callback secret 未配置")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="回调时间戳无效")
    if abs(int(time.time()) - ts) > settings.LLM_USER_CALLBACK_MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="回调已超出重放窗口")
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + raw, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature or "", expected):
        raise HTTPException(status_code=403, detail="回调签名无效")


@router.post("/evaluations")
async def evaluation_callback(
    request: Request,
    x_llm_user_timestamp: str = Header(default="", alias="X-LLM-User-Timestamp"),
    x_llm_user_signature: str = Header(default="", alias="X-LLM-User-Signature"),
):
    raw = await request.body()
    _verify(raw, x_llm_user_timestamp, x_llm_user_signature)
    try:
        body = LlmUserEvaluationEvent.model_validate(json.loads(raw))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"评测事件格式无效: {type(exc).__name__}")
    event_id = await publish_event(
        "llm_user.eval.completed", body.model_dump(mode="json"),
        source="llm-user-suite", aggregate_type="llm_user_run", aggregate_id=body.runId,
        tenant_id=body.tenantId, idempotency_key=body.eventId,
        correlation_id=body.runId,
    )
    return success({"eventId": event_id, "accepted": True}, "评测事件已接收")
