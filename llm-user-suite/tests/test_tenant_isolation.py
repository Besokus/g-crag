import httpx
import pytest
from llm_user_suite.auth import Principal, current_principal
from llm_user_suite.db import SessionLocal
from llm_user_suite.main import app
from llm_user_suite.models import (
    BehaviorSession,
    Evaluation,
    EvolutionRetest,
    Report,
    Scenario,
    ScenarioVersion,
)
from llm_user_suite.models import TestRun as SuiteRun


async def _tenant_a_principal() -> Principal:
    return Principal(username="admin-a", role="admin", tenant_id="tenant-a")


@pytest.mark.asyncio
async def test_control_api_isolates_tenant_owned_resources():
    async with SessionLocal() as db:
        scenario_a = Scenario(
            tenant_id="tenant-a", name="case-a", signature="same-signature", status="active",
        )
        scenario_b = Scenario(
            tenant_id="tenant-b", name="case-b", signature="same-signature", status="active",
        )
        db.add_all([scenario_a, scenario_b])
        await db.flush()
        spec = {
            "name": "case", "goal": "health",
            "stages": [{
                "intent": "health",
                "requestTemplate": {"method": "GET", "path": "/health"},
            }],
        }
        version_a = ScenarioVersion(
            scenario_id=scenario_a.id, version=1, status="approved", spec=spec,
        )
        version_b = ScenarioVersion(
            scenario_id=scenario_b.id, version=1, status="approved", spec=spec,
        )
        db.add_all([version_a, version_b])
        await db.flush()
        run_a = SuiteRun(
            tenant_id="tenant-a", scenario_version_id=version_a.id,
            status="completed", target_base_url="https://testserver",
        )
        run_b = SuiteRun(
            tenant_id="tenant-b", scenario_version_id=version_b.id,
            status="completed", target_base_url="https://testserver",
        )
        db.add_all([run_a, run_b])
        await db.flush()
        report_a = Report(run_id=run_a.id, verdict="passed", summary="a")
        report_b = Report(run_id=run_b.id, verdict="failed", summary="b")
        db.add_all([
            BehaviorSession(id="session-a", tenant_id="tenant-a", user_hash="a"),
            BehaviorSession(id="session-b", tenant_id="tenant-b", user_hash="b"),
            Evaluation(run_id=run_a.id, dimension="outcome", score=1.0),
            Evaluation(run_id=run_b.id, dimension="outcome", score=0.0),
            report_a,
            report_b,
            EvolutionRetest(
                source_event_id="event-a", tenant_id="tenant-a", draft_id="draft-a",
                scenario_version_id=version_a.id, baseline_run_id=run_a.id,
            ),
            EvolutionRetest(
                source_event_id="event-b", tenant_id="tenant-b", draft_id="draft-b",
                scenario_version_id=version_b.id, baseline_run_id=run_b.id,
            ),
        ])
        await db.commit()

    app.dependency_overrides[current_principal] = _tenant_a_principal
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            sessions = await client.get("/v1/sessions")
            scenarios = await client.get("/v1/scenarios")
            runs = await client.get("/v1/runs")
            evaluations = await client.get("/v1/evaluations")
            retests = await client.get("/v1/evaluations/retests")
            reports = await client.get("/v1/reports")

            assert [row["id"] for row in sessions.json()] == ["session-a"]
            assert [row["id"] for row in scenarios.json()] == [scenario_a.id]
            assert [row["id"] for row in runs.json()] == [run_a.id]
            assert [row["runId"] for row in evaluations.json()] == [run_a.id]
            assert [row["tenantId"] for row in retests.json()] == ["tenant-a"]
            assert [row["runId"] for row in reports.json()] == [run_a.id]

            review = await client.post(
                f"/v1/scenarios/{scenario_a.id}/review", json={"action": "approve"},
            )
            assert review.status_code == 200
            assert review.json()["status"] == "active"

            assert (await client.get(f"/v1/scenarios/{scenario_b.id}")).status_code == 404
            assert (await client.get(f"/v1/runs/{run_b.id}")).status_code == 404
            assert (await client.get(f"/v1/reports/{report_b.id}")).status_code == 404
            assert (await client.delete(f"/v1/runs/{run_b.id}")).status_code == 404
            assert (await client.post(
                f"/v1/scenarios/{scenario_b.id}/review", json={"action": "approve"},
            )).status_code == 404
            assert (await client.post("/v1/runs", json={
                "scenarioVersionId": version_b.id,
                "targetBaseUrl": "https://testserver",
            })).status_code == 400
            assert (await client.post("/v1/raw-artifacts", json={
                "tenantId": "tenant-b",
                "contentBase64": "YXV0aG9yaXplZC1ieXRlcw==",
            })).status_code == 403
    finally:
        app.dependency_overrides.pop(current_principal, None)


@pytest.mark.asyncio
async def test_evolution_event_rejects_tenant_mismatch(monkeypatch):
    from llm_user_suite.config import settings

    monkeypatch.setattr(settings, "GRID_EVENT_SECRET", "event-secret")
    async with SessionLocal() as db:
        scenario = Scenario(
            tenant_id="tenant-a", name="case", signature="case", status="active",
        )
        db.add(scenario)
        await db.flush()
        version = ScenarioVersion(
            scenario_id=scenario.id, version=1, status="approved",
            spec={
                "name": "case", "goal": "health",
                "stages": [{
                    "intent": "health",
                    "requestTemplate": {"method": "GET", "path": "/health"},
                }],
            },
        )
        db.add(version)
        await db.flush()
        run = SuiteRun(
            tenant_id="tenant-a", scenario_version_id=version.id,
            status="completed", target_base_url="https://testserver",
        )
        db.add(run)
        await db.commit()

    import hashlib
    import hmac
    import json
    import time

    body = {
        "eventId": "wrong-tenant", "draftId": "draft-1", "runId": run.id,
        "scenarioVersionId": version.id, "tenantId": "tenant-b", "status": "indexed",
    }
    raw = json.dumps(body).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b"event-secret", timestamp.encode() + b"." + raw, hashlib.sha256,
    ).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/integrations/grid-qa/evolution-events", content=raw,
            headers={
                "content-type": "application/json",
                "X-LLM-User-Timestamp": timestamp,
                "X-LLM-User-Signature": signature,
            },
        )

    assert response.status_code == 404
