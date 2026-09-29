"""图谱定位证据（设计文档 §8、§14、§16.6）。

遍历规则连真实 Neo4j（实体名带随机前缀，用例结束清理，连不上则跳过）：无向可达、
枢纽实体不作中间节点、起点为枢纽只扩 1 跳、无出处的边不走。候选排序与截断、状态、
chunk 取回用桩或隔离测试库。
"""
import asyncio
import uuid

import pytest

from app.clients import neo4j_client
from app.core.domain_pack import GraphEvidenceConfig
from app.rag import graph_evidence as ge
from tests.test_neo4j_provenance import _in_ns, _t

HUB = 3   # 测试用 hub_degree：度数 > 3 即为枢纽


async def _build(ns):
    """A -发布-> B <-属于- C；A -相关-> D（无出处）；A -位于-> H；H 连 X1..X4；X1 -包含-> Y。"""
    ts = [_t(ns, "A", "发布", "B", "cAB"), _t(ns, "C", "属于", "B", "cCB"),
          _t(ns, "A", "相关", "D", ""), _t(ns, "A", "位于", "H", "cAH"),
          _t(ns, "X1", "包含", "Y", "cXY")]
    ts += [_t(ns, "H", "包含", f"X{i}", f"cHX{i}") for i in range(1, 5)]
    await neo4j_client.upsert_triples(ts, "d1", "文档1")
    await neo4j_client.refresh_degree(None)


async def _paths(ns, start, **kw):
    kw = {"max_hops": 2, "hub_degree": HUB, "start_hub_max_hops": 1, **kw}
    rows = await neo4j_client.find_evidence_paths([ns + start], **kw)
    return {tuple(n.removeprefix(ns) for n in r["chain"]) for r in rows}


def test_undirected_reachable_and_chunkless_edges_skipped():
    async def go(ns):
        await _build(ns)
        return await _paths(ns, "A")
    chains = _in_ns(go)
    assert ("A", "B", "C") in chains                    # A->B<-C：无向遍历才可达
    assert not any("D" in c for c in chains)            # A-D 无出处，不走


def test_hub_is_endpoint_but_not_intermediate():
    async def go(ns):
        await _build(ns)
        return await _paths(ns, "A")
    chains = _in_ns(go)
    assert ("A", "H") in chains                         # 枢纽可作终点
    assert not any(len(c) == 3 and c[1] == "H" for c in chains)   # 不经枢纽继续扩展


def test_hub_start_only_one_hop():
    async def go(ns):
        await _build(ns)
        return await _paths(ns, "H"), await _paths(ns, "H", start_hub_max_hops=0)
    chains, none = _in_ns(go)
    assert {c for c in chains if len(c) == 2} >= {("H", "X1"), ("H", "A")}
    assert all(len(c) == 2 for c in chains)             # H-X1-Y 这类 2 跳不出现
    assert none == set()                                # start_hub_max_hops=0：不从枢纽出发


def test_edge_direction_kept():
    async def go(ns):
        await _build(ns)
        rows = await neo4j_client.find_evidence_paths([ns + "A"], max_hops=2, hub_degree=HUB,
                                                      start_hub_max_hops=1)
        for r in rows:
            if [n.removeprefix(ns) for n in r["chain"]] == ["A", "B", "C"]:
                return [(x["from"].removeprefix(ns), x["type"], x["to"].removeprefix(ns)) for x in r["rels"]]
    assert _in_ns(go) == [("A", "发布", "B"), ("C", "属于", "B")]


def test_invalid_hops_rejected():
    with pytest.raises(ValueError):
        asyncio.run(neo4j_client.find_evidence_paths(["x"], max_hops="2 RETURN 1//", hub_degree=3,
                                                     start_hub_max_hops=1))


# ---- 候选排序、截断、状态（Neo4j 与 chunk 读取打桩）----

def _p(pid, start, hops, *chunk_ids):
    edges = [ge.EvidenceEdge("s", "t", "r", list(chunk_ids))] + \
            [ge.EvidenceEdge("t", "u", "r", []) for _ in range(hops - 1)]
    return ge.EvidencePath(id=pid, nodes=["s"] * (hops + 1), edges=edges, hops=hops, start=start)


def test_rank_prefers_chunks_shared_by_several_entities():
    paths = [_p(0, "北京", 1, "c_hub1"), _p(1, "北京", 1, "c_hub2"),
             _p(2, "碧水源", 2, "c_answer"), _p(3, "疏勒河项目", 2, "c_answer"),
             _p(4, "碧水源", 1, "c_one")]
    ranked = ge.rank_chunks(paths, exclude=set())
    assert ranked[0] == ("c_answer", [2, 3])            # 两个实体共同指向，胜过 1 跳的单实体 chunk
    assert [c for c, _ in ranked[1:]] == ["c_hub1", "c_hub2", "c_one"]


def test_rank_excludes_existing_context():
    ranked = ge.rank_chunks([_p(0, "a", 1, "c1", "c2")], exclude={"c1"})
    assert [c for c, _ in ranked] == ["c2"]


