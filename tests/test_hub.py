"""Hub: registration, lookup, removal, events, stats and batching."""
import time

import pytest

from fuseiot import Config, Dimmable, Hub, HubConfig, Motor, Sensor, Switchable, VirtualDevice
from fuseiot.capabilities.base import CapabilityConfig
from fuseiot.exceptions import ConfigurationError, DeviceError
from fuseiot.result import CommandStatus
from fuseiot.state.persistence import SQLiteBackend
from fuseiot.utils.circuit_breaker import CircuitBreaker
from fuseiot.utils.rate_limiter import RateLimiter
from tests.helpers import StubProtocol, fast_config


class TestRegistration:
    def test_add_and_lookup(self, hub):
        lamp = hub.add_device(VirtualDevice("switchable"), Switchable, device_id="lamp")
        assert lamp.id == "lamp"
        assert "lamp" in hub and "other" not in hub
        assert hub["lamp"] is lamp and hub.get("lamp") is lamp
        assert len(hub) == 1 and list(hub) == ["lamp"]
        assert lamp.protocol.is_connected
        assert "devices=1" in repr(hub)

    def test_get_unknown_raises(self, hub):
        with pytest.raises(DeviceError):
            hub.get("ghost")
        with pytest.raises(DeviceError):
            hub["ghost"]

    def test_auto_ids(self, hub):
        a = hub.add_device(VirtualDevice(), Switchable)
        b = hub.add_device(VirtualDevice(), Switchable)
        assert (a.id, b.id) == ("switchable_1", "switchable_2")
        hub.remove("switchable_1")
        assert hub.add_device(VirtualDevice(), Switchable).id == "switchable_1"
        assert hub.add_device(VirtualDevice("dimmable"), Dimmable).id == "dimmable_1"

    def test_duplicate_id_rejected(self, hub):
        hub.add_device(VirtualDevice(), Switchable, device_id="x")
        with pytest.raises(ConfigurationError, match="already exists"):
            hub.add_device(VirtualDevice(), Switchable, device_id="x")

    def test_max_devices(self):
        with Hub(HubConfig(max_devices=1)) as h:
            h.add_device(VirtualDevice(), Switchable)
            with pytest.raises(ConfigurationError, match="Max devices"):
                h.add_device(VirtualDevice(), Switchable)

    def test_capability_by_name(self, hub):
        d = hub.add_device(VirtualDevice("dimmable"), " Dimmable ", device_id="d")
        assert isinstance(d, Dimmable)
        with pytest.raises(ConfigurationError, match="Unknown capability"):
            hub.add_device(VirtualDevice(), "teleporter")

    def test_connection_failure(self, hub):
        proto = StubProtocol(connect_ok=False)
        with pytest.raises(ConfigurationError, match="stub unreachable"):
            hub.add_device(proto, Switchable, device_id="x")
        assert "x" not in hub

    def test_connect_false_registers_lazily(self, hub):
        proto = StubProtocol(connect_ok=False)
        dev = hub.add_device(proto, Switchable, device_id="x", connect=False)
        assert proto.connect_calls == 0 and hub["x"] is dev

    def test_capability_construction_error(self, hub):
        with pytest.raises(ConfigurationError, match="Failed to create Motor"):
            hub.add_device(VirtualDevice("motor"), Motor, device_id="m", position_range=5)
        assert "m" not in hub

    def test_capability_kwargs_and_explicit_config(self, hub):
        cfg = fast_config(name="Probe")
        s = hub.add_device(VirtualDevice("sensor"), Sensor, device_id="s", config=cfg, unit="kPa")
        assert s.unit == "kPa"
        assert s.config.name == "Probe" and s.config is not cfg

    def test_default_ttl_argument(self):
        with Hub(default_ttl=0.25) as h:
            assert h.config.default_ttl == 0.25


