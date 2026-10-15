"""Capabilities and the shared command pipeline, driven by in-process VirtualDevices."""
import asyncio
import time

import pytest

from fuseiot import (
    Color,
    Dimmable,
    EnergyMonitor,
    Lock,
    LockState,
    Motor,
    RGBLight,
    Sensor,
    SmartPlug,
    Switchable,
    Thermostat,
    ThermostatMode,
    VirtualDevice,
)
from fuseiot.capabilities.base import CapabilityConfig
from fuseiot.capabilities.energy_monitor import EnergyReading
from fuseiot.capabilities.thermostat import ThermostatConfig
from fuseiot.exceptions import StateError
from fuseiot.result import CommandResult, CommandStatus
from fuseiot.state.cache import StateCache
from tests.helpers import FlakyVirtualDevice, StubProtocol, fast_config


def gets(device: VirtualDevice) -> int:
    return sum(1 for r in device.requests if r.get("_method") == "GET")


# =========================================================================== pipeline


class TestPipeline:
    def test_on_is_confirmed(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        r = lamp.on()
        assert r.success and r.confirmed
        assert r.status is CommandStatus.CONFIRMED
        assert r.device_id == "lamp" and r.command == "on"
        assert r.state_after == {"power": True}
        assert r.attempts == 1 and r.retries == 0
        assert r.latency_ms > 0 and r.network_latency_ms is not None
        assert r.confirmation_latency_ms is not None
        assert lamp.protocol.state["power"] is True

    def test_command_payload_follows_contract(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.on()
        post = [r for r in lamp.protocol.requests if r["_method"] == "POST"]
        assert post == [{"_method": "POST", "_path": "/on", "power": True}]

    def test_state_before_is_last_known(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        assert lamp.on().state_before is None
        assert lamp.off().state_before == {"power": True}

    def test_confirmed_state_is_cached_under_device_id(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.on()
        assert hub.cache.get("lamp") == {"power": True}
        before = gets(lamp.protocol)
        assert lamp.is_on is True
        assert gets(lamp.protocol) == before, "cached read should not hit the device"

    def test_confirm_false_acknowledges_and_invalidates_cache(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.read_state()
        r = lamp.on(confirm=False)
        assert r.success and not r.confirmed
        assert r.status is CommandStatus.ACKNOWLEDGED
        assert r.raw_response == {"status": "accepted", "path": "/on"}
        assert hub.cache.get("lamp") is None
        assert gets(lamp.protocol) == 1  # only the explicit read_state above

    def test_config_confirm_false_is_default_for_calls(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp", config=fast_config(confirm=False))
        assert lamp.on().status is CommandStatus.ACKNOWLEDGED
        assert lamp.on(confirm=True).status is CommandStatus.CONFIRMED

    def test_slow_actuator_is_confirmed_by_polling(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp", settle_ms=120)
        r = lamp.on()
        assert r.confirmed
        assert r.confirmation_latency_ms >= 100
        assert gets(lamp.protocol) >= 2

    def test_unreached_state_times_out(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.protocol.on_request("/on", lambda payload: {"status": "ignored"})
        start = time.monotonic()
        r = lamp.on(timeout=0.2)
        assert time.monotonic() - start < 1.0
        assert not r.success and not r.confirmed
        assert r.status is CommandStatus.TIMEOUT
        assert r.error_code == "CONFIRMATION_TIMEOUT"
        assert r.state_after == {"power": False}
        assert hub.cache.peek("lamp") is None

    def test_transport_failure_retries_then_fails(self, add_virtual):
        cfg = fast_config(retry_attempts=3)
        lamp = add_virtual("switchable", Switchable, "lamp", config=cfg, failure_rate=1.0)
        r = lamp.on()
        assert r.status is CommandStatus.FAILED
        assert not r.success
        assert r.attempts == 3 and r.retries == 2
        assert "Simulated device failure" in r.error
        assert r.error_code == "PROTOCOL_VIRTUAL_ERROR"
        assert len(lamp.protocol.requests) == 3

    def test_retry_recovers_from_transient_failure(self, hub):
        dev = FlakyVirtualDevice("switchable", fail_sends=1)
        lamp = hub.add_device(dev, Switchable, device_id="lamp", config=fast_config(retry_attempts=3))
        r = lamp.on()
        assert r.confirmed and r.attempts == 2 and r.retries == 1

    def test_retry_backoff_respects_deadline(self, add_virtual):
        cfg = fast_config(retry_attempts=5, retry_delay=1.0)
        lamp = add_virtual("switchable", Switchable, "lamp", config=cfg, failure_rate=1.0)
        start = time.monotonic()
        r = lamp.on(timeout=0.3)
        assert time.monotonic() - start < 0.8
        assert r.status is CommandStatus.FAILED and r.attempts == 1

    def test_failed_confirmation_polls_are_tolerated(self, hub):
        dev = FlakyVirtualDevice("switchable", fail_gets=2)
        lamp = hub.add_device(dev, Switchable, device_id="lamp", config=fast_config())
        assert lamp.on().confirmed

    def test_never_raises_for_device_problems(self, hub):
        proto = StubProtocol(lambda p: (_ for _ in ()).throw(RuntimeError("kaput")))
        lamp = hub.add_device(proto, Switchable, device_id="lamp", config=fast_config())
        r = lamp.on()
        assert r.status is CommandStatus.FAILED
        assert r.error == "kaput" and r.error_code == "RuntimeError"


class TestReadState:
    def test_read_state_uses_cache_unless_fresh(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.read_state()
        lamp.read_state()
        assert gets(lamp.protocol) == 1
        lamp.read_state(fresh=True)
        assert gets(lamp.protocol) == 2

    def test_read_state_raises_state_error(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp", failure_rate=1.0)
        with pytest.raises(StateError) as exc:
            lamp.read_state(fresh=True)
        assert "lamp" in exc.value.message
        assert exc.value.details["device_id"] == "lamp"

    def test_last_known_state_survives_expiry(self, hub):
        lamp = hub.add_device(VirtualDevice("switchable"), Switchable, device_id="lamp",
                              config=fast_config())
        hub.cache.set("lamp", {"power": True}, ttl=0.01)
        time.sleep(0.03)
        assert hub.cache.get("lamp") is None
        assert lamp.last_known_state() == {"power": True}

    def test_poll_change_emits_event(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        events = []
        hub.on_state_change(events.append, device_id="lamp")
        lamp.read_state()
        assert events == []  # nothing known before: no change event
        lamp.protocol.set_state({"power": True})
        lamp.read_state(fresh=True)
        assert len(events) == 1
        assert events[0].source == "poll"
        assert events[0].old_state == {"power": False} and events[0].new_state == {"power": True}
        lamp.read_state(fresh=True)
        assert len(events) == 1  # unchanged -> no event

    def test_command_emits_event(self, hub, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        events = []
        hub.on_state_change(events.append)
        lamp.on()
        assert len(events) == 1
        e = events[0]
        assert e.device_id == "lamp" and e.source == "command"
        assert e.new_state == {"power": True} and e.latency_ms is not None


class TestListenersAndMetrics:
    def test_result_listener_and_removal(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        seen = []
        remove = lamp.add_result_listener(lambda cap, res: seen.append((cap.id, res.command)))
        lamp.on()
        remove()
        lamp.off()
        assert seen == [("lamp", "on")]

    def test_failing_listener_does_not_break_command(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.add_result_listener(lambda cap, res: 1 / 0)
        assert lamp.on().confirmed

    def test_metrics(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        lamp.on()
        lamp.protocol.on_request("/off", lambda p: {})
        lamp.off(timeout=0.05)
        m = lamp.get_metrics()
        assert m["commands_total"] == 2
        assert m["commands_success"] == 1 and m["commands_failed"] == 1
        assert m["confirmation_failures"] == 1
        assert m["avg_latency_ms"] > 0

    def test_metrics_can_be_disabled(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp", config=fast_config(metrics_enabled=False))
        lamp.on()
        assert lamp.get_metrics()["commands_total"] == 0


class TestAsyncSugar:
    async def test_every_command_has_async_twin(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        r = await lamp.on_async()
        assert isinstance(r, CommandResult) and r.confirmed
        assert (await lamp.read_state_async(fresh=True)) == {"power": True}
        r2 = await lamp.toggle_async(confirm=False)
        assert r2.status is CommandStatus.ACKNOWLEDGED

    async def test_concurrent_async_commands(self, add_virtual):
        lamps = [add_virtual("switchable", Switchable, f"l{i}", latency_ms=30) for i in range(4)]
        results = await asyncio.gather(*(lamp.on_async() for lamp in lamps))
        assert all(r.confirmed for r in results)

    def test_unknown_async_attribute(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        with pytest.raises(AttributeError):
            lamp.fly_async
        with pytest.raises(AttributeError):
            lamp._fetch_state_async
        with pytest.raises(AttributeError):
            lamp.is_on_async  # property, not callable
        with pytest.raises(AttributeError):
            lamp.nonexistent


class TestIdentity:
    def test_describe(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp", config=fast_config(name="Lamp"))
        d = lamp.describe()
        assert d == {"device_id": "lamp", "name": "Lamp", "category": "switchable",
                     "protocol": "virtual", "endpoint": "virtual://switchable",
                     "connected": True, "commands": ["on", "off", "toggle"]}
        assert "lamp" in repr(lamp)

    def test_generated_id_when_constructed_directly(self):
        cap = Switchable(VirtualDevice("switchable"), StateCache())
        assert cap.id.startswith("switchable_") and cap.device_id == cap.id

    def test_config_is_copied_per_device(self, hub):
        shared = CapabilityConfig(tolerance=1e-6)
        m1 = hub.add_device(VirtualDevice("motor"), Motor, device_id="m1", config=shared)
        assert m1.config.tolerance == 0.5
        assert shared.tolerance == 1e-6
        assert m1.config is not shared


# =========================================================================== capabilities


class TestSwitchable:
    def test_toggle(self, add_virtual):
        lamp = add_virtual("switchable", Switchable, "lamp")
        assert lamp.toggle().state_after == {"power": True}
        assert lamp.toggle().state_after == {"power": False}

    @pytest.mark.parametrize("raw,expected", [
        ({"power": True}, True), ({"state": "on"}, True), ({"state": "ON"}, True),
        ({"power": "yes"}, True), ({"power": "1"}, True), ({"state": "off"}, False),
        ({"power": 0}, False), ({}, False), ("garbage", False),
    ])
    def test_state_normalisation(self, hub, raw, expected):
        lamp = hub.add_device(StubProtocol(lambda p: raw), Switchable, device_id="s",
                              config=fast_config())
        assert lamp.read_state(fresh=True) == {"power": expected}


class TestDimmable:
    def test_on_with_brightness(self, add_virtual):
        light = add_virtual("dimmable", Dimmable, "dim")
        r = light.on(brightness=40)
        assert r.confirmed and r.state_after == {"power": True, "brightness": 40}
        assert light.brightness == 40 and light.is_on

    @pytest.mark.parametrize("value,expected", [(150, 100), (-5, 0), (0.5, 50), (1.0, 100), (42.6, 43), (1, 1)])
    def test_brightness_clamping(self, add_virtual, value, expected):
        light = add_virtual("dimmable", Dimmable, "dim")
        r = light.set_brightness(value)
        assert r.confirmed and r.state_after["brightness"] == expected

    def test_custom_brightness_range(self, add_virtual):
        light = add_virtual("dimmable", Dimmable, "dim", cap_kwargs={"min_brightness": 10, "max_brightness": 80})
        assert light.set_brightness(5).state_after["brightness"] == 10
        assert light.set_brightness(95).state_after["brightness"] == 80

    def test_fade_to(self, add_virtual):
        light = add_virtual("dimmable", Dimmable, "dim")
        r = light.fade_to(70, duration_ms=200)
        assert r.confirmed
        fade = [p for p in light.protocol.requests if p["_path"] == "/fade"]
        assert fade == [{"_method": "POST", "_path": "/fade", "brightness": 70, "duration_ms": 200}]

    def test_toggle_and_off(self, add_virtual):
        light = add_virtual("dimmable", Dimmable, "dim")
        assert light.toggle().state_after["power"] is True
        assert light.off().state_after["power"] is False


class TestSensor:
    def test_read(self, add_virtual):
        s = add_virtual("sensor", Sensor, "t", initial_state={"value": 21.5})
        reading = s.read(fresh=True)
        assert reading["value"] == 21.5
        assert reading["unit"] == "celsius"
        assert reading["fresh"] is True
        assert 0 <= reading["age_seconds"] < 5
        assert s.value == 21.5

    def test_unit_fallback_and_scalar_response(self, hub):
        s = hub.add_device(StubProtocol(lambda p: 7), Sensor, device_id="t", config=fast_config(), unit="kg")
        state = s.read_state(fresh=True)
        assert state["value"] == 7.0 and state["unit"] == "kg"

    def test_reading_alias_and_timestamp(self, hub):
        s = hub.add_device(StubProtocol(lambda p: {"reading": "3.5", "timestamp": 100.0}), Sensor,
                           device_id="t", config=fast_config())
        state = s.read_state(fresh=True)
        assert state["value"] == 3.5 and state["timestamp"] == 100.0

    def test_missing_value_is_state_error(self, hub):
        s = hub.add_device(StubProtocol(lambda p: {"unit": "x"}), Sensor, device_id="t", config=fast_config())
        with pytest.raises(StateError):
            s.read_state(fresh=True)

    def test_noise_is_bounded(self, add_virtual):
        s = add_virtual("sensor", Sensor, "t", sensor_noise=0.5, seed=1, initial_state={"value": 10.0})
        values = [s.read(fresh=True)["value"] for _ in range(20)]
        assert all(9.5 <= v <= 10.5 for v in values)
        assert len(set(values)) > 1

    def test_calibrate(self, add_virtual):
        s = add_virtual("sensor", Sensor, "t")
        r = s.calibrate(20.0)
        assert r.confirmed and r.state_after["calibrated"] is True
        assert {"_method": "POST", "_path": "/calibrate", "reference": 20.0} in s.protocol.requests


class TestMotor:
    def test_move_to_clamps(self, add_virtual):
        m = add_virtual("motor", Motor, "m", cap_kwargs={"position_range": (0, 1000)})
        r = m.move_to(1500, speed=500)
        assert r.confirmed and r.state_after["position"] == 1000.0
        move = [p for p in m.protocol.requests if p["_path"] == "/move"][0]
        assert move["position"] == 1000.0 and move["speed"] == 100.0
        assert m.position == 1000.0 and m.is_moving is False

    def test_position_tolerance(self, add_virtual):
        m = add_virtual("motor", Motor, "m")

        def sloppy(payload):
            m.protocol.set_state({"position": payload["position"] + 0.3, "moving": False})
            return {"status": "accepted"}

        m.protocol.on_request("/move", sloppy)
        assert m.move_to(90).confirmed

        def way_off(payload):
            m.protocol.set_state({"position": payload["position"] + 2.0, "moving": False})
            return {"status": "accepted"}

        m.protocol.on_request("/move", way_off)
        r = m.move_to(45, timeout=0.15)
        assert r.status is CommandStatus.TIMEOUT

    def test_speed_stop_home(self, add_virtual):
        m = add_virtual("motor", Motor, "m")
        assert m.set_speed(250).state_after["speed"] == 100.0
        m.protocol.set_state({"moving": True})
        assert m.stop().confirmed
        m.protocol.set_state({"position": 42.0, "homed": False})
        r = m.home()
        assert r.confirmed and r.state_after["position"] == 0.0 and r.state_after["homed"] is True


class TestThermostat:
    def test_set_temperature_clamps(self, add_virtual):
        t = add_virtual("thermostat", Thermostat, "th")
        assert t.set_temperature(35).state_after["target_temperature"] == 30.0
        assert t.set_temperature(2).state_after["target_temperature"] == 10.0
        assert t.target_temperature == 10.0

    def test_limits_from_config_and_kwargs(self, hub):
        cfg = ThermostatConfig(timeout=1.0, retry_attempts=1, poll_interval=0.01, min_temp=5, max_temp=25)
        t = hub.add_device(VirtualDevice("thermostat"), Thermostat, device_id="a", config=cfg)
        assert (t.min_temp, t.max_temp) == (5, 25)
        t2 = hub.add_device(VirtualDevice("thermostat"), Thermostat, device_id="b", config=cfg, max_temp=22)
        assert t2.set_temperature(28).state_after["target_temperature"] == 22.0

    def test_modes_and_heating_flag(self, add_virtual):
        t = add_virtual("thermostat", Thermostat, "th")
        assert t.set_temperature(25).confirmed
        assert t.set_mode(ThermostatMode.HEAT).state_after["mode"] == "heat"
        t.read_state(fresh=True)
        assert t.is_heating and not t.is_cooling
        assert t.set_mode("cool").confirmed and t.mode == "cool"

    def test_invalid_mode_raises(self, add_virtual):
        t = add_virtual("thermostat", Thermostat, "th")
        with pytest.raises(ValueError):
            t.set_mode("turbo")

    def test_fan(self, add_virtual):
        t = add_virtual("thermostat", Thermostat, "th")
        assert t.fan_on().state_after["fan_running"] is True
        assert t.fan_off().state_after["fan_running"] is False


class TestLock:
    def test_lock_unlock(self, add_virtual):
        lock = add_virtual("lock", Lock, "door")
        r = lock.unlock(user_code="1234")
        assert r.confirmed and r.state_after["lock_state"] == "unlocked"
        sent = [p for p in lock.protocol.requests if p["_path"] == "/lock"][0]
        assert sent["user_code"] == "1234" and sent["action"] == "unlock"
        assert lock.is_locked is False
        assert lock.lock().confirmed and lock.is_locked
        assert lock.lock_state is LockState.LOCKED
        assert lock.battery_level == 100

    def test_unknown_lock_state_is_normalised(self, add_virtual):
        lock = add_virtual("lock", Lock, "door", initial_state={"lock_state": "HALFWAY"})
        assert lock.read_state(fresh=True)["lock_state"] == "unknown"
        assert lock.lock_state is LockState.UNKNOWN


class TestRGBLight:
    def test_set_color(self, add_virtual):
        light = add_virtual("rgb_light", RGBLight, "rgb")
        r = light.set_color(Color(10, 20, 30), brightness=0.5)
        assert r.confirmed
        assert r.state_after["hex"] == "#0a141e" and r.state_after["brightness"] == 50
        assert light.color == Color(10, 20, 30)
        assert light.set_color((1, 2, 3)).state_after["r"] == 1

    def test_set_hex_and_hsv(self, add_virtual):
        light = add_virtual("rgb_light", RGBLight, "rgb")
        assert light.set_hex("#00ff00").state_after["g"] == 255
        r = light.set_hsv(240, 1.0, 1.0)
        assert r.command == "set_hsv"
        assert (r.state_after["r"], r.state_after["g"], r.state_after["b"]) == (0, 0, 255)
        h, s, v = light.hsv
        assert round(h) == 240 and s == 1.0 and v == 1.0

    def test_invalid_color_is_rejected_before_sending(self, add_virtual):
        light = add_virtual("rgb_light", RGBLight, "rgb")
        with pytest.raises(ValueError):
            light.set_color((300, 0, 0))
        with pytest.raises(ValueError):
            light.set_hex("nope")
        assert light.protocol.requests == []

    def test_on_effect_brightness(self, add_virtual):
        light = add_virtual("rgb_light", RGBLight, "rgb")
        r = light.on(color=(255, 0, 0), brightness=30)
        assert r.confirmed and r.state_after["power"] is True and r.state_after["r"] == 255
        assert light.set_effect("rainbow", speed=80).state_after["effect"] == "rainbow"
        assert light.set_brightness(0.25).state_after["brightness"] == 25
        assert light.brightness == 25 and light.is_on
        assert light.toggle().state_after["power"] is False


class TestEnergyMonitor:
    def test_read_and_cost(self, add_virtual):
        em = add_virtual("energy_monitor", EnergyMonitor, "em",
                         initial_state={"power": 120.0, "current": 0.5, "energy": 2.0, "frequency": 50},
                         cap_kwargs={"cost_per_kwh": 0.25, "currency": "EUR"})
        reading = em.read(fresh=True)
        assert isinstance(reading, EnergyReading)
        assert reading.power_watts == 120.0 and reading.energy_kwh == 2.0 and reading.frequency_hz == 50
        state = em.read_state()
        assert state["cost"] == 0.5 and state["currency"] == "EUR"
        assert em.total_cost == 0.5 and em.today_cost == 0.5
        assert em.power == 120.0 and em.energy_kwh == 2.0
        assert em.calculate_cost(4) == 1.0

    def test_reset_counter(self, add_virtual):
        em = add_virtual("energy_monitor", EnergyMonitor, "em", initial_state={"energy": 5.0})
        r = em.reset_counter()
        assert r.confirmed and r.state_after["energy_kwh"] == 0.0


class TestSmartPlug:
    def test_sub_capability_ids(self, add_virtual):
        plug = add_virtual("smart_plug", SmartPlug, "plug")
        assert plug.switch.id == "plug.switch" and plug.energy.id == "plug.energy"
        assert plug.get_capability("energy") is plug.energy
        assert plug.category == "smart_plug"
        assert plug.last_known_state() is None

    def test_on_draws_power(self, hub, add_virtual):
        plug = add_virtual("smart_plug", SmartPlug, "plug", load_watts=100.0)
        r = plug.on()
        assert r.confirmed and r.device_id == "plug.switch"
        assert plug.is_on
        assert plug.power == 100.0
        state = plug.read_state()
        assert set(state) == {"switch", "energy"}
        assert hub.cache.get("plug.switch") == {"power": True}
        assert plug.last_known_state()["switch"] == {"power": True}
        assert plug.off().confirmed and plug.toggle().confirmed

    def test_reset_counter_and_cost(self, add_virtual):
        plug = add_virtual("smart_plug", SmartPlug, "plug", cap_kwargs={"cost_per_kwh": 0.5})
        assert plug.reset_counter().confirmed
        assert plug.today_cost == 0.0
        assert plug.energy.cost_per_kwh == 0.5

    def test_hub_sees_sub_results_and_events(self, hub, add_virtual):
        plug = add_virtual("smart_plug", SmartPlug, "plug")
        events = []
        hub.on_state_change(events.append, pattern="plug.*")
        plug.on()
        assert [e.device_id for e in events] == ["plug.switch"]
        assert hub.stats()["metrics"]["commands_total"] == 1