@pytest.fixture
def stub_graph(monkeypatch):
    box = {"rows": [], "err": None, "loaded": None}

    async def fake_paths(starts, **kw):
        if box["err"]:
            raise box["err"]
        return box["rows"]

    async def fake_load(db, ids, *, expand_parent, origin, sources):
        box["loaded"] = list(ids)
        return [{"chunkId": i, "chunk": f"正文{i}", "score": 0.0, "origin": origin, "sources": sources}
                for i in ids if i != "missing"]
    monkeypatch.setattr(ge.neo4j_client, "find_evidence_paths", fake_paths)
    monkeypatch.setattr("app.services.chunk_loader.load_items", fake_load)
    return box


def _row(start, *rels):
    chain = [start] + [f"n{i}" for i in range(len(rels))]
    return {"start": start, "chain": chain,
            "rels": [{"type": "r", "chunk_ids": cids, "from": chain[i], "to": chain[i + 1]}
                     for i, cids in enumerate(rels)]}


def _find(entities, cfg=None, exclude=()):
    return asyncio.run(ge.find(None, entities, cfg=cfg or GraphEvidenceConfig(),
                               exclude_chunk_ids=set(exclude)))


def test_find_truncates_and_attaches_path_ids(stub_graph):
    stub_graph["rows"] = [_row("e", [f"c{i}"]) for i in range(5)]
    res = _find(["e"], GraphEvidenceConfig(max_candidates=3))
    assert res.status == "done" and res.truncated
    assert [c["chunkId"] for c in res.candidates] == ["c0", "c1", "c2"]
    assert all(c["origin"] == "graph" and c["sources"] == ["graph"] for c in res.candidates)
    assert [c["pathIds"] for c in res.candidates] == [[0], [1], [2]]
    assert {p.id for p in res.paths} == {0, 1, 2}         # 只保留支撑了候选的路径


def test_find_skips_missing_chunks(stub_graph):
    stub_graph["rows"] = [_row("e", ["missing", "c1"])]
    res = _find(["e"])
    assert [c["chunkId"] for c in res.candidates] == ["c1"]


def test_find_statuses(stub_graph):
    assert _find([]).status == "no_path"
    assert _find(["e"]).status == "no_path"                 # 图谱中没有路径
    stub_graph["err"] = RuntimeError("Neo4j down")
    assert _find(["e"]).status == "unavailable"


def test_path_render_keeps_direction():
    p = ge.EvidencePath(id=0, nodes=["A", "B", "C"], hops=2,
                        edges=[ge.EvidenceEdge("A", "B", "发布", []), ge.EvidenceEdge("C", "B", "属于", [])])
    assert p.render() == "A -[发布]-> B <-[属于]- C"


# ---- chunk_loader（隔离测试库）----

def test_load_items_order_missing_and_parent_expand(monkeypatch):
    from sqlalchemy import delete

    from app.db.session import AsyncSessionLocal
    from app.models.chunk import Chunk
    from app.models.document import Document
    from app.services import chunk_loader

    doc = f"t{uuid.uuid4().hex[:12]}"

    async def run():
        async with AsyncSessionLocal() as db:
            db.add(Document(id=doc, doc_name="测试.txt", doc_type="新闻", minio_object="x"))
            await db.flush()
            db.add_all([Chunk(id=f"{doc}_0", doc_id=doc, chunk_idx=0, content="第一段", parent_idx=0),
                        Chunk(id=f"{doc}_1", doc_id=doc, chunk_idx=1, content="第二段", parent_idx=0),
                        Chunk(id=f"{doc}_2", doc_id=doc, chunk_idx=2, content="第三段", parent_idx=1)])
            await db.commit()
        try:
            async with AsyncSessionLocal() as db:
                flat = await chunk_loader.load_items(db, [f"{doc}_2", "nope", f"{doc}_0"],
                                                     expand_parent=False, origin="graph", sources=["graph"])
                from app.services import retrieval_service as rs
                monkeypatch.setattr(rs.settings, "SMALL_TO_BIG_ENABLE", True)
                big = await chunk_loader.load_items(db, [f"{doc}_0", f"{doc}_1", f"{doc}_2"],
                                                    expand_parent=True, origin="graph", sources=["graph"])
            return flat, big
        finally:
            async with AsyncSessionLocal() as db:
                await db.execute(delete(Chunk).where(Chunk.doc_id == doc))
                await db.execute(delete(Document).where(Document.id == doc))
                await db.commit()

    flat, big = asyncio.run(run())
    assert [i["chunkId"] for i in flat] == [f"{doc}_2", f"{doc}_0"]   # 保持输入顺序，缺失跳过
    assert flat[0] == {"chunk": "第三段", "score": 0.0, "docId": doc, "docName": "测试.txt",
                       "docType": "新闻", "chunkIdx": 2, "chunkId": f"{doc}_2",
                       "sources": ["graph"], "origin": "graph"}
    assert [i["chunk"] for i in big] == ["第一段\n第二段", "第三段"]   # 按父块拼全文并去重
