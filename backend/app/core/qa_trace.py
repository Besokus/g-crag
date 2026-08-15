"""问答全链路 trace 采集器（per-request，前端瀑布图可视化数据源）。

自建轻量 span 采集：各阶段 ``with span("name")`` 记 (start, dur, status)，随响应返回
前端做瀑布图+明细表，定位"某次问答卡在哪个节点"；同步可落 qa_trace 表供历史诊断页。
复用 ``otel_genai.trace_span`` 做 Langfuse 兜底归档（双写，互不依赖）。

设计要点（规避两个真实坑）：
1. contextvar 跨 ``asyncio.to_thread`` 丢失：``mixed_search`` 把 milvus 检索丢线程池，
   contextvar 不会自动传到线程内。对策——contextvar 只挂 collector 单对象引用，asyncio.gather
   同 loop 自动继承；含 to_thread 的函数（如 mixed_search）入口先 ``get_collector()`` 取出
   collector，内部 async 阶段直接用 ``collector.span()``，to_thread 的同步检索在外层 async
   包一个整体 span，不深入线程内部。
2. 全程异常隔离：trace 任何失败绝不影响主问答链路。``get_collector()`` 返回 None 时
   ``span()`` 返回 no-op contextmanager；span 内部 finally 记录再 try/except 兜底。
   业务异常照常向主链路传播（trace 不改变异常语义，只顺带记 status=error）。
"""
import contextvars
import time
import uuid
from contextlib import contextmanager
from typing import Any

_current_collector: contextvars.ContextVar["TraceCollector | None"] = contextvars.ContextVar(
    "qa_trace_collector", default=None
)

# 阶段 key → 中文 label（前端展示用；key 稳定不可改，label 可调）
_LABELS: dict[str, str] = {
    # 主链路（group=main）
    "normalize": "词表归一", "safety": "安全检查", "self_rag": "Self-RAG 路由",
    "hotqa": "高频问答命中", "cache_lookup": "三级缓存查询",
    "standalone_rewrite": "多轮指代消解", "routing": "智能路由",
    "retrieval": "混合检索", "crag": "CRAG 自纠错", "graphrag": "GraphRAG 融合",
    "llm": "LLM 生成", "citation": "引用校验",
    # 检索子链（group=retrieval）
    "query_rewrite": "查询改写", "hyde": "HyDE 假设文档",
    "embedding": "向量化", "dense_search": "向量检索(双路)",
    "sparse_search": "BM25 检索", "rrf": "RRF 融合", "rerank": "重排",
    "filter_acl": "过滤/权限", "mmr": "MMR 多样性",
    "small_to_big": "父块召回", "raptor": "RAPTOR 摘要", "governance": "治理门禁",
}


class TraceCollector:
    """单次问答的 trace 容器。请求入口 new_collector() 创建并绑 contextvar。"""

    def __init__(self, query: str = "", trace_id: str = ""):
        self.trace_id: str = trace_id or uuid.uuid4().hex
        self.query: str = (query or "")[:200]
        self.t0: float = time.time()
        self._spans: list[dict] = []
        self.marks: dict[str, Any] = {}

    @contextmanager
    def span(self, name: str, group: str = "main", **attrs):
        """记一段耗时（ms）。业务异常照常抛给主链路，finally 顺带记 span status。"""
        t0 = time.time()
        status, err = "ok", None
        try:
            yield self
        except Exception as e:
            status, err = "error", f"{type(e).__name__}: {e}"
            raise
        finally:
            try:
                rec: dict = {
                    "name": name, "label": _LABELS.get(name, name),
                    "group": group, "dur": round((time.time() - t0) * 1000, 1),
                    "status": status,
                }
                if attrs:
                    rec["attrs"] = attrs
                if err:
                    rec["err"] = err[:200]
                self._spans.append(rec)
            except Exception:
                pass  # 记 span 失败绝不影响主链路

    def mark(self, key: str, value: Any) -> None:
        """无耗时的标记（cacheLayer / route / confidence 等），进 trace 顶层 marks。"""
        try:
            self.marks[key] = value
        except Exception:
            pass

    def record(self, name: str, dur_s: float, group: str = "main",
               status: str = "ok", **attrs) -> None:
        """手动记一段已测耗时（秒→毫秒）；用于 with 难包裹的阶段（如 LLM 的 if/else 块）。

        span() 是自动计时的上下文管理器，record() 由调用方传入已测 dur_s，二者互补。
        """
        try:
            rec: dict = {
                "name": name, "label": _LABELS.get(name, name), "group": group,
                "dur": round(float(dur_s) * 1000, 1), "status": status,
            }
            if attrs:
                rec["attrs"] = attrs
            self._spans.append(rec)
        except Exception:
            pass

    def to_dict(self) -> dict:
        """输出前端瀑布图数据。pct 基于 totalMs；bottleneck=dur 最大的节点。"""
        total_ms = round((time.time() - self.t0) * 1000, 1)
        spans = [dict(s) for s in self._spans]  # 浅拷贝，避免改到已落库对象
        # pct 相对"已打点耗时之和"(sum=100%)，而非总响应时间：避免某阶段未打点时
        # 其余节点占比被稀释（如 stream 路径 LLM 未打点 → 看不出瓶颈）。totalMs 仍单独返回供参考。
        sum_dur = sum(s.get("dur", 0) for s in spans)
        for s in spans:
            s["pct"] = round(s.get("dur", 0) / sum_dur * 100, 1) if sum_dur > 0 else 0.0
        bottleneck = max(spans, key=lambda x: x.get("dur", 0))["name"] if spans else ""
        return {
            "traceId": self.trace_id,
            "totalMs": total_ms,
            "bottleneck": bottleneck,
            "bottleneckLabel": _LABELS.get(bottleneck, bottleneck),
            "marks": dict(self.marks),
            "spans": spans,
        }


def new_collector(query: str = "") -> TraceCollector:
    """请求入口调用：创建 collector 并绑到当前 contextvar。"""
    trace_id = ""
    try:
        from app.core.otel_genai import get_trace_id
        trace_id = get_trace_id()
    except Exception:
        pass
    c = TraceCollector(query, trace_id=trace_id)
    _current_collector.set(c)
    return c


def bind_collector(c: TraceCollector) -> None:
    """把已有 collector 绑到当前上下文（跨任务边界手动接续时用）。"""
    _current_collector.set(c)


def get_collector() -> TraceCollector | None:
    """取当前请求的 collector（取不到返回 None，调用方自行判空）。"""
    return _current_collector.get()


@contextmanager
def _noop():
    yield None


def span(name: str, group: str = "main", **attrs):
    """模块级便捷打点：自动取当前 collector；无 collector 时 no-op（``with span("x"):``）。

    含 to_thread 的函数（如 mixed_search）：不要用本函数（contextvar 跨线程丢），
    应在入口 ``c = get_collector()`` 取出后直接用 ``c.span(...)``。
    """
    c = _current_collector.get()
    if c is None:
        return _noop()
    return c.span(name, group=group, **attrs)
