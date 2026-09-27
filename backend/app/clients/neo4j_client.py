"""Neo4j 异步驱动封装（知识图谱：设备-故障-处置 多跳推理）。

节点 :Entity {name, type, degree}；统一有向关系 :REL {type, doc_id, doc_name, doc_ids, chunk_ids}
（type 承载语义：发生/表现为/处置方法/属于…）。固定关系类型保证多跳 [:REL*1..N] 通用查询，
避免 Cypher 关系类型不能参数化、中文关系类型不优雅的问题。

G-CRAG（《设计文档》§16.7）：同一 (s, r, o) 边可来自多篇文档、多个 chunk，来源记在
doc_ids / chunk_ids 列表（chunk_ids 为空的边不参与图谱定位证据）；doc_id / doc_name 保留首个来源，
兼容旧读取代码。实体 type 来自领域包 schema；degree 为无向度数，用于枢纽实体判断。
"""
from neo4j import AsyncGraphDatabase

from app.config import settings

_driver = None

_FAULT_KW = ("故障", "异常", "过热", "漏气", "失效", "短路", "断路", "跳闸", "损坏", "磨损", "老化", "泄漏", "振动", "噪声")
_ACTION_KW = ("处置", "处理", "维修", "更换", "操作", "步骤", "方法", "检查", "巡视", "隔离", "送电", "停电", "检修", "试验", "测试", "分析", "检测", "监测", "预防", "校验")


def _infer_type(name: str, relation: str = "", as_subject: bool = True) -> str:
    """从实体名/关系推断语义类型：Fault/Action/Equipment（3D 着色用；存量无 type 数据也兜底）。"""
    if not name:
        return "Equipment"
    if any(k in name for k in _FAULT_KW):
        return "Fault"
    if any(k in name for k in _ACTION_KW):
        return "Action"
    if not as_subject and relation:
        if any(k in relation for k in ("故障", "异常", "现象", "表现", "发生")):
            return "Fault"
        if any(k in relation for k in ("处置", "处理", "方法", "步骤", "操作", "维修")):
            return "Action"
    return "Equipment"


def _get():
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)
        )
    return _driver


async def close():
    global _driver
    if _driver is not None:
        await _driver.close()
        _driver = None


async def ensure_constraint():
    """建索引（幂等）：加速按 name 查节点。Neo4j 未启动时抛异常，由调用方兜底。"""
    async with _get().session() as s:
        await s.run("CREATE INDEX entity_name IF NOT EXISTS FOR (n:Entity) ON (n.name)")


_UPSERT_CYPHER = """
UNWIND $rows AS t
MERGE (a:Entity {name: t.s})
  ON CREATE SET a.type = t.s_type
  ON MATCH SET a.type = CASE WHEN coalesce(a.type, '') = '' THEN t.s_type ELSE a.type END
MERGE (b:Entity {name: t.o})
  ON CREATE SET b.type = t.o_type
  ON MATCH SET b.type = CASE WHEN coalesce(b.type, '') = '' THEN t.o_type ELSE b.type END
MERGE (a)-[r:REL {type: t.r}]->(b)
  ON CREATE SET r.doc_id = $doc_id, r.doc_name = $doc_name,
                r.doc_ids = [$doc_id],
                r.chunk_ids = CASE WHEN t.chunk_id = '' THEN [] ELSE [t.chunk_id] END
  ON MATCH SET  r.doc_ids = CASE WHEN $doc_id IN coalesce(r.doc_ids, [r.doc_id])
                                 THEN coalesce(r.doc_ids, [r.doc_id])
                                 ELSE coalesce(r.doc_ids, [r.doc_id]) + $doc_id END,
                r.chunk_ids = CASE WHEN t.chunk_id = '' OR t.chunk_id IN coalesce(r.chunk_ids, [])
                                   THEN coalesce(r.chunk_ids, [])
                                   ELSE coalesce(r.chunk_ids, []) + t.chunk_id END
"""


async def upsert_triples(triples: list[dict], doc_id: str, doc_name: str) -> int:
    """批量 MERGE 写入三元组（幂等，重复抽取不产生重复边），维护来源列表并刷新端点度数。"""
    if not triples:
        return 0
    rows = [{"s": t["s"], "r": t["r"], "o": t["o"],
             "s_type": t.get("s_type") or "", "o_type": t.get("o_type") or "",
             "chunk_id": t.get("chunk_id") or ""} for t in triples]
    async with _get().session() as s:
        for i in range(0, len(rows), 500):
            await s.run(_UPSERT_CYPHER, rows=rows[i:i + 500], doc_id=doc_id, doc_name=doc_name)
    await refresh_degree({n for t in rows for n in (t["s"], t["o"])})
    return len(triples)


