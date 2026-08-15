from types import SimpleNamespace

import pytest
from llm_user_suite.config import settings
from llm_user_suite.llm import chat, used_config


@pytest.mark.asyncio
async def test_role_provider_falls_back_to_ollama(monkeypatch):
    monkeypatch.setattr(settings, "ACTOR_BASE_URL", "https://cloud.example/v1")
    monkeypatch.setattr(settings, "ACTOR_API_KEY", "cloud-secret")
    monkeypatch.setattr(settings, "ACTOR_MODEL", "cloud-model")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434/v1")
    monkeypatch.setattr(settings, "OLLAMA_MODEL", "local-model")

    class Completions:
        def __init__(self, base_url):
            self.base_url = base_url

        async def create(self, **kwargs):
            if "cloud.example" in self.base_url:
                raise RuntimeError("cloud unavailable")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="local answer"))])

    class Client:
        def __init__(self, base_url, **kwargs):
            self.chat = SimpleNamespace(completions=Completions(str(base_url)))

    monkeypatch.setattr("llm_user_suite.llm.AsyncOpenAI", Client)
    assert await chat("actor", [{"role": "user", "content": "hello"}]) == "local answer"
    assert used_config("actor").model == "local-model"
