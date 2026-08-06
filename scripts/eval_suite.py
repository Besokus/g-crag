"""RAG 离线评测套件：一键跑 检索+生成+引用 三维度，汇总报告 + 总门禁（CI 用）。

复用现有 scripts/eval_retrieval.py / eval_generation.py / eval_citation.py（各自已含
CI 门禁 + md 报告）。本套件串行调起 + 汇总三维度到一个总报告 + 综合门禁
（任一维度 FAIL → 套件 FAIL，退出码 1，供 CI 卡点）。

底层逻辑：不重写各维度评测（它们已成熟），只做"统一入口 + 汇总 + 总门禁"这层缺失的编排。

用法：
  python scripts/eval_suite.py                         # 跑全 3 维度（默认 args）
  python scripts/eval_suite.py --dims retrieval        # 只跑检索
  python scripts/eval_suite.py --generation-args "--limit 10"   # 透传某维度 args

依赖：backend 运行中（generation/citation 走 HTTP+LLM）+ backend/data/golden_qa.json。
     单维脚本各自有 PYTHONPATH/venv 要求，本套件透传同一 python 解释器。
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

# (维度名, 脚本, 默认透传 args)
DIMS = [
    ("retrieval", "scripts/eval_retrieval.py", ""),
    ("generation", "scripts/eval_generation.py", "--limit 5"),
    ("citation", "scripts/eval_citation.py", ""),
]


def run_dim(name: str, script: str, args: str, timeout: int = 900) -> dict:
    """跑单个维度脚本，返回 {ok, dur, stdout_tail, stderr_tail}。"""
    cmd = [sys.executable, script] + (args.split() if args else [])
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8")
        ok = r.returncode == 0
        out, err = r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as e:
        ok, out, err = False, (e.stdout or ""), f"TIMEOUT ({timeout}s)"
    except Exception as e:
        ok, out, err = False, "", f"{type(e).__name__}: {e}"
    return {
        "name": name, "ok": ok, "dur": round(time.time() - t0, 1),
        "stdout_tail": (out or "")[-800:], "stderr_tail": (err or "")[-300:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="RAG 离线评测套件（检索+生成+引用 汇总+门禁）")
    ap.add_argument("--dims", default="retrieval,generation,citation",
                    help="跑哪些维度（逗号分隔；默认全跑）")
    ap.add_argument("--retrieval-args", default="", help="透传 eval_retrieval 的 args")
    ap.add_argument("--generation-args", default="--limit 5", help="透传 eval_generation 的 args")
    ap.add_argument("--citation-args", default="", help="透传 eval_citation 的 args")
    args = ap.parse_args()

    wanted = {d.strip() for d in args.dims.split(",") if d.strip()}
    extra = {"retrieval": args.retrieval_args, "generation": args.generation_args,
             "citation": args.citation_args}
    tasks = [(n, s, extra.get(n, a)) for (n, s, a) in DIMS if n in wanted]
    if not tasks:
        print(f"无匹配维度：{args.dims}（可选：retrieval,generation,citation）")
        return 2

    print(f"=== RAG 离线评测套件：{','.join(n for n, _, _ in tasks)} ===\n")
    results = []
    for n, s, a in tasks:
        print(f">>> 跑 {n}: python {s} {a}".rstrip())
        r = run_dim(n, s, a)
        results.append(r)
        print(f"    {n}: {'PASS' if r['ok'] else 'FAIL'} ({r['dur']}s)\n")

    all_ok = all(r["ok"] for r in results)

    # 汇总报告
    ts = time.strftime("%Y%m%d_%H%M%S")
    report = Path(f"reports/eval_suite_{ts}.md")
    report.parent.mkdir(exist_ok=True)
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"# RAG 离线评测套件报告 {ts}\n\n")
        f.write(f"**总门禁：{'✅ 全过' if all_ok else '❌ 有维度 FAIL'}**\n\n")
        f.write("| 维度 | 结果 | 耗时 |\n|---|---|---|\n")
        for r in results:
            f.write(f"| {r['name']} | {'✅ PASS' if r['ok'] else '❌ FAIL'} | {r['dur']}s |\n")
        f.write("\n---\n\n")
        for r in results:
            f.write(f"## {r['name']}（{'PASS' if r['ok'] else 'FAIL'}, {r['dur']}s）\n\n```\n{r['stdout_tail']}\n```\n")
            if r["stderr_tail"].strip():
                f.write(f"\nstderr 尾部：\n```\n{r['stderr_tail']}\n```\n")
            f.write("\n")

    print(f"=== 套件完成：{'✅ 全过' if all_ok else '❌ 有 FAIL'} | 报告：{report} ===")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
