"""生成质量评测：端到端 faithfulness（LLM-judge 答案支撑率）+ 门禁（S1）。

对 golden_qa.json 每条：1) POST /qa/answer 拿答案 2) judge_hallucination 判定支撑率。
平均 supported_ratio < FAITHFULNESS_GATE(默认 0.85) → exit 1（CI 深度门禁）。

与 eval_retrieval 互补：retrieval 测"找得准不准"（recall），generation 测"答得可信不可信"
（faithfulness/真实幻觉）。需后端运行 + LLM，按需手动/夜间跑。

运行: python scripts/eval_generation.py [--limit 10] [--gate 0.85] [--base-url http://127.0.0.1:8001]
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, "backend")
BASE = "http://127.0.0.1:8001"
GOLDEN = Path(__file__).resolve().parent.parent / "backend" / "data" / "golden_qa.json"


async def run_generation_eval(base_url: str = BASE, limit: int = 10, gate: float = 0.85) -> dict:
    """可导入评测核心：POST /api/qa/answer × golden 前 limit 条 + LLM-judge 支撑率。

    返回 {faithfulness, hallucination, avgLatencyMs, sampleSize, rows, gate, pass}；
    CLI main() 薄壳打印并按 pass 退出。矩阵探针复用本函数（base_url 指向变体后端）。
    """
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[:limit]
    print(f"===== 生成质量评测 faithfulness（{len(golden)} 条）=====")
    from app.config import settings
    from app.rag.judge import judge_hallucination

    rows, sup_sum, hall_sum, lat_sum = [], 0.0, 0.0, 0.0
    async with httpx.AsyncClient(timeout=120) as c:
        token = (
            await c.post(f"{base_url}/api/system/login",
                         json={"username": "admin", "password": "admin123"})
        ).json()["data"]["token"]
        H = {"Authorization": "Bearer " + token}
        for it in golden:
            q = it["query"]
            t0 = time.perf_counter()
            r = (
                await c.post(f"{base_url}/api/qa/answer", headers=H,
                             json={"query": q, "modelType": "deepseek"})
            ).json()["data"]
            latency_ms = (time.perf_counter() - t0) * 1000
            sources = [s.get("text", "") for s in r.get("retrievalSource", [])]
            j = await judge_hallucination(r["answer"], sources, settings.LLM_PROVIDER)
            sup_sum += j["supported_ratio"]
            hall_sum += j["hallucination"]
            lat_sum += latency_ms
            rows.append({"query": q, "support": j["supported_ratio"],
                         "hallucination": j["hallucination"], "latencyMs": round(latency_ms)})
            print(f'  支撑={j["supported_ratio"]:.2f} 幻觉={j["hallucination"]:.2f} | {q[:24]}')

    n = len(rows)
    avg_sup = sup_sum / n if n else 0.0
    avg_halluc = hall_sum / n if n else 0.0
    return {
        "faithfulness": round(avg_sup, 4), "hallucination": round(avg_halluc, 4),
        "avgLatencyMs": round(lat_sum / n if n else 0.0), "sampleSize": n, "rows": rows,
        "gate": gate, "pass": avg_sup >= gate,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="评测条数（每条 1 次 LLM，控制成本）")
    ap.add_argument("--gate", type=float, default=0.85, help="平均支撑率门禁（低于 exit 1）")
    ap.add_argument("--base-url", default=BASE, help="后端地址（矩阵探针指向变体后端）")
    args = ap.parse_args()

    r = await run_generation_eval(base_url=args.base_url, limit=args.limit, gate=args.gate)
    print(f"\n平均支撑率 = {r['faithfulness']:.2%} | 平均幻觉率 = {r['hallucination']:.2%}"
          f" | 平均时延 = {r['avgLatencyMs']:.0f}ms")
    print(f"门禁 {args.gate:.0%} → {'✓ PASS' if r['pass'] else '✗ FAIL'}")
    sys.exit(0 if r["pass"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
