"""问答链路 trace 表：每次问答各阶段耗时明细，供链路诊断页查历史。

写库走 _fire_and_forget + 独立 AsyncSessionLocal（bg-task 安全，仿 rewrite_event_service.log，
避 dislike invalidate session 并发 500 教训），采样率 QA_TRACE_SAMPLE_RATE 控制写放大。
响应体里的 trace 不采样（实时展示必带），仅落库采样。
"""
from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QaTrace(Base):
    __tablename__ = "qa_trace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    query: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    tenant: Mapped[str] = mapped_column(String(32), default="default", index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    total_ms: Mapped[float] = mapped_column(Float, default=0.0)
    bottleneck: Mapped[str] = mapped_column(String(32), default="", index=True)
    cache_layer: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[str] = mapped_column(String(32), default="")
    spans_json: Mapped[str] = mapped_column(Text, default="[]")  # to_dict().spans 的 JSON
