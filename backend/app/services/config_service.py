"""运行时配置：Redis 持久化（重启不丢） + 内存热读缓存（热路径零 Redis 往返）。

底层逻辑：原 update_* 只写 Redis，运行时 settings 不读 → 配置改了不生效（假功能）。
现引入 _RUNTIME 内存缓存：启动 load_runtime() 从 Redis 载入，update_* 写 Redis 后同步刷新，
热路径（检索 ef / 生成 temperature）用 sync getter 零延迟读取 → 配置即改即生效。
"""
from app.clients import redis_client

_DEFAULT_MILVUS = {"indexType": "HNSW", "param": {"M": 16, "efConstruction": 200, "ef": 64}}
_DEFAULT_MODEL = {"modelType": "default", "param": {"temperature": 0.2, "max_tokens": 2048}}

# 内存热读缓存（启动 load_runtime 填充；update_* 同步刷新）——热路径不碰 Redis
_RUNTIME = {"ef": 64, "temperature": 0.2, "max_tokens": 2048, "system_prompt": None,
            "llm_fallback_chain": None, "tier_models": None}


async def load_runtime() -> None:
    """启动时从 Redis 载入运行时配置到内存（供热路径 sync 读）。Redis 无值则用默认。"""
    try:
        mv = await get_milvus_config()
        _RUNTIME["ef"] = int((mv.get("param") or {}).get("ef", 64))
    except Exception:
        pass
    try:
        p = (await get_model_config()).get("param") or {}
        _RUNTIME["temperature"] = float(p.get("temperature", 0.2))
        _RUNTIME["max_tokens"] = int(p.get("max_tokens", 2048))
    except Exception:
        pass
    try:
        pc = await get_prompt_config()
        sp = ((pc.get("systemPrompt") or "")).strip()
        _RUNTIME["system_prompt"] = sp or None
    except Exception:
        pass
    try:
        cv = await redis_client.cache_get_json("config:crag")
        if cv:
            if "high" in cv:
                _RUNTIME["crag_high"] = float(cv["high"])
            if "low" in cv:
                _RUNTIME["crag_low"] = float(cv["low"])
    except Exception:
        pass
    try:
        lr = await redis_client.cache_get_json("config:llm_router")
        if lr:
            if isinstance(lr.get("fallbackChain"), list):
                _RUNTIME["llm_fallback_chain"] = [p for p in lr["fallbackChain"]
                                                  if isinstance(p, str) and p.strip()]
            if isinstance(lr.get("tierModels"), dict):
                _RUNTIME["tier_models"] = lr["tierModels"]
    except Exception:
        pass


def rt_ef() -> int:
    """HNSW 查询 ef（检索召回/延迟权衡，运行时可调）。"""
    return _RUNTIME["ef"]


def rt_temperature() -> float:
    """主答案生成 temperature（运行时可调）。"""
    return _RUNTIME["temperature"]


def rt_max_tokens() -> int:
    return _RUNTIME["max_tokens"]


def rt_crag_high() -> float:
    """CRAG 分级 high 阈值（运行时可调，T6 断点 F）。默认 settings.CRAG_HIGH。"""
    from app.config import settings
    return float(_RUNTIME.get("crag_high", settings.CRAG_HIGH))


def rt_crag_low() -> float:
    """CRAG 分级 low 阈值（运行时可调，T6 断点 F）。默认 settings.CRAG_LOW。"""
    from app.config import settings
    return float(_RUNTIME.get("crag_low", settings.CRAG_LOW))


async def get_milvus_config() -> dict:
    v = await redis_client.cache_get_json("config:milvus")
    return v or _DEFAULT_MILVUS


async def update_milvus_config(index_type: str, param: dict) -> dict:
    data = {"indexType": index_type, "param": param or {}}
    await redis_client.cache_set_json_persistent("config:milvus", data)
    if "ef" in (param or {}):
        _RUNTIME["ef"] = int(param["ef"])   # 即改即生效（下次检索即用新 ef）
    return data


async def get_model_config() -> dict:
    v = await redis_client.cache_get_json("config:model")
    return v or _DEFAULT_MODEL


async def update_model_config(model_type: str, param: dict) -> dict:
    data = {"modelType": model_type, "param": param or {}}
    await redis_client.cache_set_json_persistent("config:model", data)
    p = param or {}
    if "temperature" in p:
        _RUNTIME["temperature"] = float(p["temperature"])   # 即改即生效（下次生成即用新 temperature）
    if "max_tokens" in p:
        _RUNTIME["max_tokens"] = int(p["max_tokens"])
    return data


# ===== Prompt 模板（BRD §4.4.1 后台可编辑；DB/Redis 覆盖 code 默认）=====

async def get_prompt_config() -> dict:
    """读取 system prompt 覆盖（空=用 code 默认 SYSTEM_PROMPT）。"""
    v = await redis_client.cache_get_json("config:prompt")
    return v or {"systemPrompt": ""}


async def update_prompt_config(system_prompt: str) -> dict:
    """保存 system prompt 覆盖，即改即生效（下次问答即用新 prompt）。空串=恢复默认。"""
    sp = (system_prompt or "").strip()
    data = {"systemPrompt": sp}
    await redis_client.cache_set_json_persistent("config:prompt", data)
    _RUNTIME["system_prompt"] = sp or None
    return data


def rt_system_prompt() -> str | None:
    """热路径 sync 读：system prompt 覆盖（None=用 code 默认）。"""
    return _RUNTIME.get("system_prompt")


# ===== LLM 路由（L0 fallback 链 + L2 档位 model 映射）热配置 =====

def rt_llm_fallback_chain() -> list[str]:
    """fallback 链（热读；None → 回落 settings.LLM_FALLBACK_CHAIN）。"""
    chain = _RUNTIME.get("llm_fallback_chain")
    if chain:
        return chain
    from app.config import settings
    return [s.strip() for s in settings.LLM_FALLBACK_CHAIN.split(",") if s.strip()]


def rt_tier_models() -> dict:
    """档位→model 映射（热读；形如 {"qwen": {"turbo": "qwen-turbo", "plus": "qwen-plus"}}）。"""
    return _RUNTIME.get("tier_models") or {}


async def get_llm_router_config() -> dict:
    v = await redis_client.cache_get_json("config:llm_router")
    return v or {"fallbackChain": [], "tierModels": {}}


async def update_llm_router_config(fallback_chain: list[str], tier_models: dict) -> dict:
    """保存 LLM 路由配置，即改即生效（下次 resolve 即用新链/档位）。"""
    data = {"fallbackChain": fallback_chain or [], "tierModels": tier_models or {}}
    await redis_client.cache_set_json_persistent("config:llm_router", data)
    _RUNTIME["llm_fallback_chain"] = [p for p in (fallback_chain or [])
                                      if isinstance(p, str) and p.strip()]
    _RUNTIME["tier_models"] = tier_models or {}
    return data
