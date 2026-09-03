"""A4 治理传播 dry-run 回归：候选报告只读、确认后才真实清理（三线调研 A4 验收）。"""
from collections import namedtuple

import pytest

from app.config import settings
from app.services import governance_propagate_service as svc

_Row = namedtuple("_Row", ["id", "cache_key", "answer", "retrieval_sources"])


class _FakeResult:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows


class _FakeDB:
    """按 SQL 表名路由的假 session：qa_cache 扫描 → rows，其余（kg_triples 计数）→ scalar。"""

    async def execute(self, stmt, params=None):
        sql = str(stmt).lower()
        if "qa_cache" in sql:
            return _FakeResult(rows=[_Row(1, "qa:abc", "答案含 \"doc-9\"", None),
                                     _Row(2, "qa:def", "x", '["docId":"doc-9"]')])
        return _FakeResult(scalar=3)

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSessionLocal:
    def __call__(self):
        return _FakeDB()


@pytest.fixture(autouse=True)
def _fake_db(monkeypatch):
    # governance_propagate_service 模块级引用 AsyncSessionLocal，须打它命名空间里那份
    monkeypatch.setattr(svc, "AsyncSessionLocal", _FakeSessionLocal())


@pytest.mark.asyncio
async def test_build_cleanup_candidates_counts_without_deleting(monkeypatch):
    """候选报告：四路只读计数 + 缓存 key 预览，不调用任何删除。"""
    import app.clients.milvus_client as milvus
    import app.clients.neo4j_client as neo4j

    calls = {"deleted": 0}

    def _boom_delete(doc_id):
        calls["deleted"] += 1

    async def _boom_delete_async(doc_id):
        calls["deleted"] += 1

    monkeypatch.setattr(milvus, "count_by_doc", lambda doc_id: {"grid_chunks": 5, "grid_chunks_bge": 2})
    monkeypatch.setattr(milvus, "delete_by_doc", _boom_delete)
    monkeypatch.setattr(neo4j, "count_by_doc", _fake_neo4j_count)
    monkeypatch.setattr(neo4j, "delete_by_doc", _boom_delete_async)

    out = await svc.build_cleanup_candidates("doc-9")
    assert out["milvus"] == {"grid_chunks": 5, "grid_chunks_bge": 2}
    assert out["neo4jEdges"] == 7
    assert out["kgTriples"] == 3
    assert out["qaCacheRows"] == 2
    assert out["cacheKeyPreview"][:2] == ["qa:abc", "qa:def"]
    assert out["totalEstimate"] == 5 + 2 + 7 + 3 + 2
    assert calls["deleted"] == 0  # 只读，绝不删除


async def _fake_neo4j_count(doc_id):
    return 7


@pytest.mark.asyncio
async def test_handler_dry_run_emits_report_and_skips_cleanup(monkeypatch):
    """dry-run 开：doc_blocked 事件 → 只产出 cleanup_dry_run 质量事件，不动存储。"""
    from app.services import quality_event_bus

    emitted: list[tuple] = []

    async def _fake_emit(source, type, payload=None, tenant="default"):
        emitted.append((source, type, payload))
        return "evt-1"

    executed: list[str] = []

    async def _no_exec(doc_id, reason="unknown"):
        executed.append(doc_id)
        return {}

    monkeypatch.setattr(settings, "GOVERNANCE_PROPAGATE_ENABLE", True)
    monkeypatch.setattr(settings, "GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE", True)
    monkeypatch.setattr(quality_event_bus, "emit", _fake_emit)
    monkeypatch.setattr(svc, "build_cleanup_candidates",
                        lambda doc_id: _async_const({"milvus": {}, "totalEstimate": 0}))
    monkeypatch.setattr(svc, "execute_propagate", _no_exec)

    await svc.propagate_handler("e1", "governance", "doc_blocked",
                                {"doc_id": "doc-9", "reason": "withdrawn"}, "t1")
    assert executed == []            # 未执行清理
    assert len(emitted) == 1         # 产出候选报告
    src, typ, payload = emitted[0]
    assert (src, typ) == ("governance", "cleanup_dry_run")
    assert payload["doc_id"] == "doc-9"
    assert "candidates" in payload


async def _async_const(v):
    return v


@pytest.mark.asyncio
async def test_handler_real_mode_executes_cleanup(monkeypatch):
    """dry-run 关（默认）= 原行为：事件直接触发真实清理。"""
    executed: list[tuple] = []

    async def _exec(doc_id, reason="unknown"):
        executed.append((doc_id, reason))
        return {"milvus": True}

    monkeypatch.setattr(settings, "GOVERNANCE_PROPAGATE_ENABLE", True)
    monkeypatch.setattr(settings, "GOVERNANCE_PROPAGATE_DRY_RUN_ENABLE", False)
    monkeypatch.setattr(svc, "execute_propagate", _exec)

    await svc.propagate_handler("e1", "governance", "doc_blocked",
                                {"doc_id": "doc-9", "reason": "withdrawn"}, "t1")
    assert executed == [("doc-9", "withdrawn")]


@pytest.mark.asyncio
async def test_handler_disabled_is_noop(monkeypatch):
    """GOVERNANCE_PROPAGATE_ENABLE 关（默认）= 现状：handler 直接返回。"""
    executed: list[str] = []

    async def _exec(doc_id, reason="unknown"):
        executed.append(doc_id)
        return {}

    monkeypatch.setattr(settings, "GOVERNANCE_PROPAGATE_ENABLE", False)
    monkeypatch.setattr(svc, "execute_propagate", _exec)
    await svc.propagate_handler("e1", "governance", "doc_blocked", {"doc_id": "doc-9"}, "t1")
    assert executed == []


@pytest.mark.asyncio
async def test_execute_propagate_runs_all_paths(monkeypatch):
    """execute_propagate：四路清理 + 治理代际 bump 全部执行。"""
    import app.clients.milvus_client as milvus
    import app.clients.neo4j_client as neo4j

    calls: list[str] = []

    def _milvus_del(doc_id):
        calls.append("milvus")

    async def _neo4j_del(doc_id):
        calls.append("neo4j")

    async def _invalidate(doc_id):
        calls.append("qa_cache")
        return 2

    async def _bump():
        calls.append("bump")

    monkeypatch.setattr(milvus, "delete_by_doc", _milvus_del)
    monkeypatch.setattr(neo4j, "delete_by_doc", _neo4j_del)
    monkeypatch.setattr(svc, "_purge_neo4j_for_doc", _neo4j_del)
    monkeypatch.setattr(svc, "_invalidate_qa_cache_for_doc", _invalidate)
    monkeypatch.setattr(svc, "_bump_gov_generation", _bump)

    out = await svc.execute_propagate("doc-9", "withdrawn")
    assert out["milvus"] is True and out["govGenerationBumped"] is True
    assert out["qaCacheDeleted"] == 2
    assert set(calls) == {"milvus", "neo4j", "qa_cache", "bump"}
