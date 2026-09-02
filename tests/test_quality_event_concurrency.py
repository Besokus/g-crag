from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import mysql

from app.services import quality_event_service


class _Result:
    def __init__(self, row):
        self.row = row

    def scalar_one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.row)

    async def commit(self):
        return None

    async def refresh(self, row):
        return None

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_status_history_read_modify_write_uses_row_lock():
    seed = {
        "from": "open",
        "to": "open",
        "operator": "seed",
        "note": "existing",
        "at": "2026-07-27T08:00:00",
    }
    row = SimpleNamespace(
        id="quality-1",
        tenant="tenant-a",
        status="pending",
        payload={"management": {"history": [seed]}},
        handled_at=None,
    )
    db = _Session(row)

    await quality_event_service.update_status(
        db,
        row.id,
        tenant_id="tenant-a",
        status="processing",
        operator="operator-a",
        note="triage",
    )
    await quality_event_service.update_status(
        db,
        row.id,
        tenant_id="tenant-a",
        status="resolved",
        operator="operator-b",
        note="fixed",
    )

    for statement in db.statements:
        sql = str(statement.compile(dialect=mysql.dialect()))
        assert "FOR UPDATE" in sql

    history = row.payload["management"]["history"]
    assert [(item["from"], item["to"]) for item in history] == [
        ("open", "open"),
        ("open", "processing"),
        ("processing", "resolved"),
    ]
    assert row.payload["management"]["operator"] == "operator-b"
    assert row.handled_at is not None
