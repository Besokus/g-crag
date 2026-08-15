from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean

import httpx
from sqlalchemy import select

try:
    from llm_eval_core import (
        input_digest,
        judge_completeness,
        judge_context_relevance,
        judge_hallucination,
    )
except ImportError:  # local monorepo execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
    from llm_eval_core import (
        input_digest,
        judge_completeness,
        judge_context_relevance,
        judge_hallucination,
    )

from . import metrics
from .config import settings
from .db import SessionLocal
from .llm import chat, chat_json, used_config
from .models import (
    Evaluation,
    EvolutionRetest,
    Report,
    RunStep,
    ScenarioVersion,
    TestRun,
)
from .privacy import redact
from .reports import write_report
from .schemas import EvaluationCallback, ScenarioSpec


async def _judge_chat(messages, temperature=0, max_tokens=800):
    return await chat("judge", messages, temperature=temperature, max_tokens=max_tokens)


def _payload_body(response: dict) -> dict:
    body = response.get("body")
    if isinstance(body, dict):
        return body.get("data") if isinstance(body.get("data"), dict) else body
    events = response.get("events") or []
    done = next((item for item in reversed(events) if item.get("type") in {"done", "error"}), {})
    meta = next((item for item in events if item.get("type") == "meta"), {})
    tokens = "".join(str(item.get("content", "")) for item in events if item.get("type") == "token")[:16000]
    payload = {**meta, **done}
    if not payload.get("answer") and not payload.get("content") and not payload.get("annotatedAnswer") and tokens:
        payload["answer"] = tokens
    return payload


def _extract_case(steps: list[RunStep]) -> tuple[str, str, list[str], str]:
    query = answer = reason = ""
    sources: list[str] = []
    for step in steps:
        body = step.request.get("body") if isinstance(step.request, dict) else {}
        if isinstance(body, dict):
            query = query or str(body.get("query", ""))
            reason = reason or str(body.get("reason", ""))
        payload = _payload_body(step.response or {})
        answer = answer or str(payload.get("answer", payload.get("content", "")))
        for source in payload.get("sources", []) or []:
            if isinstance(source, dict):
                text = source.get("chunk") or source.get("text") or source.get("docName")
            else:
                text = str(source)
            if text:
                sources.append(str(text))
    return query, answer, sources, reason


def _dimension(name: str, score: float | None, reason: str, *, hard_fail: bool = False, evidence: dict | None = None) -> dict:
    if score is None:
        verdict = "inconclusive"
    elif hard_fail or score < 0.60:
        verdict = "failed"
    elif score < 0.85:
        verdict = "warning"
    else:
        verdict = "passed"
    return {"dimension": name, "score": score, "verdict": verdict, "reason": reason, "hardFail": hard_fail, "evidence": evidence or {}}


async def _feedback_alignment(query: str, answer: str, reason: str) -> tuple[float, str]:
    if not reason:
        return 0.8, "原行为未提供明确反馈理由"
    try:
        data = await chat_json("judge", [{"role": "user", "content": (
            "判断新答案是否解决用户差评理由。输出 JSON："
            '{"score":0到1,"reason":"说明"}\n'
            f"问题：{query}\n差评理由：{reason}\n新答案：{answer[:4000]}"
        )}], temperature=0, max_tokens=300)
        return max(0.0, min(1.0, float(data.get("score", 0.5)))), str(data.get("reason", ""))
    except Exception:
        return 0.5, "反馈对齐 Judge 不可用"


def _root_cause(
    dimensions: dict[str, dict],
    sources: list[str],
    run_error: str,
    *,
    observed_interruption: bool = False,
) -> str:
    lowered_error = (run_error or "").lower()
    if any(token in lowered_error for token in (
        "login", "credential", "fixture", "permission", "forbidden", "safety",
        "401", "403", "openapi",
    )):
        return "test_data"
    if run_error or dimensions["outcome"]["hardFail"] and "5" in dimensions["outcome"]["reason"]:
        return "stability"
    # A scenario dreamed from a real user-aborted stream is itself stability evidence.
    # Keep that attribution even when the replay intentionally reproduces the abort and
    # therefore satisfies the deterministic stage assertion.
    if observed_interruption:
        return "stability"
    if not sources:
        return "no_result"
    if (dimensions["relevance"]["score"] or 0) < 0.6:
        return "retrieval"
    if (dimensions["faithfulness"]["score"] or 0) < settings.FAITHFULNESS_GATE:
        return "citation_gap"
    if (dimensions["completeness"]["score"] or 0) < 0.6:
        return "knowledge_gap"
    if (dimensions["feedback_alignment"]["score"] or 0) < 0.6:
        return "generation"
    return "none"


