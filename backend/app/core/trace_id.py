"""问答链路 trace ID 的兼容解析与生成。"""
import re
import uuid


_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,63}$")


def valid_trace_id(value: object) -> str:
    """返回合法 trace ID；非法或空值返回空串。"""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _TRACE_ID_PATTERN.fullmatch(candidate) else ""


def resolve_trace_id(*candidates: object) -> str:
    """优先复用调用方 trace ID，否则复用当前 OTel trace，最后生成 UUID。"""
    for candidate in candidates:
        normalized = valid_trace_id(candidate)
        if normalized:
            return normalized

    try:
        from app.core.otel_genai import get_trace_id

        current = valid_trace_id(get_trace_id())
        if current:
            return current
    except Exception:
        pass
    return uuid.uuid4().hex
