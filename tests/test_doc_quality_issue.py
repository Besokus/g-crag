"""B3: judge 聚合差评文档 → KnowledgeGovernanceIssue(quality_low)。

Feedback 表无 tenant 列（仅 username），用唯一 doc_name 隔离 Feedback 聚合命中；
Document/Issue 按 tenant_id 隔离计数与清理。
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.document import Document
from app.models.feedback import Feedback
from app.models.knowledge_governance import KnowledgeGovernanceIssue
from app.services import doc_quality_service as svc
from app.services import knowledge_governance_service as gov

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


@pytest_asyncio.fixture
async def isolated_tenant_and_doc():
    """唯一 tenant_id + 唯一 doc_name，确保聚合率基线干净。

    Feedback 无 tenant 列 → 用唯一 doc_name 隔离 Feedback 行；Document/Issue 用
    tenant_id 隔离计数与清理。setup 清理同 doc_name 历史残留（failed run 遗留）；
    teardown 按精确 doc_name + tenant_id 清理本测试造的行，不污染共享库。
    """
    tenant = f"test_docqual_{uuid4().hex[:8]}"
    stamp = uuid4().hex[:8]
    doc_name = f"测试差评文档_{stamp}"

    # setup：清理同 doc_name 的 Feedback 历史残留（聚合全表扫描，防累积污染基线）
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Feedback).where(Feedback.retrieval_sources.like(f"%{doc_name}%")))
        await db.commit()

    yield {"tenant": tenant, "doc_name": doc_name}

    # teardown：精确清理本测试造的行
    async with AsyncSessionLocal() as db:
        # 先查 doc_id 用于清 issue（issue 有 tenant_id 索引可先按 tenant 删）
        await db.execute(delete(KnowledgeGovernanceIssue).where(
            KnowledgeGovernanceIssue.tenant_id == tenant))
        docs = (await db.execute(select(Document.id).where(Document.tenant_id == tenant))).scalars().all()
        if docs:
            await db.execute(delete(Document).where(Document.id.in_(docs)))
        await db.execute(delete(Feedback).where(Feedback.retrieval_sources.like(f"%{doc_name}%")))
        await db.commit()


def _make_feedback(doc_name: str, feedback: str, days_ago: int = 0) -> Feedback:
    return Feedback(
        query=f"q_{doc_name}",
        feedback=feedback,
        retrieval_sources=doc_name,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


@pytest.mark.asyncio
async def test_generate_issue_when_high_dislike_rate(isolated_tenant_and_doc):
    """3 dislike + 1 like（rate=0.75 ≥ 0.5，count=3 ≥ 3）→ 生成 quality_low issue。"""
    ctx = isolated_tenant_and_doc
    tenant, doc_name = ctx["tenant"], ctx["doc_name"]
    async with AsyncSessionLocal() as db:
        doc = Document(doc_name=doc_name, minio_object="x", tenant_id=tenant)
        db.add(doc)
        await db.flush()
        db.add_all([
            _make_feedback(doc_name, "dislike", days_ago=1),
            _make_feedback(doc_name, "dislike", days_ago=1),
            _make_feedback(doc_name, "dislike", days_ago=0),
            _make_feedback(doc_name, "like", days_ago=0),
        ])
        await db.commit()

        result = await svc.aggregate_doc_quality(db, tenant)
        assert result["generated"] == 1
        assert result["deduped"] == 0

        issue = (await db.execute(select(KnowledgeGovernanceIssue).where(
            KnowledgeGovernanceIssue.tenant_id == tenant,
            KnowledgeGovernanceIssue.issue_type == "quality_low",
        ))).scalar_one()
        assert issue.doc_id == doc.id
        assert issue.fingerprint == f"quality_low:{doc.id}"
        assert issue.occurrence_count == 1


@pytest.mark.asyncio
async def test_dedup_on_rescan(isolated_tenant_and_doc):
    """重复扫描 → 不重复 insert，occurrence_count 累加。"""
    ctx = isolated_tenant_and_doc
    tenant, doc_name = ctx["tenant"], ctx["doc_name"]
    async with AsyncSessionLocal() as db:
        doc = Document(doc_name=doc_name, minio_object="x", tenant_id=tenant)
        db.add(doc)
        await db.flush()
        db.add_all([_make_feedback(doc_name, "dislike", days_ago=i) for i in range(3)])
        await db.commit()

        first = await svc.aggregate_doc_quality(db, tenant)
        assert first["generated"] == 1
        assert first["deduped"] == 0

        second = await svc.aggregate_doc_quality(db, tenant)
        assert second["generated"] == 0
        assert second["deduped"] == 1

        issues = (await db.execute(select(KnowledgeGovernanceIssue).where(
            KnowledgeGovernanceIssue.tenant_id == tenant,
            KnowledgeGovernanceIssue.issue_type == "quality_low",
        ))).scalars().all()
        assert len(issues) == 1
        assert issues[0].occurrence_count == 2


@pytest.mark.asyncio
async def test_no_issue_below_threshold(isolated_tenant_and_doc):
    """率低于阈值（1 dislike + 3 like = 0.25 < 0.5）→ 不生成。"""
    ctx = isolated_tenant_and_doc
    tenant, doc_name = ctx["tenant"], ctx["doc_name"]
    async with AsyncSessionLocal() as db:
        doc = Document(doc_name=doc_name, minio_object="x", tenant_id=tenant)
        db.add(doc)
        await db.flush()
        db.add_all([
            _make_feedback(doc_name, "dislike", days_ago=0),
            _make_feedback(doc_name, "like", days_ago=0),
            _make_feedback(doc_name, "like", days_ago=0),
            _make_feedback(doc_name, "like", days_ago=0),
        ])
        await db.commit()

        result = await svc.aggregate_doc_quality(db, tenant)
        assert result["generated"] == 0

        issues = (await db.execute(select(KnowledgeGovernanceIssue).where(
            KnowledgeGovernanceIssue.tenant_id == tenant,
        ))).scalars().all()
        assert len(issues) == 0


@pytest.mark.asyncio
async def test_no_issue_below_min_count(isolated_tenant_and_doc):
    """count 不足（2 dislike，rate=1.0 但 count=2 < 3）→ 不生成。"""
    ctx = isolated_tenant_and_doc
    tenant, doc_name = ctx["tenant"], ctx["doc_name"]
    async with AsyncSessionLocal() as db:
        doc = Document(doc_name=doc_name, minio_object="x", tenant_id=tenant)
        db.add(doc)
        await db.flush()
        db.add_all([
            _make_feedback(doc_name, "dislike", days_ago=0),
            _make_feedback(doc_name, "dislike", days_ago=0),
        ])
        await db.commit()

        result = await svc.aggregate_doc_quality(db, tenant)
        assert result["generated"] == 0


@pytest.mark.asyncio
async def test_skipped_when_disabled(isolated_tenant_and_doc, monkeypatch):
    """DOC_QUALITY_ISSUE_ENABLE=False → 直接返回零值，不查不写。"""
    ctx = isolated_tenant_and_doc
    tenant, doc_name = ctx["tenant"], ctx["doc_name"]
    monkeypatch.setattr(settings, "DOC_QUALITY_ISSUE_ENABLE", False)
    async with AsyncSessionLocal() as db:
        doc = Document(doc_name=doc_name, minio_object="x", tenant_id=tenant)
        db.add(doc)
        await db.flush()
        db.add_all([_make_feedback(doc_name, "dislike", days_ago=i) for i in range(5)])
        await db.commit()

        result = await svc.aggregate_doc_quality(db, tenant)
        assert result == {"scanned": 0, "generated": 0, "deduped": 0}

        issues = (await db.execute(select(KnowledgeGovernanceIssue).where(
            KnowledgeGovernanceIssue.tenant_id == tenant,
        ))).scalars().all()
        assert len(issues) == 0


@pytest.mark.asyncio
async def test_run_scan_invokes_aggregation(isolated_tenant_and_doc):
    """governance run_scan 末尾调 aggregate_doc_quality → docQuality 字段返回。"""
    ctx = isolated_tenant_and_doc
    tenant, doc_name = ctx["tenant"], ctx["doc_name"]
    async with AsyncSessionLocal() as db:
        doc = Document(doc_name=doc_name, minio_object="x", tenant_id=tenant)
        db.add(doc)
        await db.flush()
        db.add_all([_make_feedback(doc_name, "dislike", days_ago=i) for i in range(3)])
        await db.commit()

        scan_result = await gov.run_scan(
            db, tenant, include_conflicts=False, max_documents=10,
        )
        assert "docQuality" in scan_result
        assert scan_result["docQuality"]["generated"] == 1
