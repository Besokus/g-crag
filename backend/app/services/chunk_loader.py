"""按 Chunk.id 批量取正文，组装成与检索结果同构的条目（设计文档 §16.1）。

图谱定位证据拿到的是出处 chunk_id，需要变成与 mixed_search 输出相同的结构，才能与原上下文合并、
统一重打分、进入生成与引用。small-to-big 规则直接复用 retrieval_service._expand_parents。
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.document import Document


async def load_items(db: AsyncSession, chunk_ids: list[str], *, expand_parent: bool,
                     origin: str, sources: list[str]) -> list[dict]:
    """按输入顺序返回检索条目（score=0.0）；expand_parent=True 时按父块拼全文并按父块去重。
    缺失的 id 静默跳过。"""
    from app.services import retrieval_service as rs
    if not chunk_ids:
        return []
    rows = (await db.execute(
        select(Chunk.id, Chunk.doc_id, Chunk.chunk_idx, Chunk.content, Document.doc_name, Document.doc_type)
        .join(Document, Document.id == Chunk.doc_id)
        .where(Chunk.id.in_(chunk_ids))
    )).all()
    by_id = {r[0]: r for r in rows}
    hits = []
    for cid in chunk_ids:
        r = by_id.get(cid)
        if r is None:
            continue
        hits.append({"chunk_id": r[0], "doc_id": r[1], "chunk_idx": r[2], "text": r[3] or "",
                     "doc_name": r[4] or "", "doc_type": r[5] or "", "score": 0.0,
                     "srcs": list(sources)})
    if expand_parent:
        hits = await rs._expand_parents(db, hits)   # 分数全为 0，排序稳定，保持输入顺序
    return [{**rs._to_item(h), "origin": origin} for h in hits]
