"""B2: KB_FRESHNESS = active未过期文档占比（governance scan 末尾 set）。"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.session import AsyncSessionLocal
from app.models.document import Document
from app.models.knowledge_governance import KnowledgeDocumentMetadata
from app.services import knowledge_governance_service as gov

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


@pytest_asyncio.fixture
async def isolated_tenant():
    """唯一 tenant_id 隔离 Document/metadata 全表计数。

    _set_freshness_metric 按租户全表算占比；default 租户在共享 DB 上有历史
    Document/metadata 残留 → 固定 3 文档造数会被污染致非确定性 FAIL。用唯一
    tenant 确保 total=3、active=1 的确定基线（比唯一 doc_name 更强的隔离）。
    teardown 按 tenant_id 精确清理本测试造的行，不污染共享库。
    """
    tenant = f"test_freshness_{uuid4().hex[:8]}"
    yield tenant
    # teardown：先删 metadata 再删 document（不依赖 FK CASCADE 是否落地）
    async with AsyncSessionLocal() as db:
        await db.execute(delete(KnowledgeDocumentMetadata).where(
            KnowledgeDocumentMetadata.tenant_id == tenant))
        await db.execute(delete(Document).where(Document.tenant_id == tenant))
        await db.commit()


@pytest.mark.asyncio
async def test_freshness_active_ratio(isolated_tenant):
    """3 文档：1 active未过期 + 1 expired + 1 draft → freshness = 1/3 ≈ 0.333。"""
    tenant = isolated_tenant
    async with AsyncSessionLocal() as db:
        # 用唯一 doc_name 便于排查（tenant_id 已隔离计数，doc_name 仅做卫生标记）
        stamp = uuid4().hex[:8]
        d1 = Document(doc_name=f"测试fresh_active_{stamp}", minio_object="x1",
                      tenant_id=tenant)
        d2 = Document(doc_name=f"测试fresh_expired_{stamp}", minio_object="x2",
                      tenant_id=tenant)
        d3 = Document(doc_name=f"测试fresh_draft_{stamp}", minio_object="x3",
                      tenant_id=tenant)
        db.add_all([d1, d2, d3])
        await db.flush()
        now = datetime.now(timezone.utc)
        db.add(KnowledgeDocumentMetadata(
            doc_id=d1.id, tenant_id=tenant,
            version_status="active", is_permanent=True))
        db.add(KnowledgeDocumentMetadata(
            doc_id=d2.id, tenant_id=tenant,
            version_status="active",
            expires_at=now - timedelta(days=1)))   # 已过期
        db.add(KnowledgeDocumentMetadata(
            doc_id=d3.id, tenant_id=tenant,
            version_status="draft"))                # 非 active
        await db.commit()

        rate = await gov._set_freshness_metric(db, tenant)
        assert rate == round(1 / 3, 3)


@pytest.mark.asyncio
async def test_freshness_empty_tenant(isolated_tenant):
    """空租户（无文档）→ rate = 0.0，不抛零除。"""
    tenant = isolated_tenant
    async with AsyncSessionLocal() as db:
        rate = await gov._set_freshness_metric(db, tenant)
        assert rate == 0.0
