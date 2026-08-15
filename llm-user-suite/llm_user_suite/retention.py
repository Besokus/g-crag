from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, or_, select

from .artifacts import remove
from .config import settings
from .db import SessionLocal
from .models import (
    BehaviorEvent,
    BehaviorSession,
    Evaluation,
    EvolutionRetest,
    MetricAggregate,
    RawArtifact,
    Report,
    RunStep,
    TestRun,
)
from .telemetry import utcnow


async def purge_expired() -> dict:
    event_cutoff = utcnow() - timedelta(days=settings.EVENT_RETENTION_DAYS)
    report_cutoff = utcnow() - timedelta(days=settings.REPORT_RETENTION_DAYS)
    async with SessionLocal() as db:
        old_runs = (await db.execute(select(TestRun).where(
            TestRun.created_at < report_cutoff,
            TestRun.status.in_(["completed", "failed", "cancelled"]),
        ))).scalars().all()
        old_run_ids = [run.id for run in old_runs]
        report_filter = Report.created_at < report_cutoff
        if old_run_ids:
            report_filter = or_(report_filter, Report.run_id.in_(old_run_ids))
        reports = (await db.execute(select(Report).where(report_filter))).scalars().all()
        for report in reports:
            try:
                remove(report.json_path)
                remove(report.markdown_path)
                remove(report.html_path)
            except Exception:
                pass
        raw_artifacts = (await db.execute(select(RawArtifact).where(RawArtifact.created_at < event_cutoff))).scalars().all()
        for artifact in raw_artifacts:
            try:
                remove(artifact.object_uri)
            except Exception:
                pass
        event_result = await db.execute(delete(BehaviorEvent).where(BehaviorEvent.occurred_at < event_cutoff))
        metric_result = await db.execute(delete(MetricAggregate).where(MetricAggregate.minute < event_cutoff))
        raw_result = await db.execute(delete(RawArtifact).where(RawArtifact.created_at < event_cutoff))
        session_result = await db.execute(delete(BehaviorSession).where(
            BehaviorSession.status == "closed", BehaviorSession.closed_at < event_cutoff
        ))
        evaluation_result = step_result = run_result = None
        if old_run_ids:
            evaluation_result = await db.execute(delete(Evaluation).where(Evaluation.run_id.in_(old_run_ids)))
            step_result = await db.execute(delete(RunStep).where(RunStep.run_id.in_(old_run_ids)))
        report_result = await db.execute(delete(Report).where(report_filter))
        retest_filter = EvolutionRetest.created_at < report_cutoff
        if old_run_ids:
            retest_filter = or_(
                retest_filter,
                EvolutionRetest.baseline_run_id.in_(old_run_ids),
                EvolutionRetest.rerun_id.in_(old_run_ids),
            )
        await db.execute(delete(EvolutionRetest).where(retest_filter))
        if old_run_ids:
            run_result = await db.execute(delete(TestRun).where(TestRun.id.in_(old_run_ids)))
        await db.commit()
    return {
        "events": event_result.rowcount or 0, "metrics": metric_result.rowcount or 0,
        "rawArtifacts": raw_result.rowcount or 0, "sessions": session_result.rowcount or 0,
        "reports": report_result.rowcount or 0,
        "evaluations": evaluation_result.rowcount if evaluation_result is not None else 0,
        "steps": step_result.rowcount if step_result is not None else 0,
        "runs": run_result.rowcount if run_result is not None else 0,
    }
