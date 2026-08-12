"""OLLAMA_ENABLE 热配置开关单测：更新/读取/从 fallback 链剔除，均不落库到真实 Redis。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.services import config_service
from app.providers.llm_router import _fallback_chain


def test_update_and_read_ollama_enable():
    async def go():
        with patch.object(config_service.redis_client, "cache_set_json_persistent", AsyncMock()):
            data = await config_service.update_llm_router_config(
                ["qwen", "deepseek", "doubao", "ollama"], {}, ollama_enable=False)
        assert data["ollamaEnable"] is False
        assert config_service.rt_ollama_enable() is False
    asyncio.run(go())
    config_service._RUNTIME["ollama_enable"] = None  # 复原，避免污染其它测试


def test_rt_ollama_enable_defaults_to_true_when_unset():
    config_service._RUNTIME["ollama_enable"] = None
    assert config_service.rt_ollama_enable() is True


def test_fallback_chain_excludes_ollama_when_disabled():
    config_service._RUNTIME["llm_fallback_chain"] = ["qwen", "deepseek", "doubao", "ollama"]
    config_service._RUNTIME["ollama_enable"] = False
    try:
        assert "ollama" not in _fallback_chain()
    finally:
        config_service._RUNTIME["llm_fallback_chain"] = None
        config_service._RUNTIME["ollama_enable"] = None


def test_fallback_chain_includes_ollama_when_enabled():
    config_service._RUNTIME["llm_fallback_chain"] = ["qwen", "deepseek", "doubao", "ollama"]
    config_service._RUNTIME["ollama_enable"] = True
    try:
        assert "ollama" in _fallback_chain()
    finally:
        config_service._RUNTIME["llm_fallback_chain"] = None
        config_service._RUNTIME["ollama_enable"] = None
