from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest_asyncio

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "llm-user-suite"
BACKEND = ROOT / "backend"
sys.path.insert(0, str(SUITE))
sys.path.insert(0, str(BACKEND))

DB_PATH = Path("/tmp/grid_qa_llm_user_suite_tests.db")
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["LLM_USER_DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"
os.environ["LLM_USER_AUTH_DISABLED"] = "true"
os.environ["LLM_USER_ALLOWED_TARGET_HOSTS"] = "testserver,grid-qa-test"
os.environ["LLM_USER_OLLAMA_BASE_URL"] = ""
os.environ["LLM_USER_USER_HASH_SECRET"] = "test-hash-secret"


@pytest_asyncio.fixture(autouse=True)
async def clean_db():
    from llm_user_suite import models  # noqa: F401
    from llm_user_suite.db import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
