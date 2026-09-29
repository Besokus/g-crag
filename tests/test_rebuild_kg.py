"""scripts/rebuild_kg.py 的断点续跑与失败重试逻辑（数据库/LLM 相关函数均替换为假实现）。"""
import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "rebuild_kg.py"


@pytest.fixture
def rk(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("rebuild_kg_under_test", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    calls = {"clear": 0, "extract": [], "degree": 0}
    mod._calls = calls
    mod._fail = set()

    async def clear():
        calls["clear"] += 1

    async def docs():
        return [("d1", "文档1"), ("d2", "文档2"), ("d3", "文档3")]

    async def extract_one(doc_id, lock):
        calls["extract"].append(doc_id)
        if doc_id in mod._fail:
            raise RuntimeError("LLM 超时")
        return {"triples": 10, "provenance": {"verified": 8, "rematched": 1, "none": 1},
                "rejectedBatches": 1 if doc_id == "d3" else 0}

    async def refresh(names=None):
        calls["degree"] += 1

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(mod, "_clear_graph", clear)
    monkeypatch.setattr(mod, "_docs_with_chunks", docs)
    monkeypatch.setattr(mod, "_extract_one", extract_one)
    monkeypatch.setattr(mod.neo4j_client, "refresh_degree", refresh)
    monkeypatch.setattr(mod.neo4j_client, "close", noop)
    monkeypatch.setattr(mod, "_embed_entities", noop)
    monkeypatch.setattr(mod, "_export_sample", noop)
    return mod


def _run(mod, *argv):
    old = sys.argv
    sys.argv = ["rebuild_kg.py", *argv]
    try:
        return asyncio.run(mod.main())
    finally:
        sys.argv = old


def test_fresh_build_completes(rk):
    assert _run(rk) == 0
    st = rk._load_state(rk.get_pack().name)
    assert st["finished"] and set(st["done"]) == {"d1", "d2", "d3"} and st["failed"] == {}
    assert rk._calls["clear"] == 1
    assert "with_chunk=90.0%" in rk._summary(st)
    assert "rejected_batches=1" in rk._summary(st)   # 被审核拒绝的批次算完成，但在汇总中列出


def test_finished_build_refuses_without_fresh(rk):
    _run(rk)
    assert _run(rk) == 2
    assert rk._calls["clear"] == 1
    assert _run(rk, "--fresh") == 0
    assert rk._calls["clear"] == 2


def test_failures_are_retried_on_resume_without_clearing(rk):
    rk._fail = {"d2"}
    assert _run(rk) == 1
    st = rk._load_state(rk.get_pack().name)
    assert set(st["done"]) == {"d1", "d3"} and "d2" in st["failed"] and not st["finished"]
    rk._fail = set()
    rk._calls["extract"].clear()
    assert _run(rk) == 0
    assert rk._calls["extract"] == ["d2"]          # 只重试失败的那篇
    assert rk._calls["clear"] == 1                 # 续跑不清空
    assert rk._load_state(rk.get_pack().name)["finished"]


def test_limit_processes_partial_and_resumes(rk):
    assert _run(rk, "--limit", "2") == 0
    st = rk._load_state(rk.get_pack().name)
    assert len(st["done"]) == 2 and not st["finished"]
    assert _run(rk) == 0
    assert rk._calls["extract"] == ["d1", "d2", "d3"]
