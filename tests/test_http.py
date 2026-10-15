"""HTTP / HTTPS transports (mocked with `responses` and against the real simulated device)."""
import json
import urllib.error
import urllib.request

import pytest
import responses

from fuseiot import HTTP, HTTPS, Dimmable, Switchable
from fuseiot.exceptions import AuthenticationError, ConfigurationError, ProtocolError
from fuseiot.protocols.base import ConnectionState
from fuseiot.result import CommandStatus
from tests.helpers import fast_config

BASE = "http://device.test/api"


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as r:
        yield r


class TestHTTPConstruction:
    def test_url_normalisation(self):
        h = HTTP("192.168.1.5/api/", timeout=1.5)
        assert h.base_url == "http://192.168.1.5/api"
        assert h.endpoint == h.base_url and h.timeout == 1.5 and h.name == "http"
        assert not h.is_connected and h.connection_state is ConnectionState.DISCONNECTED

    def test_split_payload(self):
        assert HTTP._split({"_method": "get", "_path": "state", "a": 1}) == ("GET", "/state", {"a": 1})
        assert HTTP._split({}) == ("POST", "/", {})
        with pytest.raises(ValueError):
            HTTP._split({"_method": "TRACE"})

    def test_headers(self):
        h = HTTP(BASE, bearer_token="tok", headers={"X-Site": "lab"})
        headers = h._get_headers()
        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-Site"] == "lab" and headers["Content-Type"] == "application/json"


class TestHTTPConnect:
    @pytest.mark.parametrize("status,ok", [(200, True), (404, True), (405, True), (403, True),
                                           (401, False), (500, False), (503, False)])
    def test_connect_by_status(self, rsps, status, ok):
        rsps.add(responses.GET, BASE, status=status)
        h = HTTP(BASE)
        assert h.connect() is ok
        assert h.is_connected is ok
        if not ok:
            assert h.connection_state is ConnectionState.FAILED
            assert h.last_error

    def test_connect_uses_health_path(self, rsps):
        rsps.add(responses.GET, BASE + "/health", json={"ok": True})
        assert HTTP(BASE, health_path="/health").connect()

    def test_connect_network_error(self, closed_port):
        h = HTTP(f"http://127.0.0.1:{closed_port}", timeout=1.0)
        assert h.connect() is False
        assert h.connection_state is ConnectionState.FAILED
        assert "ConnectionError" in h.last_error
        h.disconnect()
        assert h.connection_state is ConnectionState.DISCONNECTED


