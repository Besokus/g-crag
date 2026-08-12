# 云端LLM/Embedding熔断降级 + 本地Ollama兜底 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让云端 LLM/Embedding（qwen/deepseek/doubao/DashScope）全部不可用时，系统能自动切到本地 Ollama 模型/bge embedding 兜底作答，并让前端用户看到"本次答案由备用/本地模型生成""本次检索云端降级"的明确提示，而不是裸 500 或语焉不详的"流式中断"。

**Architecture:** 在现有 L0 fallback 链（`FallbackLLMProvider`，`backend/app/providers/llm_router.py`）末位追加本地 `OllamaLLM` provider；给它加 `degraded`/`degrade_reason` 属性，`qa_service.py` 读取后把"实际命中的 provider"（而非请求参数）写回前端；检索层复用已有的 trace mark 机制透出"云端向量检索是否降级"；LLM 链路全灭时优雅拒答而非裸抛异常；`Admin.vue` 新增一个热配置开关，可不重启关闭本地兜底。

**Tech Stack:** FastAPI / Pydantic v2 / SQLAlchemy async / Redis / Milvus / Vue 3 / pytest + pytest-asyncio（部分既有测试用 `asyncio.run` 包同步测试函数，两种写法在本仓库共存，跟随所改文件的既有写法）/ Docker Compose / Ollama（OpenAI 兼容 API）。

## Global Constraints

- 测试真实运行位置是仓库根目录 `tests/`（不是 `backend/tests/`），运行命令 `venv/Scripts/python.exe -m pytest tests/xxx.py -v`（`tests/conftest.py` 把 `backend/` 加入 `sys.path`）。`backend/tests/` 是历史遗留的错误位置（`5de2f01 fix(citation): 测试从 backend/tests/ 迁回 repo root tests/`），本计划顺手把误留在那里的 `test_llm_router.py` 迁回来。
- 所有新增的 `except` 必须走 `app.core.obs.degraded(tag, exc)`，不允许裸 `except: pass`（项目硬性规范）。
- 新配置项默认值必须保证**不改现有部署行为**：`OLLAMA_ENABLE`（热配置）默认 `true`，但 Ollama 服务本身没起来时，fallback 链里排最后一位、真实调用失败即被熔断，不影响云端 provider 正常工作。
- 前端 badge 用现有 CSS 类名 `badge-warning`（不要新造类名——`Chat.vue` 里已有的 `judgeHalluc`/`confidence=medium` 徽章都用这个类）。
- `docker-compose.yml` 是本地/单机部署（非 swarm），资源限制用顶层 `mem_limit`/`cpus` 字段，不要用只在 `docker stack deploy` 下生效的 `deploy.resources`。

---

### Task 1: 配置项 + OllamaLLM Provider + factory 注册

**Files:**
- Modify: `backend/app/config.py:70-75`
- Create: `backend/app/providers/llm/ollama_llm.py`
- Modify: `backend/app/providers/factory.py:23-34`
- Test: `tests/test_ollama_llm.py`

**Interfaces:**
- Produces: `OllamaLLM`（`backend/app/providers/llm/ollama_llm.py`）——`LLMProvider` 子类，方法 `chat(messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> str`、`chat_with_usage(...) -> tuple[str, dict|None]`、`stream(...) -> AsyncIterator[str]`。不实现 `chat_with_tools`（沿用基类 `NotImplementedError`，本地应急模型不支持 Agent 工具调用，超出本次范围）。
- Produces: `settings.OLLAMA_BASE_URL`、`settings.OLLAMA_MODEL`、`settings.LLM_LOCAL_TIMEOUT`（后续 Task 3/6/7 直接引用）。
- Produces: `factory._get_raw_provider("ollama")` 返回可用的 `OllamaLLM()` 实例。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_ollama_llm.py`：

```python
"""OllamaLLM 单测：本地应急模型 chat/chat_with_usage/stream，mock AsyncOpenAI，
不依赖真实 Ollama 服务（照 test_provider_tools.py 的 mock 范式）。"""
import asyncio
from types import SimpleNamespace

from app.providers.llm.ollama_llm import OllamaLLM


def _make_resp(content, usage=None):
    msg = SimpleNamespace(content=content)
    u = SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=u)


def test_chat_returns_content(monkeypatch):
    p = OllamaLLM()

    async def fake_create(**kw):
        assert kw["model"] == p.model
        return _make_resp("本地应急回答")

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    r = asyncio.run(p.chat([{"role": "user", "content": "主变异常怎么处置"}]))
    assert r == "本地应急回答"


def test_chat_with_usage_returns_token_counts(monkeypatch):
    p = OllamaLLM()

    async def fake_create(**kw):
        return _make_resp("答案", usage=(10, 20))

    monkeypatch.setattr(p.client.chat.completions, "create", fake_create)
    content, usage = asyncio.run(p.chat_with_usage([{"role": "user", "content": "x"}]))
    assert content == "答案"
    assert usage == {"input": 10, "output": 20}


def test_stream_yields_tokens(monkeypatch):
    p = OllamaLLM()

    async def fake_stream(**kw):
        async def gen():
            for tok in ["本地", "应急", "回答"]:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=tok))])
        return gen()

    monkeypatch.setattr(p.client.chat.completions, "create", fake_stream)

    async def collect():
        out = []
        async for tok in p.stream([{"role": "user", "content": "x"}]):
            out.append(tok)
        return out

    assert asyncio.run(collect()) == ["本地", "应急", "回答"]


def test_uses_ollama_base_url_and_local_timeout():
    """base_url 走 OLLAMA_BASE_URL + /v1；timeout 用独立的 LLM_LOCAL_TIMEOUT（CPU 推理更慢）。"""
    from app.config import settings
    p = OllamaLLM()
    assert str(p.client.base_url).rstrip("/") == f"{settings.OLLAMA_BASE_URL}/v1"
    assert p.client.timeout == settings.LLM_LOCAL_TIMEOUT
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_ollama_llm.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.providers.llm.ollama_llm'`

- [ ] **Step 3: 加配置项**

在 `backend/app/config.py` 第 70-75 行（现状）：

```python
    LLM_FALLBACK_CHAIN: str = "qwen,deepseek,doubao"   # fallback 链（逗号分隔，首选在前；用户手选仍优先）
    LLM_FALLBACK_ON_EMPTY: bool = True                 # 空 answer(如 deepseek 0字)也触发 fallback
    LLM_HEALTH_PROBE_ENABLE: bool = True               # 后台周期探活 provider（熔断底座）
    LLM_CIRCUIT_FAIL_N: int = 3                        # 连续失败 N 次 → 熔断冷却
    LLM_CIRCUIT_COOLDOWN: int = 60                     # 熔断冷却秒数
    LLM_PROBE_INTERVAL: int = 30                       # 健康探活周期秒
```

改成：

```python
    LLM_FALLBACK_CHAIN: str = "qwen,deepseek,doubao,ollama"   # fallback 链末位追加本地应急模型
    LLM_FALLBACK_ON_EMPTY: bool = True                 # 空 answer(如 deepseek 0字)也触发 fallback
    LLM_HEALTH_PROBE_ENABLE: bool = True               # 后台周期探活 provider（熔断底座）
    LLM_CIRCUIT_FAIL_N: int = 3                        # 连续失败 N 次 → 熔断冷却
    LLM_CIRCUIT_COOLDOWN: int = 60                     # 熔断冷却秒数
    LLM_PROBE_INTERVAL: int = 30                       # 健康探活周期秒
    LLM_LOCAL_TIMEOUT: float = 60.0                    # 本地 Ollama 兜底超时（CPU 推理慢于云端 API）
```

紧接着在 `# --- 火山方舟 Ark ---` 段之前（即 DashScope 段之后）新增：

```python
    # --- 本地 Ollama（云端 LLM 全部不可用时的应急兜底，L0 fallback 链末位）---
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b-instruct-q4_K_M"
```

- [ ] **Step 4: 创建 OllamaLLM provider**

创建 `backend/app/providers/llm/ollama_llm.py`：

```python
"""本地 Ollama LLM（应急兜底）：云端 fallback 链全灭时兜底作答。

Ollama 原生兼容 OpenAI /v1/chat/completions，复用 openai SDK；api_key 用占位值
（Ollama 不校验）。CPU 推理明显慢于云端 API，用独立的 LLM_LOCAL_TIMEOUT。
不实现 chat_with_tools：本地应急模型不承担 Agent 工具调用职责，沿用基类
NotImplementedError（若被 agent 模式误用会立刻报错，而不是静默返回错误结果）。
"""
from openai import AsyncOpenAI

from app.config import settings
from app.providers.base import LLMProvider


class OllamaLLM(LLMProvider):
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key="ollama", base_url=f"{settings.OLLAMA_BASE_URL}/v1",
            timeout=settings.LLM_LOCAL_TIMEOUT, max_retries=settings.LLM_MAX_RETRIES,
        )
        self.model = settings.OLLAMA_MODEL

    async def chat_with_usage(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> tuple[str, dict | None]:
        _model = model or self.model
        r = await self.client.chat.completions.create(
            model=_model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, **kw,
        )
        content = r.choices[0].message.content
        usage = None
        if r.usage:
            usage = {"input": r.usage.prompt_tokens or 0, "output": r.usage.completion_tokens or 0}
        return content, usage

    async def chat(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw) -> str:
        content, _ = await self.chat_with_usage(
            messages, temperature=temperature, max_tokens=max_tokens, model=model, **kw)
        return content

    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        _model = model or self.model
        r = await self.client.chat.completions.create(
            model=_model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, stream=True, **kw,
        )
        async for chunk in r:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
```

