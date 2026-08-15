from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import metrics
from .artifacts import raw_capture_ready, store_raw
from .auth import Principal, current_principal, require_admin
from .config import settings
from .db import SessionLocal, get_db
from .metric_store import ingest_otlp_metrics
from .models import (
    BehaviorSession,
    Evaluation,
    EvolutionRetest,
    MetricAggregate,
    RawArtifact,
    Report,
    RunStep,
    Scenario,
    ScenarioVersion,
    TestRun,
)
from .privacy import redact
from .replay import validate_target
from .run_service import cancel_run, dispatch_pending_runs, dispatch_run
from .schemas import (
    BehaviorEventIn,
    EvolutionIndexedEvent,
    RawArtifactCreate,
    RunCreate,
    ScenarioApproval,
)
from .telemetry import decode_otlp_json, ingest_event

router = APIRouter(prefix="/v1")
logger = logging.getLogger(__name__)


def _verify_signed(raw: bytes, timestamp: str, signature: str, secret: str) -> None:
    if not secret:
        raise HTTPException(status_code=503, detail="integration secret is not configured")
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="invalid callback timestamp")
    if abs(int(time.time()) - value) > 300:
        raise HTTPException(status_code=401, detail="callback timestamp is outside replay window")
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + raw, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature or "", expected):
        raise HTTPException(status_code=403, detail="invalid callback signature")


def _scenario_dict(row: Scenario, version: ScenarioVersion | None = None) -> dict:
    data = {
        "id": row.id, "tenantId": row.tenant_id,
        "name": row.name, "signature": row.signature,
        "currentVersion": row.current_version, "status": row.status,
        "createdAt": row.created_at, "updatedAt": row.updated_at,
    }
    if version:
        data["version"] = {
            "id": version.id, "version": version.version, "status": version.status,
            "spec": version.spec, "sourceSessionIds": version.source_session_ids,
            "approvedBy": version.approved_by, "approvedAt": version.approved_at,
        }
    return data


async def _scenario_for_tenant(
    db: AsyncSession, scenario_id: str, tenant_id: str,
) -> Scenario | None:
    return (await db.execute(select(Scenario).where(
        Scenario.id == scenario_id,
        Scenario.tenant_id == tenant_id,
    ))).scalar_one_or_none()


