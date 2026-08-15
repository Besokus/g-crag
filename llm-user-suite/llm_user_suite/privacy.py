from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any

from .config import settings

_TOKEN_PATTERNS = [
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"(?i)eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+"),
]
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_CARD = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")


def hash_user(value: str) -> str:
    if not value or not settings.USER_HASH_SECRET:
        return "anonymous"
    return hmac.new(
        settings.USER_HASH_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def redact_text(value: str, limit: int = 8000) -> str:
    text = str(value or "")[:limit]
    for pattern in _TOKEN_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = _PHONE.sub("[PHONE]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _ID_CARD.sub("[ID_CARD]", text)
    return text


def _is_sensitive_key(key: str) -> bool:
    key = key.lower().replace("-", "_")
    return any(item in key for item in settings.REDACT_FIELDS)


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(k)[:128]: "[REDACTED]" if _is_sensitive_key(str(k)) else redact(v, depth=depth + 1)
            for k, v in list(value.items())[:200]
        }
    if isinstance(value, list):
        return [redact(v, depth=depth + 1) for v in value[:200]]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))
