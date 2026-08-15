from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from . import metrics
from .config import settings
from .db import SessionLocal
from .models import EvolutionRetest, TestRun

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def _namespace() -> str:
    return settings.RUNNER_NAMESPACE or (_SA / "namespace").read_text().strip()


def _client_args() -> tuple[str, dict, str]:
    namespace = _namespace()
    token = (_SA / "token").read_text().strip()
    api = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    base = f"https://{api}:{port}/apis/batch/v1/namespaces/{namespace}/jobs"
    return base, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, namespace


async def launch(run_id: str) -> str:
    _, _, namespace = _client_args()
    if not settings.RUNNER_IMAGE:
        raise RuntimeError("LLM_USER_RUNNER_IMAGE is required for kubernetes runner")
    name = f"llm-user-run-{run_id[:12]}"
    manifest = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/name": "llm-user-runner", "llm-user/run-id": run_id}},
        "spec": {
            "backoffLimit": 0, "activeDeadlineSeconds": settings.MAX_RUN_SECONDS + 60,
            "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": "llm-user-runner", "llm-user/run-id": run_id}},
                "spec": {
                    "restartPolicy": "Never", "serviceAccountName": settings.RUNNER_SERVICE_ACCOUNT,
                    "containers": [{
                        "name": "runner", "image": settings.RUNNER_IMAGE, "imagePullPolicy": "IfNotPresent",
                        "command": ["python", "-m", "llm_user_suite.runner", "--run-id", run_id],
                        "envFrom": [
                            {"configMapRef": {"name": settings.RUNNER_ENV_CONFIGMAP}},
                            {"secretRef": {"name": settings.RUNNER_ENV_SECRET}},
                        ],
                        "resources": {"requests": {"cpu": "250m", "memory": "256Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
                    }],
                },
            },
        },
    }
    url, headers, _ = _client_args()
    async with httpx.AsyncClient(verify=str(_SA / "ca.crt"), timeout=20) as client:
        response = await client.post(url, content=json.dumps(manifest), headers=headers)
        response.raise_for_status()
    metrics.RUNNER_JOBS.labels("created").inc()
    return name


async def cancel(job_name: str) -> None:
    base, headers, _ = _client_args()
    async with httpx.AsyncClient(verify=str(_SA / "ca.crt"), timeout=20) as client:
        response = await client.delete(
            f"{base}/{job_name}", headers=headers,
            params={"propagationPolicy": "Background"},
        )
        if response.status_code not in {200, 202, 404}:
            response.raise_for_status()
    metrics.RUNNER_JOBS.labels("cancelled").inc()


async def _job(job_name: str) -> dict | None:
    base, headers, _ = _client_args()
    async with httpx.AsyncClient(verify=str(_SA / "ca.crt"), timeout=20) as client:
        response = await client.get(f"{base}/{job_name}", headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


async def reconcile() -> int:
    """Reconcile orphaned/failed Job state without overwriting completed run results."""
    from datetime import datetime

    from sqlalchemy import select

    async with SessionLocal() as db:
        rows = (await db.execute(select(TestRun).where(
            TestRun.runner_job_name != "",
            TestRun.status.in_(["queued", "running", "evaluating"]),
        ))).scalars().all()
    changed = 0
    for current in rows:
        document = await _job(current.runner_job_name)
        status = (document or {}).get("status") or {}
        failed = int(status.get("failed") or 0) > 0
        active = int(status.get("active") or 0) > 0
        succeeded = int(status.get("succeeded") or 0) > 0
        async with SessionLocal() as db:
            row = await db.get(TestRun, current.id)
            if not row or row.status not in {"queued", "running", "evaluating"}:
                continue
            if document is None:
                row.status = "failed"
                row.error = "runner job disappeared before completion"
                row.finished_at = datetime.now()
                changed += 1
                metrics.RUNNER_JOBS.labels("missing").inc()
            elif failed:
                row.status = "failed"
                conditions = status.get("conditions") or []
                row.error = "runner job failed: " + "; ".join(
                    str(item.get("message") or item.get("reason") or "failed") for item in conditions
                )[:7900]
                row.finished_at = datetime.now()
                changed += 1
                metrics.RUNNER_JOBS.labels("failed").inc()
            elif succeeded:
                row.status = "failed"
                row.error = "runner job completed without persisting a terminal run result"
                row.finished_at = datetime.now()
                changed += 1
                metrics.RUNNER_JOBS.labels("orphaned_success").inc()
            elif active and row.status == "queued":
                row.status = "running"
                row.started_at = row.started_at or datetime.now()
                changed += 1
                metrics.RUNNER_JOBS.labels("running").inc()
            if row.status == "failed":
                retest = (await db.execute(select(EvolutionRetest).where(
                    EvolutionRetest.rerun_id == row.id,
                ))).scalar_one_or_none()
                if retest:
                    retest.status = "failed"
                    retest.error = row.error
                    retest.finished_at = row.finished_at
            await db.commit()
    return changed
