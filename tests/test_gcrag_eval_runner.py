"""评测 run / judge / calibrate / report（scripts/gcrag_eval_runner.py；设计文档 §11、§16.13）。"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gcrag_eval_runner.py"
spec = importlib.util.spec_from_file_location("gcrag_eval_runner_ut", _PATH)
R = importlib.util.module_from_spec(spec)
sys.modules["gcrag_eval_runner_ut"] = R
spec.loader.exec_module(R)


def Q(qid, answerable=True, qtype="single", split="calib"):
    return {"id": qid, "question": f"问题{qid}", "answer": f"答案{qid}", "answerable": answerable,
            "type": qtype, "split": split}


def test_to_record_fields():
    data = {"answer": "a", "confidence": "medium", "initialGrade": "ambiguous", "cragGrade": "correct",
            "cragAction": "graph_recovered", "correctionRounds": [{"action": "graph_evidence"}],
            "evidencePaths": [{}, {}], "retrievalSource": [{"score": 0.4, "docName": "x.txt"},
                                                           {"score": 0.8, "docName": "y.txt", "origin": "graph"}],
            "responseTime": 3.2, "trace": {"traceId": "t1"}, "scoringOk": False}
    r = R.to_record(Q("s01"), "gcrag", data, 3300)
    assert (r["top1"], r["evidencePathCount"], r["traceId"], r["degraded"]) == (0.8, 2, "t1", ["scoring"])
    assert r["sources"][1] == {"docName": "y.txt", "origin": "graph"}


def test_rule_and_parse_verdict():
    assert R.rule_verdict({"cragAction": "refused", "answer": "根据现有资料无法回答"}) == "refused"
    assert R.rule_verdict({"cragAction": "normal", "answer": " "}) == "refused"
    assert R.rule_verdict({"cragAction": "normal", "answer": "x"}) is None
    assert R.parse_verdict('好的 {"verdict": "Correct", "reason": "数字一致"}') == ("correct", "数字一致")
    assert R.parse_verdict("wrong，遗漏了比例")[0] == "wrong"
    assert R.parse_verdict("???")[0] == "wrong"


def test_calibrate_respects_false_refusal_cap():
    qs = [Q(f"a{i}") for i in range(10)] + [Q(f"u{i}", answerable=False) for i in range(4)]
    tops = [0.95, 0.9, 0.85, 0.8, 0.6, 0.5, 0.4, 0.3, 0.25, 0.12] + [0.1, 0.15, 0.2, 0.28]
    recs = [{"id": q["id"], "top1": t} for q, t in zip(qs, tops)]
    correct = {"a0", "a1", "a2", "a3", "a4", "a6"}                 # a5、a7~a9 答错
    verdicts = {(q["id"], "none"): ("correct" if q["id"] in correct else "wrong") for q in qs}
    res = R.calibrate(recs, verdicts, qs, max_false_refusal=0.1, high_precision=0.9)
    # 误拒率上限 10% → 最多拒 1 道可答题（top1 0.12）：low ≤ 0.25；low ∈ [0.21, 0.25] 时不可答题
    # 0.1/0.15/0.2 都被拒、目标值最大（6 + 3）；并列取最优区间中点
    assert res["bestLow"]["tieRange"] == [0.21, 0.25] and res["chosen"]["low"] == 0.23
    assert res["bestLow"]["falseRefusal"] <= 0.1 and res["bestLow"]["unansRefused"] == 3
    # high：top1 ≥ h 的可答题答对率 ≥ 90% 的最小 h（0.6 起 5/5 全对；0.5 起 5/6）
    assert res["chosen"]["high"] == 0.51


def test_metrics_rates_and_rescue():
    qs = [Q("a", qtype="double"), Q("b", qtype="double"), Q("u", answerable=False)]
    runs = {"gcrag": [
        {"id": "a", "initialGrade": "ambiguous", "cragAction": "graph_recovered", "latencyMs": 100,
         "correctionRounds": [{"action": "graph_evidence", "status": "done", "gradeAfter": "correct"}]},
        {"id": "b", "initialGrade": "incorrect", "cragAction": "refused", "latencyMs": 300,
         "correctionRounds": [{"action": "graph_evidence", "status": "skipped_no_entity", "gradeAfter": "incorrect"},
                              {"action": "rewrite", "status": "done", "gradeAfter": "incorrect"}]},
        {"id": "u", "initialGrade": "incorrect", "cragAction": "refused", "latencyMs": 200, "degraded": ["scoring"],
         "correctionRounds": []}]}
    verdicts = {("a", "gcrag"): "correct", ("b", "gcrag"): "refused", ("u", "gcrag"): "refused"}
    m = R.metrics(qs, runs, verdicts)["gcrag"]
    assert m["accuracy"]["double"] == {"n": 2, "correct": 1, "rate": 0.5}
    assert m["unanswerableRefusal"]["rate"] == 1.0 and m["falseRefusal"]["rate"] == 0.5
    assert m["rescue"]["graph_evidence"] == {"den": 1, "num": 1, "rate": 1.0}   # b 未执行图谱（跳过）不进分母
    assert m["rescue"]["rewrite"] == {"den": 1, "num": 0, "rate": 0.0}
    assert m["degraded"] == 1 and m["latencyMs"]["p50"] == 200
    assert "graph_recovered" in R.render_report({"gcrag": m}, {"runId": "t"})


def test_run_resumes_and_paces(tmp_path, monkeypatch):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"code": 200, "data": {"token": "tok"}})
        body = json.loads(req.content)
        calls.append((body["query"], body["correctionMode"], body["noCache"], req.headers["authorization"]))
        if body["query"] == "问题bad":
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"code": 200, "data": {"answer": "ok", "cragAction": "normal",
                                                               "retrievalSource": []}})
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def closed(q):
        return "闭卷答案"
    qs = [Q("a"), Q("b"), Q("bad")]
    s1 = asyncio.run(R.run(qs, ["legacy", "closed_book"], tmp_path, base="http://x", username="u", password="p",
                           concurrency=2, interval=0.0, timeout=5, closed_book_fn=closed))
    assert s1["legacy"]["done"] == 2 and len(s1["legacy"]["failed"]) == 1 and s1["closed_book"]["done"] == 3
    assert all(c[1] == "legacy" and c[2] is True and c[3] == "Bearer tok" for c in calls)
    calls.clear()
    asyncio.run(R.run(qs, ["legacy"], tmp_path, base="http://x", username="u", password="p",
                      concurrency=2, interval=0.0, timeout=5, closed_book_fn=closed))
    assert [c[0] for c in calls] == ["问题bad"]                    # 续跑只重试失败的题


def test_pacer_spacing():
    async def go():
        p = R._Pacer(0.05)
        import time
        t = []
        for _ in range(3):
            await p.wait()
            t.append(time.monotonic())
        return t
    t = asyncio.run(go())
    assert t[2] - t[0] >= 0.095


def test_judge_rules_and_resume(tmp_path):
    qs = [Q("a"), Q("u", answerable=False)]
    R.append_jsonl(tmp_path / "gcrag.jsonl", {"id": "a", "mode": "gcrag", "answer": "答案a", "cragAction": "normal"})
    R.append_jsonl(tmp_path / "gcrag.jsonl", {"id": "u", "mode": "gcrag", "answer": "拒答", "cragAction": "refused"})
    prompts = []

    async def chat(p):
        prompts.append(p)
        return '{"verdict": "correct", "reason": "一致"}'
    assert asyncio.run(R.judge(tmp_path, qs, chat_fn=chat, judge_model="m")) == {"correct": 1, "refused": 1}
    assert len(prompts) == 1 and "答案a" in prompts[0]                  # 拒答走规则，不调判定模型
    assert asyncio.run(R.judge(tmp_path, qs, chat_fn=chat, judge_model="m")) == {}   # 已判过的跳过
    js = R.load_jsonl(tmp_path / "judgments.jsonl")
    assert {(j["id"], j["verdict"], j["judgeModel"]) for j in js} == {("a", "correct", "m"), ("u", "refused", "rule")}