- [ ] **Step 5: 注册进 factory**

在 `backend/app/providers/factory.py` 的 `_get_raw_provider`（第 23-34 行），现状：

```python
def _get_raw_provider(p: str) -> LLMProvider:
    """构造单个原始 provider 实例（不包 fallback）。check_llm_health/探活用，避免掩盖故障。"""
    if p == "deepseek":
        from app.providers.llm.deepseek_llm import DeepSeekLLM
        return DeepSeekLLM()
    if p == "qwen":
        from app.providers.llm.qwen_llm import QwenLLM
        return QwenLLM()
    if p == "doubao":
        from app.providers.llm.doubao_llm import DoubaoLLM
        return DoubaoLLM()
    raise ValueError(f"未知 LLM_PROVIDER: {p}（支持: deepseek | qwen | doubao）")
```

改成：

```python
def _get_raw_provider(p: str) -> LLMProvider:
    """构造单个原始 provider 实例（不包 fallback）。check_llm_health/探活用，避免掩盖故障。"""
    if p == "deepseek":
        from app.providers.llm.deepseek_llm import DeepSeekLLM
        return DeepSeekLLM()
    if p == "qwen":
        from app.providers.llm.qwen_llm import QwenLLM
        return QwenLLM()
    if p == "doubao":
        from app.providers.llm.doubao_llm import DoubaoLLM
        return DoubaoLLM()
    if p == "ollama":
        from app.providers.llm.ollama_llm import OllamaLLM
        return OllamaLLM()
    raise ValueError(f"未知 LLM_PROVIDER: {p}（支持: deepseek | qwen | doubao | ollama）")
```

- [ ] **Step 6: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_ollama_llm.py -v`
Expected: 4 passed

- [ ] **Step 7: Commit**

```bash
git add backend/app/config.py backend/app/providers/llm/ollama_llm.py backend/app/providers/factory.py tests/test_ollama_llm.py
git commit -m "feat(providers): 新增本地Ollama LLM provider作为fallback链末位应急兜底"
```

---

### Task 2: FallbackLLMProvider 增加 degraded/degrade_reason + 迁移测试文件

**Files:**
- Modify: `backend/app/providers/llm_router.py:102-224`（`FallbackLLMProvider` 类）
- Move + Modify: `backend/tests/test_llm_router.py` → `tests/test_llm_router.py`（历史遗留错误位置，本任务顺手迁回，见 Global Constraints）

**Interfaces:**
- Consumes: 无新依赖（沿用 Task 1 的 `OllamaLLM`，测试里仍用现有 `FakeProv`）
- Produces: `FallbackLLMProvider.degraded: bool`、`FallbackLLMProvider.degrade_reason: str`——Task 6/7 的 `qa_service._llm_degradation_fields()` 读取这两个属性。
- Produces: `llm_router._should_actively_probe(name: str) -> bool`——`_refresh_llm_health_loop` 内部使用，无外部消费方。

- [ ] **Step 1: 迁移文件到正确位置**

```bash
git mv backend/tests/test_llm_router.py tests/test_llm_router.py
rmdir backend/tests 2>/dev/null || true
```

（`backend/tests/` 目录在这之后应为空，`rmdir` 静默清理；Windows 下 `rmdir` 失败不影响后续步骤。）

- [ ] **Step 2: 在迁移后的 `tests/test_llm_router.py` 末尾追加失败的测试**

```python
# ===== L0 degraded/degrade_reason 标记（云端全灭+本地兜底可见性）=====

@pytest.mark.asyncio
async def test_fallback_sets_degraded_flag_and_reason():
    """切备成功后 degraded=True，degrade_reason 带原因。"""
    p1 = FakeProv("deepseek", exc=RuntimeError("boom"))
    p2 = FakeProv("qwen", chat_res="from qwen")
    fb = FallbackLLMProvider([p1, p2], ["deepseek", "qwen"])
    await fb.chat([{"role": "user", "content": "hi"}])
    assert fb.degraded is True
    assert "deepseek" in fb.degrade_reason


@pytest.mark.asyncio
async def test_no_fallback_not_degraded():
    """首选 provider 直接成功 → degraded=False，degrade_reason 空。"""
    p1 = FakeProv("deepseek", chat_res="ok")
    fb = FallbackLLMProvider([p1], ["deepseek"])
    await fb.chat([{"role": "user", "content": "hi"}])
    assert fb.degraded is False
    assert fb.degrade_reason == ""


@pytest.mark.asyncio
async def test_ollama_fallback_has_fixed_reason():
    """最终命中本地 ollama → 固定文案标注，而不是拼接原始异常信息。"""
    p1 = FakeProv("deepseek", exc=RuntimeError("boom"))
    p2 = FakeProv("ollama", chat_res="本地应急回答")
    fb = FallbackLLMProvider([p1, p2], ["deepseek", "ollama"])
    await fb.chat([{"role": "user", "content": "hi"}])
    assert fb.last_used_name == "ollama"
    assert fb.degrade_reason == "云端模型全部不可用，已使用本地应急模型"


@pytest.mark.asyncio
async def test_stream_fallback_sets_degraded():
    """流式切备后同样置位 degraded（首 token 前异常触发切备场景）。"""
    class MidFail:
        async def stream(self, messages, **kw):
            raise RuntimeError("connect fail")
            yield
        async def chat(self, *a, **kw):
            return ""
    fb = FallbackLLMProvider([MidFail(), FakeProv("qwen", stream_tokens=["a"])], ["deepseek", "qwen"])
    toks = [t async for t in fb.stream([{"role": "user", "content": "hi"}])]
    assert toks == ["a"]
    assert fb.degraded is True


@pytest.mark.asyncio
async def test_stream_no_fallback_not_degraded():
    """流式首选直接成功 → degraded=False。"""
    fb = FallbackLLMProvider([FakeProv("qwen", stream_tokens=["a", "b"])], ["qwen"])
    toks = [t async for t in fb.stream([{"role": "user", "content": "hi"}])]
    assert toks == ["a", "b"]
    assert fb.degraded is False
```

- [ ] **Step 3: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_llm_router.py -v`
Expected: 之前迁移过来的用例通过，新增 5 个用例 FAIL，`AttributeError: 'FallbackLLMProvider' object has no attribute 'degraded'`

- [ ] **Step 4: 实现 degraded/degrade_reason**

在 `backend/app/providers/llm_router.py`，`__init__`（现状第 109-113 行）：

```python
    def __init__(self, providers: list, names: list[str], tier: str = "plus"):
        self._providers = providers
        self._names = names
        self._tier = tier
        self.last_used_name = names[0] if names else "unknown"  # 实际命中的 provider（切备后更新，供 trace mark）
```

改成：

```python
    def __init__(self, providers: list, names: list[str], tier: str = "plus"):
        self._providers = providers
        self._names = names
        self._tier = tier
        self.last_used_name = names[0] if names else "unknown"  # 实际命中的 provider（切备后更新，供 trace mark）
        self.degraded = False       # 是否没用上第一顺位 provider（前端据此展示"备用模型"提示）
        self.degrade_reason = ""    # 切换前最后一次失败原因（前端 badge title）
```

`_try_providers`（现状第 143-174 行）：

```python
    async def _try_providers(self, method: str, check_empty: bool, **kw):
        """非流式：按链试，异常/空输出→切备。"""
        last_exc: Exception | None = None
        last_res = None
        for i, prov in enumerate(self._providers):
            name = self._names[i]
            nxt = self._names[i + 1] if i + 1 < len(self._names) else "-"
            model = self._tier_model(i)
            try:
                if method == "chat":
                    res = await prov.chat(model=model, **kw)
                elif method == "chat_with_usage":
                    res = await prov.chat_with_usage(model=model, **kw)
                else:
                    res = await prov.chat_with_tools(model=model, **kw)
                if check_empty and settings.LLM_FALLBACK_ON_EMPTY and self._is_empty(method, res):
                    record_fail(name)
                    self._fb_metric(name, nxt, "empty")
                    degraded("llm_fallback", Exception(f"{name} 空输出"), f"{method} 切备 {nxt}")
                    last_res = res
                    continue
                record_ok(name)
                self.last_used_name = name
                return res
            except Exception as e:
                last_exc = e
                record_fail(name)
                self._fb_metric(name, nxt, "exception")
                degraded("llm_fallback", e, f"{name} {method} 异常切备 {nxt}")
        if last_exc is not None:
            raise last_exc
        return last_res  # 全空：返回最后空结果（上层走兜底）
```

改成：

```python
    async def _try_providers(self, method: str, check_empty: bool, **kw):
        """非流式：按链试，异常/空输出→切备。切备成功后置 degraded/degrade_reason，
        供上层（qa_service）透出"本次答案由备用/本地模型生成"提示给前端。"""
        last_exc: Exception | None = None
        last_res = None
        last_reason = ""
        for i, prov in enumerate(self._providers):
            name = self._names[i]
            nxt = self._names[i + 1] if i + 1 < len(self._names) else "-"
            model = self._tier_model(i)
            try:
                if method == "chat":
                    res = await prov.chat(model=model, **kw)
                elif method == "chat_with_usage":
                    res = await prov.chat_with_usage(model=model, **kw)
                else:
                    res = await prov.chat_with_tools(model=model, **kw)
                if check_empty and settings.LLM_FALLBACK_ON_EMPTY and self._is_empty(method, res):
                    record_fail(name)
                    self._fb_metric(name, nxt, "empty")
                    degraded("llm_fallback", Exception(f"{name} 空输出"), f"{method} 切备 {nxt}")
                    last_res = res
                    last_reason = f"{name} 返回空输出，已切换至 {nxt}"
                    continue
                record_ok(name)
                self.last_used_name = name
                self.degraded = i > 0
                self.degrade_reason = (
                    "云端模型全部不可用，已使用本地应急模型" if (self.degraded and name == "ollama")
                    else (last_reason if self.degraded else "")
                )
                return res
            except Exception as e:
                last_exc = e
                record_fail(name)
                self._fb_metric(name, nxt, "exception")
                degraded("llm_fallback", e, f"{name} {method} 异常切备 {nxt}")
                last_reason = f"{name} 不可用({type(e).__name__})，已切换至 {nxt}"
        if last_exc is not None:
            raise last_exc
        return last_res  # 全空：返回最后空结果（上层走兜底）
```

