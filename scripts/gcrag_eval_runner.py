"""G-CRAG 评测的 run / judge / calibrate / report（《设计文档》§11、§16.13）。由 gcrag_eval.py 子命令调用。

- run：每道题按模式调用 /qa/answer（noCache=true，逐请求指定 correctionMode）；closed_book 直接调 LLM、
  不给任何资料。结果按模式写 <mode>.jsonl，已有的题跳过（断点续跑）。打分器录制回放由服务端
  SCORER_CACHE 控制，结束时把录制文件复制进运行目录。
- judge：系统拒答（cragAction=refused）按规则判 refused；其余由判定模型对照标准答案判
  correct / wrong / refused。判定模型默认 qwen-max：生成用 Claude，换一个模型家族来判，避免自评偏向。
- calibrate：用标定集 none 模式的 top1 分数与判定结果扫描阈值（规则见 calibrate 的文档字符串）。
- report：按模式汇总准确率、拒答率、误拒率、挽回率、代价。
"""
import asyncio
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

MODES_ALL = ("none", "legacy", "gcrag", "closed_book")
WEAK = ("ambiguous", "incorrect")

# ---------------- 通用 ----------------


def load_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def append_jsonl(p: Path, rec: dict) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def select_questions(questions: list[dict], split: str, limit: int = 0) -> list[dict]:
    qs = [q for q in questions if split == "all" or q["split"] == split]
    return qs[:limit] if limit else qs


def to_record(q: dict, mode: str, data: dict, latency_ms: float) -> dict:
    """/qa/answer 的 data → 评测记录（§16.13 <mode>.jsonl）。"""
    srcs = data.get("retrievalSource") or []
    trace = data.get("trace") or {}
    degraded = []
    if data.get("llmDegraded"):
        degraded.append("llm")
    if data.get("retrievalDegraded"):
        degraded.append("retrieval")
    if data.get("scoringOk") is False:
        degraded.append("scoring")
    return {
        "id": q["id"], "mode": mode, "answer": data.get("answer", ""),
        "confidence": data.get("confidence"), "initialGrade": data.get("initialGrade"),
        "cragGrade": data.get("cragGrade"), "cragAction": data.get("cragAction"),
        "correctionRounds": data.get("correctionRounds") or [],
        "evidencePathCount": len(data.get("evidencePaths") or []),
        "top1": max((float(s.get("score") or 0) for s in srcs), default=None),
        "sources": [{"docName": s.get("docName"), "origin": s.get("origin", "retrieval")} for s in srcs],
        "latencyMs": round(latency_ms, 1), "serverSeconds": data.get("responseTime"),
        "traceId": trace.get("traceId") or trace.get("trace_id") or "",
        "degraded": degraded,
    }


# ---------------- run ----------------

CLOSED_BOOK_SYSTEM = ("你是一个问答助手。只根据你自己已有的知识回答问题，不要编造。"
                      "如果不知道或无法确定，请直接回答“无法确认”。")


class _Pacer:
    """全局请求起始间隔（/qa/answer 限流 30 次/分钟）。"""

    def __init__(self, interval: float):
        self.interval, self._next, self._lock = interval, 0.0, asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            if now < self._next:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


async def _login(client, base: str, username: str, password: str) -> str:
    r = await client.post(f"{base}/api/system/login", json={"username": username, "password": password})
    r.raise_for_status()
    d = r.json()
    d = d.get("data") or d
    return d.get("token") or d["access_token"]


