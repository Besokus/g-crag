from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from .api import router
from .config import settings
from .db import SessionLocal, init_db
from .metric_store import scrape_prometheus
from .task_queue import enqueue, worker_loop
from .telemetry import close_idle_sessions


async def scheduler_loop(stop: asyncio.Event) -> None:
    last_dream_bucket = -1
    last_retention_day = -1
    last_scrape_bucket = -1
    last_runner_reconcile_bucket = -1
    while not stop.is_set():
        async with SessionLocal() as db:
            await close_idle_sessions(db)
        dream_bucket = int(time.time() // max(60, settings.DREAM_INTERVAL_SECONDS))
        if dream_bucket != last_dream_bucket:
            await enqueue("dream", {}, idempotency_key=f"dream:{dream_bucket}")
            last_dream_bucket = dream_bucket
        retention_day = int(time.time() // 86400)
        if retention_day != last_retention_day:
            await enqueue("retention", {}, idempotency_key=f"retention:{retention_day}")
            last_retention_day = retention_day
        scrape_bucket = int(time.time() // max(30, settings.METRICS_SCRAPE_INTERVAL_SECONDS))
        if settings.METRICS_SCRAPE_URL and scrape_bucket != last_scrape_bucket:
            try:
                await scrape_prometheus()
            except Exception:
                from . import metrics
                metrics.METRIC_SCRAPES.labels("failed").inc()
            last_scrape_bucket = scrape_bucket
        runner_bucket = int(time.time() // max(15, settings.RUNNER_RECONCILE_INTERVAL_SECONDS))
        if settings.RUNNER_BACKEND == "kubernetes" and runner_bucket != last_runner_reconcile_bucket:
            try:
                from .k8s_runner import reconcile
                await reconcile()
            except Exception:
                pass
            last_runner_reconcile_bucket = runner_bucket
        try:
            from .run_service import dispatch_pending_runs
            await dispatch_pending_runs()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    stop = asyncio.Event()
    tasks = []
    if settings.ROLE in {"all", "worker"}:
        tasks.append(asyncio.create_task(worker_loop(stop), name="llm-user-worker"))
    if settings.ROLE in {"all", "scheduler"}:
        tasks.append(asyncio.create_task(scheduler_loop(stop), name="llm-user-scheduler"))
    yield
    stop.set()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        database = "up"
    except Exception:
        database = "down"
    payload = {
        "status": "healthy" if database == "up" else "unhealthy",
        "version": settings.APP_VERSION,
        "role": settings.ROLE,
        "database": database,
    }
    return JSONResponse(payload, status_code=200 if database == "up" else 503)


@app.get("/metrics")
async def prometheus_metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
