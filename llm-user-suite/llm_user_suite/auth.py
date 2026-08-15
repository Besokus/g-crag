from __future__ import annotations

from dataclasses import dataclass

import httpx
from fastapi import Header, HTTPException

from .config import settings


@dataclass(frozen=True)
class Principal:
    username: str
    role: str
    tenant_id: str


async def current_principal(authorization: str = Header(default="")) -> Principal:
    if settings.AUTH_DISABLED:
        return Principal(username="local-admin", role="admin", tenant_id="default")
    if not authorization.startswith("Bearer ") or not settings.GRID_AUTH_BASE_URL:
        raise HTTPException(status_code=401, detail="missing control-plane authentication")
    try:
        from .replay import validate_target
        auth_target = validate_target(settings.GRID_AUTH_BASE_URL, settings.TARGET_ENVIRONMENT)
        async with httpx.AsyncClient(base_url=auth_target, timeout=10) as client:
            response = await client.get("/api/system/me", headers={"Authorization": authorization})
            response.raise_for_status()
            data = response.json().get("data") or {}
        role = str(data.get("role", ""))
        if role not in {"admin", "auditor"}:
            raise HTTPException(status_code=403, detail="admin or auditor role required")
        tenant_id = str(data.get("tenantId") or "default")[:64]
        return Principal(
            username=str(data.get("username", "unknown")),
            role=role,
            tenant_id=tenant_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"authentication failed: {type(exc).__name__}")


def require_admin(principal: Principal) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="admin role required")
    return principal
