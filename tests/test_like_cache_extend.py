"""点赞续期（ADR 0008，替代 hotqa）：只延长已有问答缓存的有效期到 LIKE_CACHE_TTL，不新增条目（容器真 Redis/MySQL）。"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.config import settings
from app.services import feedback_optimizer_service as fo

pytestmark = pytest.mark.integration


def _keys(nq, tenant):
    from app.models.qa_cache import QaCache
    from app.providers.factory import llm_cache_tag
    from app.rag.gcrag import resolve_mode
    from app.services.qa_service import _cache_key
    ck_model = f"{llm_cache_tag(None)}#{resolve_mode(None)}"    # 问答页不指定模型与纠错模式
    return ck_model, _cache_key(ck_model, nq, tenant), QaCache.build_hash(ck_model, nq, tenant)


async def _seed(nq, tenant, ttl):
    """按问答链路的写法落一条 high 置信缓存（Redis + MySQL）。"""
    from app.clients import redis_client
    from app.db.session import AsyncSessionLocal
    from app.services.cache_persist import cache_set_mysql
    ck_model, rkey, _ = _keys(nq, tenant)
    result = {"answer": "13.57亿元[1]", "confidence": "high", "retrievalSource": []}
    async with AsyncSessionLocal() as db:
        await cache_set_mysql(db, ck_model, nq, nq, result, tenant_id=tenant)
    await redis_client.cache_set_json(rkey, result, ttl)


async def _state(nq, tenant):
    from app.clients import redis_client
    from app.db.session import AsyncSessionLocal
    from app.models.qa_cache import QaCache
    _, rkey, qh = _keys(nq, tenant)
    left = await redis_client.get_redis().ttl(rkey)
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(QaCache).where(QaCache.tenant_id == tenant, QaCache.query_hash == qh))).scalar_one_or_none()
    return left, (row.expires_at - datetime.utcnow()).total_seconds() if row else None


def test_like_cache_extension(monkeypatch):
    """同一事件循环内跑完全部场景；Redis 单例与 DB 连接池绑定创建时的事件循环，开始前换新、结束时释放。"""
    from app.clients import redis_client
    from app.db.session import engine
    monkeypatch.setattr(redis_client, "_pool", None)

    async def go():
        try:
            await _scenarios()
        finally:
            await redis_client.get_redis().aclose()
            await engine.dispose()
    asyncio.run(go())


async def _scenarios():
    week, ts = settings.LIKE_CACHE_TTL, datetime.now().timestamp()
    assert week == 7 * 86400

    # 已有 3 天缓存 → 点赞延长到 7 天（Redis + MySQL）
    nq = f"测试点赞续期{ts}"
    await _seed(nq, "t_like", settings.QA_CACHE_TTL)
    assert await fo.extend_cache_on_like(nq, "t_like") == 2
    left, mysql_left = await _state(nq, "t_like")
    assert week - 60 < left <= week and week - 60 < mysql_left <= week + 2   # DATETIME 小数秒四舍五入

    # 没有缓存（拒答 / 证据有限）→ 不新增；别的租户的缓存不受影响
    nq = f"测试点赞无缓存{ts}"
    assert await fo.extend_cache_on_like(nq, "t_like") == 0
    assert await _state(nq, "t_like") == (-2, None)
    await _seed(nq, "t_other", settings.QA_CACHE_TTL)
    assert await fo.extend_cache_on_like(nq, "t_like") == 0
    assert (await _state(nq, "t_other"))[0] <= settings.QA_CACHE_TTL

    # 已有更长有效期 → 不缩短
    nq = f"测试点赞不缩短{ts}"
    await _seed(nq, "t_like", week * 2)
    assert await fo.extend_cache_on_like(nq, "t_like") == 2
    assert (await _state(nq, "t_like"))[0] > week


def test_hotqa_disabled_by_default():
    assert settings.HOTQA_ENABLE is False