class TestHTTPSend:
    def test_get_sends_query_params(self, rsps):
        rsps.add(responses.GET, BASE + "/state", json={"power": True})
        h = HTTP(BASE)
        assert h.send({"_method": "GET", "_path": "/state", "verbose": 1}) == {"power": True}
        assert rsps.calls[0].request.url.endswith("/state?verbose=1")
        assert h.is_connected, "a successful request marks the transport connected"

    def test_post_sends_json_body_without_metadata(self, rsps):
        rsps.add(responses.POST, BASE + "/on", json={"status": "accepted"})
        h = HTTP(BASE, bearer_token="secret", auth=("u", "p"))
        h.send({"_method": "POST", "_path": "/on", "power": True})
        req = rsps.calls[0].request
        assert json.loads(req.body) == {"power": True}
        assert req.headers["Authorization"].startswith("Basic ")  # requests auth overrides header

    def test_bearer_header_sent(self, rsps):
        rsps.add(responses.PUT, BASE + "/x", json={})
        HTTP(BASE, bearer_token="secret").send({"_method": "PUT", "_path": "/x"})
        assert rsps.calls[0].request.headers["Authorization"] == "Bearer secret"

    @pytest.mark.parametrize("body,expected", [
        ("", {}), ("[1, 2]", {"value": [1, 2]}), ("7", {"value": 7}), ('{"a": 1}', {"a": 1}),
    ])
    def test_response_decoding(self, rsps, body, expected):
        rsps.add(responses.GET, BASE + "/s", body=body)
        assert HTTP(BASE).send({"_method": "GET", "_path": "/s"}) == expected

    def test_invalid_json(self, rsps):
        rsps.add(responses.GET, BASE + "/s", body="<html>")
        with pytest.raises(ProtocolError, match="Invalid JSON"):
            HTTP(BASE).send({"_method": "GET", "_path": "/s"})

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_errors(self, rsps, status):
        rsps.add(responses.POST, BASE + "/on", status=status)
        with pytest.raises(AuthenticationError):
            HTTP(BASE).send({"_path": "/on"})

    def test_http_error_status(self, rsps):
        rsps.add(responses.POST, BASE + "/on", status=404, body="no such path")
        h = HTTP(BASE)
        with pytest.raises(ProtocolError) as exc:
            h.send({"_path": "/on"})
        assert exc.value.details["status"] == 404
        assert "no such path" in exc.value.message
        assert h.health_check()["metrics"]["errors"] == 1

    def test_network_error(self, closed_port):
        h = HTTP(f"http://127.0.0.1:{closed_port}", timeout=1.0)
        with pytest.raises(ProtocolError, match="HTTP request failed"):
            h.send({"_method": "GET", "_path": "/state"})
        assert h.connection_state is ConnectionState.FAILED

    def test_network_error_counted_once(self, closed_port):
        h = HTTP(f"http://127.0.0.1:{closed_port}", timeout=1.0)
        with pytest.raises(ProtocolError):
            h.send({"_method": "GET", "_path": "/state"})
        assert h.health_check()["metrics"]["errors"] == 1

    def test_health_check_metrics(self, rsps):
        rsps.add(responses.GET, BASE + "/s", json={})
        h = HTTP(BASE)
        h.send({"_method": "GET", "_path": "/s"})
        h.send({"_method": "GET", "_path": "/s"})
        hc = h.health_check()
        assert hc["metrics"]["messages_sent"] == 2 and hc["connected"] is True
        assert hc["protocol"] == "http" and hc["uptime_seconds"] is not None


