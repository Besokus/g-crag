from datetime import datetime, timedelta

import pytest
from llm_user_suite.config import settings
from llm_user_suite.db import SessionLocal
from llm_user_suite.models import Evaluation, Report, RunStep
from llm_user_suite.models import TestRun as SuiteRun
from llm_user_suite.retention import purge_expired
from sqlalchemy import func, select


@pytest.mark.asyncio
async def test_report_retention_removes_run_evidence(monkeypatch):
    monkeypatch.setattr(settings, "REPORT_RETENTION_DAYS", 1)
    old = datetime.now() - timedelta(days=3)
    async with SessionLocal() as db:
        run = SuiteRun(scenario_version_id="sv", status="completed", created_at=old)
        db.add(run)
        await db.flush()
        db.add(RunStep(run_id=run.id, step_index=0))
        db.add(Evaluation(run_id=run.id, dimension="outcome"))
        db.add(Report(run_id=run.id, created_at=old))
        await db.commit()
    result = await purge_expired()
    assert result["reports"] == result["runs"] == 1
    async with SessionLocal() as db:
        assert (await db.execute(select(func.count()).select_from(Evaluation))).scalar() == 0
        assert (await db.execute(select(func.count()).select_from(RunStep))).scalar() == 0


@pytest.mark.asyncio
async def test_terminal_run_without_report_is_still_purged(monkeypatch):
    monkeypatch.setattr(settings, "REPORT_RETENTION_DAYS", 1)
    old = datetime.now() - timedelta(days=3)
    async with SessionLocal() as db:
        run = SuiteRun(scenario_version_id="sv", status="failed", created_at=old)
        db.add(run)
        await db.flush()
        db.add(RunStep(run_id=run.id, step_index=0))
        db.add(Evaluation(run_id=run.id, dimension="outcome"))
        await db.commit()

    result = await purge_expired()

    assert result["runs"] == result["evaluations"] == result["steps"] == 1
