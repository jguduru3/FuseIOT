"""VirtualDevice and SimulatedHTTPDevice behaviour (the reference device contract)."""
import threading
import time

import pytest

from fuseiot.exceptions import ProtocolError
from fuseiot.simulation import DEFAULT_STATES, SimulatedHTTPDevice, VirtualDevice, VirtualDeviceConfig


class TestVirtualDevice:
    def test_types_and_options(self):
        assert set(DEFAULT_STATES) >= {"switchable", "dimmable", "sensor", "motor", "smart_plug"}
        with pytest.raises(ValueError, match="Unknown virtual device type"):
            VirtualDevice("toaster")
        with pytest.raises(TypeError, match="Unknown simulation option"):
            VirtualDevice("switchable", warp=1)
        d = VirtualDevice("dimmable", initial_state={"brightness": 30})
        assert d.state == {"power": False, "brightness": 30}
        assert d.name == "virtual" and d.endpoint == "virtual://dimmable"
        assert "dimmable" in repr(d)

    def test_config_object_is_copied(self):
        cfg = VirtualDeviceConfig(latency_ms=1.0)
        d = VirtualDevice("switchable", cfg, failure_rate=0.5)
        assert d.sim_config.failure_rate == 0.5 and cfg.failure_rate == 0.0

    def test_request_log_and_lazy_connect(self):
        d = VirtualDevice("switchable")
        assert not d.is_connected
        d.send({"_method": "POST", "_path": "/on"})
        assert d.is_connected
        assert d.requests == [{"_method": "POST", "_path": "/on"}]
        assert d.health_check()["metrics"]["messages_sent"] == 1

    def test_state_is_a_copy(self):
        d = VirtualDevice("rgb_light")
        s = d.state
        s["r"] = 0
        assert d.state["r"] == 255

    def test_settle_delay(self):
        d = VirtualDevice("switchable", settle_ms=60)
        d.send({"_path": "/on"})
        assert d.state["power"] is False
        time.sleep(0.08)
        assert d.state["power"] is True

    def test_latency(self):
        d = VirtualDevice("switchable", latency_ms=50)
        start = time.monotonic()
        d.send({"_method": "GET", "_path": "/state"})
        assert time.monotonic() - start >= 0.045

    def test_failure_rate_is_reproducible(self):
        def outcomes(seed):
            d = VirtualDevice("switchable", failure_rate=0.5, seed=seed)
            out = []
            for _ in range(20):
                try:
                    d.send({"_method": "GET", "_path": "/state"})
                    out.append(True)
                except ProtocolError:
                    out.append(False)
            return out

        assert outcomes(7) == outcomes(7)
        assert True in outcomes(7) and False in outcomes(7)

    def test_custom_handler(self):
        d = VirtualDevice("switchable")
        d.on_request("/firmware", lambda payload: {"version": "1.2.3", "echo": payload["x"]})
        assert d.send({"_method": "GET", "_path": "/firmware", "x": 5}) == {"version": "1.2.3", "echo": 5}

    def test_unknown_path_merges_body(self):
        d = VirtualDevice("switchable")
        assert d.send({"_path": "/config", "mode": "eco", "skip": None}) == \
            {"status": "accepted", "path": "/config"}
        assert d.state == {"power": False, "mode": "eco"}

    def test_thermostat_flags(self):
        d = VirtualDevice("thermostat", initial_state={"temperature": 25.0})
        d.send({"_path": "/mode", "mode": "cool"})
        assert d.state["cooling"] is True and d.state["heating"] is False
        d.send({"_path": "/target", "temperature": 30})
        assert d.state["cooling"] is False

    def test_smart_plug_energy(self):
        d = VirtualDevice("smart_plug", load_watts=115.0)
        assert d.send({"_method": "GET", "_path": "/energy"})["power"] == 0.0
        d.send({"_path": "/on"})
        energy = d.send({"_method": "GET", "_path": "/energy"})
        assert energy["power"] == 115.0 and energy["current"] == 0.5
        d.send({"_path": "/reset"})
        assert d.send({"_method": "GET", "_path": "/energy"})["energy"] == 0.0

    def test_handle_entry_point(self):
        d = VirtualDevice("lock")
        d.handle("POST", "/lock", {"action": "unlock"})
        assert d.handle("GET", "/status")["lock_state"] == "unlocked"

    def test_thread_safety(self):
        d = VirtualDevice("dimmable")
        errors = []

        def worker(n):
            try:
                for i in range(50):
                    d.send({"_path": "/brightness", "brightness": n * 100 + i})
                    d.send({"_method": "GET", "_path": "/state"})
            except Exception as e:  # pragma: no cover - would indicate a bug
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [] and len(d.requests) == 400


class TestSimulatedHTTPDevice:
    def test_url_and_port(self, sim_http):
        dev = sim_http("sensor")
        assert dev.url == f"http://127.0.0.1:{dev.port}" and dev.port > 0

    def test_context_manager(self):
        with SimulatedHTTPDevice("switchable") as dev:
            assert dev._thread.is_alive()
        assert not dev._thread.is_alive()

    def test_unknown_type(self):
        with pytest.raises(ValueError):
            SimulatedHTTPDevice("toaster")

    def test_stop_without_start_returns(self):
        dev = SimulatedHTTPDevice("switchable")
        t = threading.Thread(target=dev.stop, daemon=True)
        t.start()
        t.join(1.0)
        try:
            assert not t.is_alive()
        finally:
            dev._server._BaseServer__is_shut_down.set()  # release the stuck thread
            t.join(5)