class TestSimulatedHTTPDevice:
    def test_root_and_contract(self, sim_http):
        dev = sim_http("switchable")
        with urllib.request.urlopen(dev.url + "/") as resp:
            assert json.loads(resp.read()) == {"device_type": "switchable", "status": "ok"}
        h = HTTP(dev.url)
        assert h.send({"_method": "GET", "_path": "/state"}) == {"power": False}
        h.send({"_method": "PUT", "_path": "/on"})
        assert dev.device.state["power"] is True
        h.disconnect()

    def test_auth_token_required(self, sim_http):
        dev = sim_http("switchable", auth_token="s3cret")
        assert HTTP(dev.url).connect() is False
        with pytest.raises(AuthenticationError):
            HTTP(dev.url).send({"_method": "GET", "_path": "/state"})
        assert HTTP(dev.url, bearer_token="s3cret").send({"_method": "GET", "_path": "/state"}) == {"power": False}

    def test_invalid_json_body_rejected(self, sim_http):
        dev = sim_http("switchable")
        req = urllib.request.Request(dev.url + "/on", data=b"{nope", method="POST",
                                     headers={"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 400
        exc.value.close()

    def test_query_parameters_reach_device(self, sim_http):
        dev = sim_http("switchable")
        with urllib.request.urlopen(dev.url + "/custom?mode=eco") as resp:
            resp.read()
        assert dev.device.requests[-1] == {"mode": "eco", "_method": "GET", "_path": "/custom"}


class TestHTTPEndToEnd:
    def test_switchable_over_http(self, hub, sim_http):
        dev = sim_http("switchable", settle_ms=50)
        lamp = hub.add_device(HTTP(dev.url, timeout=2.0), Switchable, device_id="lamp", config=fast_config())
        r = lamp.on()
        assert r.confirmed and r.state_after == {"power": True}
        assert lamp.off().confirmed
        assert dev.device.state == {"power": False}

    def test_dimmable_over_http(self, hub, sim_http):
        dev = sim_http("dimmable")
        light = hub.add_device(HTTP(dev.url), Dimmable, device_id="dim", config=fast_config())
        assert light.on(brightness=30).confirmed
        assert light.set_brightness(75).state_after == {"power": True, "brightness": 75}

    def test_device_failures_become_failed_results(self, hub, sim_http):
        dev = sim_http("switchable", failure_rate=1.0)
        lamp = hub.add_device(HTTP(dev.url), Switchable, device_id="lamp",
                              config=fast_config(retry_attempts=2))
        r = lamp.on()
        assert r.status is CommandStatus.FAILED and r.attempts == 2
        assert "503" in r.error

    def test_bad_token_fails_registration(self, hub, sim_http):
        dev = sim_http("switchable", auth_token="right")
        with pytest.raises(ConfigurationError, match="401"):
            hub.add_device(HTTP(dev.url, bearer_token="wrong"), Switchable, device_id="lamp")
        ok = hub.add_device(HTTP(dev.url, bearer_token="right"), Switchable, device_id="lamp2",
                            config=fast_config())
        assert ok.on().confirmed

    def test_unreachable_device_rejected(self, hub, closed_port):
        with pytest.raises(ConfigurationError, match="Cannot connect"):
            hub.add_device(HTTP(f"http://127.0.0.1:{closed_port}", timeout=1.0), Switchable, device_id="x")


class TestHTTPAsync:
    async def test_send_async_short_lived_session(self, sim_http):
        dev = sim_http("switchable")
        h = HTTP(dev.url)
        await h.send_async({"_method": "POST", "_path": "/on"})
        assert await h.send_async({"_method": "GET", "_path": "/state", "x": 1}) == {"power": True}
        assert dev.device.requests[-1]["x"] == "1"

    async def test_connect_async_persistent_session(self, sim_http):
        dev = sim_http("switchable")
        h = HTTP(dev.url)
        assert await h.connect_async() is True
        assert h.is_connected and h._has_live_async_session()
        assert await h.send_async({"_method": "GET", "_path": "/state"}) == {"power": False}
        await h.disconnect_async()
        assert h._async_session is None and not h.is_connected

    async def test_async_errors(self, sim_http, closed_port):
        dev = sim_http("switchable", auth_token="t")
        with pytest.raises(AuthenticationError):
            await HTTP(dev.url).send_async({"_method": "GET", "_path": "/state"})
        with pytest.raises(ProtocolError):
            await HTTP(f"http://127.0.0.1:{closed_port}", timeout=1.0).send_async({"_path": "/on"})
        h = HTTP(f"http://127.0.0.1:{closed_port}", timeout=1.0)
        assert await h.connect_async() is False
        await h.disconnect_async()

    async def test_async_http_error_status(self, sim_http):
        dev = sim_http("switchable")
        with pytest.raises(ProtocolError) as exc:
            await HTTP(dev.url).send_async({"_method": "DELETE", "_path": "/state"})
        assert exc.value.details["status"] == 501

    async def test_capability_async_over_http(self, hub, sim_http):
        dev = sim_http("switchable")
        lamp = hub.add_device(HTTP(dev.url), Switchable, device_id="lamp", config=fast_config())
        assert (await lamp.on_async()).confirmed


class TestHTTPS:
    def test_scheme_upgrade(self):
        assert HTTPS("http://host/api").base_url == "https://host/api"
        assert HTTPS("host:8443").base_url == "https://host:8443"
        assert HTTPS("https://host").endpoint == "https://host"

    def test_ca_bundle_and_verification(self):
        assert HTTPS("https://h", ca_bundle="/ca.pem").verify_ssl == "/ca.pem"
        assert HTTPS("https://h", ca_bundle="/ca.pem", verify_ssl=False).verify_ssl is False
        assert HTTPS("https://h").verify_ssl is True

    def test_client_certificates(self):
        with pytest.raises(ValueError, match="client_key"):
            HTTPS("https://h", client_cert="/c.pem")
        assert HTTPS("https://h", client_cert="/c.pem", client_key="/k.pem").client_cert == ("/c.pem", "/k.pem")
        assert HTTPS("https://h", client_cert=["/c.pem", "/k.pem"]).client_cert == ("/c.pem", "/k.pem")

    def test_extra_kwargs_reach_http(self):
        h = HTTPS("https://h", bearer_token="t", timeout=3)
        assert h.bearer_token == "t" and h.timeout == 3.0

    def test_session_uses_tls_options(self):
        h = HTTPS("https://h", ca_bundle="/ca.pem", client_cert="/c.pem", client_key="/k.pem")
        session = h._session()
        assert session.verify == "/ca.pem" and session.cert == ("/c.pem", "/k.pem")
        h.disconnect()