def _recommendation(root_cause: str) -> str:
    return {
        "knowledge_gap": "进入测试实例 Evidence Gap，并生成待人工审核的知识自进化草稿。",
        "citation_gap": "检查引用证据覆盖与答案生成约束；必要时进入 Evidence Gap。",
        "no_result": "补充测试知识库覆盖，并检查文档向量化状态。",
        "retrieval": "进入 retrieval tune 建议链，检查召回路由、阈值和排序。",
        "generation": "优化 Prompt、模型路由或回答格式，不向知识库写入内容。",
        "stability": "检查超时、5xx、SSE 中断和 Provider 降级，不触发知识自进化。",
        "test_data": "修复 Fixture、账号或环境配置后重跑，本次结果不进入优化链。",
        "none": "未发现需要自动联动的问题。",
    }[root_cause]


def _final_verdict(score: float, hard_fail: bool, challenger: float | None = None) -> str:
    verdict = "failed" if hard_fail or score < 0.60 else "warning" if score < 0.85 else "passed"
    if challenger is not None and abs(challenger - score) > 0.15:
        return "inconclusive"
    return verdict


def _callback_policy(root_cause: str, baseline_run_id: str) -> str:
    """Return whether the run may start another Grid-QA optimization cycle."""
    if baseline_run_id:
        return "skipped_retest"
    return "send" if root_cause != "none" else "skipped"


async def _challenger_score(query: str, answer: str, sources: list[str]) -> float | None:
    if not settings.CHALLENGER_MODEL:
        return None
    try:
        data = await chat_json("challenger", [{"role": "user", "content": (
            "独立审核 RAG 回答质量。输出 JSON："
            '{"score":0到1,"reason":"说明"}\n'
            f"问题：{query}\n答案：{answer[:4000]}\n资料：{json.dumps(sources[:8], ensure_ascii=False)}"
        )}], temperature=0, max_tokens=400)
        return max(0.0, min(1.0, float(data.get("score", 0.5))))
    except Exception:
        return None


async def _send_callback(callback: EvaluationCallback) -> str:
    if not settings.GRID_CALLBACK_URL or not settings.GRID_CALLBACK_SECRET:
        return "skipped"
    raw = callback.model_dump_json(exclude_none=True).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.GRID_CALLBACK_SECRET.encode("utf-8"), timestamp.encode("utf-8") + b"." + raw,
        hashlib.sha256,
    ).hexdigest()
    try:
        from .replay import validate_target
        callback_url = validate_target(settings.GRID_CALLBACK_URL, settings.TARGET_ENVIRONMENT)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                callback_url, content=raw,
                headers={"content-type": "application/json", "X-LLM-User-Timestamp": timestamp, "X-LLM-User-Signature": signature},
            )
            response.raise_for_status()
        metrics.CALLBACKS.labels("success").inc()
        return "success"
    except Exception:
        metrics.CALLBACKS.labels("failed").inc()
        return "failed"


