"""红队回归：过时知识 / 缓存污染（离线，sqlite）。

攻击面：知识被撤回/替代/过期后，历史缓存答案仍带着旧证据继续回答；
跨租户缓存条目串台；被多人点踩的坏答案通过缓存反复命中。

当前真实防线（file:line 以调研时代码为准）：
- backend/app/services/qa_service.py:167-204 _cache_knowledge_valid：
  缓存命中前复核证据文档——文档不在本租户 documents 表 → False；
  治理状态 withdrawn/superseded/not_yet_effective/expired → False；
  治理查询异常且带租户 → fail-closed False
- backend/app/services/knowledge_governance_service.py:194-210 effective_state；
  213-221 is_retrievable；224-241 blocked_document_ids
- backend/app/services/qa_service.py:112-116 _is_blacklisted →
  backend/app/services/feedback_optimizer_service.py:261-267 is_query_blacklisted
  （Redis set 黑名单，dislike≥3 写入）
- backend/app/services/qa_service.py:1096-1098 只有 confidence=="high" 才写缓存
  （低置信/黑名单不落缓存，防污染写路径）

已知缺口（不为缺失防御写断言）见 docs/redteam/README.md。
"""
import asyncio
from datetime import datetime

import pytest

from app.models.document import Document
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.services import feedback_optimizer_service, knowledge_governance_service
from app.services import qa_service


async def _seed_doc(db, doc_id: str, tenant: str) -> None:
    db.add(Document(
        id=doc_id,
        doc_name=f"{doc_id}.pdf",
        minio_object=f"kb/{tenant}/{doc_id}.pdf",
        status="vectorized",
        tenant_id=tenant,
    ))
    await db.flush()


def _meta(doc_id: str, tenant: str, **kw) -> KnowledgeDocumentMetadata:
    base = dict(
        doc_id=doc_id,
        tenant_id=tenant,
        version_status="active",
        effective_at=datetime(2026, 1, 1),
        expires_at=datetime(2030, 1, 1),
        is_permanent=False,
    )
    base.update(kw)
    return KnowledgeDocumentMetadata(**base)


def _cached_entry(doc_ids: list[str]) -> dict:
    return {
        "answer": "油温限值 85℃（来自旧版规程）",
        "confidence": "high",
        "retrievalSource": [{"docId": d, "docName": f"{d}.pdf"} for d in doc_ids],
    }


# ===== 缓存答案引用的知识时效复核 =====


@pytest.mark.asyncio
async def test_cache_entry_referencing_deleted_document_rejected(test_db):
    """引用的文档已被删除（documents 表无记录）→ 缓存不可用。"""
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["deleted-doc-1"]), "t1",
    ) is False


@pytest.mark.asyncio
async def test_cache_entry_referencing_withdrawn_document_rejected(test_db):
    await _seed_doc(test_db, "doc-wd", "t1")
    test_db.add(_meta("doc-wd", "t1", version_status="withdrawn"))
    await test_db.commit()
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["doc-wd"]), "t1",
    ) is False


@pytest.mark.asyncio
async def test_cache_entry_referencing_expired_document_rejected(test_db):
    await _seed_doc(test_db, "doc-exp", "t1")
    test_db.add(_meta(
        "doc-exp", "t1",
        effective_at=datetime(2025, 1, 1),
        expires_at=datetime(2026, 1, 1),      # 已过期
    ))
    await test_db.commit()
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["doc-exp"]), "t1",
    ) is False


@pytest.mark.asyncio
async def test_cache_entry_referencing_superseded_document_rejected(test_db):
    await _seed_doc(test_db, "doc-sup", "t1")
    test_db.add(_meta("doc-sup", "t1", version_status="superseded"))
    await test_db.commit()
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["doc-sup"]), "t1",
    ) is False


@pytest.mark.asyncio
async def test_cache_entry_with_valid_active_document_accepted(test_db):
    await _seed_doc(test_db, "doc-ok", "t1")
    test_db.add(_meta("doc-ok", "t1"))
    await test_db.commit()
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["doc-ok"]), "t1",
    ) is True


@pytest.mark.asyncio
async def test_cache_entry_cross_tenant_document_rejected(test_db):
    """缓存条目引用别的租户的文档（缓存串台/跨租户投毒）→ 不可用。"""
    await _seed_doc(test_db, "doc-b", "tenant-b")
    test_db.add(_meta("doc-b", "tenant-b"))
    await test_db.commit()
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["doc-b"]), "t1",
    ) is False


