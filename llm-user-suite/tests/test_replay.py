import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from llm_user_suite.dreamer import _fallback_spec
from llm_user_suite.replay import (
    _actor_action,
    _approved_spec_operations,
    _call_sse,
    _completion,
    _operation_allowed,
    _safe_request,
    validate_target,
)
from llm_user_suite.schemas import ScenarioSpec, ScenarioStage


def _spec():
    return ScenarioSpec(
        name="test", goal="ask", stages=[ScenarioStage(
            intent="ask", requestTemplate={"method": "POST", "path": "/api/qa/answer/stream"},
        )], safety={"denyPathPrefixes": ["/api/system/users"]},
    )


def test_scenario_stage_defaults_to_all_progressive_disclosure_levels():
    assert ScenarioStage(intent="health").maxAttempts == 4


def test_target_guard_rejects_production_and_unknown_host():
    with pytest.raises(ValueError):
        validate_target("https://testserver", "production")
    with pytest.raises(ValueError):
        validate_target("https://unlisted.example", "test")
    with pytest.raises(ValueError):
        validate_target("https://prod.testserver", "test")
    assert validate_target("https://testserver", "test") == "https://testserver"


def test_openapi_and_destructive_guards():
    assert _operation_allowed("GET", "/api/items/123", {("GET", "/api/items/{item_id}")})
    assert not _operation_allowed("GET", "/api/items/a/b", {("GET", "/api/items/{item_id}")})
    action = _safe_request(
        {"method": "POST", "path": "/api/qa/answer/stream", "body": {}}, _spec(),
        {("POST", "/api/qa/answer/stream")},
    )
    assert action["method"] == "POST"
    with pytest.raises(PermissionError):
        _safe_request({"method": "DELETE", "path": "/api/system/users/1"}, _spec())
    with pytest.raises(PermissionError):
        _safe_request({"method": "POST", "path": "/api/qa/answer", "headers": {"Authorization": "secret"}}, _spec())


def test_reviewed_scenario_is_the_fallback_allowlist_when_openapi_is_unavailable():
    spec = _spec()
    operations = _approved_spec_operations(spec)
    assert _operation_allowed("POST", "/api/qa/answer/stream", operations)
    with pytest.raises(PermissionError):
        _safe_request({"method": "POST", "path": "/api/qa/feedback"}, spec, operations)


def test_completion_contract():
    assert _completion({"statusCode": 200, "completed": True}, {"statusCode": 200, "streamCompleted": True})
    assert not _completion({"statusCode": 500}, {"statusCode": 200})
    assert not _completion({"statusCode": 200, "terminalType": "error"}, {"statusCode": 200})
    assert not _completion({"statusCode": 200, "body": "not-json"}, {"bodyCode": 0})


@pytest.mark.asyncio
async def test_actor_progressively_discloses_only_current_stage(monkeypatch):
    captured = []

    async def fake_chat(role, messages, **kwargs):
        captured.append(messages[0]["content"])
        return {"method": "GET", "path": "/health"}

    spec = ScenarioSpec(
        name="progressive", goal="finish task", persona={"role": "operator"},
        hiddenOracle={"answer": "never disclose"},
        stages=[
            ScenarioStage(intent="current", businessHint="business", apiHint="GET /health", requestTemplate={"method": "GET", "path": "/health"}),
            ScenarioStage(intent="future-secret", requestTemplate={"method": "GET", "path": "/future"}),
        ],
    )
    monkeypatch.setattr("llm_user_suite.replay.configured", lambda role: True)
    monkeypatch.setattr("llm_user_suite.replay.chat_json", fake_chat)
    await _actor_action(spec, spec.stages[0], 0, [], [])
    assert "never disclose" not in captured[-1]
    assert "future-secret" not in captured[-1]
    assert "businessHint" not in captured[-1]
    await _actor_action(spec, spec.stages[0], 3, [], [])
    assert "requestShape" in captured[-1] and "businessHint" in captured[-1]


@pytest.mark.asyncio
async def test_sse_client_distinguishes_done_from_error():
    async def handler(request):
        payload = "data: " + json.dumps({"type": "done", "answer": "ok"}) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://testserver") as client:
        result, _ = await _call_sse(client, {"method": "POST", "path": "/stream", "body": {}})
    assert result["completed"] and result["terminalType"] == "done"

    async def error_handler(request):
        payload = "data: " + json.dumps({"type": "error", "message": "failed"}) + "\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(error_handler), base_url="https://testserver") as client:
        result, _ = await _call_sse(client, {"method": "POST", "path": "/stream", "body": {}})
    assert not result["completed"] and result["terminalType"] == "error"


@pytest.mark.asyncio
async def test_sse_client_can_intentionally_replay_user_interruption():
    async def handler(request):
        payload = "".join([
            "data: " + json.dumps({"type": "meta"}) + "\n\n",
            "data: " + json.dumps({"type": "token", "content": "a"}) + "\n\n",
            "data: " + json.dumps({"type": "token", "content": "b"}) + "\n\n",
        ])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=payload)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://testserver",
    ) as client:
        result, _ = await _call_sse(client, {
            "method": "POST", "path": "/stream", "body": {},
            "interruptAfterTokens": 1,
        })

    assert result["interrupted"] is True
    assert result["completed"] is False
    assert result["tokenEvents"] == 1
    assert _completion(result, {"statusCode": 200, "streamInterrupted": True})


def test_dreamer_turns_observed_abort_into_replay_control():
    now = datetime.now(UTC)
    spec = _fallback_spec([
        SimpleNamespace(
            kind="qa.stream.started", method="POST", path="/api/qa/answer/stream",
            status_code=200, occurred_at=now,
            payload={"query": "question", "request": {"query": "question"}},
        ),
        SimpleNamespace(
            kind="qa.stream.aborted", method="POST", path="/api/qa/answer/stream",
            status_code=200, occurred_at=now + timedelta(seconds=1),
            payload={"tokenEvents": 2},
        ),
    ], 1)

    stage = spec.stages[0]
    assert stage.requestTemplate["interruptAfterTokens"] == 2
    assert stage.completion == {"statusCode": 200, "streamInterrupted": True}