`stream`（现状第 190-224 行）：

```python
    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        """流式：首 token 前切备（启动异常/空流→切下家）；首 token 后不切（避免重复吐字）。"""
        last_exc: Exception | None = None
        for i, prov in enumerate(self._providers):
            name = self._names[i]
            nxt = self._names[i + 1] if i + 1 < len(self._names) else "-"
            tier_model = self._tier_model(i)
            yielded = False
            try:
                gen = prov.stream(messages, temperature=temperature, max_tokens=max_tokens,
                                  model=tier_model, **kw)
                async for tok in gen:
                    yielded = True
                    record_ok(name)
                    self.last_used_name = name
                    yield tok
                if yielded:
                    return  # 正常结束
                # 空流（首 token 都没出）
                if settings.LLM_FALLBACK_ON_EMPTY:
                    record_fail(name)
                    self._fb_metric(name, nxt, "empty_stream")
                    degraded("llm_fallback_stream", Exception(f"{name} 空流"), f"切备 {nxt}")
                    continue
                return
            except Exception as e:
                if yielded:
                    raise  # 已吐 token，中途异常不切（避免重复），抛给上层 degraded
                last_exc = e
                record_fail(name)
                self._fb_metric(name, nxt, "stream_exception")
                degraded("llm_fallback_stream", e, f"{name} 流启动失败切备 {nxt}")
        if last_exc is not None:
            raise last_exc
```

改成：

```python
    async def stream(self, messages, temperature=0.2, max_tokens=2048, model=None, **kw):
        """流式：首 token 前切备（启动异常/空流→切下家）；首 token 后不切（避免重复吐字）。
        首 token 出现时置 degraded/degrade_reason（同 _try_providers 语义）。"""
        last_exc: Exception | None = None
        last_reason = ""
        for i, prov in enumerate(self._providers):
            name = self._names[i]
            nxt = self._names[i + 1] if i + 1 < len(self._names) else "-"
            tier_model = self._tier_model(i)
            yielded = False
            try:
                gen = prov.stream(messages, temperature=temperature, max_tokens=max_tokens,
                                  model=tier_model, **kw)
                async for tok in gen:
                    if not yielded:
                        record_ok(name)
                        self.last_used_name = name
                        self.degraded = i > 0
                        self.degrade_reason = (
                            "云端模型全部不可用，已使用本地应急模型" if (self.degraded and name == "ollama")
                            else (last_reason if self.degraded else "")
                        )
                    yielded = True
                    yield tok
                if yielded:
                    return  # 正常结束
                # 空流（首 token 都没出）
                if settings.LLM_FALLBACK_ON_EMPTY:
                    record_fail(name)
                    self._fb_metric(name, nxt, "empty_stream")
                    degraded("llm_fallback_stream", Exception(f"{name} 空流"), f"切备 {nxt}")
                    last_reason = f"{name} 空流，已切换至 {nxt}"
                    continue
                return
            except Exception as e:
                if yielded:
                    raise  # 已吐 token，中途异常不切（避免重复），抛给上层 degraded
                last_exc = e
                record_fail(name)
                self._fb_metric(name, nxt, "stream_exception")
                degraded("llm_fallback_stream", e, f"{name} 流启动失败切备 {nxt}")
                last_reason = f"{name} 不可用({type(e).__name__})，已切换至 {nxt}"
        if last_exc is not None:
            raise last_exc
```

- [ ] **Step 5: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_llm_router.py -v`
Expected: 全部 passed（原有 9 个 + 新增 5 个）

- [ ] **Step 6: ollama 不参与周期主动探活（只被动熔断）——先写失败的测试**

设计决策（见 spec 文档第 3 节）：本地 Ollama 是"应急兼底"，平时几乎不该被调用；但 `_refresh_llm_health_loop` 现状对链上每个 provider 都每 `LLM_PROBE_INTERVAL`（默认 30s）真实发一次 chat 请求探活——如果 ollama 也走这套，就是无条件每 30s 跟本地 CPU 模型打一次推理请求，白白占用 CPU。改成：ollama 的健康状态完全依赖 `record_fail`/`record_ok`（真实调用失败时被动计熔断），不参与周期主动探活。

在 `tests/test_llm_router.py` 末尾追加：

```python
# ===== L1 本地模型不参与主动探活 =====

def test_should_actively_probe_excludes_ollama():
    from app.providers.llm_router import _should_actively_probe
    assert _should_actively_probe("ollama") is False


def test_should_actively_probe_includes_cloud_providers():
    from app.providers.llm_router import _should_actively_probe
    assert _should_actively_probe("qwen") is True
    assert _should_actively_probe("deepseek") is True
    assert _should_actively_probe("doubao") is True
```

Run: `venv/Scripts/python.exe -m pytest tests/test_llm_router.py -v -k should_actively_probe`
Expected: FAIL，`ImportError: cannot import name '_should_actively_probe'`

- [ ] **Step 7: 实现**

在 `backend/app/providers/llm_router.py` 的 `_refresh_llm_health_loop` 之前新增一个纯函数：

```python
def _should_actively_probe(name: str) -> bool:
    """本地应急模型(ollama)不参与周期主动探活——避免平时空闲时每个探活周期都被迫做一次
    CPU 推理。它的健康状态完全靠真实调用失败时的被动熔断(record_fail/record_ok)判定。"""
    return name != "ollama"
```

`_refresh_llm_health_loop`（现状第 227-256 行）的 for 循环体：

```python
            for p in _fallback_chain():
                try:
                    res = await check_llm_health(p)
                    ok = res.get("status") == "ok"
                    try:
                        metrics.LLM_PROVIDER_HEALTH.labels(p).set(1 if ok else 0)
                    except Exception:
                        pass
                    if ok:
                        record_ok(p)
                    else:
                        record_fail(p)
                except Exception:
                    pass
```

改成：

```python
            for p in _fallback_chain():
                if not _should_actively_probe(p):
                    continue
                try:
                    res = await check_llm_health(p)
                    ok = res.get("status") == "ok"
                    try:
                        metrics.LLM_PROVIDER_HEALTH.labels(p).set(1 if ok else 0)
                    except Exception:
                        pass
                    if ok:
                        record_ok(p)
                    else:
                        record_fail(p)
                except Exception:
                    pass
```

- [ ] **Step 8: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_llm_router.py -v`
Expected: 全部 passed（原有 9 个 + 新增 7 个）

- [ ] **Step 9: Commit**

```bash
git add backend/app/providers/llm_router.py tests/test_llm_router.py
git status   # 确认 backend/tests/test_llm_router.py 显示为 deleted（git mv 已处理）
git commit -m "feat(providers): FallbackLLMProvider增加degraded/degrade_reason，ollama不参与主动探活，修回test_llm_router.py到repo根tests/"
```

---

### Task 3: OLLAMA_ENABLE 热配置开关（不重启、不影响进行中会话）

**Files:**
- Modify: `backend/app/services/config_service.py`
- Modify: `backend/app/providers/llm_router.py:73-82`（`_fallback_chain`）
- Modify: `backend/app/schemas/system.py`
- Modify: `backend/app/routers/system.py`
- Test: `tests/test_config_service_llm_router.py`（新建）

**Interfaces:**
- Consumes: `config_service._RUNTIME`、`redis_client.cache_get_json`/`cache_set_json_persistent`（已有）
- Produces: `config_service.rt_ollama_enable() -> bool`——Task 2 之外，`llm_router._fallback_chain()` 直接调用；`GET/PUT /system/config/llm-router` API。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_config_service_llm_router.py`：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_config_service_llm_router.py -v`
Expected: FAIL，`update_llm_router_config() got an unexpected keyword argument 'ollama_enable'`

- [ ] **Step 3: config_service.py 加热配置**

`_RUNTIME` 初始字典（现状第 13-14 行）：

```python
_RUNTIME = {"ef": 64, "temperature": 0.2, "max_tokens": 2048, "system_prompt": None,
            "llm_fallback_chain": None, "tier_models": None}
```

改成：

```python
_RUNTIME = {"ef": 64, "temperature": 0.2, "max_tokens": 2048, "system_prompt": None,
            "llm_fallback_chain": None, "tier_models": None, "ollama_enable": None}
```

`load_runtime()` 里读 `config:llm_router` 的分支（现状第 45-54 行）：

```python
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
```

改成（新增 `ollamaEnable` 读取）：

```python
    try:
        lr = await redis_client.cache_get_json("config:llm_router")
        if lr:
            if isinstance(lr.get("fallbackChain"), list):
                _RUNTIME["llm_fallback_chain"] = [p for p in lr["fallbackChain"]
                                                  if isinstance(p, str) and p.strip()]
            if isinstance(lr.get("tierModels"), dict):
                _RUNTIME["tier_models"] = lr["tierModels"]
            if "ollamaEnable" in lr:
                _RUNTIME["ollama_enable"] = bool(lr["ollamaEnable"])
    except Exception:
        pass
```

在 `rt_tier_models()` 之后（现状第 145-147 行之后）新增 getter：

