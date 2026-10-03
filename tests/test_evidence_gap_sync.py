"""证据补全回流（evidence_gap_service.confirm_and_sync）：入库 + 向量化 + 缓存键与问答读取一致（容器真 DB）。

原缺陷：回流写的缓存键是 `qa:{tenant}:{model_type or 'default'}:...`，而问答读的是
`provider@模型#纠错模式`，确认入库后再问永远命中不到；medium 旧答案也不会被覆盖。
"""
import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select

from app.services import evidence_gap_service as svc

pytestmark = pytest.mark.integration


def test_confirm_and_sync_writes_cache_for_every_mode(monkeypatch):
    from app.clients import minio_client, redis_client
    from app.providers.factory import llm_cache_tag
    from app.rag.gcrag import MODES
    from app.services import cache_persist, document_service, qa_service

    redis_keys, mysql_keys, vectorized = [], [], []

    async def fake_redis_set(key, value, ttl=None):
        redis_keys.append(key)

    async def fake_mysql_set(db, model_type, nq, q, result, tenant_id="default"):
        mysql_keys.append((model_type, nq, tenant_id))

    async def fake_vectorize(db, doc_id):
        vectorized.append(doc_id)

    monkeypatch.setattr(minio_client, "put_object", lambda *a, **k: None)
    monkeypatch.setattr(document_service, "vectorize_document", fake_vectorize)
    monkeypatch.setattr(cache_persist, "cache_set_mysql", fake_mysql_set)
    monkeypatch.setattr(redis_client, "cache_set_json", fake_redis_set)

    async def go():
        from app.db.session import AsyncSessionLocal
        from app.models.chunk import Chunk
        from app.models.evidence_gap import EvidenceGap
        q = f"测试_回流_{datetime.now().isoformat()}"
        gid = await svc.collect(q, "", "refused", "incorrect", "refused", "auto_crag", "t_sync")
        res = await svc.confirm_and_sync(gid, "人工确认的答案：碧水源中标 13.57 亿元。", "admin")
        assert res["ok"] and vectorized == [res["docId"]]
        async with AsyncSessionLocal() as db:
            row = (await db.execute(select(EvidenceGap).where(EvidenceGap.id == gid))).scalar_one()
            chunk = (await db.execute(select(Chunk).where(Chunk.doc_id == res["docId"]))).scalar_one()
        assert (row.status, row.synced_doc_id) == ("synced", res["docId"]) and "13.57" in chunk.content
        expect = {f"{llm_cache_tag(None)}#{m}" for m in MODES}
        assert {mt for mt, _, _ in mysql_keys} == expect and {t for _, _, t in mysql_keys} == {"t_sync"}
        assert set(redis_keys) == {qa_service._cache_key(ck, q, "t_sync") for ck in expect}   # 与问答读取同一个键
    asyncio.run(go())