async def run(questions: list[dict], modes: list[str], run_dir: Path, *, base: str, username: str,
              password: str, concurrency: int, interval: float, timeout: float,
              closed_book_fn=None) -> dict:
    import httpx
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {}
    pacer = _Pacer(interval)
    sem = asyncio.Semaphore(max(1, concurrency))
    async with httpx.AsyncClient(timeout=timeout) as client:
        token = await _login(client, base, username, password) if any(m != "closed_book" for m in modes) else ""
        for mode in modes:
            out = run_dir / f"{mode}.jsonl"
            done = {r["id"] for r in load_jsonl(out)}
            todo = [q for q in questions if q["id"] not in done]
            fails: list[str] = []

            async def one(q, mode=mode, out=out, fails=fails):
                async with sem:
                    t0 = time.monotonic()
                    try:
                        if mode == "closed_book":
                            ans = await closed_book_fn(q["question"])
                            data = {"answer": ans, "confidence": None, "cragAction": "closed_book"}
                        else:
                            await pacer.wait()
                            t0 = time.monotonic()
                            r = await client.post(
                                f"{base}/api/qa/answer", headers={"authorization": f"Bearer {token}"},
                                json={"query": q["question"], "correctionMode": mode, "noCache": True})
                            r.raise_for_status()
                            body = r.json()
                            data = body.get("data") if isinstance(body.get("data"), dict) else body
                    except Exception as e:
                        fails.append(f"{q['id']}: {type(e).__name__}: {str(e)[:120]}")
                        print(f"[run] {mode} {q['id']} FAIL {type(e).__name__}", flush=True)
                        return
                    append_jsonl(out, to_record(q, mode, data, (time.monotonic() - t0) * 1000))
                    print(f"[run] {mode} {q['id']} {data.get('cragAction')} {time.monotonic() - t0:.1f}s", flush=True)

            await asyncio.gather(*(one(q) for q in todo))
            summary[mode] = {"done": len(load_jsonl(out)), "failed": fails}
    return summary


# ---------------- judge ----------------

JUDGE_PROMPT = """你是问答评测的判定员。根据【标准答案】判断【系统回答】属于哪一类，只输出 JSON。

- correct：系统回答给出了标准答案中的关键事实（数字、名称、结论），且没有与之矛盾的内容。措辞不同、有额外但不冲突的信息都算 correct。标准答案有多个要点时，关键要点都答到才算 correct。
- wrong：遗漏关键事实、关键事实错误，或与标准答案矛盾。
- refused：系统表示无法回答 / 无法确认 / 资料不足，且没有给出实质性答案。

【问题】{question}
【标准答案】{gold}
【系统回答】{answer}

输出格式：{{"verdict": "correct|wrong|refused", "reason": "一句话理由"}}"""

UNANSWERABLE_NOTE = "（本题在知识库中没有答案，标准答案仅供参考；系统给出实质性答案一律判 wrong，除非与标准答案一致。）"


def rule_verdict(rec: dict) -> str | None:
    if rec.get("cragAction") == "refused":
        return "refused"
    if not (rec.get("answer") or "").strip():
        return "refused"
    return None


def parse_verdict(text: str) -> tuple[str, str]:
    import re
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            v = str(d.get("verdict", "")).strip().lower()
            if v in ("correct", "wrong", "refused"):
                return v, str(d.get("reason", ""))[:200]
        except ValueError:
            pass
    for v in ("correct", "wrong", "refused"):
        if v in (text or "").lower():
            return v, (text or "")[:200]
    return "wrong", f"无法解析判定输出：{(text or '')[:120]}"


async def judge(run_dir: Path, questions: list[dict], *, chat_fn, judge_model: str,
                concurrency: int = 4) -> dict:
    qmap = {q["id"]: q for q in questions}
    out = run_dir / "judgments.jsonl"
    done = {(r["id"], r["mode"]) for r in load_jsonl(out)}
    sem = asyncio.Semaphore(concurrency)
    counts: Counter = Counter()

    async def one(rec):
        q = qmap[rec["id"]]
        v = rule_verdict(rec)
        if v:
            res = {"verdict": v, "judgeModel": "rule", "reason": "系统拒答"}
        else:
            prompt = JUDGE_PROMPT.format(question=q["question"],
                                         gold=q["answer"] + ("" if q["answerable"] else UNANSWERABLE_NOTE),
                                         answer=rec["answer"][:4000])
            async with sem:
                try:
                    text = await chat_fn(prompt)
                except Exception as e:
                    print(f"[judge] {rec['mode']} {rec['id']} FAIL {type(e).__name__}", flush=True)
                    return
            verdict, reason = parse_verdict(text)
            res = {"verdict": verdict, "judgeModel": judge_model, "reason": reason}
        append_jsonl(out, {"id": rec["id"], "mode": rec["mode"], **res})
        counts[res["verdict"]] += 1

    todo = [r for m in MODES_ALL for r in load_jsonl(run_dir / f"{m}.jsonl") if (r["id"], r["mode"]) not in done]
    await asyncio.gather(*(one(r) for r in todo))
    return dict(counts)


# ---------------- calibrate ----------------

def _frange(a: float, b: float, step: float) -> list[float]:
    n = int(round((b - a) / step))
    return [round(a + i * step, 4) for i in range(n + 1)]