async def _version_for_tenant(
    db: AsyncSession, version_id: str, tenant_id: str,
) -> ScenarioVersion | None:
    return (await db.execute(
        select(ScenarioVersion).join(
            Scenario, Scenario.id == ScenarioVersion.scenario_id,
        ).where(
            ScenarioVersion.id == version_id,
            Scenario.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()


async def _run_for_tenant(
    db: AsyncSession, run_id: str, tenant_id: str,
) -> TestRun | None:
    return (await db.execute(select(TestRun).where(
        TestRun.id == run_id,
        TestRun.tenant_id == tenant_id,
    ))).scalar_one_or_none()


@router.post("/auth/login")
async def control_login(body: dict):
    if settings.AUTH_DISABLED:
        return {"token": "local-dev", "username": "local-admin", "role": "admin"}
    username, password = str(body.get("username", "")), str(body.get("password", ""))
    if not settings.GRID_AUTH_BASE_URL or not username or not password:
        raise HTTPException(status_code=400, detail="Grid-QA auth target or credentials missing")
    auth_target = validate_target(settings.GRID_AUTH_BASE_URL, settings.TARGET_ENVIRONMENT)
    async with httpx.AsyncClient(base_url=auth_target, timeout=15) as client:
        response = await client.post("/api/system/login", json={"username": username, "password": password})
    if response.status_code >= 400:
        raise HTTPException(status_code=401, detail="login failed")
    data = response.json().get("data") or {}
    if data.get("role") not in {"admin", "auditor"}:
        raise HTTPException(status_code=403, detail="admin or auditor role required")
    return data


@router.post("/telemetry/events")
async def telemetry_events(items: BehaviorEventIn | list[BehaviorEventIn], db: AsyncSession = Depends(get_db)):
    rows = items if isinstance(items, list) else [items]
    accepted = 0
    for item in rows[:1000]:
        accepted += 1 if await ingest_event(db, item) else 0
    return {"accepted": accepted, "received": len(rows)}


async def _otlp(signal: str, request: Request, db: AsyncSession) -> Response:
    raw = await request.body()
    if len(raw) > settings.OTLP_MAX_REQUEST_BYTES:
        metrics.EVENTS_DROPPED.labels("otlp_too_large").inc()
        raise HTTPException(status_code=413, detail="OTLP request is too large")
    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding:
        if content_encoding != "gzip":
            metrics.EVENTS_DROPPED.labels("otlp_unsupported_encoding").inc()
            raise HTTPException(status_code=415, detail="unsupported OTLP content encoding")
        try:
            raw = gzip.decompress(raw)
        except (EOFError, OSError) as exc:
            metrics.EVENTS_DROPPED.labels("otlp_invalid_gzip").inc()
            raise HTTPException(status_code=400, detail="invalid gzip OTLP payload") from exc
        if len(raw) > settings.OTLP_MAX_REQUEST_BYTES:
            metrics.EVENTS_DROPPED.labels("otlp_too_large").inc()
            raise HTTPException(status_code=413, detail="decompressed OTLP request is too large")
    content_type = request.headers.get("content-type", "")
    try:
        if "json" in content_type:
            document = json.loads(raw or b"{}")
        else:
            from google.protobuf.json_format import MessageToDict
            if signal == "traces":
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                    ExportTraceServiceRequest,
                )
                message = ExportTraceServiceRequest()
            elif signal == "logs":
                from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
                    ExportLogsServiceRequest,
                )
                message = ExportLogsServiceRequest()
            else:
                from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
                    ExportMetricsServiceRequest,
                )
                message = ExportMetricsServiceRequest()
            message.ParseFromString(raw)
            document = MessageToDict(message, preserving_proto_field_name=False)
        if signal == "metrics":
            await ingest_otlp_metrics(db, document)
            return Response(content=b"", media_type="application/x-protobuf")
        items = decode_otlp_json(signal, document)
        for item in items[:2000]:
            await ingest_event(db, item)
    except Exception as exc:
        metrics.EVENTS_DROPPED.labels(f"invalid_otlp_{signal}").inc()
        logger.warning(
            "invalid OTLP %s payload: %s: %s",
            signal,
            type(exc).__name__,
            str(exc)[:500],
        )
        raise HTTPException(status_code=400, detail=f"invalid OTLP {signal}: {type(exc).__name__}: {exc}")
    return Response(content=b"", media_type="application/x-protobuf")


@router.post("/traces")
async def otlp_traces(request: Request, db: AsyncSession = Depends(get_db)):
    return await _otlp("traces", request, db)


@router.post("/logs")
async def otlp_logs(request: Request, db: AsyncSession = Depends(get_db)):
    return await _otlp("logs", request, db)


@router.post("/metrics")
async def otlp_metrics(request: Request, db: AsyncSession = Depends(get_db)):
    return await _otlp("metrics", request, db)


@router.get("/sessions")
async def sessions(page: int = 1, size: int = 50, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(BehaviorSession).where(
            BehaviorSession.tenant_id == principal.tenant_id,
        ).order_by(desc(BehaviorSession.last_event_at)).offset((page - 1) * size).limit(min(size, 200))
    )).scalars().all()
    return [{
        "id": row.id, "tenantId": row.tenant_id, "userHash": row.user_hash,
        "conversationId": row.conversation_id, "status": row.status,
        "eventCount": row.event_count, "hasDislike": row.has_dislike,
        "hasFailure": row.has_failure, "retryCount": row.retry_count,
        "hasDegradation": row.has_degradation, "minFaithfulness": row.min_faithfulness,
        "startedAt": row.started_at, "lastEventAt": row.last_event_at,
    } for row in rows]


