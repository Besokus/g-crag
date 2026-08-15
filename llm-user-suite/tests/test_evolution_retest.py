import hashlib
import hmac
import json
import time

import httpx
import pytest
from llm_user_suite.config import settings
from llm_user_suite.db import SessionLocal
from llm_user_suite.main import app
from llm_user_suite.models import EvolutionRetest, Scenario, ScenarioVersion
from llm_user_suite.models import TestRun as SuiteRun
from sqlalchemy import select


@pytest.mark.asyncio
async def test_indexed_draft_event_schedules_same_scenario_version(monkeypatch):
    monkeypatch.setattr(settings, "GRID_EVENT_SECRET", "event-secret")
    async with SessionLocal() as db:
        scenario = Scenario(id="scenario-1", tenant_id="tenant-a", name="case", signature="case")
        db.add(scenario)
        version = ScenarioVersion(scenario_id=scenario.id, version=1, status="approved", spec={
            "name": "case", "goal": "ask", "stages": [{"intent": "health", "requestTemplate": {"method": "GET", "path": "/health"}}],
        })
        db.add(version)
        await db.flush()
        baseline = SuiteRun(
            tenant_id="tenant-a",
            scenario_version_id=version.id, environment="test",
            target_base_url="https://testserver", score=0.55, status="completed",
        )
        db.add(baseline)
        await db.commit()
        await db.refresh(baseline)

    async def no_dispatch():
        return 0

    monkeypatch.setattr("llm_user_suite.api.dispatch_pending_runs", no_dispatch)
    body = {
        "eventId": "event-1", "draftId": "draft-1", "runId": baseline.id,
        "scenarioVersionId": version.id, "tenantId": "tenant-a", "status": "indexed",
    }
    raw = json.dumps(body).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(b"event-secret", timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/integrations/grid-qa/evolution-events", content=raw,
            headers={"content-type": "application/json", "X-LLM-User-Timestamp": timestamp, "X-LLM-User-Signature": signature},
        )
    assert response.status_code == 200, response.text
    async with SessionLocal() as db:
        link = (await db.execute(select(EvolutionRetest))).scalar_one()
        rerun = await db.get(SuiteRun, link.rerun_id)
    assert rerun.scenario_version_id == baseline.scenario_version_id
    assert rerun.baseline_run_id == baseline.id
    assert rerun.tenant_id == "tenant-a"
