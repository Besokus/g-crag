"""Agent/MCP/数字孪生共享的最小可信调用上下文。"""
from dataclasses import dataclass
from typing import Mapping


VALID_MEMORY_SCOPES = {"user", "device"}


def normalize_tenant(value: object) -> str:
    """统一租户键；空值只能收敛到 default，不能透传给 provider。"""
    tenant_id = str(value or "default").strip()
    return tenant_id or "default"


def normalize_agent(value: object) -> str:
    """统一 Agent 能力边界；空值收敛到 default。"""
    agent_id = str(value or "default").strip()
    return agent_id or "default"


def normalize_scope(value: object) -> str:
    """Only supported memory scopes may participate in an ownership key."""
    scope = str(value or "user").strip().lower()
    return scope if scope in VALID_MEMORY_SCOPES else "user"


def explicit_enabled(value: object) -> bool:
    """解析显式 opt-in 开关，避免非空字符串如 "false" 被当成 True。"""
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "enabled", "write"}
    return False


@dataclass(frozen=True)
class CapabilityContext:
    """可注册能力的可信边界，不包含用户可覆盖的工具参数。"""

    tenant_id: str = "default"
    actor_id: str = ""
    agent_id: str = ""
    role: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", normalize_tenant(self.tenant_id))
        object.__setattr__(self, "actor_id", str(self.actor_id or "").strip())
        object.__setattr__(self, "agent_id", normalize_agent(self.agent_id))
        object.__setattr__(self, "role", str(self.role or "").strip())
        object.__setattr__(self, "request_id", str(self.request_id or "").strip())

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object] | None,
        *,
        trusted_agent_id: str = "",
    ) -> "CapabilityContext":
        data = value or {}
        return cls(
            tenant_id=data.get("tenant") or data.get("tenant_id") or "default",
            actor_id=data.get("username") or data.get("actor_id") or "",
            agent_id=trusted_agent_id or str(data.get("agent_id") or data.get("persona") or "default"),
            role=str(data.get("role") or ""),
            request_id=str(data.get("request_id") or data.get("trace_id") or ""),
        )

    def memory_write_enabled(self, raw_ctx: Mapping[str, object] | None) -> bool:
        """单次调用必须通过 ctx.memoryWrite/memory_write 显式授权写长期记忆。"""
        data = raw_ctx or {}
        return explicit_enabled(data.get("memoryWrite")) or explicit_enabled(data.get("memory_write"))

    def memory_read_enabled(self, raw_ctx: Mapping[str, object] | None) -> bool:
        """memoryWrite consent includes read; otherwise read needs its own opt-in."""
        data = raw_ctx or {}
        return (
            self.memory_write_enabled(data)
            or explicit_enabled(data.get("memoryRead"))
            or explicit_enabled(data.get("memory_read"))
        )