class TestLookupAndRemoval:
    def test_find_and_list(self, hub):
        hub.add_device(VirtualDevice(), Switchable, device_id="a")
        hub.add_device(VirtualDevice("dimmable"), Dimmable, device_id="b")
        hub.add_device(StubProtocol(), Switchable, device_id="c")
        assert [d.id for d in hub.find(category="switchable")] == ["a", "c"]
        assert [d.id for d in hub.find(protocol="virtual")] == ["a", "b"]
        assert [d.id for d in hub.find(category="switchable", protocol="stubprotocol")] == ["c"]
        assert hub.list_devices() == ["a", "b", "c"]
        assert hub.list_devices(category="dimmable") == ["b"]
        assert hub.list_devices(as_dict=True) == {"a": "switchable", "b": "dimmable", "c": "switchable"}
        assert [d.id for d in hub.devices()] == ["a", "b", "c"]

    def test_remove(self, hub):
        lamp = hub.add_device(VirtualDevice(), Switchable, device_id="lamp", config=fast_config())
        lamp.on()
        assert hub.remove("lamp") is True
        assert "lamp" not in hub
        assert not lamp.protocol.is_connected
        assert hub.cache.peek("lamp") is None
        assert hub.remove("lamp") is False

    def test_shared_protocol_stays_connected_while_used(self, hub):
        proto = StubProtocol(lambda p: {"value": 1.0})
        hub.add_device(proto, Sensor, device_id="s1")
        hub.add_device(proto, Sensor, device_id="s2")
        hub.remove("s1")
        assert proto.disconnect_calls == 0
        hub.remove("s2")
        assert proto.disconnect_calls == 1

    def test_clear_disconnects_each_protocol_once(self, hub):
        shared = StubProtocol()
        other = StubProtocol()
        hub.add_device(shared, Switchable, device_id="a")
        hub.add_device(shared, Switchable, device_id="b")
        hub.add_device(other, Switchable, device_id="c")
        hub.cache.set("a", {"power": True})
        hub.clear()
        assert len(hub) == 0
        assert shared.disconnect_calls == 1 and other.disconnect_calls == 1
        assert hub.cache.keys() == set()
        assert hub.stats()["devices_removed_total"] == 3

    def test_close_is_idempotent_and_context_manager(self):
        proto = StubProtocol()
        with Hub() as h:
            h.add_device(proto, Switchable, device_id="a")
        assert proto.disconnect_calls == 1
        h.close()
        assert proto.disconnect_calls == 1

    async def test_async_context_manager(self):
        proto = StubProtocol()
        async with Hub() as h:
            h.add_device(proto, Switchable, device_id="a")
        assert proto.disconnect_calls == 1

    def test_disconnect_errors_are_swallowed(self, hub):
        proto = StubProtocol()
        proto.disconnect = lambda: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[assignment]
        hub.add_device(proto, Switchable, device_id="a")
        assert hub.remove("a") is True


class TestEventsAndStats:
    def test_on_state_change_filters(self, hub):
        a = hub.add_device(VirtualDevice(), Switchable, device_id="living_lamp", config=fast_config())
        b = hub.add_device(VirtualDevice(), Switchable, device_id="kitchen_lamp", config=fast_config())
        by_id, by_pattern, everything = [], [], []
        hub.on_state_change(by_id.append, device_id="living_lamp")
        hub.on_state_change(by_pattern.append, pattern="kitchen_*")
        unsubscribe = hub.on_state_change(everything.append)
        a.on()
        b.on()
        assert [e.device_id for e in by_id] == ["living_lamp"]
        assert [e.device_id for e in by_pattern] == ["kitchen_lamp"]
        assert [e.device_id for e in everything] == ["living_lamp", "kitchen_lamp"]
        unsubscribe()
        a.off()
        assert len(everything) == 2 and len(by_id) == 2

    def test_events_disabled(self):
        with Hub(HubConfig(enable_events=False)) as h:
            lamp = h.add_device(VirtualDevice(), Switchable, device_id="a", config=fast_config())
            assert lamp.on().confirmed
            assert h.event_bus is None
            with pytest.raises(ConfigurationError):
                h.on_state_change(lambda e: None)

    def test_stats_and_metrics(self, hub):
        lamp = hub.add_device(VirtualDevice(), Switchable, device_id="lamp", config=fast_config())
        broken = hub.add_device(VirtualDevice(failure_rate=1.0), Switchable, device_id="broken",
                                config=fast_config())
        lamp.on()
        broken.on()
        s = hub.stats()
        assert s["devices_active"] == 2 and s["devices_connected"] == 2
        assert s["devices_registered_total"] == 2
        assert s["metrics"] == {"commands_total": 2, "commands_success": 1, "commands_failed": 1}
        assert s["event_stats"]["total_events"] == 1
        assert "hits" in s["cache_stats"]
        counters = hub.metrics.get_stats()["counters"]
        assert counters['fuseiot_commands_total{command="on",device="lamp",status="confirmed"}'] == 1
        assert counters['fuseiot_commands_total{command="on",device="broken",status="failed"}'] == 1
        assert hub.metrics.get_stats()["gauges"]["fuseiot_devices"] == 2.0


