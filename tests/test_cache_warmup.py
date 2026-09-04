"""缓存预热 key 与读路径对齐单测（回归）。

背景：warmup 曾写不带 citation_cache_version 后缀的 key（qa:{tenant}:{model}:{nq}），
而读路径 _cache_key 恒为 qa:{tenant}:{model}:{nq}:{cv}——写读永不匹配，
golden 预热与热点回退 key 全部死写。本文件防止回归。
"""
import json

import pytest

from app.config import citation_cache_version
from app.services import cache_warmup, term_service


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_a, **_k):
        return _FakeResult(self._rows)


class _Row:
    def __init__(self, cache_key, query_normalized, answer='{"answer":"ok"}',
                 tenant_id="default", model_type="deepseek"):
        self.cache_key = cache_key
        self.query_normalized = query_normalized
        self.answer = answer
        self.tenant_id = tenant_id
        self.model_type = model_type


@pytest.fixture
def capture_redis_set(monkeypatch):
    """替换 cache_set_json_safe，捕获写入的 key，返回 True 模拟成功。"""
    keys = []

    async def fake_set(key, val, ttl=None):
        keys.append(key)
        return True

    monkeypatch.setattr("app.clients.redis_client.cache_set_json_safe", fake_set)
    return keys


@pytest.mark.asyncio
async def test_warmup_from_file_key_matches_read_path(capture_redis_set, tmp_path):
    """golden 预热 key 必须与 qa_service._cache_key 完全同构（含版本后缀）。"""
    from app.services.qa_service import _cache_key

    f = tmp_path / "golden.json"
    f.write_text(json.dumps([{"query": "主变差动保护动作怎么处置",
                              "answer": "按规程检查二次回路", "retrievalSource": []}]),
                 encoding="utf-8")
    warmed = await cache_warmup.warmup_from_file(str(f))

    assert warmed == 1
    nq = term_service.normalize("主变差动保护动作怎么处置")
    assert capture_redis_set == [_cache_key(None, nq, "default")]


@pytest.mark.asyncio
async def test_warmup_hot_queries_fallback_key_has_version(capture_redis_set):
    """cache_key 不匹配租户前缀时，回退拼 key 必须带版本后缀。"""
    rows = [
        # 旧格式 cache_key（无前缀）→ 走回退拼 key 分支
        _Row(cache_key="legacy_key", query_normalized="旧key问题"),
        # 已是租户化完整 key → 原样回写
        _Row(cache_key=f"qa:default:deepseek:完整key:{citation_cache_version()}",
             query_normalized="完整key问题"),
    ]
    warmed = await cache_warmup.warmup_hot_queries(_FakeDB(rows))

    assert warmed == 2
    assert capture_redis_set == [
        f"qa:default:deepseek:旧key问题:{citation_cache_version()}",
        f"qa:default:deepseek:完整key:{citation_cache_version()}",
    ]
