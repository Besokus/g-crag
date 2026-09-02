"""N3 数字孪生 API（多站点目录/布局/设备详情/告警订阅 WebSocket）。

安全：HTTP 走 RBAC（DOMAIN_USE 读 / ALERT_MANAGE 推送）；WS 先验 token 再订阅，
站点订阅参数透传 ws_manager 做按站广播隔离。
"""
from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from fastapi import Request

from app.core.permissions import ALERT_MANAGE, DOMAIN_USE, has_perm
from app.core.response import BizError, success
from app.core.security import decode_token
from app.core.ws_manager import (
    connect_twin,
    disconnect_twin,
    twin_client_count,
)
from app.db.session import AsyncSessionLocal
from app.dependencies import require_perm
from app.services import twin_service
from app.services.auth_service import get_user_by_id

router = APIRouter(prefix="/twin", tags=["数字孪生"])


@router.get("/stations")
async def stations(user=Depends(require_perm(DOMAIN_USE))):
    """多站点目录：配置站点 + 同目录兄弟布局文件。"""
    data = await twin_service.list_stations()
    return success(data=data)


@router.get("/station/layout")
async def station_layout(
    stationId: str = Query("", description="站点 ID（空=默认站点）"),
    user=Depends(require_perm(DOMAIN_USE)),
):
    """获取站点布局模板（areas + devices 坐标）。"""
    try:
        data = await twin_service.get_station_layout(stationId)
    except twin_service.TwinStationError as exc:
        raise BizError(str(exc), 404)
    return success(data=data)


@router.get("/station/overview")
async def station_overview(
    stationId: str = Query("", description="站点 ID（空=默认站点）"),
    user=Depends(require_perm(DOMAIN_USE)),
):
    """获取站点总览：设备列表 + 各设备状态（riskScore/颜色/告警/闪烁）。"""
    try:
        data = await twin_service.get_station_overview(stationId)
    except twin_service.TwinStationError as exc:
        raise BizError(str(exc), 404)
    return success(data=data)


@router.get("/device/{device_id}/detail")
async def device_detail(
    device_id: str,
    stationId: str = Query("", description="站点 ID（空=默认站点）"),
    user=Depends(require_perm(DOMAIN_USE)),
):
    """获取设备详情：风险/知识图谱上下文/故障传播链/告警。"""
    try:
        data = await twin_service.get_device_detail(device_id, station_id=stationId or None)
    except twin_service.TwinStationError as exc:
        raise BizError(str(exc), 404)
    if data.get("error"):
        raise BizError(data["error"], 404)
    return success(data=data)


@router.get("/device/{device_id}/fault-chain")
async def device_fault_chain(
    device_id: str,
    depth: int = Query(3, ge=1, le=5, description="传播链深度"),
    stationId: str = Query("", description="站点 ID（空=默认站点）"),
    user=Depends(require_perm(DOMAIN_USE)),
):
    """获取设备故障传播链（多跳路径）。"""
    try:
        data = await twin_service.get_fault_chain(
            device_id, depth, station_id=stationId or None
        )
    except twin_service.TwinStationError as exc:
        raise BizError(str(exc), 404)
    return success(data=data)


@router.post("/alert/push")
async def push_alert(
    request: Request,
    stationId: str = Query("", description="站点 ID（空=默认站点）"),
    user=Depends(require_perm(ALERT_MANAGE)),
):
    """告警定位推送（alert_disposal → twin → WS 按站点广播）。

    Body: {severity, title, device/deviceId, summary}
    """
    body = await request.json()
    data = await twin_service.push_alert_location(body, station_id=stationId or None)
    return success(data=data)


@router.websocket("/ws/twin")
async def twin_ws(ws: WebSocket):
    """数字孪生 WebSocket：token 校验 → 站点订阅 → 告警/刷新推送。

    query: token（JWT，必填）+ stationId（空=默认站点）。
    校验失败：accept 后回 error 帧并 1008 关闭（不进订阅集合）；
    成功：connect_twin 内部 accept 并按站点入订阅集合。
    """
    token = ws.query_params.get("token", "")
    station_id = ws.query_params.get("stationId", "")
    try:
        payload = decode_token(token)
    except Exception:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "无效 token"})
        await ws.close(code=1008)
        return

    user_id = str((payload or {}).get("sub") or "")
    role = ""
    try:
        async with AsyncSessionLocal() as db:
            user = await get_user_by_id(db, user_id)
            role = getattr(user, "role", "") or ""
    except Exception:
        role = ""
    if not has_perm(role, DOMAIN_USE):
        await ws.accept()
        await ws.send_json({"type": "error", "message": "无数字孪生访问权限"})
        await ws.close(code=1008)
        return

    try:
        layout = await twin_service.get_station_layout(station_id)
        station_id = str(layout.get("stationId") or station_id)
    except twin_service.TwinStationError as exc:
        await ws.accept()
        await ws.send_json({"type": "error", "message": str(exc)})
        await ws.close(code=1008)
        return

    await connect_twin(ws, station_id)
    try:
        await ws.send_json({
            "type": "ready",
            "stationId": station_id,
            "clients": twin_client_count(),
        })
        while True:
            await ws.receive_text()  # 保持连接（忽略客户端消息）
    except WebSocketDisconnect:
        pass
    finally:
        disconnect_twin(ws)
