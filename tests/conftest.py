"""Shared fixtures for the FuseIoT test suite."""
import os
from typing import Any, Callable, List

import pytest

from fuseiot import Hub
from fuseiot.logging_config import configure_logging
from fuseiot.simulation.http_device import SimulatedHTTPDevice
from fuseiot.simulation.virtual_device import VirtualDevice
from tests.helpers import FakeBroker, FakeClient, fast_config, start_fast

# Keep the library quiet and never bind log handlers to pytest's capture streams.
configure_logging(level="CRITICAL", console=False)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    """Strip FUSEIOT_* variables from the environment and reset logging after each test."""
    for key in list(os.environ):
        if key.startswith("FUSEIOT_"):
            monkeypatch.delenv(key, raising=False)
    yield
    configure_logging(level="CRITICAL", console=False)


@pytest.fixture
def hub():
    h = Hub(default_ttl=5.0)
    yield h
    h.close()


@pytest.fixture
def add_virtual(hub: Hub) -> Callable[..., Any]:
    """add_virtual(device_type, cls, device_id, config=None, cap_kwargs=None, **sim) -> capability"""

    def _add(device_type: str, cls: Any, device_id: str, config: Any = None,
             cap_kwargs: Any = None, **sim: Any) -> Any:
        return hub.add_device(VirtualDevice(device_type, **sim), cls, device_id=device_id,
                              config=config if config is not None else fast_config(),
                              **(cap_kwargs or {}))

    return _add


@pytest.fixture
def sim_http():
    """Factory for SimulatedHTTPDevice servers that are stopped after the test."""
    started: List[SimulatedHTTPDevice] = []

    def _make(device_type: str = "switchable", **kwargs: Any) -> SimulatedHTTPDevice:
        dev = SimulatedHTTPDevice(device_type, **kwargs)
        start_fast(dev, dev._server)
        started.append(dev)
        return dev

    yield _make
    for dev in started:
        dev.stop()


@pytest.fixture
def fake_mqtt(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    """Replace paho's Client with an in-memory fake bound to a fresh broker."""
    import fuseiot.protocols.mqtt as mqtt_module

    broker = FakeBroker()
    client_cls = type("BoundFakeClient", (FakeClient,), {"broker": broker, "instances": []})
    broker.client_cls = client_cls
    monkeypatch.setattr(mqtt_module.mqtt, "Client", client_cls)
    return broker


@pytest.fixture
def closed_port():
    """A localhost port whose 'device' accepts TCP connections and hangs up immediately.

    (A genuinely closed port is avoided because Windows retries refused
    connections for ~2 s.)
    """
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.05)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            conn.close()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(timeout=2)
    srv.close()


@pytest.fixture
def fast_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cheaper scrypt parameters for tests that create many users (hashes still self-describe n/r/p)."""
    import fuseiot.auth as auth_module

    monkeypatch.setitem(auth_module._SCRYPT, "n", 2 ** 10)
