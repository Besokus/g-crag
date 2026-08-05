"""qa_trace 采集器单测：计时/异常隔离/bottleneck/占比/None 安全。

不依赖 DB 与 event loop，纯逻辑覆盖 TraceCollector。运行：PYTHONPATH=backend pytest tests/test_qa_trace.py
"""
import time

import pytest

from app.core import qa_trace
from app.core.qa_trace import TraceCollector, get_collector, new_collector, span


def test_span_records_duration_and_label():
    c = TraceCollector("q")
    with c.span("retrieval"):
        time.sleep(0.01)
    d = c.to_dict()
    assert len(d["spans"]) == 1
    s = d["spans"][0]
    assert s["name"] == "retrieval"
    assert s["label"] == "混合检索"          # label 映射
    assert s["group"] == "main"
    assert s["dur"] >= 8                      # 约 10ms（放宽机器抖动）
    assert s["status"] == "ok"


def test_span_group_param():
    c = TraceCollector("q")
    with c.span("rerank", group="retrieval"):
        pass
    assert c.to_dict()["spans"][0]["group"] == "retrieval"


def test_span_records_error_and_reraises():
    c = TraceCollector("q")
    with pytest.raises(ValueError):
        with c.span("llm"):
            raise ValueError("boom")
    s = c.to_dict()["spans"][0]
    assert s["status"] == "error"
    assert "ValueError" in s["err"]


def test_bottleneck_is_slowest():
    c = TraceCollector("q")
    with c.span("a"):
        time.sleep(0.004)
    with c.span("b"):
        time.sleep(0.02)
    d = c.to_dict()
    assert d["bottleneck"] == "b"
    assert d["bottleneckLabel"]  # 非空（即使未映射也有 fallback）


def test_mark():
    c = TraceCollector("q")
    c.mark("cacheLayer", "redis")
    c.mark("confidence", "high")
    d = c.to_dict()
    assert d["marks"]["cacheLayer"] == "redis"
    assert d["marks"]["confidence"] == "high"


def test_pct_positive_and_bounded():
    c = TraceCollector("q")
    with c.span("a"):
        time.sleep(0.01)
    with c.span("b"):
        time.sleep(0.01)
    pcts = [s["pct"] for s in c.to_dict()["spans"]]
    assert all(0 < p <= 100 for p in pcts)


def test_empty_to_dict_no_crash():
    d = TraceCollector("q").to_dict()
    assert d["spans"] == []
    assert d["bottleneck"] == ""
    assert d["totalMs"] >= 0


def test_new_collector_binds_contextvar():
    qa_trace._current_collector.set(None)     # 清干净
    c = new_collector("q")
    assert get_collector() is c


def test_module_span_noop_without_collector():
    qa_trace._current_collector.set(None)
    assert get_collector() is None
    with span("x"):                           # 无 collector → no-op，不报错
        time.sleep(0.001)
    # 仍未产生 span（无 collector 可记）
    assert get_collector() is None


def test_module_span_uses_current_collector():
    qa_trace._current_collector.set(None)
    new_collector("q")
    with span("normalize"):                   # 模块函数自动取当前 collector
        pass
    d = get_collector().to_dict()
    assert any(s["name"] == "normalize" for s in d["spans"])