async def refresh_degree(names: set[str] | None = None) -> None:
    """刷新实体无向度数 n.degree。names=None 刷新全图（建图完成后）；否则只刷新给定实体
    （新增/删除边只会改变其端点的度数）。"""
    async with _get().session() as s:
        if names is None:
            await s.run("MATCH (n:Entity) SET n.degree = COUNT { (n)--() }")
            return
        await s.run(
            "UNWIND $names AS nm MATCH (n:Entity {name: nm}) SET n.degree = COUNT { (n)--() }",
            names=list(names),
        )


async def delete_by_doc(doc_id: str, chunk_ids: list[str] | None = None) -> None:
    """文档删除联动：从边的来源列表中移除该文档（及其 chunk），来源清空的边才删除。

    原实现按首个来源 r.doc_id 整边删除，会误删其他文档也支撑的边（《设计文档》§17）。
    删除后刷新受影响端点的度数；来源已清空且孤立的实体节点一并删除。
    """
    async with _get().session() as s:
        result = await s.run(
            """
            MATCH (a:Entity)-[r:REL]->(b:Entity)
            WHERE $doc_id IN coalesce(r.doc_ids, [r.doc_id])
            WITH a, b, r, [d IN coalesce(r.doc_ids, [r.doc_id]) WHERE d <> $doc_id] AS docs
            SET r.doc_ids = docs,
                r.chunk_ids = [c IN coalesce(r.chunk_ids, []) WHERE NOT c IN $chunk_ids],
                r.doc_id = CASE WHEN r.doc_id = $doc_id THEN coalesce(head(docs), '') ELSE r.doc_id END
            WITH a, b, r, docs
            FOREACH (_ IN CASE WHEN size(docs) = 0 THEN [1] ELSE [] END | DELETE r)
            RETURN collect(DISTINCT a.name) + collect(DISTINCT b.name) AS touched
            """,
            doc_id=doc_id, chunk_ids=list(chunk_ids or []),
        )
        rec = await result.single()
        touched = set(rec["touched"]) if rec and rec["touched"] else set()
        if touched:
            await s.run("UNWIND $names AS nm MATCH (n:Entity {name: nm}) WHERE NOT (n)--() DELETE n",
                        names=list(touched))
    if touched:
        await refresh_degree(touched)


async def count_rel_edges() -> int:
    """全库 :REL 边数（KG 对账用；与 MySQL DISTINCT(s,r,o) 应相等——MERGE 幂等去重）。"""
    async with _get().session() as s:
        result = await s.run("MATCH ()-[r:REL]->() RETURN count(r) AS c")
        rec = await result.single()
        return rec["c"] if rec else 0


async def count_by_doc(doc_id: str) -> int:
    """统计某文档产生的边数（治理 dry-run 候选报告用，不删除）。失败返回 0。"""
    try:
        async with _get().session() as s:
            result = await s.run(
                "MATCH (:Entity)-[r:REL]->() WHERE $doc_id IN coalesce(r.doc_ids, [r.doc_id]) "
                "RETURN count(r) AS c",
                doc_id=doc_id,
            )
            record = await result.single()
            return int(record["c"]) if record else 0
    except Exception as e:
        from app.core.obs import degraded
        degraded("neo4j_count_by_doc", e)
        return 0


async def get_neighbors(entity: str = "", limit: int = 800) -> dict:
    """按实体模糊查邻居子图（nodes + links）。节点带 type/outDegree（3D 着色与大小用）。"""
    async with _get().session() as s:
        if entity:
            result = await s.run(
                """
                MATCH (n:Entity)-[r:REL]-(m:Entity)
                WHERE n.name CONTAINS $entity
                RETURN n.name AS s, n.type AS s_type, r.type AS rel,
                       m.name AS o, m.type AS o_type LIMIT $limit
                """,
                entity=entity, limit=limit,
            )
        else:
            result = await s.run(
                "MATCH (a:Entity)-[r:REL]->(b:Entity) "
                "RETURN a.name AS s, a.type AS s_type, r.type AS rel, "
                "b.name AS o, b.type AS o_type LIMIT $limit",
                limit=limit,
            )
        rows = [rec async for rec in result]
    # 批量算涉及节点的出度（一次查询，避免 N+1）
    names = {row["s"] for row in rows if row["s"]} | {row["o"] for row in rows if row["o"]}
    deg: dict[str, int] = {}
    if names:
        async with _get().session() as s:
            res = await s.run(
                "UNWIND $names AS nm "
                "OPTIONAL MATCH (n:Entity {name: nm})-[r:REL]->() "
                "RETURN nm, count(r) AS d",
                names=list(names),
            )
            deg = {rec["nm"]: rec["d"] for rec in [r async for r in res]}
    nodes: dict[str, dict] = {}
    links: list[dict] = []
    for row in rows:
        s_, o_ = row["s"], row["o"]
        if s_ and s_ not in nodes:
            t = row.get("s_type") or _infer_type(s_, row.get("rel", ""), True)
            d = deg.get(s_, 0)
            nodes[s_] = {"id": s_, "name": s_, "type": t, "category": 0,
                         "outDegree": d, "symbolSize": 28 + min(20, d * 2)}
        if o_ and o_ not in nodes:
            t = row.get("o_type") or _infer_type(o_, row.get("rel", ""), False)
            d = deg.get(o_, 0)
            nodes[o_] = {"id": o_, "name": o_, "type": t, "category": 1,
                         "outDegree": d, "symbolSize": 28 + min(20, d * 2)}
        links.append({"source": s_, "target": o_, "value": row["rel"]})
    return {
        "nodes": list(nodes.values()),
        "links": links,
        "categories": [{"name": "实体"}, {"name": "属性/关系"}],
        "total": len(rows),
    }


