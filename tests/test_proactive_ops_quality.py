from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models.realtime_event import ProactiveOpsRun, RealtimeEvent
from app.models.ticket import Ticket, TicketStatus, TicketType
from app.services import realtime_event_service, ticket_lifecycle_service


@pytest_asyncio.fixture
async def proactive_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    RealtimeEvent.__table__,
                    ProactiveOpsRun.__table__,
                    Ticket.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _event(event_id: str) -> RealtimeEvent:
    return RealtimeEvent(
        id=f"db-{event_id}",
        tenant_id="tenant-a",
        event_id=event_id,
        source="scada",
        event_type="alarm",
        severity="critical",
        title="1号主变油温越限",
        summary="顶层油温持续升高",
        source_device_id="T1",
        canonical_device_id="SUB-A:T1",
        canonical_device_name="1号主变",
        device_type="main_transformer",
        station="A站",
        device_mapped=True,
        occurred_at=datetime(2026, 7, 27, 10, 0),
        last_received_at=datetime(2026, 7, 27, 10, 0),
        payload_json="{}",
        normalized_json="{}",
        processing_status="queued",
        rule_decision="trigger",
        rule_reason="critical alarm",
    )


@pytest.mark.asyncio
async def test_process_proactive_run_persists_quality_score(
    proactive_db,
    monkeypatch,
):
    event = _event("QUALITY-1")
    run = ProactiveOpsRun(
        id="run-quality-1",
        tenant_id="tenant-a",
        event_ref_id=event.id,
        triggered_by="operator-a",
        status="queued",
        risk_level="critical",
        execution_mode="read_only",
        requires_human_review=True,
        control_executed=False,
    )
    async with proactive_db() as db:
        db.add_all([event, run])
        await db.commit()

    async def fake_get_persona(name):
        assert name == "alert"
        return SimpleNamespace(
            allowed_tools=[
                "search_regulation",
                "query_equipment_graph",
                "search_similar_case",
            ],
        )

    async def fake_run_agent(db, persona, prompt, model_type, ctx):
        return SimpleNamespace(
            answer={
                "summary": "现场核验冷却系统并持续监测",
                "diagnosis": "主变冷却能力不足",
                "handling": "检查冷却器并记录温度变化",
                "urgency": "immediate",
                "riskLevel": "critical",
                "risks": ["温升持续可能导致保护动作"],
                "ticket": {
                    "task": "核验主变冷却系统",
                    "device": "1号主变",
                    "location": "A站",
                    "steps": ["核对设备", "检查冷却器"],
                    "safety": ["保持安全距离"],
                    "risks": ["误碰带电设备"],
                },
            },
            steps=[{"tool": "search_regulation"}],
            tools_used=["search_regulation"],
            iterations=2,
            degraded=False,
            degrade_reason="",
            latency_ms=10,
        )

    async def fake_publish_event_record(*args, **kwargs):
        return None

    from app.services import agent_runtime, event_center_service, persona_store

    monkeypatch.setattr(persona_store, "get_persona", fake_get_persona)
    monkeypatch.setattr(agent_runtime, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        event_center_service,
        "publish_event_record",
        fake_publish_event_record,
    )

    async with proactive_db() as db:
        result = await realtime_event_service.process_proactive_run(
            db,
            run.id,
            tenant_id="tenant-a",
        )

    async with proactive_db() as db:
        stored = await db.get(ProactiveOpsRun, run.id)

    detail = json.loads(stored.quality_detail_json)
    assert result["qualityScore"] == 100
    assert stored.quality_score == 100
    assert stored.quality_score_version == realtime_event_service.QUALITY_SCORE_VERSION
    assert detail["totalScore"] == 100
    assert detail["dimensions"]["evidenceSufficiency"]["score"] == 40
    assert detail["dimensions"]["riskUrgencyAlignment"]["score"] == 25
    assert detail["dimensions"]["actionability"]["score"] == 35
    assert result["recommendation"]["riskLevel"] == "critical"
    assert detail["riskDeclaration"] == {
        "present": True,
        "valid": True,
        "value": "critical",
        "normalized": "critical",
        "eventSeverity": "critical",
        "distance": 0,
    }


