"""Durable routing of LLM-as-a-User evaluation results into Grid-QA improvement loops."""
from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

from app.config import settings
from app.core.obs import degraded
from app.events import register_event_handler
from app.services import quality_event_bus

EVENT_TYPE = "llm_user.eval.completed"
EVOLUTION_INDEXED_EVENT_TYPE = "knowledge_evolution.draft.indexed"
KNOWLEDGE_CAUSES = {"knowledge_gap", "citation_gap", "no_result"}


async def handle_evaluation(payload: dict, context) -> dict:
    root_cause = str(payload.get("rootCause", "none"))
    tenant = context.tenant_id or payload.get("tenantId") or "default"
    run_id = str(payload.get("runId", ""))
    query = str(payload.get("query", ""))[:8000]
    answer = str(payload.get("answer", ""))[:16000]

    await quality_event_bus.emit(
        "llm_user_eval", "replay_failed" if root_cause != "none" else "replay_passed",
        {
            "runId": run_id, "scenarioVersionId": payload.get("scenarioVersionId", ""),
            "rootCause": root_cause, "scores": payload.get("scores", {}),
            "query": query[:1000], "reason": str(payload.get("reason", ""))[:1000],
            "traceIds": payload.get("traceIds", [])[:20],
        },
        tenant=tenant,
    )

    if root_cause in KNOWLEDGE_CAUSES and query:
        from app.services.evidence_gap_service import collect
        gap_id = await collect(
            query, answer, "refused" if root_cause == "no_result" else "medium",
            "incorrect", "llm_user_replay", "llm_user_replay", tenant,
        )
        try:
            from app.services.knowledge_evolution_service import enqueue_evolution_scan
            await enqueue_evolution_scan(tenant, since_hours=24 * 30)
        except Exception as exc:
            degraded("llm_user_evolution_enqueue", exc, f"run={run_id}")
        return {"routed": "evidence_gap", "gapId": gap_id or ""}

    if root_cause == "retrieval":
        try:
            from app.tasks.registry import enqueue_task
            task_id = await enqueue_task(
                "retrieval_tune_run", {"source": "llm_user", "run_id": run_id},
                queue="default", tenant_id=tenant,
                idempotency_key=f"llm-user-retrieval:{run_id}",
            )
            return {"routed": "retrieval_tune", "taskId": task_id}
        except Exception as exc:
            degraded("llm_user_retrieval_tune", exc, f"run={run_id}")

    # generation/stability/test_data remain auditable DomainEvents and quality events;
    # they intentionally do not mutate knowledge.
    return {"routed": "report_only", "rootCause": root_cause}


async def forward_evolution_indexed(payload: dict, context) -> dict:
    """Notify the suite only after an approved draft is actually indexed in a test KB."""
    if not settings.LLM_USER_SUITE_EVENT_URL or not settings.LLM_USER_SUITE_EVENT_SECRET:
        return {"status": "skipped"}
    body = {
        "eventId": context.event_id,
        "draftId": str(payload.get("draftId", "")),
        "runId": str(payload.get("runId", "")),
        "scenarioVersionId": str(payload.get("scenarioVersionId", "")),
        "tenantId": context.tenant_id or payload.get("tenantId") or "default",
        "status": "indexed",
        "indexedAt": payload.get("indexedAt"),
    }
    if not body["draftId"] or not body["runId"] or not body["scenarioVersionId"]:
        return {"status": "ignored", "reason": "missing correlation"}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.LLM_USER_SUITE_EVENT_SECRET.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            settings.LLM_USER_SUITE_EVENT_URL, content=raw,
            headers={
                "content-type": "application/json",
                "X-LLM-User-Timestamp": timestamp,
                "X-LLM-User-Signature": signature,
            },
        )
        response.raise_for_status()
    return {"status": "sent", "suiteRunId": response.json().get("runId", "")}


def register_handlers() -> None:
    try:
        register_event_handler(
            EVENT_TYPE, "llm_user.evaluation_router", handle_evaluation, max_attempts=5,
        )
    except ValueError:
        # Import may happen more than once in tests; same handler registration is idempotent.
        pass
    try:
        register_event_handler(
            EVOLUTION_INDEXED_EVENT_TYPE,
            "llm_user.evolution_retest_notifier",
            forward_evolution_indexed,
            max_attempts=8,
        )
    except ValueError:
        pass


register_handlers()
