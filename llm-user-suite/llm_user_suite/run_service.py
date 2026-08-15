from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from .config import settings
from .db import SessionLocal
from .models import EvolutionRetest, TestRun
from .task_queue import enqueue


async def dispatch_run(run_id: str) -> str:
    """Dispatch a persisted run and roll its state back cleanly when launch fails."""
    async with SessionLocal() as db:
        claimed = (await db.execute(update(TestRun).where(
            TestRun.id == run_id,
            TestRun.status == "queued",
            TestRun.dispatch_state == "pending",
        ).values(dispatch_state="dispatching").returning(TestRun.id))).scalar_one_or_none()
        await db.commit()
        if not claimed:
            row = await db.get(TestRun, run_id)
            return row.runner_job_name if row else ""
    if settings.RUNNER_BACKEND == "kubernetes":
        try:
            from .k8s_runner import launch
            job_name = await launch(run_id)
        except Exception as exc:
            async with SessionLocal() as db:
                row = await db.get(TestRun, run_id)
                if row:
                    row.status = "failed"
                    row.error = f"runner launch failed: {type(exc).__name__}: {exc}"[:8000]
                    row.finished_at = datetime.now()
                    row.dispatch_state = "failed"
                    retest = (await db.execute(select(EvolutionRetest).where(
                        EvolutionRetest.rerun_id == row.id,
                    ))).scalar_one_or_none()
                    if retest:
                        retest.status = "failed"
                        retest.error = row.error
                        retest.finished_at = row.finished_at
                    await db.commit()
            raise
        async with SessionLocal() as db:
            row = await db.get(TestRun, run_id)
            if row:
                row.runner_job_name = job_name
                row.dispatch_state = "dispatched"
                await db.commit()
        return job_name
    await enqueue("run", {"run_id": run_id}, idempotency_key=f"run:{run_id}")
    async with SessionLocal() as db:
        row = await db.get(TestRun, run_id)
        if row:
            row.dispatch_state = "dispatched"
            await db.commit()
    return ""


async def dispatch_pending_runs() -> int:
    async with SessionLocal() as db:
        await db.execute(update(TestRun).where(
            TestRun.status == "queued", TestRun.dispatch_state == "dispatching",
            TestRun.runner_job_name == "", TestRun.created_at < datetime.now() - timedelta(minutes=2),
        ).values(dispatch_state="pending"))
        await db.commit()
        active = (await db.execute(select(func.count()).select_from(TestRun).where(
            TestRun.dispatch_state == "dispatched",
            TestRun.status.in_(["queued", "running", "evaluating"]),
        ))).scalar() or 0
        slots = max(0, settings.RUN_CONCURRENCY - int(active))
        if not slots:
            return 0
        ids = (await db.execute(select(TestRun.id).where(
            TestRun.status == "queued", TestRun.dispatch_state == "pending",
        ).order_by(TestRun.created_at).limit(slots))).scalars().all()
    dispatched = 0
    for run_id in ids:
        try:
            await dispatch_run(run_id)
            dispatched += 1
        except Exception:
            pass
    return dispatched


async def cancel_run(run_id: str) -> bool:
    async with SessionLocal() as db:
        row = await db.get(TestRun, run_id)
        if not row or row.status in {"completed", "failed", "cancelled"}:
            return False
        row.cancel_requested = True
        row.status = "cancelled"
        row.error = "cancelled by operator"
        row.finished_at = datetime.now()
        row.dispatch_state = "cancelled"
        job_name = row.runner_job_name
        retest = (await db.execute(select(EvolutionRetest).where(
            EvolutionRetest.rerun_id == row.id,
        ))).scalar_one_or_none()
        if retest:
            retest.status = "cancelled"
            retest.error = row.error
            retest.finished_at = row.finished_at
        await db.commit()
    if settings.RUNNER_BACKEND == "kubernetes" and job_name:
        from .k8s_runner import cancel
        try:
            await cancel(job_name)
        except Exception:
            # The durable cancellation flag prevents the runner from starting new work even
            # when the Kubernetes delete call is temporarily unavailable.
            pass
    return True
