"""B1: 坏case修复率聚合（dislike→补全→同query再like）。"""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import text

from app.services import feedback_fix_rate_service as svc


@pytest_asyncio.fixture(autouse=True)
async def _isolate_feedback_tables():
    """每个测试前清空 feedbacks/evidence_gap（隔离真 MySQL 共享库历史数据）。

    brief 用固定业务 query 期望确定性 rate（0/1、1/1）；真库累积 dislike/like/synced
    会稀释样本导致非确定性。其他飞轮测试用 mock 或唯一 query，不依赖这两张表的累积。
    """
    from app.db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM feedbacks"))
        await db.execute(text("DELETE FROM evidence_gap"))
        await db.commit()
    yield


@pytest.mark.asyncio
async def test_recompute_full_cycle(monkeypatch, tmp_path):
    """dislike → synced → like 完整链：修复率 = 1/1 = 1.0。"""
    from app.db.session import AsyncSessionLocal
    from app.models.feedback import Feedback
    from app.models.evidence_gap import EvidenceGap
    from app.services import term_service

    nq = term_service.normalize("主变压器温度异常怎么处理")
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
async def test_recompute_dislike_only():
    """只 dislike 未补全：分子 0 → rate = 0.0。"""
    from app.db.session import AsyncSessionLocal
    from app.models.feedback import Feedback
    from app.services import term_service

    nq = term_service.normalize("SF6断路器漏气怎么办")
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 0.0


@pytest.mark.asyncio
async def test_recompute_window_excludes_old():
    """window 外的 dislike（>30天）不计入分母。"""
    from app.db.session import AsyncSessionLocal
    from app.models.feedback import Feedback
    from app.services import term_service

    nq = term_service.normalize("老旧问题")
    async with AsyncSessionLocal() as db:
        db.add(Feedback(query=nq, feedback="dislike",
                        created_at=datetime.now(timezone.utc) - timedelta(days=40)))
        await db.commit()

    rate = await svc.recompute_fix_rate("default")
    assert rate == 0.0  # 分母 0 → 返回 0（不除零）
