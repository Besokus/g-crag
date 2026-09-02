"""质量事件中心管理 API。"""
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import AUDIT_READ, FEEDBACK_MANAGE, SYSTEM_CONFIG, has_perm
from app.core.response import BizError, success
from app.db.session import get_db
from app.dependencies import get_current_user, require_perm
from app.models.user import User
from app.schemas.quality_event import QualityEventStatusUpdate
from app.services import quality_event_service
from app.services.quality_event_service import InvalidQualityEventTransition


router = APIRouter(prefix="/system/quality-events", tags=["质量事件中心"])


async def require_quality_event_manager(
    user: User = Depends(get_current_user),
) -> User:
    """质量事件状态管理：反馈治理员或系统管理员可操作。"""
    if has_perm(user.role, FEEDBACK_MANAGE) or has_perm(user.role, SYSTEM_CONFIG):
        return user
    raise BizError(f"无权限：需要 {FEEDBACK_MANAGE} 或 {SYSTEM_CONFIG}", 403)


@router.get("")
async def list_quality_events_api(
    status: str = Query("", max_length=16),
    source: str = Query("", max_length=32),
    type_: str = Query("", alias="type", max_length=32),
    eventType: str = Query("", max_length=32),
    traceId: str = Query("", max_length=128),
    conversationId: str = Query("", max_length=128),
    startAt: datetime | None = Query(default=None),
    endAt: datetime | None = Query(default=None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    if startAt and endAt and startAt > endAt:
        raise BizError("开始时间不能晚于结束时间", 400)

    total, rows = await quality_event_service.list_events(
        db,
        tenant_id=user.tenant_id,
        event_type=(type_ or eventType).strip(),
        status=status,
        source=source.strip(),
        trace_id=traceId.strip(),
        conversation_id=conversationId.strip(),
        start_at=startAt,
        end_at=endAt,
        page=page,
        size=size,
    )
    return success(
        {
            "total": total,
            "page": page,
            "size": size,
            "list": [quality_event_service.event_to_dict(row) for row in rows],
        },
        "查询成功",
    )


@router.get("/stats")
async def quality_event_stats_api(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    return success(
        await quality_event_service.stats(db, tenant_id=user.tenant_id),
        "查询成功",
    )


@router.get("/{event_id}")
async def quality_event_detail_api(
    event_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_perm(AUDIT_READ)),
):
    event = await quality_event_service.get_event(
        db, event_id, tenant_id=user.tenant_id
    )
    if not event:
        raise BizError("质量事件不存在", 404)
    return success(quality_event_service.event_to_dict(event), "查询成功")


@router.patch("/{event_id}/status")
async def update_quality_event_status_api(
    event_id: str,
    body: QualityEventStatusUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_quality_event_manager),
):
    try:
        event = await quality_event_service.update_status(
            db,
            event_id,
            tenant_id=user.tenant_id,
            status=body.status,
            operator=user.username,
            note=body.note,
        )
    except InvalidQualityEventTransition as exc:
        raise BizError(str(exc), 409)
    if not event:
        raise BizError("质量事件不存在", 404)
    return success(quality_event_service.event_to_dict(event), "状态已更新")
