from __future__ import annotations

import asyncio
import socket
import traceback
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import SessionLocal
from .models import Task


async def enqueue(task_type: str, payload: dict, *, idempotency_key: str | None = None) -> Task:
    async with SessionLocal() as db:
        if idempotency_key:
            existing = (await db.execute(select(Task).where(
                Task.task_type == task_type, Task.idempotency_key == idempotency_key
            ))).scalar_one_or_none()
            if existing:
                return existing
        row = Task(task_type=task_type, payload=payload, idempotency_key=idempotency_key)
        db.add(row)
        try:
            await db.commit()
            await db.refresh(row)
            return row
        except IntegrityError:
            await db.rollback()
            if not idempotency_key:
                raise
            return (await db.execute(select(Task).where(
                Task.task_type == task_type, Task.idempotency_key == idempotency_key
            ))).scalar_one()


async def claim() -> Task | None:
    async with SessionLocal() as db:
        now = datetime.now()
        stale_before = now - timedelta(seconds=max(60, settings.TASK_LOCK_TIMEOUT_SECONDS))
        await db.execute(update(Task).where(
            Task.status == "running", Task.locked_at < stale_before, Task.attempts < Task.max_attempts,
        ).values(status="queued", locked_by="", locked_at=None, run_after=now))
        await db.execute(update(Task).where(
            Task.status == "running", Task.locked_at < stale_before, Task.attempts >= Task.max_attempts,
        ).values(status="failed", finished_at=now, last_error="worker lease expired"))
        candidate = select(Task.id).where(
            Task.status == "queued", Task.run_after <= now,
        ).order_by(Task.run_after, Task.created_at).limit(1).scalar_subquery()
        task_id = (await db.execute(update(Task).where(
            Task.id == candidate, Task.status == "queued",
        ).values(
            status="running", attempts=Task.attempts + 1,
            locked_by=socket.gethostname(), locked_at=now,
        ).returning(Task.id))).scalar_one_or_none()
        await db.commit()
        if not task_id:
            return None
        return await db.get(Task, task_id)


async def finish(task_id: str, *, error: str = "") -> None:
    async with SessionLocal() as db:
        row = await db.get(Task, task_id)
        if not row:
            return
        if error and row.attempts < row.max_attempts:
            row.status = "queued"
            row.last_error = error[-8000:]
            row.run_after = datetime.now() + timedelta(seconds=min(60, 2 ** row.attempts))
            row.locked_by = ""
            row.locked_at = None
        else:
            row.status = "failed" if error else "succeeded"
            row.last_error = error[-8000:]
            row.finished_at = datetime.now()
        await db.commit()


async def dispatch(task: Task) -> None:
    if task.task_type == "dream":
        from .dreamer import dream
        async with SessionLocal() as db:
            await dream(db)
    elif task.task_type == "run":
        from .replay import execute_run
        await execute_run(task.payload["run_id"])
    elif task.task_type == "retention":
        from .retention import purge_expired
        await purge_expired()
    else:
        raise ValueError(f"unknown task type: {task.task_type}")


async def worker_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        task = await claim()
        if not task:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass
            continue
        try:
            await dispatch(task)
            await finish(task.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await finish(task.id, error=traceback.format_exc())
