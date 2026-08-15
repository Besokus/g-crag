from llm_user_suite.judge import _callback_policy, _final_verdict, _root_cause


def test_second_judge_disagreement_is_inconclusive():
    assert _final_verdict(0.82, False, 0.60) == "inconclusive"
    assert _final_verdict(0.82, False, 0.74) == "warning"


def test_environment_failure_is_not_routed_to_knowledge():
    dimensions = {
        "outcome": {"hardFail": True, "reason": "完成 0/1"},
        "relevance": {"score": 0.0},
        "faithfulness": {"score": 0.0},
        "completeness": {"score": 0.0},
        "feedback_alignment": {"score": 0.0},
    }
    assert _root_cause(dimensions, [], "login failed: 401") == "test_data"


def test_observed_stream_interruption_routes_to_stability():
    dimensions = {
        "outcome": {"hardFail": False, "reason": "完成 2/2"},
        "relevance": {"score": 0.2},
        "faithfulness": {"score": 0.5},
        "completeness": {"score": 0.0},
        "feedback_alignment": {"score": 0.8},
    }
    assert _root_cause(
        dimensions, ["partial source"], "", observed_interruption=True,
    ) == "stability"


def test_retest_does_not_start_recursive_optimization_callback():
    assert _callback_policy("no_result", "baseline-run") == "skipped_retest"
    assert _callback_policy("knowledge_gap", "baseline-run") == "skipped_retest"
    assert _callback_policy("no_result", "") == "send"
    assert _callback_policy("none", "") == "skipped"
