"""WebSocket transport against a real `websockets` server on 127.0.0.1."""
import pytest

from fuseiot import Dimmable, Switchable, WebSocket
from fuseiot.exceptions import ProtocolError
from fuseiot.protocols.base import ConnectionState
from tests.helpers import WebSocketDeviceServer, fast_config


@pytest.fixture
def ws_server():
    server = WebSocketDeviceServer("switchable").start()
    yield server
    server.stop()


@pytest.fixture
def ws_client():
    clients = []

    def make(uri: str, **kw) -> WebSocket:
        kw.setdefault("timeout", 2.0)
        c = WebSocket(uri, **kw)
        clients.append(c)
        return c

    yield make
    for c in clients:
        c.disconnect()


def test_connect_and_disconnect(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    assert ws.connect() is True
    assert ws.is_connected and ws.endpoint == ws_server.uri
    assert ws.connect() is True  # idempotent
    ws.disconnect()
    assert ws.connection_state is ConnectionState.DISCONNECTED


def test_request_response_matched_by_id(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    assert ws.send({"_method": "GET", "_path": "/state"}) == {"power": False}
    ws.send({"_method": "POST", "_path": "/on", "power": True})
    assert ws_server.device.state["power"] is True
    ids = [m["id"] for m in ws_server.received]
    assert ids == ["1", "2"], "each request carries a unique id"
    assert ws.health_check()["metrics"]["messages_sent"] == 2


def test_reply_without_id_goes_to_oldest_request(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    assert ws.send({"_path": "/noid"}) == {"ok": True, "noid": True}


def test_unsolicited_messages_become_latest_push(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    assert ws.send({"_path": "/push"}) == {"ok": True}
    assert ws.latest_push == {"id": "unsolicited", "pushed": True}


def test_response_timeout(ws_server, ws_client):
    ws = ws_client(ws_server.uri, timeout=0.2)
    with pytest.raises(ProtocolError, match="Response timeout"):
        ws.send({"_path": "/silent"})
    assert ws.send({"_method": "GET", "_path": "/state"}) == {"power": False}, "connection still usable"


def test_peer_close_fails_pending_request(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    ws.connect()
    with pytest.raises(ProtocolError, match="Connection closed"):
        ws.send({"_path": "/close"})
    assert ws.connection_state is ConnectionState.DISCONNECTED
    # The next request transparently reconnects.
    assert ws.send({"_method": "GET", "_path": "/state"}) == {"power": False}


def test_connect_failure(closed_port, ws_client):
    ws = ws_client(f"ws://127.0.0.1:{closed_port}", timeout=1.0)
    assert ws.connect() is False
    assert ws.connection_state is ConnectionState.FAILED and ws.last_error
    with pytest.raises(ProtocolError, match="Not connected"):
        ws.send({"_path": "/on"})


def test_reconnect_after_disconnect(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    ws.send({"_method": "GET", "_path": "/state"})
    ws.disconnect()
    assert ws.send({"_method": "GET", "_path": "/state"}) == {"power": False}


def test_disconnect_without_connect_is_safe(ws_client):
    ws = ws_client("ws://127.0.0.1:1")
    ws.disconnect()
    assert ws.connection_state is ConnectionState.DISCONNECTED


async def test_async_api(ws_server, ws_client):
    ws = ws_client(ws_server.uri)
    assert await ws.connect_async() is True
    await ws.send_async({"_method": "POST", "_path": "/on"})
    assert await ws.send_async({"_method": "GET", "_path": "/state"}) == {"power": True}
    await ws.disconnect_async()
    assert not ws.is_connected


def test_capability_over_websocket(hub, ws_server, ws_client):
    lamp = hub.add_device(ws_client(ws_server.uri), Switchable, device_id="lamp", config=fast_config())
    assert lamp.on().confirmed
    assert lamp.toggle().state_after == {"power": False}


def test_dimmable_over_websocket(hub, ws_client):
    server = WebSocketDeviceServer("dimmable").start()
    try:
        light = hub.add_device(ws_client(server.uri), Dimmable, device_id="d", config=fast_config())
        assert light.on(brightness=55).state_after == {"power": True, "brightness": 55}
    finally:
        hub.remove("d")
        server.stop()