async def evaluate_run(run_id: str) -> None:
    async with SessionLocal() as db:
        run = await db.get(TestRun, run_id)
        if not run:
            return
        version = await db.get(ScenarioVersion, run.scenario_version_id)
        spec = ScenarioSpec.model_validate(version.spec)
        steps = (await db.execute(select(RunStep).where(RunStep.run_id == run_id).order_by(RunStep.step_index, RunStep.created_at))).scalars().all()

    latest_by_index: dict[int, RunStep] = {}
    for step in steps:
        latest_by_index[step.step_index] = step
    final_steps = list(latest_by_index.values())
    expected_steps = len(spec.stages)
    succeeded = sum(1 for step in final_steps if step.success)
    unexpected_5xx = any((step.response or {}).get("statusCode", 0) >= 500 for step in final_steps)
    stream_failed = any(
        step.request.get("mode") in {"sse", "websocket"}
        and not step.response.get("completed")
        and not (
            step.step_index < len(spec.stages)
            and spec.stages[step.step_index].completion.get("streamInterrupted")
            and step.response.get("interrupted")
        )
        for step in final_steps
    )
    outcome_score = succeeded / expected_steps if expected_steps else 0.0
    outcome_hard = bool(run.error or unexpected_5xx or stream_failed or succeeded < expected_steps)
    outcome_reason = f"完成 {succeeded}/{expected_steps} 个阶段"
    if run.error:
        outcome_reason += f"；{run.error}"
    if unexpected_5xx:
        outcome_reason += "；出现非预期 5xx"
    if stream_failed:
        outcome_reason += "；流式连接未正常完成"

    query, answer, sources, feedback_reason = _extract_case(final_steps)
    answer_model = ""
    for step in final_steps:
        answer_model = answer_model or str(_payload_body(step.response or {}).get("modelType", ""))
    try:
        relevance_res = await judge_context_relevance(_judge_chat, query, sources)
        relevance = float(relevance_res.get("relevance_score", 0.5))
    except Exception:
        relevance_res, relevance = {"reason": "Judge 不可用"}, 0.5
    try:
        faith_res = await judge_hallucination(_judge_chat, answer, sources)
        faithfulness = faith_res.get("supported_ratio")
        faithfulness = float(faithfulness) if isinstance(faithfulness, (int, float)) else 0.5
    except Exception:
        faith_res, faithfulness = {"reason": "Judge 不可用"}, 0.5
    try:
        completeness = await judge_completeness(_judge_chat, query, answer)
    except Exception:
        completeness = 0.5
    feedback_score, feedback_reason_text = await _feedback_alignment(query, answer, feedback_reason)
    trajectory = max(0.0, min(1.0, outcome_score - mean([step.hint_level for step in final_steps] or [0]) * 0.08))
    latencies = [step.latency_ms for step in final_steps if step.latency_ms is not None]
    avg_latency = mean(latencies) if latencies else 0
    performance = 1.0 if avg_latency <= 3000 else 0.8 if avg_latency <= 10000 else 0.5 if avg_latency <= 30000 else 0.2

    rows = [
        _dimension("outcome", outcome_score, outcome_reason, hard_fail=outcome_hard),
        _dimension("relevance", relevance, relevance_res.get("reason", ""), evidence=relevance_res),
        _dimension("faithfulness", faithfulness, faith_res.get("reason", ""), evidence=faith_res),
        _dimension("completeness", completeness, "答案完整性"),
        _dimension("feedback_alignment", feedback_score, feedback_reason_text),
        _dimension("trajectory", trajectory, "提示披露越少、阶段完成越稳定则得分越高"),
        _dimension("performance", performance, f"平均延迟 {avg_latency:.0f}ms"),
    ]
    rag_score = relevance * 0.3 + faithfulness * 0.4 + completeness * 0.3
    score = outcome_score * 0.25 + rag_score * 0.45 + feedback_score * 0.15 + trajectory * 0.10 + performance * 0.05
    hard_fail = any(row["hardFail"] for row in rows)
    verdict = _final_verdict(score, hard_fail)
    challenger = await _challenger_score(query, answer, sources) if verdict != "passed" or score < 0.9 else None
    verdict = _final_verdict(score, hard_fail, challenger)
    dimensions = {row["dimension"]: row for row in rows}
    root_cause = _root_cause(
        dimensions,
        sources,
        run.error,
        observed_interruption=bool(spec.hiddenOracle.get("observedInterrupted")),
    )
    if verdict == "inconclusive":
        root_cause = "test_data"

    before_score = None
    lift = None
    draft_id = ""
    if run.baseline_run_id:
        async with SessionLocal() as db:
            baseline = await db.get(TestRun, run.baseline_run_id)
            retest_link = (await db.execute(select(EvolutionRetest).where(
                EvolutionRetest.rerun_id == run_id,
            ))).scalar_one_or_none()
            draft_id = retest_link.draft_id if retest_link else ""
            if baseline and baseline.score is not None:
                before_score = float(baseline.score)
                lift = round(score - before_score, 4)

    judge_cfg = used_config("judge")
    answer_label = answer_model.lower().strip()
    same_judge_model = bool(answer_label and answer_label == judge_cfg.model.lower().strip())
    if answer_label == "ollama" and settings.OLLAMA_BASE_URL:
        same_judge_model = same_judge_model or (
            judge_cfg.base_url.rstrip("/") == settings.OLLAMA_BASE_URL.rstrip("/")
        )
    judge_confidence = "lower" if same_judge_model else "normal"
    content = {
        "runId": run_id, "scenarioVersionId": run.scenario_version_id,
        "tenantId": run.tenant_id,
        "score": round(score, 4), "verdict": verdict, "rootCause": root_cause,
        "dimensions": rows, "challengerScore": challenger,
        "query": redact(query), "answer": redact(answer), "sourceCount": len(sources),
        "feedback": {
            "observedReason": redact(feedback_reason),
            "alignmentReason": redact(feedback_reason_text),
        },
        "trajectory": [{
            "index": step.step_index,
            "intent": redact(step.intent),
            "hintLevel": step.hint_level,
            "success": step.success,
            "latencyMs": step.latency_ms,
            "traceId": step.trace_id,
            "operation": {
                "method": (step.request or {}).get("method", ""),
                "path": (step.request or {}).get("path", ""),
                "mode": (step.request or {}).get("mode", "http"),
            },
        } for step in final_steps],
        "traceIds": [step.trace_id for step in final_steps if step.trace_id],
        "recommendation": _recommendation(root_cause),
        "baselineRunId": run.baseline_run_id,
        "draftId": draft_id,
        "beforeScore": before_score, "afterScore": round(score, 4), "lift": lift,
        "models": {
            "answer": answer_model, "judge": judge_cfg.model,
            "judgeProvider": judge_cfg.base_url, "confidence": judge_confidence,
        },
    }
    json_path, markdown_path, html_path = write_report(run_id, content)
    callback = EvaluationCallback(
        eventId=f"llm-user:{run_id}", runId=run_id, scenarioVersionId=run.scenario_version_id,
        tenantId=run.tenant_id,
        outcome=verdict, rootCause=root_cause, scores={row["dimension"]: row["score"] for row in rows},
        query=query, answer=answer, reason=feedback_reason_text,
        traceIds=content["traceIds"], evidence={"report": content}, occurredAt=datetime.now(),
    )
    callback_policy = _callback_policy(root_cause, run.baseline_run_id)
    callback_status = (
        await _send_callback(callback) if callback_policy == "send" else callback_policy
    )

    async with SessionLocal() as db:
        run = await db.get(TestRun, run_id)
        for row in rows:
            db.add(Evaluation(
                run_id=run_id, dimension=row["dimension"], score=row["score"], verdict=row["verdict"],
                hard_fail=row["hardFail"], reason=row["reason"], evidence=row["evidence"],
                provider=judge_cfg.base_url, model=judge_cfg.model,
                prompt_version="eval-core-rag-v1",
                prompt_hash=input_digest({"version": "eval-core-rag-v1", "dimension": row["dimension"]}),
                input_digest=input_digest({
                    "query": query, "answer": answer, "sources": sources[:20],
                    "dimension": row["dimension"],
                }),
            ))
            if row["score"] is not None:
                metrics.JUDGE_SCORE.labels(row["dimension"]).observe(row["score"])
        run.status = "completed"
        run.score = round(score, 4)
        run.verdict = verdict
        run.root_cause = root_cause
        run.finished_at = datetime.now()
        run.result = {**(run.result or {}), "report": content}
        db.add(Report(
            run_id=run_id, verdict=verdict, summary=_recommendation(root_cause), content=content,
            json_path=json_path, markdown_path=markdown_path, html_path=html_path, callback_status=callback_status,
        ))
        retest = (await db.execute(select(EvolutionRetest).where(EvolutionRetest.rerun_id == run_id))).scalar_one_or_none()
        if retest:
            retest.status = "completed"
            retest.before_score = before_score
            retest.after_score = round(score, 4)
            retest.lift = lift
            retest.finished_at = datetime.now()
        await db.commit()
    metrics.REPLAY_RUNS.labels(verdict).inc()
    try:
        from .run_service import dispatch_pending_runs
        await dispatch_pending_runs()
    except Exception:
        pass