async def get_paths(entity: str, depth: int = 3, limit: int = 20) -> list[dict]:
    """多跳影响链：从 entity 出发的 depth 跳有向路径（故障传播/影响分析）。"""
    depth = max(1, min(int(depth), 5))   # 强制 int 防 Cypher 注入 + 限深
    async with _get().session() as s:
        result = await s.run(
            f"MATCH path = (n:Entity)-[:REL*1..{depth}]->(m:Entity) "
            "WHERE n.name CONTAINS $entity "
            "RETURN [x IN nodes(path) | x.name] AS chain, "
            "[r IN relationships(path) | r.type] AS rels, length(path) AS hops "
            "ORDER BY hops LIMIT $limit",
            entity=entity, limit=limit,
        )
        rows = [rec async for rec in result]
    return [{"chain": rec["chain"], "rels": rec["rels"], "hops": rec["hops"]} for rec in rows]


async def get_hubs(limit: int = 15) -> list[dict]:
    """枢纽实体：出度最高（影响传播的源头，核心设备/故障）。"""
    async with _get().session() as s:
        result = await s.run(
            "MATCH (n:Entity)-[r:REL]->() "
            "RETURN n.name AS name, count(r) AS outDegree "
            "ORDER BY outDegree DESC LIMIT $limit",
            limit=limit,
        )
        rows = [rec async for rec in result]
    return [{"name": rec["name"], "outDegree": rec["outDegree"]} for rec in rows]


async def query_triples_by_keywords(words: list[str], limit: int = 8) -> list[dict]:
    """查 name 含任一关键词的实体一跳三元组（GraphRAG 问答增强用）。"""
    if not words:
        return []
    async with _get().session() as s:
        result = await s.run(
            """
            UNWIND $words AS w
            MATCH (n:Entity)-[r:REL]->(m:Entity)
            WHERE n.name CONTAINS w OR m.name CONTAINS w
            RETURN DISTINCT n.name AS s, r.type AS rel, m.name AS o
            LIMIT $limit
            """,
            words=words, limit=limit,
        )
        rows = [rec async for rec in result]
    return [{"s": rec["s"], "rel": rec["rel"], "o": rec["o"]} for rec in rows]


# ===== N1 Agent 记忆图操作 =====

async def upsert_user_preference(user_id: str, entity: str,
                                  category: str = "preference") -> None:
    """写入用户偏好关系：(User {id})-[:PREFERS {category}]->(Entity {name})。

    幂等 MERGE：重复写入不产生重复边。User 节点不存在时自动创建。
    """
    async with _get().session() as s:
        await s.run(
            """
            MERGE (u:User {id: $user_id})
            MERGE (e:Entity {name: $entity})
            MERGE (u)-[r:PREFERS]->(e)
              ON CREATE SET r.category = $category, r.created_at = timestamp()
            """,
            user_id=user_id, entity=entity, category=category,
        )


async def get_user_preferences(user_id: str) -> list[str]:
    """查询用户偏好实体列表（User→PREFERS→Entity）。"""
    async with _get().session() as s:
        result = await s.run(
            """
            MATCH (u:User {id: $user_id})-[:PREFERS]->(e:Entity)
            RETURN e.name AS entity, r.category AS category
            ORDER BY r.created_at DESC LIMIT 10
            """,
            user_id=user_id,
        )
        rows = [rec async for rec in result]
    return [rec["entity"] for rec in rows if rec["entity"]]


async def delete_user_preference(user_id: str, entity: str) -> None:
    """删除用户偏好关系（forget 时调用）。"""
    async with _get().session() as s:
        await s.run(
            """
            MATCH (u:User {id: $user_id})-[r:PREFERS]->(e:Entity {name: $entity})
            DELETE r
            """,
            user_id=user_id, entity=entity,
        )
