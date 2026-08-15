from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import metrics
from .config import settings
from .llm import chat_json, configured
from .models import BehaviorEvent, BehaviorSession, Scenario, ScenarioVersion, TestRun
from .privacy import redact
from .schemas import ScenarioSpec, ScenarioStage


def _interesting(sessions: list[BehaviorSession]) -> bool:
    return (
        len(sessions) >= settings.DREAM_MIN_CLUSTER_SIZE
        or any(session.has_dislike for session in sessions)
        or any(session.has_failure for session in sessions)
        or any(session.retry_count > 0 for session in sessions)
        or any(session.has_degradation for session in sessions)
        or any(
            session.min_faithfulness is not None
            and session.min_faithfulness < settings.FAITHFULNESS_GATE
            for session in sessions
        )
    )


def _ngrams(value: str) -> set[str]:
    compact = "".join(str(value or "").lower().split())
    return {compact[index:index + 2] for index in range(max(1, len(compact) - 1))} if compact else set()


def _intent_similarity(left: dict, right: dict) -> float:
    left_text = str(left.get("goal", "")) + "|" + "|".join(str(item.get("intent", "")) for item in left.get("stages", []))
    right_text = str(right.get("goal", "")) + "|" + "|".join(str(item.get("intent", "")) for item in right.get("stages", []))
    a, b = _ngrams(left_text), _ngrams(right_text)
    return len(a & b) / len(a | b) if a or b else 1.0