```python
def rt_ollama_enable() -> bool:
    """本地 Ollama 兜底开关（热读；未设置时默认启用）。管理后台可不重启即时切换。"""
    v = _RUNTIME.get("ollama_enable")
    return True if v is None else bool(v)
```

`get_llm_router_config()`（现状第 150-152 行）：

```python
async def get_llm_router_config() -> dict:
    v = await redis_client.cache_get_json("config:llm_router")
    return v or {"fallbackChain": [], "tierModels": {}}
```

改成：

```python
async def get_llm_router_config() -> dict:
    v = await redis_client.cache_get_json("config:llm_router")
    if not v:
        return {"fallbackChain": [], "tierModels": {}, "ollamaEnable": True}
    v.setdefault("ollamaEnable", True)
    return v
```

`update_llm_router_config()`（现状第 155-162 行）：

```python
async def update_llm_router_config(fallback_chain: list[str], tier_models: dict) -> dict:
    """保存 LLM 路由配置，即改即生效（下次 resolve 即用新链/档位）。"""
    data = {"fallbackChain": fallback_chain or [], "tierModels": tier_models or {}}
    await redis_client.cache_set_json_persistent("config:llm_router", data)
    _RUNTIME["llm_fallback_chain"] = [p for p in (fallback_chain or [])
                                      if isinstance(p, str) and p.strip()]
    _RUNTIME["tier_models"] = tier_models or {}
    return data
```

改成：

```python
async def update_llm_router_config(fallback_chain: list[str], tier_models: dict,
                                    ollama_enable: bool = True) -> dict:
    """保存 LLM 路由配置，即改即生效（下次 resolve 即用新链/档位/本地兜底开关）。"""
    data = {"fallbackChain": fallback_chain or [], "tierModels": tier_models or {},
            "ollamaEnable": bool(ollama_enable)}
    await redis_client.cache_set_json_persistent("config:llm_router", data)
    _RUNTIME["llm_fallback_chain"] = [p for p in (fallback_chain or [])
                                      if isinstance(p, str) and p.strip()]
    _RUNTIME["tier_models"] = tier_models or {}
    _RUNTIME["ollama_enable"] = bool(ollama_enable)
    return data
```

- [ ] **Step 4: llm_router.py 的 `_fallback_chain` 应用开关**

现状（第 73-82 行）：

```python
def _fallback_chain() -> list[str]:
    """读取 fallback 链（热配置优先，回落 settings）。"""
    try:
        from app.services.config_service import rt_llm_fallback_chain
        chain = rt_llm_fallback_chain()
        if chain:
            return [p for p in chain if isinstance(p, str) and p.strip()]
    except Exception:
        pass
    return [s.strip() for s in settings.LLM_FALLBACK_CHAIN.split(",") if s.strip()]
```

改成：

```python
def _fallback_chain() -> list[str]:
    """读取 fallback 链（热配置优先，回落 settings）；ollama 禁用时从链中剔除（管理员热开关）。"""
    try:
        from app.services.config_service import rt_llm_fallback_chain, rt_ollama_enable
        chain = rt_llm_fallback_chain()
        if not rt_ollama_enable():
            chain = [p for p in chain if p != "ollama"]
        return chain
    except Exception:
        pass
    return [s.strip() for s in settings.LLM_FALLBACK_CHAIN.split(",") if s.strip()]
```

- [ ] **Step 5: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_config_service_llm_router.py -v`
Expected: 4 passed

- [ ] **Step 6: 加管理接口（schema + router）**

`backend/app/schemas/system.py` 末尾追加：

```python
class LlmRouterConfigRequest(BaseModel):
    fallbackChain: list[str] = []
    tierModels: dict = {}
    ollamaEnable: bool = True
```

`backend/app/routers/system.py` 的 schema 导入行（现状第 15 行）：

```python
from app.schemas.system import AlertDisposeRequest, AgentRunRequest, AiDraftUpdateRequest, ConfidenceUpdateRequest, PersonaConfigRequest, MilvusConfigRequest, ModelConfigRequest
```

改成：

```python
from app.schemas.system import AlertDisposeRequest, AgentRunRequest, AiDraftUpdateRequest, ConfidenceUpdateRequest, PersonaConfigRequest, MilvusConfigRequest, ModelConfigRequest, LlmRouterConfigRequest
```

在 `@router.get("/config/prompt")` 之前（紧跟 `/config/model` 相关路由之后）新增两个路由：

```python
@router.get("/config/llm-router")
async def get_llm_router_config_route(admin: User = Depends(require_admin)):
    return success(await config_service.get_llm_router_config(), "查询成功")


