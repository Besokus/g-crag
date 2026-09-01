#!/usr/bin/env python3
"""LLM as User 套件 · 主动测试脚本。

做两件事：
1. 种子数据：用套件自己的 ORM 直接落 Scenario + ScenarioVersion
   （/v1/scenarios 由 Dreamer 每 6h 聚类生成，测试期等不起 → 直插种子，幂等可重复跑）
2. 主动打 API：health / scenarios / sessions / telemetry events / runs / evaluations / reports，
   逐步打印 PASS/FAIL 证据。

用法（在 grid 项目根目录）：
    PYTHONPATH="<grid>/llm-user-suite;<grid>/backend" LLM_USER_AUTH_DISABLED=true \
        .venv-llm-user/Scripts/python.exe llm-user-suite/scripts/active_test.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # grid 项目根目录
os.chdir(ROOT)
for p in (ROOT / "llm-user-suite", ROOT / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import httpx  # noqa: E402

BASE = os.environ.get("LLM_USER_TEST_BASE", "http://127.0.0.1:8080")
_results: list[tuple[str, bool, str]] = []


def report(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {detail}", flush=True)


async def seed_scenarios() -> int:
    """幂等落 Scenario 种子，返回当前 scenario 总数。"""
    from llm_user_suite.db import SessionLocal, init_db
    from llm_user_suite.models import Scenario, ScenarioVersion
    from sqlalchemy import func, select

    await init_db()
    seeds = [
        (
            "RAG 检索失败降级",
            {
                "steps": [
                    {"intent": "检索服务降级时是否正确拒答/兜底", "hint_level": 0},
                    {"intent": "是否有明确的降级策略提示", "hint_level": 1},
                ]
            },
        ),
        (
            "多轮对话上下文丢失",
            {"steps": [{"intent": "第二轮指代'它'是否能正确消解到上文的实体", "hint_level": 0}]},
        ),
    ]
    async with SessionLocal() as db:
        n = (await db.execute(select(func.count()).select_from(Scenario))).scalar()
        if n:
            print(f"[seed] 已有 {n} 个 scenario，跳过（幂等）", flush=True)
            return int(n)
        for name, spec in seeds:
            sig = hashlib.sha1(name.encode("utf-8")).hexdigest()[:32]
            s = Scenario(name=name, signature=sig, status="draft")
            db.add(s)
            await db.flush()
            db.add(
                ScenarioVersion(
                    scenario_id=s.id, version=1, status="draft",
                    spec=spec, source_session_ids=[],
                )
            )
        await db.commit()
        print(f"[seed] 写入 {len(seeds)} 个 scenario + 对应 version", flush=True)
        return len(seeds)


async def main() -> int:
    seed_n = await seed_scenarios()

    async with httpx.AsyncClient(timeout=20) as c:
        # ── 1. 健康 ──
        r = await c.get(f"{BASE}/health")
        report("GET /health", r.status_code == 200, r.text[:80])

        # ── 2. 场景列表（种子后必须非空）──
        r = await c.get(f"{BASE}/v1/scenarios")
        items = r.json() if r.status_code == 200 else []
        ok = r.status_code == 200 and len(items) >= seed_n
        report("GET /v1/scenarios 非空", ok, f"共 {len(items)} 个")

        # ── 3. 场景详情 ──
        if items:
            sid = items[0]["id"]
            r = await c.get(f"{BASE}/v1/scenarios/{sid}")
            report("GET /v1/scenarios/{id} 详情", r.status_code == 200,
                   f"id={sid} name={r.json().get('name','') if r.status_code==200 else ''}")

        # ── 4. 喂行为事件（telemetry intake）──
        events = [
            {
                "kind": "qa.completed", "sessionId": f"t-sess-{i}", "traceId": f"t-trace-{i}",
                "method": "POST", "path": "/api/qa", "statusCode": 200,
                "durationMs": 620 + i * 13, "payload": {"question": "什么是RAG？", "route": "hybrid"},
            }
            for i in range(2)
        ]
        r = await c.post(f"{BASE}/v1/telemetry/events", json=events)
        report("POST /v1/telemetry/events 喂事件", r.status_code in (200, 202), r.text[:80])

        r = await c.get(f"{BASE}/v1/sessions")
        sessions = r.json() if r.status_code == 200 else []
        report("GET /v1/sessions 会话已入", r.status_code == 200 and len(sessions) >= 2,
               f"共 {len(sessions)} 个会话")

        # ── 5. 审核通过种子 scenario 的 version → 发起一个回放 Run ──
        run_id = ""
        if items:
            sid = items[0]["id"]
            r2 = await c.get(f"{BASE}/v1/scenarios/{sid}")
            j2 = r2.json() if r2.status_code == 200 else {}
            version_id = (j2.get("versions") or [{}])[0].get("id")
            if version_id:
                rv = await c.post(f"{BASE}/v1/scenarios/{sid}/review", json={"action": "approve"})
                report("POST /v1/scenarios/{id}/review 审核通过", rv.status_code == 200, rv.text[:80])
                r = await c.post(f"{BASE}/v1/runs", json={
                    "scenarioVersionId": version_id,
                    "targetBaseUrl": "http://localhost:8001",
                    "environment": "test",
                })
                run_id = (r.json().get("id") if r.status_code in (200, 201) else "")
                report("POST /v1/runs 创建回放", r.status_code in (200, 201) and bool(run_id),
                       f"run_id={run_id}")

        r = await c.get(f"{BASE}/v1/runs")
        runs = r.json() if r.status_code == 200 else []
        report("GET /v1/runs 回放列表", r.status_code == 200 and len(runs) >= 1, f"共 {len(runs)} 个")

        # ── 6. 评测与报告 ──
        r = await c.get(f"{BASE}/v1/evaluations")
        report("GET /v1/evaluations", r.status_code == 200, f"HTTP {r.status_code}")

        r = await c.get(f"{BASE}/v1/reports")
        reports = r.json() if r.status_code == 200 else []
        report("GET /v1/reports", r.status_code == 200, f"共 {len(reports)} 份")

    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n===== 汇总 {passed}/{len(_results)} PASS =====", flush=True)
    if passed < len(_results):
        for name, ok, detail in _results:
            if not ok:
                print(f"  FAIL -> {name}: {detail}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
