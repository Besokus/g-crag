"""M5b：Neo4j 边的来源列表、按文档删除只移除本文档来源、度数刷新（《设计文档》§16.7）。

连真实 Neo4j（容器内 bolt://neo4j:7687）；实体名带随机前缀，用例结束清理；连不上则跳过。
"""
import asyncio
import uuid

import pytest

from app.clients import neo4j_client


class _Unavailable(Exception):
    pass


def _in_ns(body):
    """同一事件循环内：新建驱动 → 探活 → 执行 body(prefix) → 清理前缀实体 → 关闭驱动。"""
    async def main():
        neo4j_client._driver = None
        prefix = f"__t{uuid.uuid4().hex[:8]}_"
        try:
            async with neo4j_client._get().session() as s:
                await s.run("RETURN 1")
        except Exception as e:
            await neo4j_client.close()
            raise _Unavailable(str(e))
        try:
            return await body(prefix)
        finally:
            async with neo4j_client._get().session() as s:
                await s.run("MATCH (n:Entity) WHERE n.name STARTS WITH $p DETACH DELETE n", p=prefix)
            await neo4j_client.close()
    try:
        return asyncio.run(main())
    except _Unavailable:
        pytest.skip("Neo4j 不可用")


async def _edge(prefix, s, r, o):
    async with neo4j_client._get().session() as sess:
        res = await sess.run(
            "MATCH (a:Entity {name: $s})-[e:REL {type: $r}]->(b:Entity {name: $o}) "
            "RETURN e.doc_id AS doc_id, e.doc_ids AS doc_ids, e.chunk_ids AS chunk_ids, "
            "a.type AS s_type, a.degree AS s_deg, b.degree AS o_deg",
            s=prefix + s, r=r, o=prefix + o)
        return await res.single()


def _t(prefix, s, r, o, chunk_id="", s_type="组织", o_type="产品"):
    return {"s": prefix + s, "r": r, "o": prefix + o, "chunk_id": chunk_id, "s_type": s_type, "o_type": o_type}


def test_sources_accumulate_across_docs():
    async def go(ns):
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "c1")], "d1", "文档1")
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "c2")], "d2", "文档2")
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "c2")], "d2", "文档2")  # 幂等
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "")], "d3", "文档3")    # 无出处
        return await _edge(ns, "华为", "发布", "手机")
    e = _in_ns(go)
    assert e["doc_id"] == "d1"
    assert e["doc_ids"] == ["d1", "d2", "d3"]
    assert e["chunk_ids"] == ["c1", "c2"]
    assert e["s_type"] == "组织"


def test_delete_by_doc_keeps_edges_supported_by_other_docs():
    async def go(ns):
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "c1"),
                                           _t(ns, "华为", "位于", "深圳", "c1")], "d1", "文档1")
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "c2")], "d2", "文档2")
        await neo4j_client.delete_by_doc("d1", ["c1"])
        shared = await _edge(ns, "华为", "发布", "手机")
        only_d1 = await _edge(ns, "华为", "位于", "深圳")
        async with neo4j_client._get().session() as s:
            orphan = await (await s.run("MATCH (n:Entity {name: $n}) RETURN count(n) AS c", n=ns + "深圳")).single()
        return shared, only_d1, orphan["c"]
    shared, only_d1, orphan = _in_ns(go)
    assert shared["doc_ids"] == ["d2"] and shared["chunk_ids"] == ["c2"] and shared["doc_id"] == "d2"
    assert only_d1 is None          # 只由 d1 支撑的边被删除
    assert orphan == 0              # 孤立实体一并删除
    assert shared["s_deg"] == 1     # 华为 只剩一条边，度数已刷新


def test_degree_counts_undirected_edges():
    async def go(ns):
        await neo4j_client.upsert_triples([_t(ns, "A", "合作", "B", "c1"), _t(ns, "C", "合作", "A", "c1"),
                                           _t(ns, "A", "投资", "D", "c1")], "d1", "文档1")
        return await _edge(ns, "A", "合作", "B")
    e = _in_ns(go)
    assert e["s_deg"] == 3 and e["o_deg"] == 1


def test_legacy_edge_without_lists_is_upgraded():
    async def go(ns):
        async with neo4j_client._get().session() as s:   # 模拟原版写入的边：只有 doc_id
            await s.run("MERGE (a:Entity {name: $a}) MERGE (b:Entity {name: $b}) "
                        "MERGE (a)-[:REL {type: '发布', doc_id: 'old', doc_name: '旧'}]->(b)",
                        a=ns + "华为", b=ns + "手机")
        await neo4j_client.upsert_triples([_t(ns, "华为", "发布", "手机", "c9")], "d9", "新")
        e1 = await _edge(ns, "华为", "发布", "手机")
        await neo4j_client.delete_by_doc("old")
        e2 = await _edge(ns, "华为", "发布", "手机")
        return e1, e2
    e1, e2 = _in_ns(go)
    assert e1["doc_ids"] == ["old", "d9"] and e1["chunk_ids"] == ["c9"]
    assert e2["doc_ids"] == ["d9"] and e2["doc_id"] == "d9"
