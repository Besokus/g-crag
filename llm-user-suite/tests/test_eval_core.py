import pytest
from llm_eval_core import (
    Case,
    Report,
    Run,
    Score,
    judge_completeness,
    judge_context_relevance,
    judge_hallucination,
    verify_claims,
)


@pytest.mark.asyncio
async def test_shared_eval_core_structured_outputs():
    async def fake_chat(messages, temperature=0, max_tokens=800):
        prompt = messages[0]["content"]
        if "support、contradict" in prompt:
            return '{"claims":[{"text":"A","label":"support"}]}'
        if "原子事实" in prompt:
            return '{"claims":[{"text":"A","supported":true}],"supported_count":1,"total_count":1}'
        if "检索分块" in prompt:
            return '{"relevance_score":0.9,"labels":{"0":"relevant"},"reason":"命中"}'
        return '{"score":0.8,"reason":"基本完整"}'

    assert (await verify_claims(fake_chat, ["A"], ["source"]))[0]["label"] == "support"
    assert (await judge_hallucination(fake_chat, "A", ["source"]))["supported_ratio"] == 1.0
    assert (await judge_context_relevance(fake_chat, "A?", ["source"]))["relevance_score"] == 0.9
    assert await judge_completeness(fake_chat, "A?", "A") == 0.8


def test_shared_case_run_score_report_contracts():
    case = Case(id="case-1", scenario_version_id="sv-1", persona={}, goal="ask")
    run = Run(id="run-1", case_id=case.id, environment="test", target="https://testserver")
    report = Report(run=run, scores=[Score("outcome", 1.0, "passed")], verdict="passed", root_cause="none")
    assert report.to_dict()["run"]["case_id"] == "case-1"
