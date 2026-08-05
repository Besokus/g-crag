"""问答链路 trace 落库 + 历史查询（链路诊断页数据源）。

save_trace 走独立 AsyncSessionLocal（bg-task 安全，仿 rewrite_event_service.log，
避 dislike invalidate session 并发 500 教训），采样率 QA_TRACE_SAMPLE_RATE 控制写放大。
响应体里的 trace 不采样（实时展示必带），仅落库采样。
"""
import json
import random

from sqlalchemy import desc, func, select

from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.qa_trace import QaTrace


async def save_trace(trace_dict: dict, *, query: str, tenant: str, username: str,
                     cache_layer: str = "", confidence: str = "") -> None:
    """异步落一条 trace（bg task 调用）。采样 + 独立 session，失败 degraded 不抛。"""
    if random.random() > getattr(settings, "QA_TRACE_SAMPLE_RATE", 1.0):
        return
    try:
        async with AsyncSessionLocal() as db:
            db.add(QaTrace(
                trace_id=trace_dict.get("traceId", "")[:64],
                query=(query or "")[:200],
                tenant=(tenant or "default")[:32],
                username=(username or "")[:64],
                total_ms=float(trace_dict.get("totalMs", 0) or 0),
                bottleneck=(trace_dict.get("bottleneck", "") or "")[:32],
                cache_layer=(cache_layer or "")[:32],
                confidence=(confidence or "")[:32],
                spans_json=json.dumps(trace_dict.get("spans", []), ensure_ascii=False),
            ))
            await db.commit()
    except Exception as e:
        degraded("qa_trace_save", e)


async def list_traces(*, tenant: str = "default", page: int = 1, size: int = 20,
                      slow_ms: float | None = None, bottleneck: str = "",
                      username: str = "") -> dict:
    """分页列表：可过滤慢查询(slow_ms)/瓶颈节点/用户。username="" 查全租户（admin 用）。"""
    try:
        async with AsyncSessionLocal() as db:
            filters = [QaTrace.tenant == (tenant or "default")]
            if username:
                filters.append(QaTrace.username == username)
            if slow_ms:
                filters.append(QaTrace.total_ms >= float(slow_ms))
            if bottleneck:
                filters.append(QaTrace.bottleneck == bottleneck)
            total = (await db.execute(
                select(func.count()).select_from(QaTrace).where(*filters)
            )).scalar() or 0
            rows = (await db.execute(
                select(QaTrace).where(*filters).order_by(desc(QaTrace.ts))
                .offset((max(1, page) - 1) * size).limit(size)
            )).scalars().all()
            return {"total": total, "list": [{
                "traceId": r.trace_id,
                "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
                "query": r.query, "totalMs": r.total_ms,
                "bottleneck": r.bottleneck, "cacheLayer": r.cache_layer,
                "confidence": r.confidence, "username": r.username,
            } for r in rows]}
    except Exception as e:
        degraded("qa_trace_list", e)
        return {"total": 0, "list": []}


async def get_trace(trace_id: str, *, tenant: str = "default") -> dict | None:
    """单条明细：含 spans（to_dict 结构，前端复用 QaTraceChart 渲染瀑布图）。"""
    try:
        async with AsyncSessionLocal() as db:
            r = (await db.execute(
                select(QaTrace).where(QaTrace.trace_id == trace_id,
                                      QaTrace.tenant == (tenant or "default"))
            )).scalar_one_or_none()
            if not r:
                return None
            spans = json.loads(r.spans_json or "[]")
            top = max(spans, key=lambda x: x.get("dur", 0)) if spans else {}
            return {
                "traceId": r.trace_id, "totalMs": r.total_ms,
                "bottleneck": r.bottleneck,
                "bottleneckLabel": top.get("label", ""),
                "marks": {}, "spans": spans,
                "query": r.query,
                "ts": r.ts.strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
                "cacheLayer": r.cache_layer, "confidence": r.confidence,
            }
    except Exception as e:
        degraded("qa_trace_get", e)
        return None
