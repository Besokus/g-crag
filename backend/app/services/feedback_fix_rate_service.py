"""B1: 坏case修复率聚合 — dislike→补全回流→同query再like 的比率。

周期 cron 调 recompute_fix_rate → metrics.FEEDBACK_FIX_RATE.set(rate)。
Grafana data-flywheel.json 已有面板引用 grid_feedback_fix_rate，set 即亮。
"""
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import settings
from app.core.obs import degraded
from app.db.session import AsyncSessionLocal
from app.models.evidence_gap import EvidenceGap
from app.models.feedback import Feedback
from app.models.knowledge_evolution import KnowledgeEvolutionDraft
from app.services.term_service import normalize


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def recompute_fix_rate(tenant: str = "default") -> float | None:
    """返回近 WINDOW_DAYS 的修复率 = |dislike且已补全且再被like| / |dislike unique nq|。

    Feedback.query 是原始 query；EvidenceGap.query/Draft.member_queries 是 nq。
    匹配前必须 normalize Feedback.query（复用 term_service.normalize）。
    分母 0 → 返回 0.0（不除零）。异常返回 None（caller 保持上次值）。

    tenant 隔离说明：Feedback 表当前无 tenant 列，S1/S3 段为跨租户统计；
    S2 段（EvidenceGap / KnowledgeEvolutionDraft）按 tenant 过滤。
    """
    try:
        cutoff = _utcnow() - timedelta(days=settings.FIX_RATE_WINDOW_DAYS)
        async with AsyncSessionLocal() as db:
            # S1：近 window 天的 dislike unique nq（Feedback 表无 tenant 列，跨租户统计）
            dislike_rows = (await db.execute(
                select(Feedback.query).where(
                    Feedback.feedback == "dislike",
                    Feedback.created_at >= cutoff,
                )
            )).scalars().all()
            s1 = {normalize(q or "") for q in dislike_rows if q and q.strip()}
            s1.discard("")
            if not s1:
                return 0.0

            # S2：已补全回流（evidence_gap.synced ∪ evolution.indexed），按 tenant 过滤
            synced = (await db.execute(
                select(EvidenceGap.query).where(
                    EvidenceGap.query.in_(s1),
                    EvidenceGap.status == "synced",
                    EvidenceGap.tenant == tenant,
                )
            )).scalars().all()
            s2 = set(synced)
            # evolution draft：member_queries_json 含 S1 中任一 nq 且 status=indexed
            indexed_drafts = (await db.execute(
                select(KnowledgeEvolutionDraft.member_queries_json).where(
                    KnowledgeEvolutionDraft.status == "indexed",
                    KnowledgeEvolutionDraft.tenant_id == tenant,
                )
            )).scalars().all()
            for mq_raw in indexed_drafts:
                try:
                    members = set(json.loads(mq_raw or "[]"))
                except Exception:
                    members = set()
                s2 |= (members & s1)

            if not s2:
                return 0.0

            # S3：S2 中的 nq 后续被 like（Feedback 表无 tenant 列，跨租户统计）
            like_rows = (await db.execute(
                select(Feedback.query).where(
                    Feedback.feedback == "like",
                    Feedback.created_at >= cutoff,
                )
            )).scalars().all()
            like_nqs = {normalize(q or "") for q in like_rows if q and q.strip()}
            s3 = s2 & like_nqs
            return round(len(s3) / len(s1), 3)
    except Exception as e:
        degraded("fix_rate_recompute", e)
        return None


async def fix_rate_cron_loop(tenant: str = "default"):
    """定时重算修复率 → metrics.FEEDBACK_FIX_RATE.set。

    lifespan 启动；周期=FIX_RATE_CRON_MINUTES，FIX_RATE_ENABLE=False 或周期<=0 时关闭。
    """
    import asyncio
    if not getattr(settings, "FIX_RATE_ENABLE", True):
        return
    interval = float(getattr(settings, "FIX_RATE_CRON_MINUTES", 30))
    if interval <= 0:
        return
    last_rate: float | None = None
    while True:
        await asyncio.sleep(interval * 60)
        try:
            rate = await recompute_fix_rate(tenant)
            if rate is None:
                rate = last_rate or 0.0   # 异常保持上次值
            else:
                last_rate = rate
            from app.core import metrics
            metrics.FEEDBACK_FIX_RATE.set(rate)
        except Exception as e:
            degraded("fix_rate_cron_loop", e)