@router.put("/config/llm-router")
async def update_llm_router_config_route(
    body: LlmRouterConfigRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """保存 LLM 路由配置（fallback 链 / 分档 / 本地兜底开关），即改即生效，不影响进行中的会话。"""
    data = await config_service.update_llm_router_config(
        body.fallbackChain, body.tierModels, body.ollamaEnable)
    await write_log(db, admin.username, "LLM路由配置", f"ollamaEnable={data['ollamaEnable']}")
    return success(data, "已保存（下次问答生效）")
```

- [ ] **Step 7: 手动冒烟验证（无自动化测试，FastAPI 路由层）**

后端起服务后：
```bash
curl -X PUT http://localhost:8001/api/system/config/llm-router -H "Authorization: Bearer $ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"fallbackChain":[],"tierModels":{},"ollamaEnable":false}'
curl http://localhost:8001/api/system/config/llm-router -H "Authorization: Bearer $ADMIN_TOKEN"
```
Expected：PUT 返回 `ollamaEnable: false`；GET 回显一致；再 PUT 一次 `ollamaEnable: true` 恢复。

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/config_service.py backend/app/providers/llm_router.py backend/app/schemas/system.py backend/app/routers/system.py tests/test_config_service_llm_router.py
git commit -m "feat(config): OLLAMA_ENABLE热配置开关(不重启生效)+管理接口/system/config/llm-router"
```

---

### Task 4: 检索层云端向量降级可见性（`_dense_dual` trace mark）

**Files:**
- Modify: `backend/app/services/retrieval_service.py:188-204`（`_dense_dual`）
- Test: `tests/test_dense_dual_degradation.py`（新建）

**Interfaces:**
- Consumes: `app.core.qa_trace.get_collector`（已在文件顶部 `import as _get_trace`）
- Produces: trace mark `"dense_cloud_failed": True`（写入当前请求的 `TraceCollector.marks`）——Task 6/7 的 `qa_service._retrieval_degradation_fields()` 读取。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_dense_dual_degradation.py`：

```python
"""_dense_dual 云路降级可见性单测：云端 embedding/检索异常时打 trace mark，
供 qa_service 透出 retrievalDegraded 给前端（不依赖真实 Milvus/DashScope）。"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.core.qa_trace import TraceCollector
from app.services import retrieval_service


def test_cloud_failure_marks_trace():
    async def go():
        tc = TraceCollector("q")

        async def fake_embed(text, provider=None):
            if provider == "bge":
                return [0.1] * 8
            raise RuntimeError("DashScope 欠费")

        with patch.object(retrieval_service, "_get_trace", return_value=tc), \
             patch.object(retrieval_service.embedding_service, "embed_query",
                          AsyncMock(side_effect=fake_embed)), \
             patch.object(retrieval_service.milvus_client, "search", return_value=[{"score": 0.5}]):
            cloud_hits, bge_hits = await retrieval_service._dense_dual("query", 5, 64)

        assert cloud_hits == []
        assert bge_hits == [{"score": 0.5}]
        assert tc.marks.get("dense_cloud_failed") is True
    asyncio.run(go())


def test_both_paths_ok_no_mark():
    async def go():
        tc = TraceCollector("q")

        with patch.object(retrieval_service, "_get_trace", return_value=tc), \
             patch.object(retrieval_service.embedding_service, "embed_query",
                          AsyncMock(return_value=[0.1] * 8)), \
             patch.object(retrieval_service.milvus_client, "search", return_value=[{"score": 0.9}]):
            cloud_hits, bge_hits = await retrieval_service._dense_dual("query", 5, 64)

        assert cloud_hits and bge_hits
        assert "dense_cloud_failed" not in tc.marks
    asyncio.run(go())


def test_bge_failure_alone_does_not_mark_retrieval_degraded():
    """bge 路单独挂不算"云端降级"（bge 本来就是本地兜底路，它挂了是另一类问题）。"""
    async def go():
        tc = TraceCollector("q")

        async def fake_embed(text, provider=None):
            if provider == "bge":
                raise RuntimeError("bge 模型加载失败")
            return [0.1] * 8

        with patch.object(retrieval_service, "_get_trace", return_value=tc), \
             patch.object(retrieval_service.embedding_service, "embed_query",
                          AsyncMock(side_effect=fake_embed)), \
             patch.object(retrieval_service.milvus_client, "search", return_value=[{"score": 0.9}]):
            cloud_hits, bge_hits = await retrieval_service._dense_dual("query", 5, 64)

        assert bge_hits == []
        assert "dense_cloud_failed" not in tc.marks
    asyncio.run(go())
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_dense_dual_degradation.py -v`
Expected: `test_cloud_failure_marks_trace` FAIL（`tc.marks` 里没有 `dense_cloud_failed`），另外两个可能已经通过（因为现状本就不打 mark）。

- [ ] **Step 3: 实现**

`backend/app/services/retrieval_service.py` 的 `_dense_dual`（现状第 188-204 行）：

```python
async def _dense_dual(dense_q: str, cand: int, ef: int) -> tuple[list[dict], list[dict]]:
    """双路向量检索（云 grid_chunks + 本地 bge grid_chunks_bge），各自全程降级。

    云路远端欠费/限流/断网 → 空命中不阻塞；bge 本地恒走。任一路崩都不杀检索
    （对齐"降级而非崩溃"约定）。云挂时 bge 单独扛 dense 召回 + BM25 仍在。
    返回 (cloud_hits, bge_hits)。
    """
    async def _path(provider: str, collection: str, tag: str) -> list[dict]:
        try:
            vec = await embedding_service.embed_query(dense_q, provider)
            return await asyncio.to_thread(milvus_client.search, collection, vec, cand, ef)
        except Exception as e:
            degraded(f"dense_{tag}", e)
            return []
    return await asyncio.gather(
        _path(settings.EMB_PROVIDER, settings.MILVUS_COLLECTION, "cloud"),
        _path("bge", settings.MILVUS_COLLECTION_BGE, "bge"),
    )
```

改成：

```python
async def _dense_dual(dense_q: str, cand: int, ef: int) -> tuple[list[dict], list[dict]]:
    """双路向量检索（云 grid_chunks + 本地 bge grid_chunks_bge），各自全程降级。

    云路远端欠费/限流/断网 → 空命中不阻塞；bge 本地恒走。任一路崩都不杀检索
    （对齐"降级而非崩溃"约定）。云挂时 bge 单独扛 dense 召回 + BM25 仍在。
    云路失败额外打 trace mark（dense_cloud_failed），供 qa_service 透出
    retrievalDegraded 给前端（bge 路挂不算"云端降级"，不打此 mark）。
    返回 (cloud_hits, bge_hits)。
    """
    async def _path(provider: str, collection: str, tag: str) -> list[dict]:
        try:
            vec = await embedding_service.embed_query(dense_q, provider)
            return await asyncio.to_thread(milvus_client.search, collection, vec, cand, ef)
        except Exception as e:
            degraded(f"dense_{tag}", e)
            if tag == "cloud":
                tc = _get_trace()
                if tc:
                    tc.mark("dense_cloud_failed", True)
            return []
    return await asyncio.gather(
        _path(settings.EMB_PROVIDER, settings.MILVUS_COLLECTION, "cloud"),
        _path("bge", settings.MILVUS_COLLECTION_BGE, "bge"),
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_dense_dual_degradation.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval_service.py tests/test_dense_dual_degradation.py
git commit -m "feat(retrieval): _dense_dual云路失败打trace mark，供前端透出检索降级提示"
```

---

### Task 5: rewrite_evaluator.py 云端 embedding 兜底 bge（配对切换 collection）

**Files:**
- Modify: `backend/app/services/rewrite_evaluator.py:16-21`（`_light_dense`）
- Test: `tests/test_rewrite_evaluator.py`（追加用例）

**Interfaces:**
- Consumes: `embedding_service.embed_query`、`milvus_client.search`、`settings.MILVUS_COLLECTION_BGE`（已存在，检索层双路兼底同款配置）
- 无对外新接口，`evaluate()` 签名不变。

**注意（正确性约束，来自设计评审）**：`semantic_cache.py` 的云端 embedding **不**在本次兜底范围内——它的历史查询向量都存在同一个索引里，云端异常时若混入 bge 向量会导致两种向量空间混在一起比对，结果不可信且会永久污染索引（3 天 TTL 不会自动清）。只有 `rewrite_evaluator.py` 这种"embedding 和检索的 collection 天然配对切换"的场景才适合加 bge 兜底。

- [ ] **Step 1: 写失败的测试**

在 `tests/test_rewrite_evaluator.py` 末尾追加：

```python
def test_cloud_embed_failure_falls_back_to_bge():
    """云端 embedding 异常 → 回退 bge embedding + bge collection（不能只切 embedding 不切 collection，
    否则用 bge 向量查云端 collection，向量空间不匹配，分数没有意义）。"""
    async def go():
        calls = []

        async def fake_embed_query(text, provider=None):
            calls.append(provider)
            if provider != "bge":
                raise RuntimeError("DashScope 欠费")
            return [0.1] * 8

        def fake_search(collection, vec, cand):
            assert collection == rewrite_evaluator.settings.MILVUS_COLLECTION_BGE
            return [{"score": 0.5}] * 5

        with patch.object(rewrite_evaluator.embedding_service, "embed_query",
                          AsyncMock(side_effect=fake_embed_query)), \
             patch.object(rewrite_evaluator.milvus_client, "search", fake_search):
            hits = await rewrite_evaluator._light_dense("query", None)

        assert hits == [{"score": 0.5}] * 5
        assert "bge" in calls
    asyncio.run(go())


def test_cloud_embed_success_uses_cloud_collection():
    """云端 embedding 正常 → 走云端 collection，不触发 bge 回退。"""
    async def go():
        def fake_search(collection, vec, cand):
            assert collection == rewrite_evaluator.settings.MILVUS_COLLECTION
            return [{"score": 0.7}] * 5

        with patch.object(rewrite_evaluator.embedding_service, "embed_query",
                          AsyncMock(return_value=[0.2] * 8)), \
             patch.object(rewrite_evaluator.milvus_client, "search", fake_search):
            hits = await rewrite_evaluator._light_dense("query", None)

        assert hits == [{"score": 0.7}] * 5
    asyncio.run(go())
```

（该文件顶部已 `from unittest.mock import AsyncMock, patch` 和 `from app.services import rewrite_evaluator`，无需新增 import；需要额外 `import app.clients.milvus_client`——通过 `rewrite_evaluator.milvus_client` 访问，模块顶部已 `from app.clients import milvus_client`，无需改动。）

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_rewrite_evaluator.py -v`
Expected: `test_cloud_embed_failure_falls_back_to_bge` FAIL（`RuntimeError: DashScope 欠费` 未被捕获）

- [ ] **Step 3: 实现**

`backend/app/services/rewrite_evaluator.py` 的 `_light_dense`（现状第 16-21 行）：

```python
async def _light_dense(query: str, model_type: str | None) -> list[dict]:
    """单路 dense_cloud 轻量检索，返回 [{score, ...}, ...]。"""
    qvec = await embedding_service.embed_query(query, settings.EMB_PROVIDER)
    return await asyncio.to_thread(
        milvus_client.search, settings.MILVUS_COLLECTION, qvec, settings.REWRITE_EVAL_CAND,
    )
```

改成：

```python
async def _light_dense(query: str, model_type: str | None) -> list[dict]:
    """单路轻量检索：优先云端 embedding+云端 collection；云端 embedding 异常时配对回退
    bge embedding+bge collection（两者必须一起切，向量空间不同，不能用 bge 向量查云端 collection）。
    外层 evaluate() 的 try/except 仍是最终安全网（bge 也失败时兜底 not improved）。"""
    try:
        qvec = await embedding_service.embed_query(query, settings.EMB_PROVIDER)
        collection = settings.MILVUS_COLLECTION
    except Exception as e:
        degraded("rewrite_eval_embed_cloud", e)
        qvec = await embedding_service.embed_query(query, "bge")
        collection = settings.MILVUS_COLLECTION_BGE
    return await asyncio.to_thread(
        milvus_client.search, collection, qvec, settings.REWRITE_EVAL_CAND,
    )
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_rewrite_evaluator.py -v`
Expected: 5 passed（原 3 + 新增 2）

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/rewrite_evaluator.py tests/test_rewrite_evaluator.py
git commit -m "feat(rewrite): 改写评估云端embedding挂时配对回退bge embedding+bge collection"
```

---

### Task 6: qa_service.py 集成（非流式 `answer()`）——降级信号透出 + 全链路耗尽优雅拒答

**Files:**
- Modify: `backend/app/services/qa_service.py`（新增 4 个模块级 helper + `answer()` 内的 LLM 调用段 + `result` 字典）
- Test: `tests/test_qa_service_degradation.py`（新建）

**Interfaces:**
- Produces: `qa_service._llm_degradation_fields(llm_prov, model_type) -> dict`（`{modelType, llmDegraded, llmDegradedReason}`）
- Produces: `qa_service._cap_confidence_for_local_model(confidence, actual_provider) -> str`
- Produces: `qa_service._retrieval_degradation_fields() -> dict`（`{retrievalDegraded, retrievalDegradedReason}`，读 Task 4 打的 trace mark）
- Produces: `qa_service._llm_all_down_response(nq, contexts, t0, conversation_id) -> dict`（结构化拒答，替代裸抛异常）
- Consumes: Task 2 的 `FallbackLLMProvider.degraded`/`.degrade_reason`/`.last_used_name`；Task 4 的 trace mark `dense_cloud_failed`

**为什么不测完整 `answer()` 端到端**：`answer()` 依赖 DB/Redis/Milvus/多个内部子服务，本仓库现有测试（如 `tests/test_crag_refused_to_gap.py`）一贯只测被抽出的独立 helper，不 mock 整条编排链路——本任务把新逻辑抽成 4 个纯函数正是为了能这样测。编排层的接线通过 Step 7 的手动冒烟验证确认。

- [ ] **Step 1: 写失败的测试（4 个纯 helper）**

创建 `tests/test_qa_service_degradation.py`：

```python
"""云端LLM/Embedding熔断降级：qa_service 里新增的降级信号透出逻辑单测。

只测被抽出的独立 helper（不 mock 整条 answer()/stream_answer() 编排链路，
理由见 2026-08-12-llm-degradation-resilience.md Task 6 说明）。
"""
from app.core.qa_trace import new_collector
from app.services import qa_service


def test_llm_degradation_fields_no_switch():
    class Prov:
        last_used_name = "qwen"
        degraded = False
        degrade_reason = ""
    fields = qa_service._llm_degradation_fields(Prov(), "qwen")
    assert fields == {"modelType": "qwen", "llmDegraded": False, "llmDegradedReason": ""}


def test_llm_degradation_fields_switched_to_ollama():
    class Prov:
        last_used_name = "ollama"
        degraded = True
        degrade_reason = "云端模型全部不可用，已使用本地应急模型"
    fields = qa_service._llm_degradation_fields(Prov(), "qwen")
    assert fields["modelType"] == "ollama"
    assert fields["llmDegraded"] is True
    assert "本地应急模型" in fields["llmDegradedReason"]


def test_llm_degradation_fields_missing_attrs_falls_back_to_requested():
    """provider 对象缺 last_used_name（防御性分支）→ 回落请求参数。"""
    fields = qa_service._llm_degradation_fields(object(), "deepseek")
    assert fields["modelType"] == "deepseek"
    assert fields["llmDegraded"] is False


def test_cap_confidence_caps_ollama_high_to_medium():
    assert qa_service._cap_confidence_for_local_model("high", "ollama") == "medium"


def test_cap_confidence_leaves_cloud_provider_unchanged():
    assert qa_service._cap_confidence_for_local_model("high", "qwen") == "high"


def test_cap_confidence_leaves_non_high_unchanged():
    assert qa_service._cap_confidence_for_local_model("medium", "ollama") == "medium"


def test_retrieval_degradation_fields_when_marked():
    tc = new_collector("q")
    tc.mark("dense_cloud_failed", True)
    fields = qa_service._retrieval_degradation_fields()
    assert fields["retrievalDegraded"] is True
    assert "云端向量检索" in fields["retrievalDegradedReason"]


def test_retrieval_degradation_fields_when_not_marked():
    new_collector("q")
    fields = qa_service._retrieval_degradation_fields()
    assert fields["retrievalDegraded"] is False
    assert fields["retrievalDegradedReason"] == ""


def test_llm_all_down_response_structure():
    contexts = [{"docId": "d1", "docName": "规程A", "docType": "pdf", "chunkIdx": 0,
                 "chunk": "内容", "score": 0.8, "sources": ["dense_cloud"]}]
    resp = qa_service._llm_all_down_response("主变异常", contexts, 0.0, "conv1")
    assert resp["confidence"] == "refused"
    assert resp["cragAction"] == "llm_all_down"
    assert resp["conversationId"] == "conv1"
    assert len(resp["retrievalSource"]) == 1
    assert resp["retrievalSource"][0]["docId"] == "d1"
    assert "本地应急模型" in resp["answer"]
```

- [ ] **Step 2: 运行确认失败**

Run: `venv/Scripts/python.exe -m pytest tests/test_qa_service_degradation.py -v`
Expected: FAIL，`AttributeError: module 'app.services.qa_service' has no attribute '_llm_degradation_fields'`

- [ ] **Step 3: 新增 4 个 helper**

在 `backend/app/services/qa_service.py` 的 `_cache_tenant` 函数之后（现状第 35-36 行之后）插入：

```python
def _llm_degradation_fields(llm_prov, model_type: str | None) -> dict:
    """从 FallbackLLMProvider 读取真实命中的 provider + 是否发生过降级切换。

    modelType 用实际命中的 provider（而非请求参数/默认配置），修复前端
    "🤖模型"徽章失真——旧代码 modelType 恒等于 model_type or settings.LLM_PROVIDER，
    fallback 切备后前端完全看不出来。
    """
    actual = getattr(llm_prov, "last_used_name", model_type or settings.LLM_PROVIDER)
    return {
        "modelType": actual,
        "llmDegraded": bool(getattr(llm_prov, "degraded", False)),
        "llmDegradedReason": getattr(llm_prov, "degrade_reason", ""),
    }


def _cap_confidence_for_local_model(confidence: str, actual_provider: str) -> str:
    """本地应急模型(ollama)作答质量明显低于云端，confidence 封顶 medium，
    避免"高置信"徽章与"本地应急模型"警告语义打架（顺带效果：ollama 作答不会被当作
    高置信答案写入长期缓存/highbase，符合"应急答案不该被当权威沉淀"的预期）。
    """
    if actual_provider == "ollama" and confidence == "high":
        return "medium"
    return confidence


def _retrieval_degradation_fields() -> dict:
    """读取本次请求的 trace mark，判断云端向量检索是否降级（bge 独扛，见 _dense_dual）。"""
    tc = _get_trace()
    is_degraded = bool(tc and tc.marks.get("dense_cloud_failed"))
    return {
        "retrievalDegraded": is_degraded,
        "retrievalDegradedReason": ("云端向量检索不可用，已降级为本地embedding+关键词检索"
                                     if is_degraded else ""),
    }


def _llm_all_down_response(nq: str, contexts: list[dict], t0: float,
                            conversation_id: str | None) -> dict:
    """LLM fallback 链（含本地 ollama 兜底）全部耗尽时的结构化拒答，替代裸抛异常导致的
    非流式裸 500 / 流式 SSE 硬断（I2）。仍保留已检索到的证据，不让用户白等一场。"""
    return {
        "answer": "抱歉，当前所有 AI 模型（含本地应急模型）暂时不可用，请稍后重试。",
        "retrievalSource": [{
            "docId": c.get("docId", ""), "docName": c.get("docName", ""),
            "docType": c.get("docType", ""), "chunkIdx": c.get("chunkIdx"),
            "chunk": c.get("chunk", ""), "score": c.get("score", 0.0),
            "sources": c.get("sources", []),
        } for c in contexts],
        "responseTime": round(time.time() - t0, 3), "hallucinationRate": 0.0,
        "cached": False, "confidence": "refused", "cragAction": "llm_all_down",
        "conversationId": conversation_id or "",
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `venv/Scripts/python.exe -m pytest tests/test_qa_service_degradation.py -v`
Expected: 9 passed

- [ ] **Step 5: 接线到 `answer()`——LLM 调用段**

现状（第 867-882 行）：

```python
    _tc0 = _get_trace()
    if _tc0:
        _tc0.mark("llm_tier", _tier); _tc0.mark("llm_route_reason", _tier_reason)
    _llm0 = time.time()
    _llm_prov = get_llm_provider(model_type, tier=_tier)
    # B4：真实 token usage（opt-in，默认关 → 走原 chat str 路径，估算 token）
    _llm_usage: dict | None = None
    if getattr(settings, "LLM_USAGE_TRACK_ENABLE", False):
        raw, _llm_usage = await _llm_prov.chat_with_usage(
            messages, temperature=config_service.rt_temperature(), max_tokens=settings.LLM_MAX_TOKENS,
        )
    else:
        raw = await _llm_prov.chat(
            messages, temperature=config_service.rt_temperature(), max_tokens=settings.LLM_MAX_TOKENS)
    _tc = _get_trace()
    if _tc:
        _tc.record("llm", time.time() - _llm0)
        _tc.mark("provider_used", getattr(_llm_prov, "last_used_name", model_type or "default"))
    raw = safety.safe_answer(raw)  # 答案脱敏（PII_MASK_ENABLE 开启时，D4）
```

改成：

```python
    _tc0 = _get_trace()
    if _tc0:
        _tc0.mark("llm_tier", _tier); _tc0.mark("llm_route_reason", _tier_reason)
    _llm0 = time.time()
    _llm_prov = get_llm_provider(model_type, tier=_tier)
    # B4：真实 token usage（opt-in，默认关 → 走原 chat str 路径，估算 token）
    _llm_usage: dict | None = None
    try:
        if getattr(settings, "LLM_USAGE_TRACK_ENABLE", False):
            raw, _llm_usage = await _llm_prov.chat_with_usage(
                messages, temperature=config_service.rt_temperature(), max_tokens=settings.LLM_MAX_TOKENS,
            )
        else:
            raw = await _llm_prov.chat(
                messages, temperature=config_service.rt_temperature(), max_tokens=settings.LLM_MAX_TOKENS)
    except Exception as e:
        # 全部 provider（含本地 ollama 兜底）耗尽 → 优雅拒答，不裸抛 500（I2）
        degraded("llm_all_exhausted", e)
        try:
            from app.services.evidence_gap_service import collect
            await collect(nq, "", "refused", "", "", "auto_llm_down", tenant or "default")
        except Exception:
            pass
        return _llm_all_down_response(nq, contexts, t0, conversation_id)
    _llm_fields = _llm_degradation_fields(_llm_prov, model_type)
    _tc = _get_trace()
    if _tc:
        _tc.record("llm", time.time() - _llm0)
        _tc.mark("provider_used", _llm_fields["modelType"])
    raw = safety.safe_answer(raw)  # 答案脱敏（PII_MASK_ENABLE 开启时，D4）
```

- [ ] **Step 6: 接线 confidence 封顶 + `result` 字典新增字段**

在 `ans, confidence = await _maybe_debate_augment(db, nq, ans, confidence, crag_extras, model_type)`（现状第 944 行）之后新增一行：

```python
    ans, confidence = await _maybe_debate_augment(db, nq, ans, confidence, crag_extras, model_type)
    confidence = _cap_confidence_for_local_model(confidence, _llm_fields["modelType"])
```

`result` 字典（现状第 967-990 行）里，`"conversationId": conversation_id,` 这一行之后（`**citation_extras,` 之前）插入新字段：

```python
        "route": routing.route if routing else "hybrid",
        "routeReason": routing.reason if routing else "",
        "conversationId": conversation_id,
        **_llm_fields,
        **_retrieval_degradation_fields(),
        **citation_extras,   # Task 10: 空 dict（开关关）时不新增字段，零破坏
        **crag_extras,       # T2: CRAG_V3 归因字段（关={}零破坏；随 result 进缓存自动同步）
    }
```

- [ ] **Step 7: 手动冒烟验证**

后端起服务，走一次正常问答确认 `result.modelType`/`llmDegraded`/`retrievalDegraded` 字段存在且默认场景下 `llmDegraded=false`、`retrievalDegraded=false`；再临时把 `DASHSCOPE_API_KEY` 改成错的重启一次，确认能看到 `llmDegraded=true` 或 `retrievalDegraded=true`（视触发的是 LLM 还是 embedding 故障），且没有抛出裸异常。改完记得把 key 改回来。

- [ ] **Step 8: Commit**

```bash
git add backend/app/services/qa_service.py tests/test_qa_service_degradation.py
git commit -m "feat(qa): answer()透出modelType/llmDegraded/retrievalDegraded+全链路耗尽优雅拒答"
```

---

### Task 7: qa_service.py 集成（流式 `stream_answer()`）

**Files:**
- Modify: `backend/app/services/qa_service.py`（`stream_answer()` 内的 LLM 流式调用段 + `done_ev` 字典）

**Interfaces:**
- Consumes: Task 6 的 4 个 helper（同一模块，直接调用）

- [ ] **Step 1: 在检索之后计算 retrieval_degraded（可选变量名统一，实际读取延后到 done_ev 构造处也可，这里直接在 done_ev 处调用 helper，无需提前变量）**

（说明：`_retrieval_degradation_fields()` 读取的是请求级 `TraceCollector`，整条请求生命周期内 marks 不会被清空，所以不需要在检索后立即读取，直接在 Step 3 的 `done_ev` 构造处调用即可，逻辑与 Task 6 的 `answer()` 等价但更省一次中间变量。）

- [ ] **Step 2: 包 try/except 到流式 LLM 调用**

现状（第 1467-1483 行）：

```python
    parts: list[str] = []
    _llm0 = time.time()
    _llm_prov = get_llm_provider(model_type, tier=_tier)
    async for token in _llm_prov.stream(
        messages, temperature=config_service.rt_temperature(), max_tokens=settings.LLM_MAX_TOKENS):
        parts.append(token)
        yield {"type": "token", "content": token}
    try:
        from app.core import metrics
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass
    _tc = _get_trace()
    if _tc:
        _tc.record("llm", time.time() - _llm0)   # 流式 LLM 总耗时(首token→末token)
        _tc.mark("provider_used", getattr(_llm_prov, "last_used_name", model_type or "default"))
```

改成：

```python
    parts: list[str] = []
    _llm0 = time.time()
    _llm_prov = get_llm_provider(model_type, tier=_tier)
    try:
        async for token in _llm_prov.stream(
            messages, temperature=config_service.rt_temperature(), max_tokens=settings.LLM_MAX_TOKENS):
            parts.append(token)
            yield {"type": "token", "content": token}
    except Exception as e:
        # 全部 provider（含本地 ollama 兜底）耗尽 → 优雅收尾，不让 SSE 硬断（I2）
        degraded("llm_all_exhausted_stream", e)
        if not parts:
            yield {
                "type": "error",
                "content": "当前所有 AI 模型（含本地应急模型）暂时不可用，请稍后重试",
                "confidence": "refused", "conversationId": conversation_id or "",
                "responseTime": round(time.time() - t0, 3),
            }
            return
        parts.append("\n（后续生成中断：服务异常）")
    _llm_fields = _llm_degradation_fields(_llm_prov, model_type)
    _p = _llm_fields["modelType"]  # 下游 metrics/modelType 统一用实际命中的 provider（修复失真）
    try:
        from app.core import metrics
        metrics.LLM_CALLS.labels(_p).inc()
        metrics.LLM_LATENCY.labels(_p).observe(time.time() - _llm0)
    except Exception:
        pass
    _tc = _get_trace()
    if _tc:
        _tc.record("llm", time.time() - _llm0)   # 流式 LLM 总耗时(首token→末token)
        _tc.mark("provider_used", _p)
```

- [ ] **Step 3: confidence 封顶 + `done_ev` 新增字段**

紧接上一步之后（`_tc.mark("provider_used", _p)` 之后，`# 3) 持久化完整答案` 之前）新增一行：

```python
    confidence = _cap_confidence_for_local_model(confidence, _p)
```

`done_ev` 字典（现状第 1588-1605 行）：

```python
    done_ev = {
        "type": "done",
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": halluc,
        "modelType": _p,  # 实际调用的 LLM（前端据此展示 🤖 模型 badge；缓存命中时不带此字段）
        "graphCount": len(graph),
        "highRisk": safety.extract_high_risk(annotated),
        "annotatedAnswer": annotated,        # 补标后全文，前端替换渲染出 [n] 角标上标
        "evidenceTrace": _trace,             # 句级溯源
        "confidence": confidence,
        "cragAction": crag_action,
        "cragGrade": crag_grade,
        "conversationId": conversation_id,
        "cached": False,
        "cacheLayer": "llm",
        "route": routing.route if routing else "hybrid",
        "routeReason": routing.reason if routing else "",
    }
```

改成（新增最后 4 个字段；`"modelType": _p` 因为上一步已把 `_p` 重赋值为实际命中 provider，这里不用改就自动修复了）：

```python
    done_ev = {
        "type": "done",
        "responseTime": round(time.time() - t0, 3),
        "hallucinationRate": halluc,
        "modelType": _p,  # 实际调用的 LLM（前端据此展示 🤖 模型 badge；缓存命中时不带此字段）
        "graphCount": len(graph),
        "highRisk": safety.extract_high_risk(annotated),
        "annotatedAnswer": annotated,        # 补标后全文，前端替换渲染出 [n] 角标上标
        "evidenceTrace": _trace,             # 句级溯源
        "confidence": confidence,
        "cragAction": crag_action,
        "cragGrade": crag_grade,
        "conversationId": conversation_id,
        "cached": False,
        "cacheLayer": "llm",
        "route": routing.route if routing else "hybrid",
        "routeReason": routing.reason if routing else "",
        "llmDegraded": _llm_fields["llmDegraded"],
        "llmDegradedReason": _llm_fields["llmDegradedReason"],
        **_retrieval_degradation_fields(),
    }
```

- [ ] **Step 4: 运行既有回归测试确认没有改坏**

Run: `venv/Scripts/python.exe -m pytest tests/test_qa_trace.py tests/test_confidence_integration.py tests/test_crag_refused_to_gap.py tests/test_hotqa_hit.py -v`
Expected: 全部 passed（这几个文件此前已 import/使用 `qa_service` 模块，用于确认新增代码没有语法错误或破坏既有导入）

- [ ] **Step 5: 手动冒烟验证**

前端 Chat 页面正常问一次问题，确认流式返回、SSE 不中断；F12 Network 面板看 `done` 事件 payload 含 `llmDegraded`/`retrievalDegraded` 字段（默认场景应为 `false`）。

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/qa_service.py
git commit -m "feat(qa): stream_answer()透出modelType/llmDegraded/retrievalDegraded+全链路耗尽优雅收尾"
```

---

### Task 8: docker-compose.yml 新增 ollama 服务

**Files:**
- Modify: `docker-compose.yml`（新增 `ollama` service + `backend.depends_on` 追加一项）

**Interfaces:**
- 无代码接口；`backend` 容器通过 `settings.OLLAMA_BASE_URL` 默认值 `http://ollama:11434` 直接按 service name 访问，无需在 `backend.environment` 里额外覆盖。

- [ ] **Step 1: 新增 ollama 服务定义**

在 `docker-compose.yml` 的 `neo4j` 服务块之后、`nacos` 服务块之前插入：

```yaml
  # ---------- Ollama：本地应急 LLM（云端 qwen/deepseek/doubao 全部不可用时的兜底，非强依赖）----------
  # LLM_FALLBACK_CHAIN 末位新增 ollama；首次启动自动拉取模型（7B q4 量化，约 4.5GB，CPU 可推理）。
  # mem_limit/cpus 防止真实触发兜底、开始推理时把 mysql/milvus/redis/neo4j 挤爆(OOM/CPU 饥饿)；
  # 按部署机器实际配置调整数值。
  ollama:
    image: ollama/ollama:latest
    container_name: grid-ollama
    restart: unless-stopped
    ports:
      - "11434:11434"
    volumes:
      - ./data/ollama:/root/.ollama
    entrypoint: ["/bin/sh", "-c"]
    command:
      - "ollama serve & sleep 3 && ollama pull ${OLLAMA_MODEL:-qwen2.5:7b-instruct-q4_K_M} && wait"
    mem_limit: 10g
    cpus: 4
```

- [ ] **Step 2: backend 服务追加 depends_on**

`backend` 服务的 `depends_on`（现状）：

```yaml
    depends_on:
      - mysql
      - minio
      - milvus
      - redis
      - neo4j
```

改成：

```yaml
    depends_on:
      - mysql
      - minio
      - milvus
      - redis
      - neo4j
      - ollama
```

（非强依赖：仅控制容器启动顺序，不等待模型拉取完成——`ollama` 未就绪时该 provider 在 fallback 链里失败即可，不阻塞 backend 启动或其余云端 provider 正常工作。）

- [ ] **Step 3: 语法校验**

Run: `docker compose config --quiet`
Expected: 无输出、无报错（纯 YAML/语义校验，不实际拉起容器）

- [ ] **Step 4: 手动验证（可选，需要本机 Docker 且能拉取镜像/模型，耗时较长）**

```bash
docker compose up -d ollama
docker compose logs -f ollama   # 观察模型拉取进度
curl http://localhost:11434/api/tags   # 拉取完成后应能看到 qwen2.5:7b-instruct-q4_K_M
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml
git commit -m "feat(deploy): docker-compose新增ollama服务(本地应急LLM，资源限制防挤爆)"
```

---

### Task 9: 前端 Chat.vue——降级 badge + 事件字段透传

**Files:**
- Modify: `frontend/src/views/Chat.vue`

**Interfaces:**
- Consumes: 后端 `done`/`error` SSE 事件新增字段 `llmDegraded`/`llmDegradedReason`/`retrievalDegraded`/`retrievalDegradedReason`（Task 6/7 产出）

- [ ] **Step 1: `onStreamEvent` 的 done/error 分支透传新字段**

现状（`runStream` 函数内，`else if (ev.type === 'done' || ev.type === 'error')` 分支）：

```js
      if (ev.cached !== undefined) msg.cached = ev.cached
      if (ev.cacheLayer) msg.cacheLayer = ev.cacheLayer
      if (ev.trace) msg.trace = ev.trace
```

改成：

```js
      if (ev.cached !== undefined) msg.cached = ev.cached
      if (ev.cacheLayer) msg.cacheLayer = ev.cacheLayer
      if (ev.llmDegraded !== undefined) msg.llmDegraded = ev.llmDegraded
      if (ev.llmDegradedReason) msg.llmDegradedReason = ev.llmDegradedReason
      if (ev.retrievalDegraded !== undefined) msg.retrievalDegraded = ev.retrievalDegraded
      if (ev.retrievalDegradedReason) msg.retrievalDegradedReason = ev.retrievalDegradedReason
      if (ev.trace) msg.trace = ev.trace
```

- [ ] **Step 2: badge 行新增两个条件 badge**

现状（`<span v-if="m.modelType" class="badge badge-info">🤖 {{ modelLabel(m.modelType) }}</span>` 这一行之后）：

```html
              <span v-if="m.modelType" class="badge badge-info">🤖 {{ modelLabel(m.modelType) }}</span>
              <span v-if="m.confidence" class="badge" :class="confBadge(m.confidence)" :title="confTitle(m.confidence)">{{ confLabel(m.confidence) }}</span>
```

改成：

```html
              <span v-if="m.modelType" class="badge badge-info">🤖 {{ modelLabel(m.modelType) }}</span>
              <span v-if="m.llmDegraded" class="badge badge-warning" :title="m.llmDegradedReason">{{ m.modelType === 'ollama' ? '🖥️ 本地应急模型' : '⚠️ 备用模型' }}</span>
              <span v-if="m.retrievalDegraded" class="badge badge-warning" :title="m.retrievalDegradedReason">⚠️ 检索降级</span>
              <span v-if="m.confidence" class="badge" :class="confBadge(m.confidence)" :title="confTitle(m.confidence)">{{ confLabel(m.confidence) }}</span>
```

- [ ] **Step 3: `modelLabel` 增加 ollama 标签**

现状：

```js
function modelLabel(m) { return ({ deepseek: 'DeepSeek', qwen: '通义千问', doubao: '豆包' })[m] || m }
```

改成：

```js
function modelLabel(m) { return ({ deepseek: 'DeepSeek', qwen: '通义千问', doubao: '豆包', ollama: '本地应急模型' })[m] || m }
```

- [ ] **Step 4: 手动验证（前端无单元测试框架，走 dev server 目测）**

```bash
cd frontend && npm run dev
```
打开 Chat 页面正常问一次问题；用浏览器 DevTools 在 Network 面板拦截 `/api/qa/answer/stream` 响应，手动改一条 `done` SSE 数据（或临时在 `onStreamEvent` 里塞一行 `ev.llmDegraded = true; ev.llmDegradedReason = '测试'`）确认两个新 badge 能正常渲染、hover 显示 title、不影响其它徽章布局。改完记得撤掉临时调试代码。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/views/Chat.vue
git commit -m "feat(chat): 新增备用模型/检索降级badge，透传llmDegraded/retrievalDegraded事件字段"
```

---

### Task 10: 前端 Admin.vue——本地兜底开关 + evidence gap source 展示映射

**Files:**
- Modify: `frontend/src/api/index.js`
- Modify: `frontend/src/views/Admin.vue`

**Interfaces:**
- Consumes: Task 3 的 `GET/PUT /system/config/llm-router`

- [ ] **Step 1: api/index.js 新增两个函数**

在 `getPromptConfig`/`updatePromptConfig` 定义行（现状第 17-18 行）之后追加：

```js
export const getLlmRouterConfig = () => request.get('/system/config/llm-router')
export const updateLlmRouterConfig = (fallbackChain, tierModels, ollamaEnable) => request.put('/system/config/llm-router', { fallbackChain, tierModels, ollamaEnable })
```

- [ ] **Step 2: Admin.vue 引入 API + reactive 状态**

`<script setup>` 顶部的 import 列表里加上 `getLlmRouterConfig, updateLlmRouterConfig`（跟 `getMilvusConfig` 等已有 import 放一起）。

reactive 定义（现状 `const model = reactive({ modelType: 'deepseek', temperature: 0.2, max_tokens: 2048 })` 这一行之后）新增：

```js
const llmRouter = reactive({ ollamaEnable: true, fallbackChain: [], tierModels: {} })
```

- [ ] **Step 3: `loadConfig()` 读取 + 新增 `saveLlmRouter()`**

现状 `loadConfig`：

```js
async function loadConfig() {
  try {
    const mv = (await getMilvusConfig()).data || {}
    const md = (await getModelConfig()).data || {}
    const mp = mv.param || {}
    milvus.indexType = mv.indexType || 'HNSW'
    milvus.M = mp.M ?? 16
    milvus.efConstruction = mp.efConstruction ?? 200
    milvus.ef = mp.ef ?? 64
    const pp = md.param || {}
    model.modelType = md.modelType || 'deepseek'
    model.temperature = pp.temperature ?? 0.2
    model.max_tokens = pp.max_tokens ?? 2048
    configLoaded.value = true
  } catch (e) { toast('读取线上配置失败') }
}
```

改成（新增 `lr` 读取）：

```js
async function loadConfig() {
  try {
    const mv = (await getMilvusConfig()).data || {}
    const md = (await getModelConfig()).data || {}
    const lr = (await getLlmRouterConfig()).data || {}
    const mp = mv.param || {}
    milvus.indexType = mv.indexType || 'HNSW'
    milvus.M = mp.M ?? 16
    milvus.efConstruction = mp.efConstruction ?? 200
    milvus.ef = mp.ef ?? 64
    const pp = md.param || {}
    model.modelType = md.modelType || 'deepseek'
    model.temperature = pp.temperature ?? 0.2
    model.max_tokens = pp.max_tokens ?? 2048
    llmRouter.ollamaEnable = lr.ollamaEnable ?? true
    llmRouter.fallbackChain = lr.fallbackChain || []
    llmRouter.tierModels = lr.tierModels || {}
    configLoaded.value = true
  } catch (e) { toast('读取线上配置失败') }
}
```

在 `saveModel` 函数（现状 `async function saveModel() { ... }`）之后新增：

```js
async function saveLlmRouter() {
  // fallbackChain/tierModels 原样回传（本卡片只暴露 ollamaEnable 开关，避免用没编辑过的空值覆盖掉后端已有配置）
  try { await updateLlmRouterConfig(llmRouter.fallbackChain, llmRouter.tierModels, llmRouter.ollamaEnable); toast('本地兜底配置已保存（立即生效，不影响进行中的会话）') }
  catch (e) { toast('保存失败') }
}
```

- [ ] **Step 4: 模板新增卡片**

在"模型参数配置"卡片（`<div class="card">...<button class="btn btn-primary" @click="saveModel">保存</button></div>`）之后，`config-grid` 的 `</div>` 闭合标签之前，新增一张卡片：

```html
        <div class="card">
          <div class="card-header"><h3 class="card-title">本地应急模型（Ollama）</h3><span v-if="configLoaded" class="badge badge-success">已读取线上值</span></div>
          <label class="field-label" style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input type="checkbox" v-model="llmRouter.ollamaEnable" />
            启用本地应急模型兜底
          </label>
          <p class="hint" style="margin-top:8px">云端 LLM（qwen/deepseek/doubao）全部不可用时是否切到本地 Ollama 模型应急作答。关闭后云端全部不可用时将直接拒答，不使用本地模型（适合内存不足以运行本地模型的部署环境）。</p>
          <button class="btn btn-primary" @click="saveLlmRouter">保存</button>
        </div>
```

- [ ] **Step 5: evidence gap source 展示映射补一条**

现状（第 401 行）：

```html
<span v-if="g.source" class="badge badge-neutral" :title="`来源: ${g.source}`">{{ {auto:'自动',auto_crag:'CRAG',auto_no_recall:'无结果',overconfident:'过自信',feedback_dislike:'点踩',manual:'人工'}[g.source] || g.source }}</span>
```

改成：

```html
<span v-if="g.source" class="badge badge-neutral" :title="`来源: ${g.source}`">{{ {auto:'自动',auto_crag:'CRAG',auto_no_recall:'无结果',overconfident:'过自信',feedback_dislike:'点踩',manual:'人工',auto_llm_down:'服务不可用'}[g.source] || g.source }}</span>
```

- [ ] **Step 6: 手动验证**

```bash
cd frontend && npm run dev
```
以 admin 账号登录，进入"系统管理 → ⚙️ 系统配置"，确认新卡片渲染、勾选/取消勾选后点"保存"能成功 toast；刷新页面确认状态持久化（读回来的值和保存的一致）。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/api/index.js frontend/src/views/Admin.vue
git commit -m "feat(admin): 本地应急模型热开关卡片+evidence gap来源映射补auto_llm_down"
```
