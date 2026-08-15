from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
import websockets
from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .llm import chat_json, configured
from .models import EvolutionRetest, RunStep, ScenarioVersion, TestRun
from .privacy import redact
from .schemas import ScenarioSpec, ScenarioStage

_PRODUCTION_MARKERS = {"prod", "production", "online"}
_HARD_DENY_PREFIXES = (
    "/api/system/users", "/api/system/config", "/api/system/backup",
    "/api/document/delete", "/api/knowledge-evolution/drafts/",
)
_SAFE_AGENT_HEADERS = {"accept", "content-type", "idempotency-key", "x-request-id", "x-grid-session-id"}
_PRODUCTION_HOST = re.compile(r"(^|[.-])(prod|production|online)([.-]|$)", re.I)


def validate_target(base_url: str, environment: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("targetBaseUrl 必须是有效的 http(s) 地址")
    if environment.lower() in _PRODUCTION_MARKERS:
        raise ValueError("Runner 禁止指向生产环境")
    hostname = parsed.hostname.lower()
    if hostname in settings.denied_target_hosts() or _PRODUCTION_HOST.search(hostname):
        raise ValueError("Runner 禁止指向生产主机")
    allowed = settings.allowed_target_hosts()
    if settings.REQUIRE_TARGET_ALLOWLIST and not allowed:
        raise ValueError("必须配置 LLM_USER_ALLOWED_TARGET_HOSTS")
    if allowed and hostname not in allowed:
        raise ValueError("目标主机不在 LLM_USER_ALLOWED_TARGET_HOSTS 白名单")
    return base_url.rstrip("/")


def _substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, variables) for item in value]
    if not isinstance(value, str):
        return value
    exact = re.fullmatch(r"\$\{([A-Za-z0-9_.-]+)}", value)
    if exact:
        return variables.get(exact.group(1), "")
    for key, item in variables.items():
        value = value.replace("${" + key + "}", str(item))
    return value


def _operation_allowed(method: str, path: str, operations: set[tuple[str, str]]) -> bool:
    for allowed_method, template in operations:
        if allowed_method != method:
            continue
        parts = re.split(r"(\{[^/{}]+\})", template)
        pattern = "^" + "".join(r"[^/?#]+" if part.startswith("{") else re.escape(part) for part in parts) + "$"
        if re.match(pattern, path):
            return True
    return False


def _approved_spec_operations(spec: ScenarioSpec) -> set[tuple[str, str]]:
    """Build the fallback allowlist from the human-reviewed scenario itself."""
    operations: set[tuple[str, str]] = set()
    requests = [stage.requestTemplate for stage in spec.stages] + list(spec.cleanup)
    for request in requests:
        method = str(request.get("method", "GET")).upper()
        path = str(request.get("path", ""))
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not path.startswith("/"):
            continue
        # Runtime fixture substitutions remain one path segment and therefore use the
        # same matcher as OpenAPI's /resource/{id} templates.
        path = re.sub(r"\$\{[A-Za-z0-9_.-]+\}", "{fixture}", path)
        operations.add((method, path))
    return operations


def _safe_request(action: dict, spec: ScenarioSpec, allowed_operations: set[tuple[str, str]] | None = None) -> dict:
    method = str(action.get("method", "GET")).upper()
    path = str(action.get("path", ""))
    if not path.startswith("/") or "//" in path:
        raise ValueError("Agent 返回了非法 API path")
    denied = tuple(spec.safety.get("denyPathPrefixes", [])) + _HARD_DENY_PREFIXES
    if any(path.startswith(prefix) for prefix in denied):
        raise PermissionError(f"API path 被安全策略禁止: {path}")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ValueError(f"不支持的 HTTP method: {method}")
    if method == "DELETE" and not any(
        path.startswith(prefix) for prefix in spec.safety.get("allowDeletePrefixes", [])
    ):
        raise PermissionError(f"DELETE 默认禁止，剧本未显式授权: {path}")
    mode = str(action.get("mode", "http")).lower()
    if mode not in {"http", "sse", "websocket"}:
        raise ValueError(f"不支持的回放模式: {mode}")
    websocket_paths = set(spec.safety.get("websocketPaths", ["/api/qa/ws"]))
    if allowed_operations and not (
        mode == "websocket" and path in websocket_paths
    ) and not _operation_allowed(method, path, allowed_operations):
        raise PermissionError(f"API operation 不在目标 OpenAPI 中: {method} {path}")
    headers = {}
    for key, value in (action.get("headers") or {}).items():
        if str(key).lower() not in _SAFE_AGENT_HEADERS:
            raise PermissionError(f"Agent header 被安全策略禁止: {key}")
        headers[str(key)] = str(value)[:1000]
    interrupt_after_tokens = action.get("interruptAfterTokens")
    if interrupt_after_tokens is not None:
        try:
            interrupt_after_tokens = max(1, min(500, int(interrupt_after_tokens)))
        except (TypeError, ValueError):
            raise ValueError("interruptAfterTokens 必须是 1 到 500 的整数")
    return {
        "method": method, "path": path, "mode": mode,
        "body": action.get("body", {}), "query": action.get("query", {}),
        "headers": headers, "interruptAfterTokens": interrupt_after_tokens,
    }