@pytest.mark.parametrize(
    ("declared_risk", "expected_score", "reason_fragment", "distance"),
    [
        ("critical", 25, "一致", 0),
        ("warning", 10, "偏差较大", 2),
        ("", 10, "缺少事件或建议风险等级", None),
    ],
)
def test_quality_score_uses_explicit_agent_risk_declaration(
    declared_risk,
    expected_score,
    reason_fragment,
    distance,
):
    detail = realtime_event_service.score_proactive_recommendation(
        event_severity="critical",
        risk_level=declared_risk,
        recommendation={
            "urgency": "immediate",
            "risks": ["保护动作风险"],
        },
        evidence={},
        ticket_draft={},
    )

    dimension = detail["dimensions"]["riskUrgencyAlignment"]
    assert dimension["score"] == expected_score
    assert reason_fragment in dimension["reason"]
    assert detail["riskDeclaration"]["present"] is bool(declared_risk)
    assert detail["riskDeclaration"]["distance"] == distance


def test_agent_risk_declaration_accepts_camel_and_snake_case_only():
    assert realtime_event_service._explicit_risk_level(
        {"riskLevel": "MAJOR"}
    ) == "major"
    assert realtime_event_service._explicit_risk_level(
        {"risk_level": "warning"}
    ) == "warning"
    assert realtime_event_service._explicit_risk_level(
        {"riskLevel": "unknown"}
    ) == ""
    assert realtime_event_service._explicit_risk_level({}) == ""


@pytest.mark.asyncio
async def test_proactive_ticket_lifecycle_backwrites_tenant_scoped_timeline(
    proactive_db,
    monkeypatch,
):
    event = _event("TICKET-1")
    event.processing_status = "completed"
    run = ProactiveOpsRun(
        id="run-ticket-1",
        tenant_id="tenant-a",
        event_ref_id=event.id,
        triggered_by="operator-a",
        status="confirmed",
        risk_level="critical",
        ticket_draft_json=json.dumps(
            {
                "ticketType": "操作票",
                "task": "核验主变冷却系统",
                "device": "1号主变",
                "location": "A站",
                "steps": ["核对设备", "检查冷却器"],
                "safety": ["保持安全距离"],
                "risks": ["误碰带电设备"],
            },
            ensure_ascii=False,
        ),
        execution_mode="read_only",
        requires_human_review=True,
        control_executed=False,
    )
    async with proactive_db() as db:
        db.add_all([event, run])
        await db.commit()

        created = await realtime_event_service.run_to_ticket(
            db,
            run.id,
            tenant_id="tenant-a",
            creator="operator-a",
        )
        ticket_id = created["ticket"]["id"]
        source_ref = created["ticket"]["sourceRef"]
        assert created["run"]["ticketStatus"] == "draft"
        assert [item["status"] for item in created["run"]["ticketTimeline"]] == [
            "draft"
        ]

        assert await realtime_event_service.sync_proactive_ticket_status(
            db,
            ticket_id=ticket_id,
            tenant_id="tenant-a",
            source_ref=source_ref,
            status="draft",
            action="create",
            actor="operator-a",
        ) is False
        assert await realtime_event_service.sync_proactive_ticket_status(
            db,
            ticket_id=ticket_id,
            tenant_id="tenant-b",
            source_ref=source_ref,
            status="issued",
            action="issue",
            actor="other-tenant",
        ) is False

        async def low_score_audit(*args, **kwargs):
            return {"score": 70, "items": []}

        monkeypatch.setattr(
            ticket_lifecycle_service.ticket_audit_service,
            "audit_ticket",
            low_score_audit,
        )
        submitted = await ticket_lifecycle_service.submit_for_review(
            db,
            ticket_id,
            tenant="tenant-a",
        )
        assert submitted["status"] == "pending_review"
        await ticket_lifecycle_service.review_ticket(
            db,
            ticket_id,
            approved=True,
            reviewer="reviewer-a",
            tenant="tenant-a",
        )
        await ticket_lifecycle_service.issue_ticket(
            db,
            ticket_id,
            issuer="issuer-a",
            tenant="tenant-a",
        )
        await ticket_lifecycle_service.start_execution(
            db,
            ticket_id,
            executor="executor-a",
            supervisor="supervisor-a",
            tenant="tenant-a",
        )
        await ticket_lifecycle_service.complete_execution(
            db,
            ticket_id,
            log="执行完成",
            tenant="tenant-a",
        )
        await ticket_lifecycle_service.archive_ticket(
            db,
            ticket_id,
            tenant="tenant-a",
        )
        assert await ticket_lifecycle_service.delete_ticket(
            db,
            ticket_id,
            tenant="tenant-a",
        ) is True

        stored = await db.get(ProactiveOpsRun, run.id)
        timeline = json.loads(stored.ticket_timeline_json)

    assert stored.ticket_status == "deleted"
    assert stored.ticket_status_updated_at is not None
    assert [item["status"] for item in timeline] == [
        "draft",
        "pending_review",
        "reviewed",
        "issued",
        "in_execution",
        "completed",
        "archived",
        "deleted",
    ]
    assert len(timeline) == 8