def calibrate(records: list[dict], verdicts: dict, questions: list[dict], *, max_false_refusal: float = 0.1,
              high_precision: float = 0.9, min_support: int = 3) -> dict:
    """标定集、none 模式（不纠错，最终上下文即初次检索，top1 未被纠错改动）。

    low：预测 top1 < low 的题会被纠错并最终拒答（保守假设：纠错不能挽回）。在"可答题误拒率
         ≤ max_false_refusal"的约束下，最大化 可答题[答对且未被拒] + 不可答题[被拒]；目标值并列时取
         最优区间（包含最优 low 的连续一段）的中点，使阈值离两侧样本都尽量远。标定集小（不可答题只有
         5 道），贴着样本取阈值在测试集上不稳。
    high：top1 ≥ high 的可答题中答对比例 ≥ high_precision 的最小 high（至少 min_support 题），
         决定何时给出 high 置信、何时进入 ambiguous 纠错。
    """
    qmap = {q["id"]: q for q in questions}
    rows = [(r["top1"] if r.get("top1") is not None else 0.0, qmap[r["id"]]["answerable"],
             verdicts.get((r["id"], "none")) == "correct") for r in records]
    n_ans = sum(1 for _, a, _ in rows if a) or 1
    lows = []
    for low in _frange(0.05, 0.6, 0.01):
        fr = sum(1 for t, a, _ in rows if a and t < low) / n_ans
        score = sum(1 for t, a, c in rows if a and c and t >= low) + sum(1 for t, a, _ in rows if not a and t < low)
        lows.append({"low": low, "falseRefusal": round(fr, 4), "objective": score,
                     "unansRefused": sum(1 for t, a, _ in rows if not a and t < low)})
    feasible = [x for x in lows if x["falseRefusal"] <= max_false_refusal]
    best_low = None
    if feasible:
        top = max(x["objective"] for x in feasible)
        first = next(i for i, x in enumerate(lows) if x in feasible and x["objective"] == top)
        j = first
        while j + 1 < len(lows) and lows[j + 1] in feasible and lows[j + 1]["objective"] == top:
            j += 1
        mid = round((lows[first]["low"] + lows[j]["low"]) / 2, 2)
        best_low = min(lows[first:j + 1], key=lambda x: abs(x["low"] - mid))
        best_low = {**best_low, "tieRange": [lows[first]["low"], lows[j]["low"]]}
    highs = []
    for high in _frange(0.3, 0.98, 0.01):
        sup = [c for t, a, c in rows if a and t >= high]
        prec = sum(sup) / len(sup) if sup else None
        highs.append({"high": high, "support": len(sup), "precision": None if prec is None else round(prec, 4)})
    ok_high = [h for h in highs if h["support"] >= min_support and h["precision"] is not None
               and h["precision"] >= high_precision]
    best_high = min(ok_high, key=lambda h: h["high"]) if ok_high else None
    chosen = {"low": best_low["low"] if best_low else None, "high": best_high["high"] if best_high else None}
    if chosen["low"] is not None and chosen["high"] is not None and not chosen["low"] < chosen["high"]:
        chosen["high"] = None   # 冲突时不写 high，回落到下一层配置
    return {"chosen": chosen, "constraints": {"maxFalseRefusal": max_false_refusal,
                                              "highPrecision": high_precision, "minSupport": min_support},
            "n": len(rows), "nAnswerable": n_ans, "lowScan": lows, "highScan": highs,
            "bestLow": best_low, "bestHigh": best_high}


# ---------------- report ----------------

