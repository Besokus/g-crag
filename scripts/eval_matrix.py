"""开关对照评测矩阵 runner：variants × dims 子进程采集 + 聚合报告。

spec: docs/superpowers/specs/2026-09-01-eval-matrix-design.md
两种用法：
1. 编排模式（默认）：
   python scripts/eval_matrix.py --dims retrieval            # 快速档（需 Milvus+embedding+seed 数据）
   python scripts/eval_matrix.py                             # 全量（生成维另需 LLM key）
   python scripts/eval_matrix.py --variants baseline,hyde,crag_v3 --limit 3
2. 探针模式（内部，由编排模式以 env 覆盖后的子进程调起，勿手工调）：
   python scripts/eval_matrix.py --probe retrieval --json-out reports/x.json [--topk 5]
   python scripts/eval_matrix.py --probe generation --base-url http://127.0.0.1:8011 --json-out ... [--limit 5]

产物：reports/eval_matrix_<ts>/probe_*.json + reports/eval_matrix_<ts>.md + .json
前置：docker compose 起数据服务 + scripts/seed_demo.py 建库（同 eval_retrieval 口径）；
     矩阵实跑需真实服务，定位手动/夜间；CI 只跑其纯函数单测（tests/test_eval_matrix.py）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.services import eval_matrix_service as svc  # noqa: E402


def _write_probe(json_out: Path, dim: str, metrics: dict) -> None:
    """探针结果落盘；env 摘要只留 _ENABLE=true 键做溯源。"""
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps({
        "variant": os.environ.get("EVAL_MATRIX_VARIANT", "unknown"),
        "dim": dim,
        "env": {k: v for k, v in os.environ.items()
                if k.endswith("_ENABLE") and v.lower() == "true"},
        "metrics": metrics,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def run_probe_retrieval(json_out: Path, topk: int) -> int:
    """检索探针：本进程 env 已被父进程覆盖 → settings 即变体配置；直调服务化评测。"""
    from app.db.session import AsyncSessionLocal
    from app.services import retrieval_eval_service

    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            return await retrieval_eval_service.evaluate_over_golden(db, None, topk=topk)

    metrics = asyncio.run(_run())
    _write_probe(json_out, "retrieval", metrics)
    print(f"[probe:retrieval variant={os.environ.get('EVAL_MATRIX_VARIANT', '?')}] "
          f"recall={metrics.get('recall')} sampleSize={metrics.get('sampleSize')}")
    return 0


def run_probe_generation(json_out: Path, base_url: str, limit: int) -> int:
    """生成探针：base_url 指向**父进程已按变体 env 起好的后端**；gate 传 1.01（矩阵只比数值不卡门禁）。"""
    from eval_generation import run_generation_eval

    metrics = asyncio.run(run_generation_eval(base_url=base_url, limit=limit, gate=1.01))
    slim = {k: v for k, v in metrics.items() if k != "rows"}
    _write_probe(json_out, "generation", slim)
    print(f"[probe:generation variant={os.environ.get('EVAL_MATRIX_VARIANT', '?')}] "
          f"faithfulness={metrics.get('faithfulness')}")
    return 0


def start_backend(port: int, env: dict, log_path: Path | None = None) -> subprocess.Popen:
    """起变体后端子进程（uvicorn --app-dir backend，同 AGENTS 开发口径）。

    子进程输出落盘到 log_path（报告目录内），不再 DEVNULL——首跑"后端未就绪"
    因日志被吞排查了一圈才发现是就绪探针自身 bug。
    """
    if log_path is None:
        log_path = Path(os.environ.get("EVAL_MATRIX_LOG_DIR", "reports")) / f"_backend_{port}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--app-dir", "backend"],
        cwd=str(ROOT), env=env,
        stdout=open(log_path, "wb"), stderr=subprocess.STDOUT,
    )


def _login_probe(base_url: str) -> bool:
    """就绪探针：HTTP 层有任何响应即视为就绪。

    首跑教训（2026-09-03 实跑）：原实现断言 `status_code == 200`，但空 body 的
    POST /login 会被 FastAPI 参数校验直接回 422（BizError 约定只管业务层错误，
    请求体验证错误仍是 4xx）——探针永远 False，生成维后端被误判"未就绪"。
    422 本身已证明 uvicorn 在监听，探针语义应为"端口可达"而非"登录成功"。

    trust_env=False：Windows 系统代理（注册表级）会被 httpx 默认信任，把 127.0.0.1
    请求也转发给 loopback-only 代理 → 502 空 body——首跑"后端未就绪"的真正根因
    （本机回环目标永远不该走代理）。
    """
    import httpx

    try:
        httpx.post(f"{base_url}/api/system/login", json={}, timeout=5, trust_env=False)
        return True
    except Exception:
        return False


def wait_backend_ready(base_url: str, timeout_s: float, prober=None,
                       interval_s: float = 2.0) -> bool:
    """轮询到就绪；prober 可注入供单测。bge 预热 ~20s，编排默认等 240s。"""
    deadline = time.time() + timeout_s
    prober = prober or _login_probe
    while time.time() < deadline:
        if prober(base_url):
            return True
        time.sleep(interval_s)
    return False


def stop_backend(proc: subprocess.Popen) -> None:
    """terminate→wait(10)→kill 兜底；已退出进程跳过。"""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _run_probe_sync(cli_args: list[str], env: dict, timeout_s: int = 1800) -> bool:
    """以 env 覆盖后的子进程跑探针；utf-8 强制与超时兜底同 eval_suite.run_dim。

    timeout 默认 1800s 按纯检索口径定；LLM 逐条调用的变体（hyde/multi_query/self_rag
    的改写分支）在 golden 扩充后（109 条）单条 ~15-25s，1800s 不够——编排层按
    --probe-timeout 传入。
    """
    cmd = [sys.executable, str(ROOT / "scripts" / "eval_matrix.py")] + cli_args
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(f"    探针超时（{timeout_s}s）")
        return False
    if r.returncode != 0:
        print((r.stdout or "")[-800:])
        print((r.stderr or "")[-300:])
    return r.returncode == 0


def _run_generation_with_backend(v: dict, json_out: Path, env: dict, port: int, args) -> bool:
    """起变体后端 → 等就绪 → generation 探针 → 停后端；未就绪跳过不中断整场。"""
    base_url = f"http://127.0.0.1:{port}"
    print(f">>> 起变体后端 {v['name']} @:{port} ...")
    proc = start_backend(port, env, log_path=json_out.parent / f"_backend_{v['name']}_{port}.log")
    try:
        if not wait_backend_ready(base_url, args.health_timeout):
            print(f"    后端未就绪（>{args.health_timeout}s），跳过 {v['name']}")
            return False
        return _run_probe_sync(
            ["--probe", "generation", "--json-out", str(json_out),
             "--base-url", base_url, "--limit", str(args.limit)], env,
            timeout_s=args.probe_timeout)
    finally:
        stop_backend(proc)


def main_with_args(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="开关对照评测矩阵（variants × dims → 能力收益矩阵）")
    ap.add_argument("--dims", default="retrieval,generation")
    ap.add_argument("--variants", default="all", help="all 或逗号名单（baseline 恒含）")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--limit", type=int, default=5, help="生成维每变体评测条数（控 LLM 成本）")
    ap.add_argument("--out-dir", default=str(ROOT / "reports"))
    ap.add_argument("--backend-port-base", type=int, default=8010)
    ap.add_argument("--health-timeout", type=float, default=240.0,
                    help="后端子进程就绪等待秒（bge 预热 ~20s）")
    ap.add_argument("--probe-timeout", type=int, default=3600,
                    help="单个探针子进程超时秒（LLM 逐条变体在 golden 扩充后需要 >1800s）")
    ap.add_argument("--probe", choices=["retrieval", "generation"], default=None,
                    help=argparse.SUPPRESS)
    ap.add_argument("--base-url", default="", help=argparse.SUPPRESS)
    ap.add_argument("--json-out", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.probe:
        if not args.json_out:
            print("探针模式必须给 --json-out")
            return 2
        if args.probe == "retrieval":
            return run_probe_retrieval(Path(args.json_out), args.topk)
        if not args.base_url:
            print("generation 探针必须给 --base-url")
            return 2
        return run_probe_generation(Path(args.json_out), args.base_url, args.limit)

    dims = {d.strip() for d in args.dims.split(",") if d.strip()}
    unknown = dims - {"retrieval", "generation"}
    if unknown:
        print(f"不支持维度: {','.join(sorted(unknown))}（可选 retrieval,generation）")
        return 2
    try:
        variants = svc.select_variants(args.variants, dims)
    except ValueError as e:
        print(str(e))
        return 2

    out_base = Path(args.out_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    outdir = out_base / f"eval_matrix_{ts}"
    probes: list[dict] = []
    port = args.backend_port_base
    for v in variants:
        env = svc.build_env_overlay(v, dict(os.environ))
        env["EVAL_MATRIX_VARIANT"] = v["name"]
        for dim in v["dims"]:
            json_out = outdir / f"probe_{v['name']}_{dim}.json"
            if dim == "retrieval":
                print(f">>> 探针 {v['name']}/{dim}")
                ok = _run_probe_sync(
                    ["--probe", "retrieval", "--json-out", str(json_out),
                     "--topk", str(args.topk)], env, timeout_s=args.probe_timeout)
            else:
                ok = _run_generation_with_backend(v, json_out, env, port, args)
                port += 1
            if ok:
                probes.append(json.loads(json_out.read_text(encoding="utf-8")))
            else:
                print(f"    {v['name']}/{dim}: 探针失败（详见上方输出）")

    if not probes:
        print("无成功探针，无法聚合")
        return 1

    agg = svc.aggregate(probes)
    meta = {
        "envSummary": f"provider={os.environ.get('LLM_PROVIDER', '')}",
        "topk": args.topk, "limit": args.limit,
        "goldenSize": next((p["metrics"].get("sampleSize") for p in probes
                            if p["dim"] == "retrieval"), None),
    }
    md_path = out_base / f"eval_matrix_{ts}.md"
    json_path = out_base / f"eval_matrix_{ts}.json"
    md_path.write_text(svc.render_markdown(agg, meta), encoding="utf-8")
    json_path.write_text(json.dumps({"meta": meta, "matrix": agg},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== 矩阵完成：报告 {md_path} | JSON {json_path} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main_with_args())
