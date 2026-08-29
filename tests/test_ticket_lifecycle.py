"""两票生命周期 + 流转事件单测（sqlite test_db，不依赖 MySQL/Milvus/LLM）。"""
import asyncio


def test_issue_emits_event(test_db, monkeypatch):
    import app.services.ticket_lifecycle_service as tl
    events = []

    async def fake_emit(event_type, ticket, tenant="default"):
        events.append((event_type, ticket.id))
    monkeypatch.setattr(tl, "_emit_ticket_event", fake_emit)
    t = asyncio.run(tl.create_ticket(test_db, task="主变检修", device="1号主变", creator="a"))
    asyncio.run(tl.submit_for_review(test_db, t["id"]))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True, reviewer="r"))
    asyncio.run(tl.issue_ticket(test_db, t["id"], issuer="i"))
    assert events and events[0][0] == "ticket.issued"
    assert events[0][1] == t["id"]


def test_complete_emits_event(test_db, monkeypatch):
    import app.services.ticket_lifecycle_service as tl
    events = []

    async def fake_emit(event_type, ticket, tenant="default"):
        events.append(event_type)
    monkeypatch.setattr(tl, "_emit_ticket_event", fake_emit)
    t = asyncio.run(tl.create_ticket(test_db, task="x"))
    asyncio.run(tl.submit_for_review(test_db, t["id"]))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True))
    asyncio.run(tl.issue_ticket(test_db, t["id"]))
    asyncio.run(tl.start_execution(test_db, t["id"]))
    asyncio.run(tl.complete_execution(test_db, t["id"], log="完成", deviation="无"))
    assert "ticket.completed" in events


def test_emit_disabled_by_flag(test_db, monkeypatch):
    """开关关：签发不 emit（关=现状）。"""
    import app.services.ticket_lifecycle_service as tl
    called = []
    monkeypatch.setattr(tl.settings, "TICKET_ACTION_LOOP_ENABLE", False)

    async def fake_bus_emit(*a, **kw):
        called.append(a)
    monkeypatch.setattr(tl.quality_event_bus, "emit", fake_bus_emit)
    t = asyncio.run(tl.create_ticket(test_db, task="y"))
    asyncio.run(tl.submit_for_review(test_db, t["id"]))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True))
    asyncio.run(tl.issue_ticket(test_db, t["id"]))
    assert called == []


def test_emit_failure_does_not_block(test_db, monkeypatch):
    """emit 抛错走 degraded，不阻塞签发流转（降级不崩铁律）。"""
    import app.services.ticket_lifecycle_service as tl
    monkeypatch.setattr(tl.settings, "TICKET_ACTION_LOOP_ENABLE", True)

    async def boom(*a, **kw):
        raise RuntimeError("bus down")
    monkeypatch.setattr(tl.quality_event_bus, "emit", boom)
    t = asyncio.run(tl.create_ticket(test_db, task="z"))
    asyncio.run(tl.submit_for_review(test_db, t["id"]))
    asyncio.run(tl.review_ticket(test_db, t["id"], approved=True))
    r = asyncio.run(tl.issue_ticket(test_db, t["id"]))
    assert r["status"] == "issued"