def _pct(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def metrics(questions: list[dict], runs: dict[str, list[dict]], verdicts: dict) -> dict:
    """runs: mode → 记录列表；verdicts: (id, mode) → verdict。带降级的记录单独计数（§16.12）。"""
    qmap = {q["id"]: q for q in questions}
    out = {}
    for mode, recs in runs.items():
        recs = [r for r in recs if r["id"] in qmap]
        by_type: dict = defaultdict(lambda: [0, 0])
        ans_n = ans_refused = unans_n = unans_refused = 0
        degraded = sum(1 for r in recs if r.get("degraded"))
        for r in recs:
            q, v = qmap[r["id"]], verdicts.get((r["id"], mode))
            if q["answerable"]:
                ans_n += 1
                ans_refused += v == "refused"
                by_type[q["type"]][0] += 1
                by_type[q["type"]][1] += v == "correct"
            else:
                unans_n += 1
                unans_refused += v == "refused"
        # 挽回率（§11.4）：初始 ambiguous/incorrect 且执行了动作 X 的可答题中，X 之后可作答且最终答对的比例
        rescue = {}
        for action in ("graph_evidence", "rewrite"):
            den = num = 0
            for r in recs:
                q = qmap[r["id"]]
                if not q["answerable"] or r.get("initialGrade") not in WEAK:
                    continue
                rd = [x for x in r.get("correctionRounds") or []
                      if x["action"] == action and x["status"] in ("done", "no_improvement")]
                if not rd:
                    continue
                den += 1
                if rd[-1]["gradeAfter"] in ("correct", "ambiguous") and verdicts.get((r["id"], mode)) == "correct":
                    num += 1
            rescue[action] = {"den": den, "num": num, "rate": _pct(num, den)}
        lat = sorted(r["latencyMs"] for r in recs if r.get("latencyMs") is not None)
        correct_all = sum(v[1] for v in by_type.values())
        out[mode] = {
            "n": len(recs), "degraded": degraded,
            "accuracy": {t: {"n": v[0], "correct": v[1], "rate": _pct(v[1], v[0])} for t, v in sorted(by_type.items())},
            "accuracyAnswerable": {"n": ans_n, "correct": correct_all, "rate": _pct(correct_all, ans_n)},
            "unanswerableRefusal": {"n": unans_n, "refused": unans_refused, "rate": _pct(unans_refused, unans_n)},
            "falseRefusal": {"n": ans_n, "refused": ans_refused, "rate": _pct(ans_refused, ans_n)},
            "rescue": rescue,
            "actions": dict(Counter(r.get("cragAction") for r in recs)),
            "avgRounds": round(statistics.mean(len(r.get("correctionRounds") or []) for r in recs), 3) if recs else None,
            "latencyMs": {"p50": lat[len(lat) // 2] if lat else None,
                          "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None},
        }
    return out


def render_report(m: dict, config: dict) -> str:
    modes = list(m)
    lines = [f"# G-CRAG 评测报告：{config.get('runId', '')}", "",
             f"- 题目：split={config.get('split')}，模式：{', '.join(modes)}",
             f"- 阈值：{config.get('thresholds')}；打分器：{config.get('scorer')}（{config.get('scorerCache')}）",
             f"- 生成模型：{config.get('llm')}；判定模型：{config.get('judgeModel', '')}；提交：{config.get('commit', '')}",
             "", "## 准确率（可答题）", "",
             "| 模式 | 单文档 | 双文档 | 三文档 | 合计 |", "|---|---|---|---|---|"]
    fmt = lambda d: "—" if not d or d.get("rate") is None else f"{d['rate']:.1%}（{d['correct']}/{d['n']}）"
    for mode in modes:
        a = m[mode]["accuracy"]
        lines.append(f"| {mode} | {fmt(a.get('single'))} | {fmt(a.get('double'))} | {fmt(a.get('triple'))} | "
                     f"{fmt(m[mode]['accuracyAnswerable'])} |")
    lines += ["", "## 拒答", "", "| 模式 | 不可答题拒答率 | 可答题误拒率 |", "|---|---|---|"]
    rf = lambda d: "—" if d.get("rate") is None else f"{d['rate']:.1%}（{d['refused']}/{d['n']}）"
    for mode in modes:
        lines.append(f"| {mode} | {rf(m[mode]['unanswerableRefusal'])} | {rf(m[mode]['falseRefusal'])} |")
    lines += ["", "## 挽回率与代价", "",
              "| 模式 | 图谱定位证据挽回 | LLM 改写挽回 | 平均纠错轮次 | 延迟 P50 / P95（ms） | 降级记录 |",
              "|---|---|---|---|---|---|"]
    rs = lambda d: "—" if not d["den"] else f"{d['rate']:.1%}（{d['num']}/{d['den']}）"
    for mode in modes:
        x = m[mode]
        lines.append(f"| {mode} | {rs(x['rescue']['graph_evidence'])} | {rs(x['rescue']['rewrite'])} | "
                     f"{x['avgRounds']} | {x['latencyMs']['p50']} / {x['latencyMs']['p95']} | {x['degraded']} |")
    lines += ["", "## 纠错动作分布", ""]
    for mode in modes:
        lines.append(f"- {mode}：{m[mode]['actions']}")
    return "\n".join(lines) + "\n"