class _TicketResult:
    def __init__(self, ticket):
        self.ticket = ticket

    def scalar_one_or_none(self):
        return self.ticket


class _TicketSession:
    def __init__(self, ticket):
        self.ticket = ticket
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _TicketResult(self.ticket)

    async def commit(self):
        return None

    async def refresh(self, ticket):
        return None


def _lifecycle_ticket(status: TicketStatus) -> Ticket:
    return Ticket(
        id=f"ticket-{status.value}",
        tenant_id="tenant-a",
        ticket_type=TicketType.OPERATION,
        status=status,
        title="测试票据",
        task="测试任务",
        device="1号主变",
        location="A站",
        steps="[]",
        safety_measures="[]",
        risks="[]",
        notes="",
        creator="operator-a",
        reviewer="",
        issuer="",
        executor="executor-a",
        supervisor="",
        review_score=0,
        review_comment="",
        audit_report="",
        execution_log="",
        deviation="",
        version=1,
        is_deleted=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "status"),
    [
        ("update", TicketStatus.DRAFT),
        ("submit", TicketStatus.DRAFT),
        ("review", TicketStatus.PENDING_REVIEW),
        ("issue", TicketStatus.REVIEWED),
        ("start", TicketStatus.ISSUED),
        ("complete", TicketStatus.IN_EXECUTION),
        ("archive", TicketStatus.COMPLETED),
        ("delete", TicketStatus.ARCHIVED),
    ],
)
async def test_ticket_mutations_lock_row_before_state_validation(
    monkeypatch,
    operation,
    status,
):
    ticket = _lifecycle_ticket(status)
    db = _TicketSession(ticket)

    async def no_sync(*args, **kwargs):
        return False

    async def low_score_audit(*args, **kwargs):
        return {"score": 70, "items": []}

    monkeypatch.setattr(
        ticket_lifecycle_service,
        "_sync_proactive_status",
        no_sync,
    )
    monkeypatch.setattr(
        ticket_lifecycle_service.ticket_audit_service,
        "audit_ticket",
        low_score_audit,
    )

    if operation == "update":
        await ticket_lifecycle_service.update_ticket_content(
            db, ticket.id, tenant="tenant-a", notes="updated",
        )
    elif operation == "submit":
        await ticket_lifecycle_service.submit_for_review(
            db, ticket.id, tenant="tenant-a",
        )
    elif operation == "review":
        await ticket_lifecycle_service.review_ticket(
            db, ticket.id, approved=True, tenant="tenant-a",
        )
    elif operation == "issue":
        await ticket_lifecycle_service.issue_ticket(
            db, ticket.id, tenant="tenant-a",
        )
    elif operation == "start":
        await ticket_lifecycle_service.start_execution(
            db, ticket.id, tenant="tenant-a",
        )
    elif operation == "complete":
        await ticket_lifecycle_service.complete_execution(
            db, ticket.id, tenant="tenant-a",
        )
    elif operation == "archive":
        await ticket_lifecycle_service.archive_ticket(
            db, ticket.id, tenant="tenant-a",
        )
    else:
        assert await ticket_lifecycle_service.delete_ticket(
            db, ticket.id, tenant="tenant-a",
        )

    sql = str(db.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" in sql


@pytest.mark.asyncio
async def test_ticket_read_does_not_lock_row():
    ticket = _lifecycle_ticket(TicketStatus.DRAFT)
    db = _TicketSession(ticket)

    await ticket_lifecycle_service.get_ticket(
        db, ticket.id, tenant="tenant-a",
    )

    sql = str(db.statements[0].compile(dialect=mysql.dialect()))
    assert "FOR UPDATE" not in sql