def _value_shape(value):
    if isinstance(value, dict):
        return {
            str(key): _value_shape(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [_value_shape(value[0])] if value else []
    if value is None:
        return "null"
    return type(value).__name__


def _structural(value: dict) -> dict:
    return {
        "stages": [{
            "method": (item.get("requestTemplate") or {}).get("method"),
            "path": (item.get("requestTemplate") or {}).get("path"),
            "mode": (item.get("requestTemplate") or {}).get("mode", "http"),
            "requestShape": _value_shape(item.get("requestTemplate") or {}),
            "completion": item.get("completion") or {},
        } for item in value.get("stages", [])],
        "cleanup": [{
            "method": item.get("method"),
            "path": item.get("path"),
            "mode": item.get("mode", "http"),
            "requestShape": _value_shape(item),
        } for item in value.get("cleanup", [])],
        "hiddenOracle": value.get("hiddenOracle") or {},
        "safety": value.get("safety") or {},
    }


def _fallback_spec(events: list[BehaviorEvent], cluster_size: int) -> ScenarioSpec:
    stages: list[ScenarioStage] = []
    previous_at = None
    for event in events:
        if event.kind in {"qa.stream.aborted", "qa.websocket.aborted"}:
            interrupted_mode = "websocket" if "websocket" in event.kind else "sse"
            for stage in reversed(stages):
                if stage.requestTemplate.get("mode") != interrupted_mode:
                    continue
                try:
                    observed_tokens = int(event.payload.get("tokenEvents") or 1)
                except (TypeError, ValueError):
                    observed_tokens = 1
                stage.requestTemplate["interruptAfterTokens"] = max(1, min(500, observed_tokens))
                stage.completion = {
                    "statusCode": 101 if interrupted_mode == "websocket" else 200,
                    "streamInterrupted": True,
                }
                break
            continue
        if event.kind in {
            "http.request", "qa.started", "qa.stream.started",
            "qa.websocket.started", "feedback.submitted",
        } and event.path:
            template = {
                "method": event.method or "POST",
                "path": event.path,
                "mode": (
                    "websocket" if "websocket" in event.kind
                    else "sse" if "stream" in event.kind or event.path.endswith("/stream")
                    else "http"
                ),
                "body": event.payload.get("request", event.payload.get("body", {})),
            }
            mode = template["mode"]
            completion = {
                "statusCode": 101 if mode == "websocket" else event.status_code or 200,
            }
            if mode in {"sse", "websocket"}:
                completion["streamCompleted"] = True
            delay_ms = 0
            if previous_at is not None:
                delay_ms = max(0, min(300_000, int((event.occurred_at - previous_at).total_seconds() * 1000)))
            previous_at = event.occurred_at
            stages.append(ScenarioStage(
                intent=event.payload.get("intent") or event.payload.get("query") or f"调用 {event.path}",
                businessHint=event.payload.get("reason", ""),
                apiHint=f"{template['method']} {template['path']}",
                requestTemplate=template,
                completion=completion,
                delayMs=delay_ms,
            ))
    if not stages:
        stages.append(ScenarioStage(
            intent="检查系统健康状态", apiHint="GET /health",
            requestTemplate={"method": "GET", "path": "/health", "mode": "http"},
            completion={"statusCode": 200},
        ))
    query = next((str(event.payload.get("query")) for event in events if event.payload.get("query")), "复现用户 API 行为")
    return ScenarioSpec(
        name=f"用户行为回放：{query[:60]}",
        persona={"source": "observed", "clusterSize": cluster_size},
        goal=query,
        stages=stages[:12],
        hiddenOracle={
            "observedDislike": any(event.kind == "feedback.submitted" and event.payload.get("feedback") == "dislike" for event in events),
            "observedInterrupted": any(event.kind in {
                "qa.stream.aborted", "qa.stream.error",
                "qa.websocket.aborted", "qa.websocket.error",
            } for event in events),
            "observedRetryCount": max(0, sum(
                1 for event in events if event.kind in {
                    "qa.started", "qa.stream.started", "qa.websocket.started",
                }
            ) - 1),
            "expectedFaithfulness": settings.FAITHFULNESS_GATE,
        },
        safety={
            "denyPathPrefixes": ["/api/system/users", "/api/system/config", "/api/document"],
            "maxCalls": settings.MAX_API_CALLS,
        },
    )


async def _enhance_spec(spec: ScenarioSpec, events: list[BehaviorEvent]) -> ScenarioSpec:
    if not configured("dreamer"):
        return spec
    compact = [
        {"kind": event.kind, "method": event.method, "path": event.path, "status": event.status_code, "payload": redact(event.payload)}
        for event in events[:40]
    ]
    prompt = (
        "你是 API 用户行为剧本设计器。根据脱敏事件改写剧本，使其像真实用户，但不得加入事件中不存在的高风险操作。"
        "保留 requestTemplate 作为最终兜底；hiddenOracle 不能泄露到 intent。严格输出与输入相同结构的 JSON。\n\n"
        f"候选剧本：{spec.model_dump_json()}\n事件：{json.dumps(compact, ensure_ascii=False)}"
    )
    try:
        return ScenarioSpec.model_validate(await chat_json("dreamer", [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=2200))
    except Exception:
        return spec


async def dream(db: AsyncSession) -> dict:
    sessions = (await db.execute(
        select(BehaviorSession).where(BehaviorSession.status == "closed").order_by(desc(BehaviorSession.closed_at)).limit(500)
    )).scalars().all()
    clusters: dict[tuple[str, str], list[BehaviorSession]] = defaultdict(list)
    for session in sessions:
        clusters[(session.tenant_id, session.signature or session.id)].append(session)

    created = 0
    scheduled = 0
    for (tenant_id, signature), rows in clusters.items():
        representative = rows[0]
        if not _interesting(rows):
            continue
        events = (await db.execute(
            select(BehaviorEvent).where(BehaviorEvent.session_id == representative.id).order_by(BehaviorEvent.occurred_at)
        )).scalars().all()
        spec = await _enhance_spec(_fallback_spec(events, len(rows)), events)
        spec_dict = spec.model_dump(mode="json")
        prompt_hash = hashlib.sha256(json.dumps(spec_dict, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

        scenario = (await db.execute(select(Scenario).where(
            Scenario.tenant_id == tenant_id,
            Scenario.signature == signature,
        ))).scalar_one_or_none()
        if scenario:
            latest = (await db.execute(
                select(ScenarioVersion).where(ScenarioVersion.scenario_id == scenario.id).order_by(desc(ScenarioVersion.version)).limit(1)
            )).scalar_one_or_none()
            if latest and _structural(latest.spec) == _structural(spec_dict) and _intent_similarity(
                latest.spec, spec_dict,
            ) >= settings.SCENARIO_DRIFT_SIMILARITY:
                merged_ids = list(dict.fromkeys((latest.source_session_ids or []) + [row.id for row in rows[:50]]))[-200:]
                latest.source_session_ids = merged_ids
                source_hash = hashlib.sha256("|".join(sorted(merged_ids)).encode("utf-8")).hexdigest()
                if (
                    latest.status in {"approved", "active"}
                    and settings.TARGET_BASE_URL
                    and latest.last_auto_regression_hash != source_hash
                ):
                    from .replay import validate_target
                    try:
                        target = validate_target(settings.TARGET_BASE_URL, settings.TARGET_ENVIRONMENT)
                    except ValueError:
                        target = ""
                    if target:
                        db.add(TestRun(
                            tenant_id=tenant_id,
                            scenario_version_id=latest.id,
                            environment=settings.TARGET_ENVIRONMENT,
                            target_base_url=target,
                            credential_profile="default",
                            created_by="dreamer-auto-regression",
                        ))
                        latest.last_auto_regression_hash = source_hash
                        scheduled += 1
                continue
            scenario.current_version += 1
            scenario.status = "draft"
        else:
            scenario = Scenario(
                tenant_id=tenant_id, name=spec.name, signature=signature,
                status="draft", current_version=1,
            )
            db.add(scenario)
            await db.flush()
        db.add(ScenarioVersion(
            scenario_id=scenario.id, version=scenario.current_version, status="draft",
            spec=spec_dict, source_session_ids=[row.id for row in rows[:50]], prompt_hash=prompt_hash,
        ))
        created += 1
    await db.commit()
    metrics.DREAM_RUNS.labels("success").inc()
    return {"clusters": len(clusters), "created": created, "scheduled": scheduled}