async def _actor_action(
    spec: ScenarioSpec, stage: ScenarioStage, hint_level: int,
    history: list[dict], api_catalog: list[dict],
) -> dict:
    fallback = stage.requestTemplate
    if not configured("actor"):
        return fallback
    visible = {
        "persona": spec.persona,
        "goal": spec.goal,
        "currentIntent": stage.intent,
        "previousResults": history[-5:],
        "availableApiTools": api_catalog[:200],
    }
    if hint_level >= 1:
        visible["businessHint"] = stage.businessHint
    if hint_level >= 2:
        visible["apiHint"] = stage.apiHint
    if hint_level >= 3:
        visible["requestShape"] = stage.requestTemplate
    prompt = (
        "你在专用测试环境扮演真实 API 用户。根据当前可见信息选择一次 API 操作。"
        "禁止访问用户管理、Secret、系统配置和生产环境。只输出 JSON："
        '{"method":"POST","path":"/api/...","mode":"http|sse|websocket","body":{},"query":{},"headers":{}}\n\n'
        + json.dumps(visible, ensure_ascii=False)
    )
    try:
        return await chat_json("actor", [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=700)
    except Exception:
        return fallback


async def _login(client: httpx.AsyncClient, profile_name: str) -> str:
    profile = settings.credential_profiles().get(profile_name, {})
    if not profile:
        return ""
    response = await client.post("/api/system/login", json={
        "username": profile.get("username", ""), "password": profile.get("password", ""),
    })
    response.raise_for_status()
    data = response.json()
    token = (data.get("data") or {}).get("token", "")
    if token:
        client.headers["Authorization"] = f"Bearer {token}"
    return token


async def _openapi_catalog(client: httpx.AsyncClient) -> tuple[list[dict], set[tuple[str, str]]]:
    try:
        response = await client.get("/openapi.json")
        response.raise_for_status()
        document = response.json()
    except Exception:
        return [], set()
    catalog: list[dict] = []
    operations: set[tuple[str, str]] = set()
    for path, path_item in (document.get("paths") or {}).items():
        for method, operation in path_item.items():
            method_upper = method.upper()
            if method_upper not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            operations.add((method_upper, path))
            catalog.append({
                "method": method_upper, "path": path,
                "summary": operation.get("summary", ""), "tags": operation.get("tags", []),
            })
    return catalog, operations


async def _call_http(client: httpx.AsyncClient, action: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    response = await client.request(
        action["method"], action["path"], params=action.get("query") or None,
        json=action.get("body") if action["method"] != "GET" else None,
        headers=action.get("headers") or None,
    )
    latency = (time.perf_counter() - started) * 1000
    content_type = response.headers.get("content-type", "")
    try:
        body = response.json() if "json" in content_type else response.text[:12000]
    except Exception:
        body = response.text[:12000]
    return {
        "statusCode": response.status_code, "headers": {
            "content-type": content_type,
            "x-trace-id": response.headers.get("x-trace-id", ""),
            "x-cache-hit": response.headers.get("x-cache-hit", ""),
        }, "body": redact(body),
    }, latency


async def _call_sse(client: httpx.AsyncClient, action: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    first_event_ms = None
    events: list[dict] = []
    token_events = 0
    interrupted = False
    async with client.stream(
        action["method"], action["path"], params=action.get("query") or None,
        json=action.get("body") or {}, headers=action.get("headers") or None,
    ) as response:
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            if first_event_ms is None:
                first_event_ms = (time.perf_counter() - started) * 1000
            text = line[5:].strip()
            if text == "[DONE]":
                break
            try:
                item = json.loads(text)
                events.append(redact(item))
                if item.get("type") == "token":
                    token_events += 1
            except ValueError:
                events.append({"type": "text", "content": text[:1000]})
            if action.get("interruptAfterTokens") and token_events >= action["interruptAfterTokens"]:
                interrupted = True
                break
            if len(events) >= 500:
                break
    latency = (time.perf_counter() - started) * 1000
    return {
        "statusCode": response.status_code,
        "headers": {"content-type": response.headers.get("content-type", "")},
        "events": events, "firstEventMs": first_event_ms,
        "terminalType": str(events[-1].get("type", "")) if events else "",
        "completed": bool(events and events[-1].get("type") == "done"),
        "interrupted": interrupted, "tokenEvents": token_events,
    }, latency


async def _call_websocket(base_url: str, action: dict, token: str) -> tuple[dict, float]:
    parsed = urlparse(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = dict(action.get("query") or {})
    if token:
        query.setdefault("token", token)
    uri = f"{scheme}://{parsed.netloc}{action['path']}"
    if query:
        uri += "?" + urlencode(query)
    started = time.perf_counter()
    events = []
    token_events = 0
    interrupted = False
    async with websockets.connect(uri, open_timeout=15, close_timeout=5) as ws:
        await ws.send(json.dumps(action.get("body") or {}, ensure_ascii=False))
        while len(events) < 500:
            item = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
            events.append(redact(item))
            if item.get("type") == "token":
                token_events += 1
            if action.get("interruptAfterTokens") and token_events >= action["interruptAfterTokens"]:
                interrupted = True
                await ws.close()
                break
            if item.get("type") in {"done", "error"}:
                break
    terminal_type = str(events[-1].get("type", "")) if events else ""
    return {
        "statusCode": 101, "events": events, "terminalType": terminal_type,
        "completed": terminal_type == "done", "interrupted": interrupted,
        "tokenEvents": token_events,
    }, (time.perf_counter() - started) * 1000


def _completion(response: dict, predicate: dict) -> bool:
    if response.get("error") or response.get("terminalType") == "error":
        return False
    expected_status = predicate.get("statusCode")
    if expected_status is not None and response.get("statusCode") != expected_status:
        return False
    if predicate.get("streamCompleted") and not response.get("completed"):
        return False
    if predicate.get("streamInterrupted") and not response.get("interrupted"):
        return False
    body = response.get("body")
    if predicate.get("bodyCode") is not None:
        if not isinstance(body, dict) or body.get("code") != predicate["bodyCode"]:
            return False
    return True


async def _cancel_requested(run_id: str) -> bool:
    async with SessionLocal() as db:
        row = await db.get(TestRun, run_id)
        return not row or row.cancel_requested or row.status == "cancelled"


async def _cleanup(
    client: httpx.AsyncClient, base_url: str, token: str, spec: ScenarioSpec,
    variables: dict[str, Any], allowed_operations: set[tuple[str, str]], run_id: str,
    deadline: float, max_calls: int,
) -> tuple[list[dict], int]:
    results: list[dict] = []
    calls = 0
    for index, raw in enumerate(spec.cleanup[:min(20, max(0, max_calls))]):
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                results.append({"request": redact(raw), "error": "run timeout before cleanup"})
                break
            action = _safe_request(
                _substitute(raw, variables), spec,
                allowed_operations,
            )
            action.setdefault("headers", {}).setdefault("Idempotency-Key", f"llm-user-cleanup-{run_id}-{index}")
            calls += 1
            async with asyncio.timeout(remaining):
                if action["mode"] == "websocket":
                    response, latency = await _call_websocket(base_url, action, token)
                elif action["mode"] == "sse":
                    response, latency = await _call_sse(client, action)
                else:
                    response, latency = await _call_http(client, action)
            results.append({"request": redact(action), "response": redact(response), "latencyMs": latency})
        except Exception as exc:
            results.append({"request": redact(raw), "error": f"{type(exc).__name__}: {exc}"})
    return results, calls


async def execute_run(run_id: str) -> None:
    async with SessionLocal() as db:
        run = await db.get(TestRun, run_id)
        if not run or run.status not in {"queued", "running"}:
            return
        version = await db.get(ScenarioVersion, run.scenario_version_id)
        if not version or version.status not in {"approved", "active"}:
            run.status, run.error = "failed", "scenario version is not approved"
            await db.commit()
            return
        run.status, run.started_at = "running", datetime.now()
        retest = (await db.execute(select(EvolutionRetest).where(
            EvolutionRetest.rerun_id == run.id,
        ))).scalar_one_or_none()
        if retest:
            retest.status = "running"
        await db.commit()
        spec = ScenarioSpec.model_validate(version.spec)
        base_url = validate_target(run.target_base_url or settings.TARGET_BASE_URL, run.environment)
        profile = run.credential_profile

    started = time.monotonic()
    deadline = started + settings.MAX_RUN_SECONDS
    history: list[dict] = []
    variables = dict(spec.variables)
    calls = 0
    call_limit = max(0, min(
        settings.MAX_API_CALLS,
        int(spec.safety.get("maxCalls", settings.MAX_API_CALLS)),
    ))
    hard_error = ""
    cleanup_results: list[dict] = []
    token = ""
    async with httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(180, connect=15), follow_redirects=True) as client:
        try:
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                token = await _login(client, profile)
                api_catalog, discovered_operations = await _openapi_catalog(client)
            allowed_operations = discovered_operations or _approved_spec_operations(spec)
            for index, stage in enumerate(spec.stages):
                if await _cancel_requested(run_id):
                    return
                if stage.delayMs:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("run timeout")
                    await asyncio.sleep(min(stage.delayMs / 1000, remaining))
                success = False
                for hint_level in range(0, min(3, stage.maxAttempts - 1) + 1):
                    if calls >= call_limit:
                        raise RuntimeError("API call limit exceeded")
                    if time.monotonic() - started > settings.MAX_RUN_SECONDS:
                        raise TimeoutError("run timeout")
                    raw_action: dict = {}
                    action: dict = {}
                    try:
                        raw_action = await _actor_action(
                            spec, stage, hint_level, history, api_catalog,
                        )
                        if stage.requestTemplate.get("interruptAfterTokens") is not None:
                            raw_action["interruptAfterTokens"] = stage.requestTemplate["interruptAfterTokens"]
                        try:
                            action = _safe_request(
                                _substitute(raw_action, variables), spec,
                                allowed_operations,
                            )
                        except (PermissionError, ValueError):
                            if hint_level < 3:
                                raise
                            raw_action = stage.requestTemplate
                            action = _safe_request(
                                _substitute(raw_action, variables), spec,
                                allowed_operations,
                            )
                        if action["method"] != "GET":
                            action["headers"].setdefault(
                                "Idempotency-Key", f"llm-user-{run_id}-{index}-{hint_level}",
                            )
                        calls += 1
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("run timeout")
                        async with asyncio.timeout(remaining):
                            if action["mode"] == "sse":
                                response, latency = await _call_sse(client, action)
                            elif action["mode"] == "websocket":
                                response, latency = await _call_websocket(base_url, action, token)
                            else:
                                response, latency = await _call_http(client, action)
                        success = _completion(response, stage.completion)
                    except Exception as exc:
                        response, latency, success = {"error": f"{type(exc).__name__}: {exc}"}, 0, False
                    history.append({
                        "intent": stage.intent,
                        "request": redact(action or raw_action),
                        "response": redact(response),
                        "success": success,
                        "hintLevel": hint_level,
                    })
                    async with SessionLocal() as db:
                        db.add(RunStep(
                            run_id=run_id, step_index=index, intent=stage.intent,
                            hint_level=hint_level, request=redact(action or raw_action),
                            response=redact(response),
                            success=success, latency_ms=latency,
                            trace_id=str(response.get("headers", {}).get("x-trace-id", "")),
                        ))
                        await db.commit()
                    if success:
                        break
                if not success:
                    detail = str((history[-1].get("response") or {}).get("error", "")) if history else ""
                    hard_error = f"stage {index} failed: {stage.intent}"
                    if detail:
                        hard_error += f"; {detail[:1000]}"
                    break
        except Exception as exc:
            hard_error = f"{type(exc).__name__}: {exc}"
        finally:
            cleanup_results, cleanup_calls = await _cleanup(
                client, base_url, token, spec, variables,
                allowed_operations if 'allowed_operations' in locals() else _approved_spec_operations(spec),
                run_id, deadline, call_limit - calls,
            )
            calls += cleanup_calls

    async with SessionLocal() as db:
        run = await db.get(TestRun, run_id)
        if not run or run.cancel_requested or run.status == "cancelled":
            return
        run.status = "evaluating"
        run.error = hard_error
        run.result = {
            "calls": calls, "history": history, "cleanup": cleanup_results,
            "durationSeconds": round(time.monotonic() - started, 3),
        }
        await db.commit()
    from . import metrics
    metrics.REPLAY_DURATION.observe(time.monotonic() - started)
    from .judge import evaluate_run
    await evaluate_run(run_id)