class TestBatch:
    async def test_batch_all_succeed(self, hub, add_virtual):
        lamps = [add_virtual("switchable", Switchable, f"l{i}") for i in range(5)]
        batch = await hub.batch([(lamp, "on", {}) for lamp in lamps])
        assert batch.total == 5 and batch.successful == 5 and batch.confirmed == 5
        assert batch.success_rate == 100.0 and batch.all_succeeded
        assert batch.get_by_device("l3").status is CommandStatus.CONFIRMED
        assert all(lamp.protocol.state["power"] for lamp in lamps)

    async def test_batch_reports_failures(self, hub, add_virtual):
        ok = add_virtual("switchable", Switchable, "ok")
        bad = add_virtual("switchable", Switchable, "bad", failure_rate=1.0)
        batch = await hub.batch([(ok, "on", {}), (bad, "on", {}), (ok, "fly", {})])
        assert batch.successful == 1 and batch.failed == 2
        assert batch.success_rate == pytest.approx(33.33, abs=0.01)
        errors = {r.command: r for r in batch.get_failures()}
        assert errors["on"].status is CommandStatus.FAILED
        assert errors["fly"].status is CommandStatus.FAILED and "fly" in errors["fly"].error

    async def test_batch_non_command_method_is_acknowledged(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        batch = await hub.batch([(lamp, "read_state", {"fresh": True})])
        r = batch.results[0]
        assert r.status is CommandStatus.ACKNOWLEDGED
        assert r.metadata["return_value"] == {"power": False}

    async def test_batch_stop_on_first_error(self, hub, add_virtual):
        bad = add_virtual("switchable", Switchable, "bad", failure_rate=1.0)
        slow = [add_virtual("switchable", Switchable, f"s{i}", latency_ms=300) for i in range(3)]
        ops = [(bad, "on", {})] + [(s, "on", {}) for s in slow]
        batch = await hub.batch(ops, continue_on_error=False)
        assert batch.total == 4
        assert len(batch.results) < 4
        assert batch.results[-1].device_id == "bad"
        assert batch.failed == batch.total - batch.successful

    async def test_batch_max_concurrency(self, hub, add_virtual):
        lamps = [add_virtual("switchable", Switchable, f"l{i}", latency_ms=40) for i in range(3)]
        start = time.monotonic()
        batch = await hub.batch([(lamp, "on", {}) for lamp in lamps], max_concurrent=1)
        # each command = POST + confirming GET, 40 ms each, run strictly one at a time
        assert time.monotonic() - start >= 0.22
        assert batch.successful == 3

    async def test_batch_rejects_bad_concurrency(self, hub):
        with pytest.raises(ValueError):
            await hub.batch([], max_concurrent=0)

    def test_batch_sync(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        result = hub.batch_sync([(lamp, "on", None)])
        assert result.successful == 1


class TestHubConfig:
    def test_per_device_breakers_and_limiters(self):
        cfg = HubConfig(circuit_breaker_enabled=True, circuit_failure_threshold=2,
                        rate_limit_enabled=True, rate_limit_requests=7, rate_limit_window=3.0,
                        capability_defaults=CapabilityConfig(timeout=0.7))
        with Hub(cfg) as h:
            a = h.add_device(VirtualDevice(), Switchable, device_id="a")
            b = h.add_device(VirtualDevice(), Switchable, device_id="b")
            assert isinstance(a.config.circuit_breaker, CircuitBreaker)
            assert a.config.circuit_breaker is not b.config.circuit_breaker
            assert a.config.circuit_breaker.failure_threshold == 2
            assert isinstance(a.config.rate_limiter, RateLimiter)
            assert a.config.rate_limiter is not b.config.rate_limiter
            assert (a.config.rate_limiter.requests, a.config.rate_limiter.window) == (7, 3.0)
            assert a.config.timeout == 0.7
            assert cfg.capability_defaults.circuit_breaker is None

    def test_from_config(self, tmp_path):
        config = Config(default_ttl=1.5, max_devices=3, confirm_timeout=0.8, confirm_default=False,
                        default_retry_attempts=2, confirm_poll_interval=0.05,
                        circuit_breaker_enabled=False, rate_limit_enabled=True,
                        persistence_enabled=True, persistence_path=str(tmp_path / "s.db"))
        hc = HubConfig.from_config(config)
        assert hc.default_ttl == 1.5 and hc.max_devices == 3
        assert isinstance(hc.persistence, SQLiteBackend)
        d = hc.capability_defaults
        assert (d.timeout, d.confirm, d.retry_attempts, d.poll_interval) == (0.8, False, 2, 0.05)
        assert hc.rate_limit_enabled and not hc.circuit_breaker_enabled
        assert hc.history_path is None

    def test_from_config_memory_backend(self):
        hc = HubConfig.from_config(Config(persistence_enabled=True, persistence_backend="memory"))
        assert hc.persistence is None

    def test_hub_from_config_with_history(self, tmp_path):
        config = Config(history_enabled=True, history_path=str(tmp_path / "h.db"),
                        confirm_poll_interval=0.01, circuit_breaker_enabled=True)
        with Hub(config) as h:
            assert h.source_config is config
            lamp = h.add_device(VirtualDevice(), Switchable, device_id="lamp")
            assert lamp.config.circuit_breaker is not None
            lamp.on()
            lamp.off()
            history = h.history.get_history("lamp")
            assert [entry["state"] for entry in history] == [{"power": False}, {"power": True}]

    def test_persisted_state_restored_by_new_hub(self, tmp_path):
        db = str(tmp_path / "state.db")
        with Hub(HubConfig(persistence=SQLiteBackend(db))) as h1:
            lamp = h1.add_device(VirtualDevice(), Switchable, device_id="lamp", config=fast_config())
            lamp.on()
        with Hub(HubConfig(persistence=SQLiteBackend(db))) as h2:
            lamp2 = h2.add_device(VirtualDevice(), Switchable, device_id="lamp", connect=False)
            assert lamp2.read_state() == {"power": True}  # served from persistence, device not polled
            assert lamp2.protocol.requests == []