@router.get("/scenarios")
async def scenarios(status: str = "", principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    stmt = select(Scenario).where(Scenario.tenant_id == principal.tenant_id)
    if status:
        stmt = stmt.where(Scenario.status == status)
    rows = (await db.execute(stmt.order_by(desc(Scenario.updated_at)))).scalars().all()
    return [_scenario_dict(row) for row in rows]


@router.get("/scenarios/{scenario_id}")
async def scenario_detail(scenario_id: str, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    scenario = await _scenario_for_tenant(db, scenario_id, principal.tenant_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="scenario not found")
    versions = (await db.execute(
        select(ScenarioVersion).where(ScenarioVersion.scenario_id == scenario_id).order_by(desc(ScenarioVersion.version))
    )).scalars().all()
    return {**_scenario_dict(scenario), "versions": [_scenario_dict(scenario, version)["version"] for version in versions]}


@router.post("/scenarios/{scenario_id}/review")
async def review_scenario(scenario_id: str, body: ScenarioApproval, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    require_admin(principal)
    scenario = await _scenario_for_tenant(db, scenario_id, principal.tenant_id)
    if not scenario:
        raise HTTPException(status_code=404, detail="scenario not found")
    version = (await db.execute(
        select(ScenarioVersion).where(
            ScenarioVersion.scenario_id == scenario_id,
            ScenarioVersion.version == scenario.current_version,
        )
    )).scalar_one()
    if body.action == "approve":
        version.status = "approved"
        version.approved_by = principal.username
        version.approved_at = datetime.now()
        scenario.status = "active"
    else:
        version.status = "retired"
        scenario.status = "retired"
    await db.commit()
    await db.refresh(scenario)
    await db.refresh(version)
    return _scenario_dict(scenario, version)


@router.post("/runs")
async def create_run(body: RunCreate, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    require_admin(principal)
    version = await _version_for_tenant(db, body.scenarioVersionId, principal.tenant_id)
    if not version or version.status not in {"approved", "active"}:
        raise HTTPException(status_code=400, detail="scenario version is not approved")
    if body.baselineRunId:
        baseline = await _run_for_tenant(db, body.baselineRunId, principal.tenant_id)
        if not baseline or baseline.scenario_version_id != version.id:
            raise HTTPException(status_code=400, detail="baseline run does not match scenario version")
    target = validate_target(body.targetBaseUrl or settings.TARGET_BASE_URL, body.environment)
    active = (await db.execute(select(func.count()).select_from(TestRun).where(TestRun.status.in_(["queued", "running", "evaluating"])))).scalar() or 0
    if active >= settings.RUN_CONCURRENCY:
        raise HTTPException(status_code=429, detail="run concurrency limit reached")
    row = TestRun(
        tenant_id=principal.tenant_id,
        scenario_version_id=version.id, environment=body.environment,
        target_base_url=target, credential_profile=body.credentialProfile,
        baseline_run_id=body.baselineRunId, created_by=principal.username,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    try:
        job_name = await dispatch_run(row.id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"runner launch failed: {type(exc).__name__}")
    return {"id": row.id, "status": row.status, "jobName": job_name}


@router.get("/runs")
async def runs(page: int = 1, size: int = 50, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(TestRun).where(
            TestRun.tenant_id == principal.tenant_id,
        ).order_by(desc(TestRun.created_at)).offset((page - 1) * size).limit(min(size, 200))
    )).scalars().all()
    return [{
        "id": row.id, "tenantId": row.tenant_id,
        "scenarioVersionId": row.scenario_version_id,
        "environment": row.environment, "status": row.status,
        "score": row.score, "verdict": row.verdict, "rootCause": row.root_cause,
        "createdAt": row.created_at, "finishedAt": row.finished_at,
    } for row in rows]


@router.get("/runs/{run_id}")
async def run_detail(run_id: str, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    row = await _run_for_tenant(db, run_id, principal.tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="run not found")
    steps = (await db.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.step_index, RunStep.created_at))).scalars().all()
    return {
        "id": row.id, "tenantId": row.tenant_id,
        "status": row.status, "score": row.score, "verdict": row.verdict,
        "rootCause": row.root_cause, "error": row.error, "result": row.result,
        "jobName": row.runner_job_name, "dispatchState": row.dispatch_state,
        "cancelRequested": row.cancel_requested,
        "steps": [{
            "index": step.step_index, "intent": step.intent, "hintLevel": step.hint_level,
            "request": step.request, "response": step.response, "success": step.success,
            "latencyMs": step.latency_ms, "traceId": step.trace_id,
        } for step in steps],
    }


@router.delete("/runs/{run_id}")
async def stop_run(run_id: str, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    require_admin(principal)
    if not await _run_for_tenant(db, run_id, principal.tenant_id):
        raise HTTPException(status_code=404, detail="run not found")
    changed = await cancel_run(run_id)
    return {"id": run_id, "status": "cancelled" if changed else "unchanged"}


@router.get("/evaluations")
async def evaluations(runId: str = "", principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    stmt = select(Evaluation).join(TestRun, TestRun.id == Evaluation.run_id).where(
        TestRun.tenant_id == principal.tenant_id,
    )
    if runId:
        stmt = stmt.where(Evaluation.run_id == runId)
    rows = (await db.execute(stmt.order_by(desc(Evaluation.created_at)).limit(500))).scalars().all()
    return [{
        "id": row.id, "runId": row.run_id, "dimension": row.dimension,
        "score": row.score, "verdict": row.verdict, "hardFail": row.hard_fail,
        "reason": row.reason, "evidence": row.evidence,
    } for row in rows]


@router.post("/integrations/grid-qa/evolution-events")
async def evolution_event(
    request: Request,
    x_llm_user_timestamp: str = Header(default="", alias="X-LLM-User-Timestamp"),
    x_llm_user_signature: str = Header(default="", alias="X-LLM-User-Signature"),
    db: AsyncSession = Depends(get_db),
):
    raw = await request.body()
    _verify_signed(raw, x_llm_user_timestamp, x_llm_user_signature, settings.GRID_EVENT_SECRET)
    try:
        body = EvolutionIndexedEvent.model_validate(json.loads(raw))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid evolution event: {type(exc).__name__}")
    existing = (await db.execute(select(EvolutionRetest).where(
        EvolutionRetest.source_event_id == body.eventId,
    ))).scalar_one_or_none()
    if existing:
        if existing.tenant_id != body.tenantId:
            raise HTTPException(status_code=409, detail="event id already belongs to another tenant")
        return {"id": existing.id, "status": existing.status, "runId": existing.rerun_id}
    baseline = await _run_for_tenant(db, body.runId, body.tenantId)
    version = await _version_for_tenant(db, body.scenarioVersionId, body.tenantId)
    if not baseline or baseline.scenario_version_id != body.scenarioVersionId:
        raise HTTPException(status_code=404, detail="baseline run/scenario version not found")
    if not version or version.status not in {"approved", "active"}:
        raise HTTPException(status_code=409, detail="scenario version is no longer active")
    rerun = TestRun(
        tenant_id=baseline.tenant_id,
        scenario_version_id=baseline.scenario_version_id,
        environment=baseline.environment, target_base_url=baseline.target_base_url,
        credential_profile=baseline.credential_profile, baseline_run_id=baseline.id,
        created_by="evolution-retest",
    )
    db.add(rerun)
    await db.flush()
    link = EvolutionRetest(
        source_event_id=body.eventId, tenant_id=body.tenantId, draft_id=body.draftId,
        scenario_version_id=body.scenarioVersionId, baseline_run_id=baseline.id,
        rerun_id=rerun.id, status="queued", before_score=baseline.score,
    )
    db.add(link)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(select(EvolutionRetest).where(
            EvolutionRetest.source_event_id == body.eventId,
        ))).scalar_one()
        return {"id": existing.id, "status": existing.status, "runId": existing.rerun_id}
    try:
        await dispatch_pending_runs()
    except Exception as exc:
        async with SessionLocal() as retry_db:
            stored = await retry_db.get(EvolutionRetest, link.id)
            if stored:
                stored.status = "failed"
                stored.error = f"{type(exc).__name__}: {exc}"[:8000]
                stored.finished_at = datetime.now()
                await retry_db.commit()
        raise HTTPException(status_code=503, detail="automatic retest scheduling failed")
    await db.refresh(rerun)
    return {"id": link.id, "status": rerun.dispatch_state, "runId": rerun.id}


@router.get("/evaluations/retests")
async def evolution_retests(principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(EvolutionRetest).where(
        EvolutionRetest.tenant_id == principal.tenant_id,
    ).order_by(desc(EvolutionRetest.created_at)).limit(200))).scalars().all()
    return [{
        "id": row.id, "tenantId": row.tenant_id,
        "draftId": row.draft_id, "baselineRunId": row.baseline_run_id,
        "runId": row.rerun_id, "status": row.status, "beforeScore": row.before_score,
        "afterScore": row.after_score, "lift": row.lift, "error": row.error,
    } for row in rows]


@router.get("/telemetry/metrics")
async def metric_aggregates(
    name: str = "", limit: int = 500,
    principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db),
):
    stmt = select(MetricAggregate)
    if name:
        stmt = stmt.where(MetricAggregate.metric_name == name)
    rows = (await db.execute(stmt.order_by(desc(MetricAggregate.minute)).limit(min(limit, 2000)))).scalars().all()
    return [{
        "name": row.metric_name, "minute": row.minute, "labels": row.labels,
        "samples": row.sample_count, "sum": row.value_sum, "min": row.value_min,
        "max": row.value_max, "last": row.value_last, "source": row.source,
    } for row in rows]


@router.post("/raw-artifacts")
async def create_raw_artifact(
    body: RawArtifactCreate, principal: Principal = Depends(current_principal),
    db: AsyncSession = Depends(get_db),
):
    require_admin(principal)
    if body.tenantId not in {"default", principal.tenant_id}:
        raise HTTPException(status_code=403, detail="raw artifact tenant does not match principal")
    if not raw_capture_ready():
        metrics.RAW_ARTIFACTS.labels("disabled").inc()
        raise HTTPException(status_code=409, detail="authorized raw capture is disabled or encryption key is invalid")
    try:
        data = base64.b64decode(body.contentBase64, validate=True)
    except Exception:
        raise HTTPException(status_code=422, detail="contentBase64 is invalid")
    if not data or len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="raw artifact must be between 1 byte and 10 MiB")
    if body.sessionId:
        session = (await db.execute(select(BehaviorSession).where(
            BehaviorSession.id == body.sessionId,
            BehaviorSession.tenant_id == principal.tenant_id,
        ))).scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="behavior session not found")
    artifact_id = uuid.uuid4().hex
    try:
        uri, digest, envelope = await asyncio.to_thread(store_raw, data, artifact_id=artifact_id)
    except Exception as exc:
        metrics.RAW_ARTIFACTS.labels("failed").inc()
        raise HTTPException(status_code=500, detail=f"encrypted artifact storage failed: {type(exc).__name__}")
    row = RawArtifact(
        id=artifact_id, tenant_id=principal.tenant_id, session_id=body.sessionId,
        object_uri=uri, sha256=digest, content_type=body.contentType,
        envelope=envelope, metadata_json=redact(body.metadata), created_by=principal.username,
    )
    db.add(row)
    await db.commit()
    metrics.RAW_ARTIFACTS.labels("stored").inc()
    return {"id": row.id, "sha256": row.sha256, "contentType": row.content_type, "encrypted": True}


@router.get("/reports")
async def reports(principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(
        select(Report).join(TestRun, TestRun.id == Report.run_id).where(
            TestRun.tenant_id == principal.tenant_id,
        ).order_by(desc(Report.created_at)).limit(200)
    )).scalars().all()
    return [{
        "id": row.id, "runId": row.run_id, "verdict": row.verdict,
        "summary": row.summary, "callbackStatus": row.callback_status, "createdAt": row.created_at,
    } for row in rows]


@router.get("/reports/{report_id}")
async def report_detail(report_id: str, principal: Principal = Depends(current_principal), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(
        select(Report).join(TestRun, TestRun.id == Report.run_id).where(
            Report.id == report_id,
            TestRun.tenant_id == principal.tenant_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="report not found")
    return {
        "id": row.id, "runId": row.run_id, "verdict": row.verdict,
        "summary": row.summary, "content": row.content,
        "jsonPath": row.json_path, "markdownPath": row.markdown_path, "htmlPath": row.html_path,
        "callbackStatus": row.callback_status,
    }
