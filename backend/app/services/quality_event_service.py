"""质量事件管理查询层，复用现有 QualityEvent 表。"""
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.obs import degraded
from app.models.quality_event import QualityEvent
from app.schemas.quality_event import normalize_quality_payload


STATUS_ALIASES = {
    "open": ("open", "pending", "failed"),
    "processing": ("processing",),
    "resolved": ("resolved", "handled"),
    "ignored": ("ignored",),
}
ALLOWED_TRANSITIONS = {
    "open": {"open", "processing", "resolved", "ignored"},
    "processing": {"open", "processing", "resolved", "ignored"},
    "resolved": {"open", "resolved"},
    "ignored": {"open", "ignored"},
}


class InvalidQualityEventTransition(ValueError):
    pass


def canonical_status(status: str | None) -> str:
    value = str(status or "pending").strip().lower()
    for canonical, aliases in STATUS_ALIASES.items():
        if value in aliases:
            return canonical
    return "open"


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


async def list_events(
    db: AsyncSession,
    *,
    tenant_id: str,
    event_type: str = "",
    status: str = "",
    source: str = "",
    trace_id: str = "",
    conversation_id: str = "",
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    page: int = 1,
    size: int = 20,
) -> tuple[int, list[QualityEvent]]:
    filters = [QualityEvent.tenant == tenant_id]
    if event_type:
        filters.append(QualityEvent.type == event_type)
    if status:
        filters.append(QualityEvent.status.in_(STATUS_ALIASES.get(status, (status,))))
    if source:
        filters.append(QualityEvent.source == source)
    if trace_id:
        filters.append(or_(
            QualityEvent.payload["trace_id"].as_string() == trace_id,
            QualityEvent.payload["traceId"].as_string() == trace_id,
            QualityEvent.payload["trace"].as_string() == trace_id,
        ))
    if conversation_id:
        filters.append(or_(
            QualityEvent.payload["conversation_id"].as_string() == conversation_id,
            QualityEvent.payload["conversationId"].as_string() == conversation_id,
        ))
    if start_at:
        filters.append(QualityEvent.created_at >= _naive_utc(start_at))
    if end_at:
        filters.append(QualityEvent.created_at <= _naive_utc(end_at))

    total = (await db.execute(
        select(func.count(QualityEvent.id)).where(*filters)
    )).scalar_one()
    rows = (await db.execute(
        select(QualityEvent)
        .where(*filters)
        .order_by(QualityEvent.created_at.desc(), QualityEvent.id.desc())
        .offset((max(1, page) - 1) * size)
        .limit(size)
    )).scalars().all()
    return int(total), list(rows)


async def get_event(
    db: AsyncSession, event_id: str, *, tenant_id: str
) -> QualityEvent | None:
    return (await db.execute(
        select(QualityEvent).where(
            QualityEvent.id == event_id,
            QualityEvent.tenant == tenant_id,
        )
    )).scalar_one_or_none()


async def update_status(
    db: AsyncSession,
    event_id: str,
    *,
    tenant_id: str,
    status: str,
    operator: str,
    note: str = "",
) -> QualityEvent | None:
    row = (await db.execute(
        select(QualityEvent)
        .where(
            QualityEvent.id == event_id,
            QualityEvent.tenant == tenant_id,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        return None
    current = canonical_status(row.status)
    if status not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidQualityEventTransition(f"{current} 不能流转到 {status}")

    now = datetime.now(UTC).replace(tzinfo=None)
    payload = dict(row.payload or {})
    management = dict(payload.get("management") or {})
    history = list(management.get("history") or [])
    if status != current:
        history.append({
            "from": current,
            "to": status,
            "operator": operator,
            "note": note,
            "at": now.isoformat(),
        })
    management.update({
        "operator": operator,
        "note": note,
        "updated_at": now.isoformat(),
        "history": history,
    })
    payload["management"] = management
    row.payload = payload
    row.status = status
    row.handled_at = now if status in {"resolved", "ignored"} else None
    try:
        await db.commit()
        await db.refresh(row)
    except Exception as exc:
        await db.rollback()
        degraded("quality_event_status_update", exc, f"event_id={event_id}")
        raise
    return row


async def stats(db: AsyncSession, *, tenant_id: str) -> dict:
    status_rows = (await db.execute(
        select(QualityEvent.status, func.count(QualityEvent.id))
        .where(QualityEvent.tenant == tenant_id)
        .group_by(QualityEvent.status)
    )).all()
    counts = {key: 0 for key in STATUS_ALIASES}
    for raw_status, count in status_rows:
        counts[canonical_status(raw_status)] += int(count)
    counts["total"] = sum(counts.values())

    source_rows = (await db.execute(
        select(QualityEvent.source, func.count(QualityEvent.id))
        .where(QualityEvent.tenant == tenant_id)
        .group_by(QualityEvent.source)
        .order_by(func.count(QualityEvent.id).desc())
    )).all()
    type_rows = (await db.execute(
        select(QualityEvent.type, func.count(QualityEvent.id))
        .where(QualityEvent.tenant == tenant_id)
        .group_by(QualityEvent.type)
        .order_by(func.count(QualityEvent.id).desc())
    )).all()
    return {
        "counts": counts,
        "sources": [{"value": str(value), "count": int(count)} for value, count in source_rows],
        "eventTypes": [{"value": str(value), "count": int(count)} for value, count in type_rows],
    }


def event_to_dict(event: QualityEvent) -> dict:
    payload = normalize_quality_payload(event.payload, event.tenant)
    management = dict(payload.get("management") or {})
    return {
        "id": event.id,
        "tenantId": event.tenant,
        "source": event.source,
        "eventType": event.type,
        "status": canonical_status(event.status),
        "rawStatus": event.status,
        "traceId": payload["trace_id"],
        "conversationId": payload["conversation_id"],
        "query": payload["query"],
        "answer": payload["answer"],
        "reason": payload["reason"],
        "sources": payload["sources"],
        "user": payload["user"],
        "management": management,
        "note": str(management.get("note") or ""),
        "payload": payload,
        "createdAt": event.created_at.isoformat() if event.created_at else None,
        "handledAt": event.handled_at.isoformat() if event.handled_at else None,
    }
