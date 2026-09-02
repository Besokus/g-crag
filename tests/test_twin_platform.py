"""数字孪生多站点平台切片的聚焦测试。"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from app.config import settings
from app.core.permissions import ALERT_MANAGE, DOMAIN_USE
from app.core import ws_manager
from app.routers import twin as twin_router
from app.services import twin_service


def _layout(station_id: str, station_name: str, device_id: str) -> dict:
    return {
        "stationId": station_id,
        "stationName": station_name,
        "voltageLevel": "35kV",
        "type": "indoor",
        "areas": [{"id": "area-1", "name": "设备区"}],
        "devices": [{
            "deviceId": device_id,
            "name": f"{station_name}设备",
            "type": "circuit_breaker",
            "area": "area-1",
            "position": [1, 2, 3],
            "size": [1, 1, 1],
            "kgEntity": f"{station_name}实体",
            "connections": [],
        }],
    }


def _write_layout(path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_twin_cache():
    twin_service._layout_cache.clear()
    twin_service._layout_cache_meta.clear()
    ws_manager._twin_clients.clear()
    yield
    twin_service._layout_cache.clear()
    twin_service._layout_cache_meta.clear()
    ws_manager._twin_clients.clear()


def test_catalog_discovers_configured_file_and_sibling(tmp_path, monkeypatch):
    configured = tmp_path / "primary.json"
    sibling = tmp_path / "station_layout_backup.json"
    _write_layout(configured, _layout("station-a", "甲站", "device-a"))
    _write_layout(sibling, _layout("station-b", "乙站", "device-b"))
    monkeypatch.setattr(settings, "TWIN_LAYOUT_PATH", str(configured))

    catalog = asyncio.run(twin_service.list_stations())

    assert [item["stationId"] for item in catalog["stations"]] == [
        "station-a",
        "station-b",
    ]
    assert catalog["defaultStationId"] == "station-a"
    assert twin_service._load_layout("station-a")["devices"][0]["deviceId"] == "device-a"
    assert twin_service._load_layout("station-b")["devices"][0]["deviceId"] == "device-b"


def test_unknown_and_path_like_station_ids_are_rejected(tmp_path, monkeypatch):
    _write_layout(
        tmp_path / "station_layout_a.json",
        _layout("station-a", "甲站", "device-a"),
    )
    monkeypatch.setattr(settings, "TWIN_LAYOUT_PATH", str(tmp_path))

    with pytest.raises(twin_service.UnknownStationError):
        twin_service._load_layout("missing")
    with pytest.raises(twin_service.InvalidStationIdError):
        twin_service._load_layout("../station-a")
    with pytest.raises(twin_service.InvalidStationIdError):
        twin_service._load_layout(r"..\station-a")


def test_device_and_fault_chain_use_selected_station(tmp_path, monkeypatch):
    _write_layout(
        tmp_path / "station_layout_b.json",
        _layout("station-b", "乙站", "device-b"),
    )
    monkeypatch.setattr(settings, "TWIN_LAYOUT_PATH", str(tmp_path))

    async def fake_graph_context(entity, limit):
        return [{"entity": entity, "limit": limit}]

    async def fake_get_paths(entity, depth=3, limit=10):
        return [{"chain": [entity, "故障"], "hops": depth}]

    async def fake_predict(days=30):
        return {"items": []}

    async def fake_list_disposals(page=1, size=5):
        return {"list": []}

    monkeypatch.setattr("app.services.kg_service.graph_context", fake_graph_context)
    monkeypatch.setattr("app.services.kg_service.get_paths", fake_get_paths)
    monkeypatch.setattr("app.services.fault_prediction_service.predict", fake_predict)
    monkeypatch.setattr(
        "app.services.alert_disposal_service.list_disposals",
        fake_list_disposals,
    )

    detail = asyncio.run(
        twin_service.get_device_detail("device-b", station_id="station-b")
    )
    paths = asyncio.run(
        twin_service.get_fault_chain(
            "device-b",
            depth=4,
            station_id="station-b",
        )
    )

    assert detail["stationId"] == "station-b"
    assert detail["name"] == "乙站设备"
    assert paths[0]["chain"][0] == "乙站实体"
    assert paths[0]["hops"] == 4


def test_alert_push_carries_station_id(tmp_path, monkeypatch):
    _write_layout(
        tmp_path / "station_layout_b.json",
        _layout("station-b", "乙站", "device-b"),
    )
    monkeypatch.setattr(settings, "TWIN_LAYOUT_PATH", str(tmp_path))
    pushed = {}

    async def fake_broadcast(message):
        pushed["message"] = message

    monkeypatch.setattr("app.core.ws_manager.broadcast_twin", fake_broadcast)

    result = asyncio.run(
        twin_service.push_alert_location(
            {"deviceId": "device-b", "severity": "critical"},
            station_id="station-b",
        )
    )

    assert result["stationId"] == "station-b"
    assert pushed["message"]["stationId"] == "station-b"
    assert pushed["message"]["deviceId"] == "device-b"


def test_twin_broadcast_isolated_by_station_with_legacy_broadcast():
    class FakeWebSocket:
        def __init__(self):
            self.accepted = False
            self.sent = []

        async def accept(self):
            self.accepted = True

        async def send_json(self, payload):
            self.sent.append(payload)

    station_a = FakeWebSocket()
    station_b = FakeWebSocket()

    async def scenario():
        await ws_manager.connect_twin(station_a, "station-a")
        await ws_manager.connect_twin(station_b, "station-b")
        await ws_manager.broadcast_twin({
            "type": "alert",
            "stationId": "station-a",
        })
        await ws_manager.broadcast_twin({"type": "layout-refresh"})

    asyncio.run(scenario())

    assert station_a.accepted is True
    assert station_b.accepted is True
    assert station_a.sent == [
        {"type": "alert", "stationId": "station-a"},
        {"type": "layout-refresh"},
    ]
    assert station_b.sent == [{"type": "layout-refresh"}]
    assert ws_manager.twin_client_count() == 2

    ws_manager.disconnect_twin(station_a)
    assert ws_manager.twin_client_count() == 1
    ws_manager.disconnect_twin(station_a)
    assert ws_manager.twin_client_count() == 1


def _permission_from_route(route: APIRoute) -> str:
    dependency = route.dependant.dependencies[0].call
    return next(
        cell.cell_contents
        for cell in dependency.__closure__ or ()
        if isinstance(cell.cell_contents, str) and ":" in cell.cell_contents
    )


def test_http_routes_require_existing_permissions():
    permissions = {
        route.path: _permission_from_route(route)
        for route in twin_router.router.routes
        if isinstance(route, APIRoute)
    }

    assert permissions["/twin/stations"] == DOMAIN_USE
    assert permissions["/twin/station/layout"] == DOMAIN_USE
    assert permissions["/twin/station/overview"] == DOMAIN_USE
    assert permissions["/twin/device/{device_id}/detail"] == DOMAIN_USE
    assert permissions["/twin/device/{device_id}/fault-chain"] == DOMAIN_USE
    assert permissions["/twin/alert/push"] == ALERT_MANAGE


def test_websocket_rejects_invalid_token_before_connect(monkeypatch):
    class FakeWebSocket:
        query_params = {"token": "invalid", "stationId": "station-a"}

        def __init__(self):
            self.accepted = False
            self.sent = []
            self.closed = None

        async def accept(self):
            self.accepted = True

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, code=None):
            self.closed = code

    def invalid_token(_token):
        raise ValueError("invalid")

    connected = SimpleNamespace(value=False)

    async def fake_connect(_ws):
        connected.value = True

    monkeypatch.setattr(twin_router, "decode_token", invalid_token)
    monkeypatch.setattr(twin_router, "connect_twin", fake_connect)
    ws = FakeWebSocket()

    asyncio.run(twin_router.twin_ws(ws))

    assert ws.accepted is True
    assert ws.closed == 1008
    assert ws.sent[0]["type"] == "error"
    assert connected.value is False


def test_websocket_passes_station_subscription_to_manager(monkeypatch):
    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeWebSocket:
        query_params = {"token": "valid", "stationId": "station-b"}

        def __init__(self):
            self.sent = []

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_text(self):
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()

    async def fake_get_user(_db, _user_id):
        return SimpleNamespace(role="operator")

    async def fake_layout(station_id):
        return {"stationId": station_id}

    connected = {}
    disconnected = []

    async def fake_connect(ws, station_id):
        connected["ws"] = ws
        connected["station_id"] = station_id

    def fake_disconnect(ws):
        disconnected.append(ws)

    monkeypatch.setattr(twin_router, "decode_token", lambda _token: {"sub": "user-1"})
    monkeypatch.setattr(twin_router, "AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(twin_router, "get_user_by_id", fake_get_user)
    monkeypatch.setattr(twin_router, "has_perm", lambda _role, _perm: True)
    monkeypatch.setattr(twin_router.twin_service, "get_station_layout", fake_layout)
    monkeypatch.setattr(twin_router, "connect_twin", fake_connect)
    monkeypatch.setattr(twin_router, "disconnect_twin", fake_disconnect)
    monkeypatch.setattr(twin_router, "twin_client_count", lambda: 1)
    ws = FakeWebSocket()

    asyncio.run(twin_router.twin_ws(ws))

    assert connected == {"ws": ws, "station_id": "station-b"}
    assert disconnected == [ws]
    assert ws.sent == [{
        "type": "ready",
        "stationId": "station-b",
        "clients": 1,
    }]
