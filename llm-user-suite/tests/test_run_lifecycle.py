import asyncio
import json

import pytest
from llm_user_suite.config import settings
from llm_user_suite.db import SessionLocal
from llm_user_suite.models import TestRun as SuiteRun
from llm_user_suite.run_service import dispatch_run
from llm_user_suite.task_queue import claim, enqueue


@pytest.mark.asyncio
async def test_kubernetes_runner_inherits_configmap_and_secret(monkeypatch):
    from llm_user_suite import k8s_runner

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, content, headers):
            captured.update(url=url, content=content, headers=headers)
            return Response()

    monkeypatch.setattr(settings, "RUNNER_IMAGE", "registry.example/runner@sha256:abc")
    monkeypatch.setattr(settings, "RUNNER_ENV_CONFIGMAP", "suite-config")
    monkeypatch.setattr(settings, "RUNNER_ENV_SECRET", "suite-secret")
    monkeypatch.setattr(k8s_runner, "_client_args", lambda: (
        "https://kubernetes.example/apis/batch/v1/namespaces/test/jobs",
        {"Authorization": "Bearer test"},
        "test",
    ))
    monkeypatch.setattr(k8s_runner.httpx, "AsyncClient", Client)

    await k8s_runner.launch("1234567890abcdef")

    manifest = json.loads(captured["content"])
    env_from = manifest["spec"]["template"]["spec"]["containers"][0]["envFrom"]
    assert env_from == [
        {"configMapRef": {"name": "suite-config"}},
        {"secretRef": {"name": "suite-secret"}},
    ]


@pytest.mark.asyncio
async def test_task_enqueue_and_claim_are_idempotent():
    first = await enqueue("dream", {}, idempotency_key="bucket-1")
    second = await enqueue("dream", {}, idempotency_key="bucket-1")
    assert first.id == second.id
    claimed = await claim()
    assert claimed and claimed.id == first.id and claimed.status == "running"


@pytest.mark.asyncio
async def test_kubernetes_launch_failure_marks_run_failed(monkeypatch):
    async with SessionLocal() as db:
        row = SuiteRun(scenario_version_id="sv-1", target_base_url="https://testserver")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        run_id = row.id

    async def fail(_run_id):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr(settings, "RUNNER_BACKEND", "kubernetes")
    monkeypatch.setattr("llm_user_suite.k8s_runner.launch", fail)
    with pytest.raises(RuntimeError):
        await dispatch_run(run_id)
    async with SessionLocal() as db:
        stored = await db.get(SuiteRun, run_id)
        assert stored.status == "failed"
        assert stored.dispatch_state == "failed"
        assert "runner launch failed" in stored.error


@pytest.mark.asyncio
async def test_concurrent_dispatch_claims_launch_only_once(monkeypatch):
    async with SessionLocal() as db:
        row = SuiteRun(scenario_version_id="sv-1", target_base_url="https://testserver")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        run_id = row.id

    launches = 0

    async def launch(_run_id):
        nonlocal launches
        launches += 1
        await asyncio.sleep(0.02)
        return "runner-job"

    monkeypatch.setattr(settings, "RUNNER_BACKEND", "kubernetes")
    monkeypatch.setattr("llm_user_suite.k8s_runner.launch", launch)
    await asyncio.gather(dispatch_run(run_id), dispatch_run(run_id))

    assert launches == 1
    async with SessionLocal() as db:
        stored = await db.get(SuiteRun, run_id)
        assert stored.dispatch_state == "dispatched"
        assert stored.runner_job_name == "runner-job"
