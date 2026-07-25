"""B1: 坏case修复率聚合（dislike→补全→同query再like）。"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.db.session import AsyncSessionLocal
from app.models.evidence_gap import EvidenceGap
from app.models.feedback import Feedback
from app.services import feedback_fix_rate_service as svc

pytestmark = pytest.mark.integration  # 依赖容器真 DB（MySQL），CI 无 DB 跳过


@pytest_asyncio.fixture
async def unique_nq():
    """唯一 nq + 测试前后清理。

    非自动删除全表；仅管理 测试_ 前缀的本测试数据：
    setup 清理历史 测试_ 残留（failed run 遗留 + session 内累积），保证聚合率
    有干净基线（聚合统计全表 in-window 行，唯一 query 不足以隔离同表累积）；
    teardown 按精确 nq 删除本测试插入的行，不污染共享库。
    """
    # setup：清理测试前缀残留，确保聚合率基线干净
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Feedback).where(Feedback.query.like("测试_%")))
        await db.execute(delete(EvidenceGap).where(EvidenceGap.query.like("测试_%")))
        await db.commit()

    value = f"测试_{uuid4().hex[:8]}"
    yield value

    # teardown：按精确 nq 清理本测试行（非前缀模糊删，不影响其他测试）
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Feedback).where(Feedback.query == value))
        await db.execute(delete(EvidenceGap).where(EvidenceGap.query == value))
        await db.commit()


@pytest.mark.asyncio
async def test_recompute_full_cycle(unique_nq):
    """dislike → synced → like 完整链：修复率 = 1/1 = 1.0。"""
    nq = unique_nq
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=1)))
        db.add(EvidenceGap(query=nq, status="synced", confidence="refused",
                           original_answer="", grade="", crag_action="", tenant="default"))
        db.add(Feedback(query=nq, feedback="like",
                        created_at=datetime.now(timezone.utc)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 1.0


@pytest.mark.asyncio
async def test_recompute_dislike_only(unique_nq):
    """只 dislike 未补全：分子 0 → rate = 0.0。"""
    nq = unique_nq
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 0.0


@pytest.mark.asyncio
async def test_recompute_window_excludes_old(unique_nq):
    """window 外的 dislike（>30天）不计入分母。"""
    nq = unique_nq
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=40)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 0.0  # 分母 0 → 返回 0（不除零）
