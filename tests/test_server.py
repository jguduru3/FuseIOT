"""REST API server, dashboard, health/metrics endpoints and HealthServer."""
import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from fuseiot import Hub, HubConfig, Switchable, Thermostat, VirtualDevice
from fuseiot.edge.rules import Action, Condition, RuleEngine
from fuseiot.exceptions import ConfigurationError
from fuseiot.monitoring.health import HealthServer, HealthStatus
from fuseiot.server.app import FuseIoTServer, run_server
from fuseiot.utils.circuit_breaker import CircuitBreaker
from fuseiot.utils.rate_limiter import RateLimiter
from tests.helpers import fast_config, start_fast

TOKEN = "secret-token"


def call(server, method, path, token=TOKEN, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(server.url + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()


def call_json(server, method, path, **kw):
    status, _, body = call(server, method, path, **kw)
    return status, json.loads(body)


@pytest.fixture
def api_hub(tmp_path):
    hub = Hub(HubConfig(history_path=str(tmp_path / "history.db")))
    hub.add_device(VirtualDevice("switchable"), Switchable, device_id="lamp", config=fast_config())
    hub.add_device(VirtualDevice("thermostat"), Thermostat, device_id="thermo", config=fast_config())
    hub.add_device(VirtualDevice("switchable", failure_rate=1.0), Switchable, device_id="broken",
                   config=fast_config())
    stuck = VirtualDevice("switchable")
    stuck.on_request("/on", lambda payload: {"status": "ignored"})
    hub.add_device(stuck, Switchable, device_id="stuck", config=fast_config(timeout=0.1))
    hub.add_device(VirtualDevice("switchable"), Switchable, device_id="limited",
                   config=fast_config(rate_limiter=RateLimiter(requests=1, window=60)))
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
    breaker.record_failure()
    hub.add_device(VirtualDevice("switchable"), Switchable, device_id="tripped",
                   config=fast_config(circuit_breaker=breaker))
    yield hub
    hub.close()


def serve(hub, **kw):
    kw.setdefault("port", 0)
    server = FuseIoTServer(hub, **kw)
    start_fast(server, server._httpd)
    server.ready = True
    return server


@pytest.fixture
def engine(api_hub):
    eng = RuleEngine(api_hub)
    eng.add_simple("warm_up", Condition("thermo", "temperature", "<", 18),
                   [Action("thermo", "set_mode", {"mode": "heat"})])
    return eng


@pytest.fixture
def server(api_hub, engine):
    srv = serve(api_hub, api_token=TOKEN, rule_engine=engine)
    yield srv
    srv.stop()


class TestSecurity:
    def test_refuses_public_bind_without_token(self, api_hub):
        with pytest.raises(ConfigurationError, match="Refusing"):
            FuseIoTServer(api_hub, host="0.0.0.0", port=0)

    def test_api_requires_token(self, server):
        assert call(server, "GET", "/api/devices", token=None)[0] == 401
        assert call(server, "GET", "/api/devices", token="wrong")[0] == 401
        assert call(server, "GET", "/api/devices")[0] == 200
        # A token in the URL is only honoured for the SSE stream (it would leak into logs elsewhere).
        assert call(server, "GET", f"/api/devices?token={TOKEN}", token=None)[0] == 401

    def test_public_endpoints_need_no_token(self, server):
        assert call(server, "GET", "/health", token=None)[0] == 200
        assert call(server, "GET", "/ready", token=None)[0] == 200
        assert call(server, "GET", "/metrics", token=None)[0] == 200
        assert call(server, "GET", "/", token=None)[0] == 200

    def test_no_token_configured_means_open_api(self, api_hub):
        srv = serve(api_hub)
        try:
            assert call(srv, "GET", "/api/stats", token=None)[0] == 200
        finally:
            srv.stop()


class TestPublicEndpoints:
    def test_health_states(self, server, api_hub):
        status, body = call_json(server, "GET", "/health", token=None)
        assert status == 200 and body["status"] == "healthy"
        assert body["devices"] == 6 and body["devices_connected"] == 6
        api_hub["lamp"].protocol.disconnect()
        status, body = call_json(server, "GET", "/health", token=None)
        assert status == 200 and body["status"] == "degraded" and body["disconnected"] == ["lamp"]
        for d in api_hub.devices():
            d.protocol.disconnect()
        status, body = call_json(server, "GET", "/health", token=None)
        assert status == 503 and body["status"] == "unhealthy"

    def test_ready(self, server):
        assert call_json(server, "GET", "/ready", token=None) == (200, {"ready": True})
        server.ready = False
        assert call_json(server, "GET", "/ready", token=None)[0] == 503

    def test_metrics(self, server):
        call(server, "POST", "/api/devices/lamp/commands/on")
        status, headers, body = call(server, "GET", "/metrics", token=None)
        text = body.decode()
        assert headers["Content-Type"].startswith("text/plain; version=0.0.4")
        assert "# HELP fuseiot_devices Registered devices" in text
        assert "# TYPE fuseiot_commands_total counter" in text
        assert 'fuseiot_commands_total{command="on",device="lamp",status="confirmed"} 1' in text
        assert 'fuseiot_device_up{device="lamp"} 1.0' in text
        assert 'fuseiot_http_requests_total{method="POST",route="/api/devices",status="200"} 1' in text

    def test_dashboard(self, server, api_hub):
        status, headers, body = call(server, "GET", "/", token=None)
        assert headers["Content-Type"].startswith("text/html")
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert b"<" in body and len(body) > 100
        status, headers, body = call(server, "HEAD", "/", token=None)
        assert status == 200 and body == b""
        srv = serve(api_hub, enable_dashboard=False)
        try:
            assert call(srv, "GET", "/", token=None)[0] == 404
        finally:
            srv.stop()

    def test_unknown_paths(self, server):
        assert call(server, "GET", "/nope", token=None)[0] == 404
        assert call(server, "GET", "/api/nope")[0] == 404
        assert call(server, "POST", "/health")[0] == 404


class TestDeviceEndpoints:
    def test_list_devices(self, server):
        status, body = call_json(server, "GET", "/api/devices")
        assert status == 200
        devices = {d["device_id"]: d for d in body["devices"]}
        assert set(devices) == {"lamp", "thermo", "broken", "stuck", "limited", "tripped"}
        assert devices["lamp"]["commands"] == ["on", "off", "toggle"]
        assert devices["lamp"]["state"] is None
        assert devices["tripped"]["circuit"]["state"] == "OPEN"
        assert devices["lamp"]["circuit"] is None

    def test_get_device_and_state(self, server, api_hub):
        status, body = call_json(server, "GET", "/api/devices/lamp?fresh=1")
        assert status == 200 and body["state"] == {"power": False} and body["state_error"] is None
        api_hub["lamp"].protocol.set_state({"power": True})
        assert call_json(server, "GET", "/api/devices/lamp/state")[1]["state"] == {"power": False}
        assert call_json(server, "GET", "/api/devices/lamp/state?fresh=true")[1] == \
            {"device_id": "lamp", "state": {"power": True}}
        assert call(server, "GET", "/api/devices/ghost")[0] == 404

    def test_state_read_failure(self, server):
        status, body = call_json(server, "GET", "/api/devices/broken/state?fresh=1")
        assert status == 502 and "broken" in body["error"]
        status, body = call_json(server, "GET", "/api/devices/broken?fresh=1")
        assert status == 200 and body["state_error"]

    def test_run_command(self, server, api_hub):
        status, body = call_json(server, "POST", "/api/devices/lamp/commands/on")
        assert status == 200 and body["status"] == "CONFIRMED" and body["state_after"] == {"power": True}
        assert api_hub["lamp"].protocol.state["power"] is True
        status, body = call_json(server, "POST", "/api/devices/thermo/commands/set_temperature",
                                 body={"temperature": 23.5})
        assert status == 200 and body["state_after"]["target_temperature"] == 23.5
        status, body = call_json(server, "POST", "/api/devices/lamp/commands/off", body={"confirm": False})
        assert status == 200 and body["status"] == "ACKNOWLEDGED"

    @pytest.mark.parametrize("path,body,expected", [
        ("/api/devices/lamp/commands/read_state", None, 400),       # not in the allowlist
        ("/api/devices/lamp/commands/_fetch_state", None, 400),
        ("/api/devices/lamp/commands/on", {"brightness": 5}, 400),  # unexpected parameter
        ("/api/devices/thermo/commands/set_mode", {"mode": "turbo"}, 400),
        ("/api/devices/ghost/commands/on", None, 404),
        ("/api/devices/broken/commands/on", None, 502),
        ("/api/devices/stuck/commands/on", None, 504),
        ("/api/devices/tripped/commands/on", None, 503),
    ])
    def test_command_status_codes(self, server, path, body, expected):
        status, payload = call_json(server, "POST", path, body=body)
        assert status == expected, payload
        assert payload.get("error")

    def test_unknown_command_lists_allowed(self, server):
        status, body = call_json(server, "POST", "/api/devices/lamp/commands/explode")
        assert status == 400 and body["allowed"] == ["on", "off", "toggle"]

    def test_rate_limited_command(self, server):
        assert call(server, "POST", "/api/devices/limited/commands/on")[0] == 200
        status, body = call_json(server, "POST", "/api/devices/limited/commands/off")
        assert status == 429 and body["status"] == "RATE_LIMITED"

    @pytest.mark.parametrize("raw,fragment", [(b"{bad", "Invalid JSON"), (b"[1, 2]", "object")])
    def test_bad_bodies(self, server, raw, fragment):
        status, body = call_json(server, "POST", "/api/devices/lamp/commands/on", raw=raw)
        assert status == 400 and fragment in body["error"]

    def test_body_too_large(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.putrequest("POST", "/api/devices/lamp/commands/on")
            conn.putheader("Authorization", f"Bearer {TOKEN}")
            conn.putheader("Content-Length", str(10 ** 6))
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.status == 413
            resp.read()
        finally:
            conn.close()

    def test_history(self, server):
        call(server, "POST", "/api/devices/lamp/commands/on")
        call(server, "POST", "/api/devices/lamp/commands/off")
        status, body = call_json(server, "GET", "/api/devices/lamp/history")
        assert status == 200
        assert [h["state"] for h in body["history"]] == [{"power": False}, {"power": True}]
        assert len(call_json(server, "GET", "/api/devices/lamp/history?limit=1")[1]["history"]) == 1

    def test_history_bad_limit_is_client_error(self, server):
        assert call(server, "GET", "/api/devices/lamp/history?limit=abc")[0] == 400

    def test_history_disabled(self):
        with Hub() as hub:
            hub.add_device(VirtualDevice(), Switchable, device_id="lamp")
            srv = serve(hub)
            try:
                status, body = call_json(srv, "GET", "/api/devices/lamp/history")
                assert status == 404 and "History is disabled" in body["error"]
            finally:
                srv.stop()

    def test_stats(self, server):
        status, body = call_json(server, "GET", "/api/stats")
        assert status == 200 and body["devices_active"] == 6


class TestRulesEndpoints:
    def test_list_enable_disable(self, server, engine):
        status, body = call_json(server, "GET", "/api/rules")
        assert status == 200 and body["rules"][0]["name"] == "warm_up"
        assert call_json(server, "POST", "/api/rules/warm_up/disable") == \
            (200, {"name": "warm_up", "enabled": False})
        assert engine.rules[0].enabled is False
        assert call_json(server, "POST", "/api/rules/warm_up/enable")[1]["enabled"] is True
        assert call(server, "POST", "/api/rules/ghost/enable")[0] == 404

    def test_rules_without_engine(self, api_hub):
        srv = serve(api_hub)
        try:
            assert call(srv, "GET", "/api/rules")[0] == 404
        finally:
            srv.stop()


class TestEventStream:
    def test_sse_delivers_state_changes(self, server, api_hub):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.request("GET", f"/api/events?token={TOKEN}")
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.getheader("Content-Type") == "text/event-stream"
            assert resp.fp.readline() == b": connected\n"
            assert resp.fp.readline() == b"\n"
            api_hub["lamp"].on()
            assert resp.fp.readline() == b"event: state\n"
            data = resp.fp.readline().decode()
            assert data.startswith("data: ")
            event = json.loads(data[len("data: "):])
            assert event["device_id"] == "lamp" and event["new_state"] == {"power": True}
            assert event["source"] == "command"
        finally:
            conn.close()

    def test_sse_requires_token(self, server):
        assert call(server, "GET", "/api/events", token=None)[0] == 401


class TestLifecycle:
    def test_run_server_helper(self, api_hub):
        srv = run_server(api_hub, port=0)
        try:
            assert srv.ready and srv.url.startswith("http://127.0.0.1:")
            assert call(srv, "GET", "/ready", token=None)[0] == 200
        finally:
            srv.stop()
        assert srv.ready is False

    def test_context_manager(self, api_hub):
        srv = FuseIoTServer(api_hub, port=0)
        with srv:
            assert call(srv, "GET", "/health", token=None)[0] == 200
        assert not srv.ready

    def test_stop_without_start_returns(self, api_hub):
        srv = FuseIoTServer(api_hub, port=0)
        t = threading.Thread(target=srv.stop, daemon=True)
        t.start()
        t.join(1.0)
        try:
            assert not t.is_alive()
        finally:
            srv._httpd._BaseServer__is_shut_down.set()  # release the stuck thread
            t.join(5)


class TestHealthServer:
    def test_check_levels(self, api_hub):
        hs = HealthServer(api_hub)
        assert hs.check().status is HealthStatus.HEALTHY
        api_hub["lamp"].protocol.disconnect()
        result = hs.check()
        assert result.status is HealthStatus.DEGRADED and "1 device" in result.message
        for d in api_hub.devices():
            d.protocol.disconnect()
        assert hs.check().status is HealthStatus.UNHEALTHY

    def test_custom_checks(self):
        with Hub() as hub:
            hs = HealthServer(hub)
            assert hs.check().status is HealthStatus.HEALTHY  # no devices
            hs.add_check("db", lambda: False)
            assert hs.check().message == "Custom check failed: db"
            hs2 = HealthServer(hub)
            hs2.add_check("boom", lambda: 1 / 0)
            assert hs2.check().status is HealthStatus.UNHEALTHY

    def test_serves_health_but_not_api(self, api_hub):
        hs = HealthServer(api_hub, port=0)
        hs.add_check("disk", lambda: False)
        hs.start()
        try:
            srv = hs._server
            status, body = call_json(srv, "GET", "/health", token=None)
            assert status == 503 and body["message"] == "Custom check failed: disk"
            assert call(srv, "GET", "/api/devices", token=None)[0] == 401
            assert call(srv, "GET", "/", token=None)[0] == 404
            assert hs.port == srv.port != 0
        finally:
            hs.stop()
        assert hs._server is None
