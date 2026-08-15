"""Optional semantic telemetry probe for the external LLM-as-a-User suite.

The probe emits redacted OpenTelemetry span events.  It never sends credentials and is
strictly fail-open: telemetry failures must not change a business response.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from contextlib import contextmanager
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from app.config import settings

_SENSITIVE_KEYS = ("password", "passwd", "authorization", "cookie", "token", "secret", "api_key", "apikey", "jwt")
_TOKEN_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(password|passwd|token|secret|api[_ -]?key|authorization|cookie)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+"),
    re.compile(r"(?i)\b(?:sk|ak)-[a-z0-9_-]{12,}\b"),
)
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")


def enabled() -> bool:
    return bool(
        getattr(settings, "LLM_USER_OBSERVER_ENABLED", False)
        and getattr(settings, "LLM_USER_OBSERVER_USER_HASH_SECRET", "")
    )


def user_hash(value: str) -> str:
    if not value:
        return "anonymous"
    secret = settings.LLM_USER_OBSERVER_USER_HASH_SECRET
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def _redact_text(value: str, limit: int = 4000) -> str:
    text = str(value or "")[:limit]
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    return _ID_CARD.sub("[ID_CARD]", text)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            normalized = str(key).lower().replace("-", "_")
            if any(secret in normalized for secret in _SENSITIVE_KEYS):
                result[str(key)[:64]] = "[REDACTED]"
            else:
                result[str(key)[:64]] = _sanitize(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _redact_text(str(value))


def _flatten(payload: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in list((payload or {}).items())[:80]:
        normalized = str(key).lower().replace("-", "_")
        if any(secret in normalized for secret in _SENSITIVE_KEYS):
            continue
        attr = f"grid.event.payload.{str(key)[:64]}"
        if isinstance(value, bool):
            result[attr] = value
        elif isinstance(value, (int, float)):
            result[attr] = value
        elif isinstance(value, str):
            result[attr] = _redact_text(value)
        elif value is not None:
            try:
                result[attr] = _redact_text(json.dumps(_sanitize(value), ensure_ascii=False, default=str))
            except Exception:
                result[attr] = _redact_text(str(value))
    return result


def _set(span, key: str, value: Any) -> None:
    if value is not None and value != "":
        span.set_attribute(key, value if isinstance(value, (str, int, float, bool)) else str(value))


@contextmanager
def http_server_span(request):
    if not enabled():
        yield None
        return
    tracer = trace.get_tracer("grid-qa.llm-user-probe")
    carrier = {key: value for key, value in request.headers.items()}
    context = propagate.extract(carrier)
    with tracer.start_as_current_span(
        f"{request.method} {request.url.path}", context=context, kind=SpanKind.SERVER,
        attributes={
            "http.request.method": request.method,
            "url.path": request.url.path,
            "server.address": request.url.hostname or "",
            "grid.session.id": request.headers.get("x-grid-session-id", ""),
        },
    ) as span:
        yield span


def finish_http(span, *, status_code: int, duration_ms: float, error: BaseException | None = None) -> str:
    if span is None:
        return ""
    _set(span, "http.response.status_code", status_code)
    _set(span, "grid.duration_ms", round(duration_ms, 3))
    if error is not None:
        span.record_exception(error)
        span.set_status(Status(StatusCode.ERROR, str(error)))
    elif status_code >= 500:
        span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))
    context = span.get_span_context()
    return format(context.trace_id, "032x") if context and context.trace_id else ""


def bind_identity(*, username: str = "", tenant: str = "default", conversation_id: str = "", qa_trace_id: str = "") -> None:
    if not enabled():
        return
    span = trace.get_current_span()
    if not span or not span.is_recording():
        return
    _set(span, "grid.user.hash", user_hash(username))
    _set(span, "grid.tenant.id", tenant or "default")
    _set(span, "grid.conversation.id", conversation_id)
    _set(span, "grid.qa_trace.id", qa_trace_id)


def emit(kind: str, payload: dict[str, Any] | None = None, *, username: str = "", tenant: str = "default", conversation_id: str = "", qa_trace_id: str = "") -> None:
    if not enabled():
        return
    attrs = {
        "grid.event.id": uuid.uuid4().hex,
        "grid.user.hash": user_hash(username),
        "grid.tenant.id": tenant or "default",
        "grid.conversation.id": conversation_id or "",
        "grid.qa_trace.id": qa_trace_id or "",
        **_flatten(payload),
    }
    try:
        span = trace.get_current_span()
        if span and span.is_recording():
            span.set_attribute("grid.event.kind", kind)
            span.add_event(f"grid.user.{kind}", attributes=attrs)
            try:
                from app.core import metrics
                metrics.LLM_USER_OBSERVER_EVENTS.labels("emitted").inc()
            except Exception:
                pass
            return
        tracer = trace.get_tracer("grid-qa.llm-user-probe")
        with tracer.start_as_current_span(f"grid.user.{kind}", kind=SpanKind.INTERNAL, attributes={"grid.event.kind": kind, **attrs}):
            pass
        try:
            from app.core import metrics
            metrics.LLM_USER_OBSERVER_EVENTS.labels("emitted").inc()
        except Exception:
            pass
    except Exception:
        try:
            from app.core import metrics
            metrics.LLM_USER_OBSERVER_EVENTS.labels("dropped").inc()
        except Exception:
            pass
        return
