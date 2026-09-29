"""知识图谱服务：LLM 抽取三元组 → 双写 MySQL(统计/审计) + Neo4j(图查询/多跳推理)。

图查询与多跳推理走 Neo4j（影响链/枢纽分析）；Neo4j 未启动时 get_graph 回退 MySQL 一跳查询，
保证图谱页不崩。统计仍用 MySQL 三元组表。
"""
import contextlib
import json
import re

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients import neo4j_client
from app.clients import redis_client
from app.config import settings
from app.core.response import BizError
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.kg_triple import KgTriple
from app.core.obs import degraded
from app.providers.factory import get_llm_provider

_BATCH = 6  # 每批喂 LLM 的分块数（控制输入长度与抽取稳定性）

# 抽取 prompt 来自领域包 prompts/extract.md（《设计文档》§3.3、§4.1），批内 chunk 编号 [1]…[_BATCH]


def _parse_triples(ans: str) -> list[dict]:
    """从 LLM 输出中解析三元组 JSON（容错：去注释/代码块/截断）。"""
    m = re.search(r"\[.*\]", ans or "", re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        s, r, o = str(it.get("s", "")).strip(), str(it.get("r", "")).strip(), str(it.get("o", "")).strip()
        if s and r and o and len(s) <= 20 and len(r) <= 20 and len(o) <= 20:
            out.append({"s": s[:256], "r": r[:128], "o": o[:256]})
    return out


def _validate_triples(arr) -> list[dict]:
    """逐条校验：dict、s/r/o 非空、长度 ≤30；保留 s_type/o_type 与批内编号 chunk_no（无效置 0）。"""
    out = []
    for it in arr:
        if not isinstance(it, dict):
            continue
        s = str(it.get("s", "")).strip()
        r = str(it.get("r", "")).strip()
        o = str(it.get("o", "")).strip()
        if s and r and o and len(s) <= 30 and len(r) <= 30 and len(o) <= 30:
            try:
                chunk_no = int(it.get("chunk_no") or 0)
            except (TypeError, ValueError):
                chunk_no = 0
            out.append({"s": s, "r": r, "o": o,
                        "s_type": str(it.get("s_type") or "").strip(),
                        "o_type": str(it.get("o_type") or "").strip(),
                        "chunk_no": chunk_no})
    return out


def _parse_triples_v2(ans: str) -> list[dict]:
    """解析 LLM 三元组输出：JSON 数组优先，行式回退，逐条校验丢弃坏条目。"""
    if not ans:
        return []
    m = re.search(r"\[.*\]", ans, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return _validate_triples(arr)
        except Exception:
            pass
    # 行式回退：逐个 {...} 解析
    line_objs = []
    for frag in re.findall(r"\{[^{}]*\}", ans):
        try:
            line_objs.append(json.loads(frag))
        except Exception:
            pass
    return _validate_triples(line_objs)


_TRIVIAL_BLACK = ("本文", "本节", "本章", "章节", "附录", "摘要", "目录", "前言")
_TRIVIAL_PAT = re.compile(r"(^第[一二三四五六七八九十百0-9]+[章节条])|^[0-9]+(\.[0-9]+)+$|^[0-9]+$|^[图表][0-9一二三四五六七八九十]")


def _is_trivial(s: str) -> bool:
    """噪声判断：空/过短/章节标题/纯数字标点/黑名单词。"""
    if not s:
        return True
    s = s.strip()
    if len(s) < 2:
        return True
    if s in _TRIVIAL_BLACK:
        return True
    if _TRIVIAL_PAT.match(s):
        return True
    if re.fullmatch(r"[\d\.\s\-/,，。：:;；、]+", s):
        return True
    return False


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _surface_forms(raw: str, canonical: str) -> set[str]:
    """出处校验用的写法集合：原文写法、标准名、以及别名表中指向该标准名的别名。"""
    from app.services.term_service import _load_terms
    forms = {raw, canonical} | {alias for alias, std in _load_terms().items() if std == canonical}
    return {_compact(f) for f in forms if f and len(f.strip()) >= 1}


def _resolve_provenance(tp: dict, s: str, o: str) -> tuple[str, str]:
    """出处校验（《设计文档》§4.2）→ (chunk_id, provenance)。

    候选 chunk = 批内第 chunk_no 个；s 与 o（任一写法）都出现 → verified；
    否则在同批 chunk 中找第一个同时包含二者的 → rematched；找不到 → ("", "none")。
    """
    batch = tp.get("_batch") or []
    if not batch:
        return "", "none"
    s_forms = _surface_forms(tp.get("s", ""), s)
    o_forms = _surface_forms(tp.get("o", ""), o)

    def _hit(text: str) -> bool:
        t = _compact(text)
        return any(f in t for f in s_forms) and any(f in t for f in o_forms)

    no = tp.get("chunk_no") or 0
    if 1 <= no <= len(batch) and batch[no - 1][0] and _hit(batch[no - 1][1]):
        return batch[no - 1][0], "verified"
    for chunk_id, text in batch:
        if chunk_id and _hit(text):
            return chunk_id, "rematched"
    return "", "none"


def _normalize_triples(triples: list[dict]) -> list[dict]:
    """全局后处理：实体归一 + 关系白名单 + 噪声过滤 + 出处校验 + 去重(s,r,o,chunk_id)。

    输出字段：s/r/o（归一化后）、s_raw/o_raw（原文写法）、s_type/o_type（领域包实体类型，
    不在 schema 内置空）、chunk_id、provenance（verified|rematched|none）。
    """
    from app.core.domain_pack import get_pack
    from app.services.kg_normalize import canonical_entity, canonical_relation
    types = set(get_pack().entity_types)
    seen: set = set()
    out: list[dict] = []
    for tp in triples:
        if not isinstance(tp, dict):
            continue
        s_raw, o_raw = str(tp.get("s", "")).strip(), str(tp.get("o", "")).strip()
        s = canonical_entity(s_raw)
        r = canonical_relation(str(tp.get("r", "")))
        o = canonical_entity(o_raw)
        if not (s and r and o):
            continue
        if s == o or _is_trivial(s) or _is_trivial(o):
            continue
        chunk_id, provenance = _resolve_provenance(tp, s, o)
        key = (s, r, o, chunk_id)
        if key in seen:
            continue
        seen.add(key)
        s_type, o_type = tp.get("s_type") or "", tp.get("o_type") or ""
        out.append({"s": s[:256], "r": r[:128], "o": o[:256],
                    "s_raw": s_raw[:256], "o_raw": o_raw[:256],
                    "s_type": s_type if s_type in types else "",
                    "o_type": o_type if o_type in types else "",
                    "chunk_id": chunk_id, "provenance": provenance})
    return out


def _is_content_rejected(e: Exception) -> bool:
    """云端模型的内容审核拒绝（百炼 data_inspection_failed）：同一输入重试结果不变。"""
    return "data_inspection_failed" in str(e)


async def _extract_from_chunks(provider, chunks: list, stats: dict | None = None) -> list[dict]:
    """分批调 LLM 抽取（领域包 schema 约束 prompt，批内编号 [1]…）→ 解析为原始三元组。单批失败降级。

    chunks：(chunk_id, text) 列表；也接受纯文本列表（无 chunk_id，出处校验一律置空）。
    每条原始三元组附带 `_batch`（该批的 (chunk_id, text)），供 _normalize_triples 做出处校验。
    stats：可选，累计 {"batches": 总批数, "failed_batches": 失败批数, "rejected_batches": 被内容审核拒绝的批数}。
    失败批（超时、限流等）重跑可能成功，重建脚本据此重试；被拒批是确定性的，重试无用，单独计数。
    """
    from app.core.domain_pack import get_pack
    pack = get_pack()
    pairs = [c if isinstance(c, tuple) else ("", c) for c in chunks]
    all_triples: list[dict] = []
    for i in range(0, len(pairs), _BATCH):
        batch = pairs[i:i + _BATCH]
        if stats is not None:
            stats["batches"] = stats.get("batches", 0) + 1
        text = "\n\n".join(f"[{n}] {t}" for n, (_cid, t) in enumerate(batch, start=1))
        try:
            ans = await provider.chat(
                [{"role": "user", "content": pack.render_prompt("extract", text=text)}],
                temperature=0.1, max_tokens=4096, timeout=settings.KG_EXTRACT_TIMEOUT,
            )
            for tp in _parse_triples_v2(ans):
                tp["_batch"] = batch
                all_triples.append(tp)
        except Exception as e:
            if stats is not None:
                key = "rejected_batches" if _is_content_rejected(e) else "failed_batches"
                stats[key] = stats.get(key, 0) + 1
            degraded("kg_extract_batch", e)
    return all_triples


async def extract_triples(db: AsyncSession, doc_id: str, model_type: str | None = None,
                          *, write_lock: "asyncio.Lock | None" = None) -> dict:
    """对某文档分块批量抽取三元组，增量写 MySQL + Neo4j（不清旧，仅追加新条目）。

    write_lock：并发建图时传入同一把锁，LLM 抽取并发、写库阶段串行（避免 Neo4j 并发 MERGE
    同一实体节点时死锁）。单次调用不需要。
    """
    doc = (await db.execute(select(Document).where(Document.id == doc_id))).scalar_one_or_none()
    if not doc:
        raise BizError("文档不存在", 404)
    rows = (
        await db.execute(select(Chunk).where(Chunk.doc_id == doc_id).order_by(Chunk.chunk_idx))
    ).scalars().all()
    if not rows:
        raise BizError("文档尚未解析，请先解析", 400)

    provider = get_llm_provider(model_type)
    stats: dict = {}
    raw = await _extract_from_chunks(provider, [(c.id, c.content) for c in rows], stats)
    triples = _normalize_triples(raw)
    async with (write_lock or contextlib.nullcontext()):
        new_triples, neo4j_ok = await _save_triples(db, doc, triples)

    try:
        from app.core import metrics
        total = (await db.execute(select(func.count()).select_from(KgTriple))).scalar() or 0
        metrics.KG_EXTRACT.inc()
        metrics.KB_TRIPLES.set(total)
    except Exception:
        pass
    prov = {k: sum(1 for t in triples if t["provenance"] == k) for k in ("verified", "rematched", "none")}
    return {"tripleCount": len(triples), "newCount": len(new_triples), "provenance": prov,
            "neo4jOk": neo4j_ok, "failedBatches": stats.get("failed_batches", 0),
            "rejectedBatches": stats.get("rejected_batches", 0),
            "docName": doc.doc_name, "sample": triples[:30]}


async def _save_triples(db: AsyncSession, doc: Document, triples: list[dict]) -> tuple[list[dict], bool]:
    """写库阶段：去重后追加到 MySQL，再 MERGE 到 Neo4j；返回 (实际新增的三元组, Neo4j 是否写入成功)。"""
    doc_id = doc.id
    # ★ 增量追加：查已有三元组，只插入新条目（不清旧）；去重键含出处 chunk（§16.7）
    existing = (await db.execute(
        select(KgTriple.subject, KgTriple.relation, KgTriple.object, KgTriple.chunk_id)
        .where(KgTriple.doc_id == doc_id)
    )).all()
    existing_set = {(r[0], r[1], r[2], r[3] or "") for r in existing}
    new_triples = [t for t in triples if (t["s"], t["r"], t["o"], t["chunk_id"]) not in existing_set]

    # 写 MySQL（仅新增，不删旧）
    for tp in new_triples:
        db.add(KgTriple(subject=tp["s"], relation=tp["r"], object=tp["o"],
                        doc_id=doc_id, doc_name=doc.doc_name, chunk_id=tp["chunk_id"],
                        s_raw=tp["s_raw"], o_raw=tp["o_raw"], provenance=tp["provenance"]))
    await db.commit()

    # 写 Neo4j（MERGE 幂等，仅新增边，不去旧；来源 doc_ids/chunk_ids 列表、实体类型取领域包 schema）
    neo4j_ok = True
    if new_triples:
        try:
            await neo4j_client.upsert_triples(new_triples, doc_id, doc.doc_name)
        except Exception as e:
            neo4j_ok = False
            degraded("kg_neo4j_write", e)
    return new_triples, neo4j_ok


async def _get_graph_mysql(
    db: AsyncSession, entity: str, limit: int, *, tenant: str | None = None
) -> dict:
    """MySQL 一跳查询（Neo4j 不可用时的回退）。"""
    stmt = select(KgTriple).join(Document, Document.id == KgTriple.doc_id)
    if tenant:
        stmt = stmt.where(Document.tenant_id == tenant)
    if entity:
        kw = f"%{entity}%"
        stmt = stmt.where(or_(KgTriple.subject.like(kw), KgTriple.object.like(kw)))
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    if tenant:
        try:
            from app.services import knowledge_governance_service

            blocked = await knowledge_governance_service.blocked_document_ids(
                db, [row.doc_id for row in rows], tenant_id=tenant,
            )
            rows = [row for row in rows if row.doc_id not in blocked]
        except Exception as exc:
            degraded("kg_graph_governance_filter", exc)
            rows = []
    nodes: dict[str, dict] = {}
    links: list[dict] = []
    for t in rows:
        if t.subject not in nodes:
            nodes[t.subject] = {"id": t.subject, "name": t.subject, "category": 0, "symbolSize": 36}
        if t.object not in nodes:
            nodes[t.object] = {"id": t.object, "name": t.object, "category": 1, "symbolSize": 28}
        links.append({"source": t.subject, "target": t.object, "value": t.relation})
    return {
        "nodes": list(nodes.values()), "links": links,
        "categories": [{"name": "实体"}, {"name": "属性/关系"}], "total": len(rows),
    }


async def get_graph(
    db: AsyncSession, entity: str = "", limit: int = 300, *, tenant: str | None = None
) -> dict:
    """关系图谱：优先 Neo4j 图查询，未启动则回退 MySQL 一跳。"""
    if tenant is not None:
        return await _get_graph_mysql(db, entity, limit, tenant=tenant)
    try:
        return await neo4j_client.get_neighbors(entity, limit)
    except Exception as e:
        degraded("kg_neo4j_fallback", e, "回退 MySQL")
        return await _get_graph_mysql(db, entity, limit)


async def get_paths(entity: str, depth: int = 3, limit: int = 20) -> list[dict]:
    """多跳影响链：设备→故障→处置→关联设备 的因果传播（仅 Neo4j）。"""
    try:
        return await neo4j_client.get_paths(entity, depth, limit)
    except Exception as e:
        degraded("kg_paths", e)
        return []


async def get_hubs(limit: int = 15) -> list[dict]:
    """枢纽实体：出度最高（影响传播源头，核心设备/故障，仅 Neo4j）。"""
    try:
        return await neo4j_client.get_hubs(limit)
    except Exception as e:
        degraded("kg_hubs", e)
        return []


async def _tokenize_query(query: str) -> list[str]:
    """GraphRAG query 分词（jieba）。B5：同 query 分词结果 Redis 缓存（默认关）。

    - KG_TOKENIZE_CACHE_ENABLE=False（默认）→ 现状，每次 jieba.cut
    - KG_TOKENIZE_CACHE_ENABLE=True → Redis 缓存（key=jieba:{query}），miss 才分词 + 写回
    Redis 异常静默降级（不走缓存，回退到直接分词）。
    """
    import jieba
    if not getattr(settings, "KG_TOKENIZE_CACHE_ENABLE", False):
        return [w for w in jieba.cut(query) if len(w.strip()) > 1]
    key = f"jieba:{query}"
    try:
        cached = await redis_client.get_redis().get(key)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    words = [w for w in jieba.cut(query) if len(w.strip()) > 1]
    try:
        await redis_client.get_redis().set(key, json.dumps(words, ensure_ascii=False), ex=86400)
    except Exception:
        pass
    return words


async def graph_context(
    query: str,
    topk: int = 8,
    *,
    db: AsyncSession | None = None,
    tenant: str | None = None,
) -> list[str]:
    """GraphRAG：从 query 提取关键词查 Neo4j 关联三元组，文本化作为问答结构化上下文。

    让问答"走 Neo4j"——检索文档分块之外，补充图谱结构化关系（设备-故障-处置链）。
    """
    words = await _tokenize_query(query)
    if not words:
        return []
    if tenant is not None:
        # Neo4j 旧图尚未携带 tenant 属性；租户上下文下改走带来源文档关联的
        # MySQL 三元组，避免跨租户读取全局图。
        if db is None:
            return []
        keyword_filters = []
        for word in words[:8]:
            like = f"%{word}%"
            keyword_filters.extend((KgTriple.subject.like(like), KgTriple.object.like(like)))
        rows = (await db.execute(
            select(KgTriple)
            .join(Document, Document.id == KgTriple.doc_id)
            .where(
                Document.tenant_id == tenant,
                or_(*keyword_filters),
            )
            .limit(max(topk, 1) * 3)
        )).scalars().all()
        try:
            from app.services import knowledge_governance_service

            blocked = await knowledge_governance_service.blocked_document_ids(
                db, [row.doc_id for row in rows], tenant_id=tenant,
            )
        except Exception as exc:
            degraded("kg_graph_governance_filter", exc)
            blocked = {row.doc_id for row in rows}
        filtered = [
            f"{row.subject} --{row.relation}--> {row.object}"
            for row in rows
            if row.subject and row.object and row.doc_id not in blocked
        ]
        return filtered[:topk]
    try:
        rows = await neo4j_client.query_triples_by_keywords(words, topk)
    except Exception as e:
        degraded("kg_graph_context", e)
        return []
    return [f"{r['s']} --{r['rel']}--> {r['o']}" for r in rows if r.get("s") and r.get("o")]


async def get_stats(db: AsyncSession) -> dict:
    """知识图谱统计（MySQL 三元组表）。"""
    triple_total = (await db.execute(select(func.count()).select_from(KgTriple))).scalar() or 0
    entity_total = (await db.execute(
        select(func.count(func.distinct(KgTriple.subject)))
    )).scalar() or 0
    rel_total = (await db.execute(
        select(func.count(func.distinct(KgTriple.relation)))
    )).scalar() or 0
    by_doc = (await db.execute(
        select(KgTriple.doc_name, func.count())
        .group_by(KgTriple.doc_name).order_by(func.count().desc()).limit(10)
    )).all()
    try:
        from app.core import metrics
        metrics.KB_TRIPLES.set(triple_total)
    except Exception:
        pass
    return {
        "tripleTotal": triple_total,
        "entityTotal": entity_total,
        "relationTotal": rel_total,
        "byDoc": [{"docName": r[0] or "未知", "count": r[1]} for r in by_doc],
        "hubCount": await _hub_count(),
    }


async def _hub_count() -> int:
    """Neo4j 中有出度的实体数（可作为影响链传播源头的实体数）。"""
    try:
        from app.clients.neo4j_client import _get
        async with _get().session() as s:
            result = await s.run(
                "MATCH (n:Entity)-[r:REL]->() RETURN count(DISTINCT n) AS cnt"
            )
            rec = await result.single()
            return rec["cnt"] if rec else 0
    except Exception:
        return 0