@pytest.mark.asyncio
async def test_cache_validation_fails_closed_on_governance_error(test_db, monkeypatch):
    """治理查询异常 + 带租户 → fail-closed 拒绝缓存（qa_service.py:193-204）。"""
    await _seed_doc(test_db, "doc-x", "t1")
    await test_db.commit()

    async def _boom(*_a, **_k):
        raise RuntimeError("governance backend down")

    monkeypatch.setattr(
        knowledge_governance_service, "blocked_document_ids", _boom,
    )
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["doc-x"]), "t1",
    ) is False


@pytest.mark.asyncio
async def test_cache_validation_without_tenant_fails_open_documented_gap(test_db, monkeypatch):
    """【缺口固化】tenant 为空时 fail-open（qa_service.py:187/204 `not bool(tenant)`）：
    治理查询异常且无租户 → 缓存照常可用（离线/兼容路径不校验时效）。

    若内部调用方未来统一强制带租户或改 fail-closed，请同步更新 docs/redteam/README.md。
    """

    async def _boom(*_a, **_k):
        raise RuntimeError("governance backend down")

    monkeypatch.setattr(knowledge_governance_service, "blocked_document_ids", _boom)
    assert await qa_service._cache_knowledge_valid(
        test_db, _cached_entry(["any-doc"]), None,
    ) is True


# ===== 缓存黑名单（坏答案禁命中）=====


class _StubRedis:
    def __init__(self, members: set[str]):
        self._members = members

    async def sismember(self, key: str, value: str) -> bool:
        return value in self._members


def test_blacklisted_query_never_served_from_cache(monkeypatch):
    """被点踩拉黑的 query（dislike≥3）→ is_query_blacklisted 命中，禁止任何缓存层命中。"""
    from app.clients import redis_client

    monkeypatch.setattr(
        redis_client, "get_redis",
        lambda: _StubRedis({"1号主变油温异常怎么办"}),
    )
    assert asyncio.run(feedback_optimizer_service.is_query_blacklisted("1号主变油温异常怎么办")) is True
    assert asyncio.run(feedback_optimizer_service.is_query_blacklisted("正常问题")) is False


def test_blacklist_check_fails_open_without_redis_documented_gap(monkeypatch):
    """【缺口固化】Redis 异常 → 黑名单检查 fail-open 返回 False
    （feedback_optimizer_service.py:266-267）。此时 L2 MySQL 缓存仍可命中被拉黑答案。

    现状固化：若改为 fail-closed 或本地兜底，请同步更新 docs/redteam/README.md。
    """
    from app.clients import redis_client

    def _boom(*_a, **_k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(redis_client, "get_redis", _boom)
    assert asyncio.run(feedback_optimizer_service.is_query_blacklisted("任意问题")) is False


# ===== 治理状态矩阵（is_retrievable 纯函数基线）=====


@pytest.mark.parametrize(
    ("kw", "expected_state"),
    [
        (dict(version_status="draft", effective_at=None), "draft"),
        (dict(version_status="withdrawn"), "withdrawn"),
        (dict(version_status="superseded"), "superseded"),
        (dict(effective_at=datetime(2030, 1, 1)), "not_yet_effective"),
        (dict(effective_at=datetime(2025, 1, 1), expires_at=datetime(2026, 1, 1)), "expired"),
        (dict(effective_at=datetime(2026, 1, 1), is_permanent=True), "active"),
        (dict(effective_at=None), "metadata_incomplete"),
    ],
)
def test_effective_state_matrix(kw, expected_state):
    meta = _meta("doc-matrix", "t1", **kw)
    assert knowledge_governance_service.effective_state(meta) == expected_state
    retrievable = knowledge_governance_service.is_retrievable(meta)
    assert retrievable == (expected_state not in {
        "superseded", "withdrawn", "not_yet_effective", "expired",
    })


# ===== 写路径防污染：低置信不落缓存（条件硬编码在 answer 内，这里固化关键常量）=====


def test_cache_write_requires_high_confidence_condition():
    """缓存写入条件（qa_service.py:1096-1098）要求 confidence=="high"；
    用源码级断言防止条件被悄悄放宽（如 medium 也可写）。"""
    import inspect

    src = inspect.getsource(qa_service)
    assert 'confidence == "high" and not await _is_blacklisted(nq)' in src
