"""图谱定位证据（设计文档 §8、§16.6）：从链接到的实体出发，沿带出处的边找到证据 chunk。

路径 → 收集各边的 chunk_ids（保留"该 chunk 来自哪些路径"）→ 排序截断到 max_candidates
→ 从 MySQL 取正文组装成检索条目（origin="graph"，带 pathIds，score=0.0 待重打分）。
合并原上下文、统一重打分、再分级在 gcrag 编排里完成。

候选排序（在设计的"跳数升序、次数降序"之前加了一级）：先按被几个不同的链接实体共同指向降序。
实测问题会链接到"北京""董事长"这类泛化实体，它们一跳就连出几百条边；只按跳数排，
这些边会占满候选名额。被多个问题实体共同指向的 chunk 更可能是答案所在。
"""
from dataclasses import dataclass, field

from app.clients import neo4j_client
from app.config import settings
from app.core.obs import degraded


@dataclass
class EvidenceEdge:
    source: str
    target: str
    type: str                      # 保留原方向
    chunk_ids: list[str]


@dataclass
class EvidencePath:
    id: int
    nodes: list[str]
    edges: list[EvidenceEdge]
    hops: int
    start: str = ""

    def render(self) -> str:
        """A -[属于]- B -[导致]- C（遍历无向，箭头按边的原方向画）。"""
        out = self.nodes[0]
        for i, e in enumerate(self.edges):
            nxt = self.nodes[i + 1]
            arrow = f"-[{e.type}]->" if e.source == self.nodes[i] else f"<-[{e.type}]-"
            out += f" {arrow} {nxt}"
        return out


@dataclass
class GraphEvidenceResult:
    status: str                    # done | no_path | unavailable
    paths: list[EvidencePath] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    truncated: bool = False


def rank_chunks(paths: list[EvidencePath], exclude: set[str]) -> list[tuple[str, list[int]]]:
    """[(chunk_id, 支撑它的路径 id)]，按 (共同指向的起点数 desc, 最短跳数 asc, 路径数 desc) 排序。"""
    info: dict[str, dict] = {}
    for p in paths:
        for e in p.edges:
            for cid in e.chunk_ids:
                if not cid or cid in exclude:
                    continue
                d = info.setdefault(cid, {"starts": set(), "hops": p.hops, "paths": []})
                d["starts"].add(p.start)
                d["hops"] = min(d["hops"], p.hops)
                if p.id not in d["paths"]:
                    d["paths"].append(p.id)
    order = sorted(info.items(), key=lambda kv: (-len(kv[1]["starts"]), kv[1]["hops"], -len(kv[1]["paths"])))
    return [(cid, d["paths"]) for cid, d in order]


async def find(db, entities: list[str], *, cfg, exclude_chunk_ids: set[str]) -> GraphEvidenceResult:
    """cfg 为领域包的 GraphEvidenceConfig。exclude_chunk_ids = 当前上下文已有的 chunkId。"""
    from app.services.chunk_loader import load_items
    if not entities:
        return GraphEvidenceResult("no_path")
    try:
        rows = await neo4j_client.find_evidence_paths(
            entities, max_hops=cfg.max_hops, hub_degree=cfg.hub_degree,
            start_hub_max_hops=cfg.start_hub_max_hops)
    except Exception as e:
        degraded("graph_evidence_neo4j", e, "Neo4j 不可用 → 跳过图谱定位证据")
        return GraphEvidenceResult("unavailable")
    paths = [EvidencePath(id=i, nodes=r["chain"], hops=len(r["rels"]), start=r["start"],
                          edges=[EvidenceEdge(x["from"], x["to"], x["type"], list(x["chunk_ids"] or []))
                                 for x in r["rels"]])
             for i, r in enumerate(rows)]
    if not paths:
        return GraphEvidenceResult("no_path")
    ranked = rank_chunks(paths, exclude_chunk_ids)
    truncated = len(ranked) > cfg.max_candidates
    ranked = ranked[:cfg.max_candidates]
    try:
        items = await load_items(db, [cid for cid, _ in ranked],
                                 expand_parent=bool(getattr(settings, "SMALL_TO_BIG_ENABLE", False)),
                                 origin="graph", sources=["graph"])
    except Exception as e:
        degraded("graph_evidence_chunks", e, "出处 chunk 读取失败 → 跳过图谱定位证据")
        return GraphEvidenceResult("unavailable", paths=paths)
    path_ids = dict(ranked)
    for it in items:
        it["pathIds"] = path_ids.get(it.get("chunkId"), [])
    used = {pid for it in items for pid in it["pathIds"]}
    return GraphEvidenceResult("done", paths=[p for p in paths if p.id in used],
                               candidates=items, truncated=truncated)
