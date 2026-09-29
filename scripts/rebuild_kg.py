"""按当前领域包全量重建知识图谱（《设计文档》§16.7"重建"；ADR 0004）。

流程：清空 kg_triples 与 Neo4j 全部 :Entity/:REL → 逐文档抽取（LLM 并发、写库串行）
→ 刷新全图度数 → 实体向量化（M6 实现后接入）→ 导出出处抽查样本。

断点续跑：进度写在数据卷 data/rebuild_kg/<pack>.json，每完成一篇落盘一次。中断后不带参数
重跑即从断点继续（已完成的跳过，失败的重试）；上次已跑完、或状态文件属于其他领域包时拒绝执行，
需显式 --fresh（会清空图谱重来）。

用法（容器内，复用后端 env 与驱动）：
    docker exec grid-backend python /app/scripts/rebuild_kg.py            # 新建或续跑
    docker exec grid-backend python /app/scripts/rebuild_kg.py --fresh    # 清空重来
    docker exec grid-backend python /app/scripts/rebuild_kg.py --limit 50 # 只处理 50 篇（抽样验证）
    docker exec grid-backend python /app/scripts/rebuild_kg.py --status   # 只看进度
成功输出 REBUILD_OK ...；有失败文档时输出 REBUILD_PARTIAL ... 并以 1 退出（重跑即重试失败的）。
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from sqlalchemy import delete, select  # noqa: E402

from app.clients import neo4j_client  # noqa: E402
from app.config import settings  # noqa: E402
from app.core.domain_pack import get_pack  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.models.chunk import Chunk  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.kg_triple import KgTriple  # noqa: E402
from app.services import kg_service  # noqa: E402

STATE_DIR = Path(os.getenv("REBUILD_KG_DIR", "/app/data/rebuild_kg"))


def _state_path(pack: str) -> Path:
    return STATE_DIR / f"{pack}.json"


def _load_state(pack: str) -> dict | None:
    p = _state_path(pack)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _state_path(state["pack"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)   # 原子替换，中断时不会留下半截文件


async def _clear_graph() -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(KgTriple))
        await db.commit()
    async with neo4j_client._get().session() as s:
        while True:   # 分批删，避免单事务过大
            rec = await (await s.run(
                "MATCH (n:Entity) WITH n LIMIT 5000 DETACH DELETE n RETURN count(n) AS c")).single()
            if not rec or rec["c"] == 0:
                break
    # entity_embedding 表在 M6 建立；存在时一并清空
    try:
        from app.models.entity_embedding import EntityEmbedding  # type: ignore
        async with AsyncSessionLocal() as db:
            await db.execute(delete(EntityEmbedding))
            await db.commit()
    except ImportError:
        pass


async def _docs_with_chunks() -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Document.id, Document.doc_name)
            .where(Document.id.in_(select(Chunk.doc_id).distinct()))
            .order_by(Document.created_at, Document.id)
        )).all()
    return [(r[0], r[1]) for r in rows]


async def _extract_one(doc_id: str, lock: asyncio.Lock) -> dict:
    async with AsyncSessionLocal() as db:
        res = await kg_service.extract_triples(db, doc_id, write_lock=lock)
    if res.get("failedBatches") or not res.get("neo4jOk", True):
        raise RuntimeError(f"failedBatches={res.get('failedBatches')} neo4jOk={res.get('neo4jOk')}")
    return {"triples": res["tripleCount"], "provenance": res["provenance"],
            "rejectedBatches": res.get("rejectedBatches", 0)}


async def _embed_entities() -> str:
    """实体名向量化写 entity_embedding（M6 的 EntityIndex 实现后接入）。"""
    try:
        from app.rag.entity_index import rebuild_entity_embeddings  # type: ignore
    except ImportError:
        return "skipped (M6 未实现)"
    n = await rebuild_entity_embeddings()
    return f"{n} entities"


async def _export_sample(pack: str, n: int) -> Path | None:
    """随机抽 n 条有出处的三元组，连同出处 chunk 原文导出，供人工核对出处正确率（M5 验收）。"""
    if n <= 0:
        return None
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(KgTriple).where(KgTriple.chunk_id != ""))).scalars().all()
        picked = random.Random(42).sample(rows, min(n, len(rows)))
        chunks = {c.id: c.content for c in (await db.execute(
            select(Chunk).where(Chunk.id.in_([t.chunk_id for t in picked])))).scalars().all()}
    out = STATE_DIR / f"{pack}_provenance_sample.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for t in picked:
            f.write(json.dumps({"s": t.subject, "r": t.relation, "o": t.object, "s_raw": t.s_raw,
                                "o_raw": t.o_raw, "provenance": t.provenance, "doc": t.doc_name,
                                "chunk": chunks.get(t.chunk_id, ""), "correct": None},
                               ensure_ascii=False) + "\n")
    return out


def _summary(state: dict) -> str:
    prov = {"verified": 0, "rematched": 0, "none": 0}
    triples = 0
    for d in state["done"].values():
        triples += d["triples"]
        for k in prov:
            prov[k] += d["provenance"].get(k, 0)
    linked = (prov["verified"] + prov["rematched"]) / max(1, triples)
    rejected = sum(d.get("rejectedBatches", 0) for d in state["done"].values())
    return (f"pack={state['pack']} docs={len(state['done'])}/{state['total']} failed={len(state['failed'])} "
            f"triples={triples} verified={prov['verified']} rematched={prov['rematched']} none={prov['none']} "
            f"with_chunk={linked:.1%} rejected_batches={rejected}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fresh", action="store_true", help="清空图谱与进度，从头重建")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少篇（0=全部）")
    ap.add_argument("--concurrency", type=int, default=4, help="LLM 抽取并发数（写库始终串行）")
    ap.add_argument("--sample", type=int, default=50, help="完成后导出多少条出处抽查样本")
    ap.add_argument("--status", action="store_true", help="只打印进度")
    args = ap.parse_args()

    pack = get_pack().name
    state = _load_state(pack)
    if args.status:
        print(_summary(state) if state else f"NO_STATE pack={pack}")
        return 0
    if state and not args.fresh:
        if state.get("finished"):
            print(f"REBUILD_REFUSED 领域包 {pack} 上次已重建完成（{_summary(state)}）；如需重来请加 --fresh")
            return 2
        print(f"[rebuild] 续跑：{_summary(state)}")
    else:
        print(f"[rebuild] 清空图谱，按领域包 {pack} 从头重建")
        await _clear_graph()
        state = {"pack": pack, "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "total": 0,
                 "done": {}, "failed": {}, "finished": False}
        _save_state(state)

    docs = await _docs_with_chunks()
    state["total"] = len(docs)
    todo = [(i, n) for i, n in docs if i not in state["done"]]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[rebuild] 文档 {len(docs)} 篇，本次处理 {len(todo)} 篇，并发 {args.concurrency}")

    lock, sem = asyncio.Lock(), asyncio.Semaphore(max(1, args.concurrency))
    t0 = time.time()

    async def worker(doc_id: str, name: str) -> None:
        async with sem:
            try:
                state["done"][doc_id] = await _extract_one(doc_id, lock)
                state["failed"].pop(doc_id, None)
            except Exception as e:
                state["failed"][doc_id] = f"{type(e).__name__}: {e}"[:300]
            _save_state(state)
            k = len(state["done"])
            print(f"[rebuild] {k}/{len(docs)} {'FAIL' if doc_id in state['failed'] else 'ok'} {name[:40]}"
                  f"  ({time.time() - t0:.0f}s)", flush=True)

    await asyncio.gather(*(worker(i, n) for i, n in todo))

    complete = len(state["done"]) == len(docs)
    if complete:
        await neo4j_client.refresh_degree(None)
        emb = await _embed_entities()
        sample = await _export_sample(pack, args.sample)
        state["finished"] = True
        state["finishedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _save_state(state)
        print(f"[rebuild] 度数已刷新；实体向量：{emb}；出处抽查样本：{sample}")
    else:
        await neo4j_client.refresh_degree(None)   # 部分完成也刷新，便于中途查看
    await neo4j_client.close()

    if state["failed"]:
        print(f"REBUILD_PARTIAL {_summary(state)}（重跑即重试失败文档）")
        return 1
    print(f"{'REBUILD_OK' if complete else 'REBUILD_PROGRESS'} {_summary(state)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
