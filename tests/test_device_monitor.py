"""Background device monitor: online/offline transitions, listeners and alerts."""
import time

import pytest

from fuseiot import Switchable
from fuseiot.monitoring.device_monitor import DeviceMonitor, DeviceStatus
from fuseiot.notify import LogChannel, NotificationManager
from tests.helpers import fast_config, wait_for


@pytest.fixture
def make_monitor(hub):
    created = []

    def _make(**kw):
        mon = DeviceMonitor(hub, **kw)
        created.append(mon)
        return mon

    yield _make
    for mon in created:
        mon.stop()


@pytest.fixture
def lamp(add_virtual):
    return add_virtual("switchable", Switchable, "lamp")


def fail(device, on=True):
    device.protocol.sim.failure_rate = 1.0 if on else 0.0


class TestTransitions:
    def test_first_successful_poll_marks_online(self, make_monitor, lamp):
        mon = make_monitor(interval=1)
        events = []
        mon.listeners.append(lambda dev, st: events.append((dev, st["online"])))
        assert mon.get("lamp") is None
        mon.poll_all()
        st = mon.get("lamp")
        assert st.online is True and st.failures == 0 and st.last_seen is not None and st.changed_at is not None
        assert events == [("lamp", True)]
        mon.poll_all()
        assert events == [("lamp", True)]  # no change, no event

    def test_offline_after_consecutive_failures(self, make_monitor, lamp):
        mon = make_monitor(interval=1, offline_after=3)
        mon.poll_all()
        fail(lamp)
        mon.poll_all()
        mon.poll_all()
        st = mon.get("lamp")
        assert st.online is True and st.failures == 2 and "lamp" in st.last_error
        mon.poll_all()
        assert mon.get("lamp").online is False and mon.offline_devices() == ["lamp"]

    def test_recovery_resets_failures(self, make_monitor, lamp):
        mon = make_monitor(interval=1, offline_after=2)
        fail(lamp)
        mon.poll_all()
        assert mon.get("lamp").online is None  # unknown until offline_after reached
        mon.poll_all()
        assert mon.get("lamp").online is False
        fail(lamp, False)
        mon.poll_all()
        st = mon.get("lamp")
        assert st.online is True and st.failures == 0 and st.last_error is None
        assert mon.offline_devices() == []

    def test_single_failure_does_not_flap(self, make_monitor, lamp):
        mon = make_monitor(interval=1, offline_after=2)
        events = []
        mon.listeners.append(lambda dev, st: events.append(st["online"]))
        mon.poll_all()
        fail(lamp)
        mon.poll_all()
        fail(lamp, False)
        mon.poll_all()
        assert events == [True]

    def test_offline_after_minimum_is_one(self, make_monitor, lamp):
        mon = make_monitor(interval=1, offline_after=0)
        assert mon.offline_after == 1
        fail(lamp)
        mon.poll_all()
        assert mon.get("lamp").online is False

    def test_invalid_interval(self, hub):
        with pytest.raises(ValueError):
            DeviceMonitor(hub, interval=0)

    def test_poll_updates_cache_for_manual_changes(self, make_monitor, lamp):
        mon = make_monitor(interval=1)
        mon.poll_all()
        lamp.protocol.set_state({"power": True})
        mon.poll_all()
        assert lamp.last_known_state() == {"power": True}


class TestListenersAndAlerts:
    def test_listener_payload_and_errors_are_contained(self, make_monitor, lamp):
        mon = make_monitor(interval=1, offline_after=1)
        seen = []

        def broken(dev, st):
            raise RuntimeError("listener bug")

        mon.listeners.append(broken)
        mon.listeners.append(lambda dev, st: seen.append((dev, st)))
        fail(lamp)
        mon.poll_all()
        dev, st = seen[0]
        assert dev == "lamp" and st["online"] is False and st["failures"] == 1
        assert set(st) == {"online", "failures", "last_seen", "last_error", "changed_at"}

    def test_offline_and_online_alerts(self, make_monitor, add_virtual):
        porch = add_virtual("switchable", Switchable, "porch", config=fast_config(name="Porch Light"))
        channel = LogChannel("log")
        notifier = NotificationManager({"log": channel}, alerts={"device_offline": ["log"],
                                                                 "device_online": ["log"]}, cooldown=0)
        mon = make_monitor(interval=1, offline_after=1, notifier=notifier)
        mon.poll_all()
        assert channel.sent == []  # the first "online" is not alerted
        fail(porch)
        mon.poll_all()
        assert len(channel.sent) == 1
        offline = channel.sent[0]
        assert offline["title"] == "Porch Light is offline" and offline["tags"] == ["warning"]
        assert "Porch Light (porch) stopped responding" in offline["message"]
        assert offline["priority"] == "high"
        mon.poll_all()  # still offline: no repeat
        assert len(channel.sent) == 1
        fail(porch, False)
        mon.poll_all()
        assert channel.sent[-1]["title"] == "Porch Light is back online"
        assert channel.sent[-1]["tags"] == ["white_check_mark"]
        assert len(channel.sent) == 2

    def test_initially_unreachable_device_alerts_offline_only(self, make_monitor, lamp):
        channel = LogChannel("log")
        notifier = NotificationManager({"log": channel}, alerts={"device_offline": ["log"],
                                                                 "device_online": ["log"]})
        mon = make_monitor(interval=1, offline_after=1, notifier=notifier)
        fail(lamp)
        mon.poll_all()
        fail(lamp, False)
        mon.poll_all()
        titles = [s["title"] for s in channel.sent]
        assert titles == ["lamp is offline", "lamp is back online"]


class TestSnapshotAndPruning:
    def test_snapshot_and_removed_devices_pruned(self, hub, make_monitor, lamp, add_virtual):
        add_virtual("switchable", Switchable, "fan")
        mon = make_monitor(interval=1)
        mon.poll_all()
        snap = mon.snapshot()
        assert set(snap) == {"lamp", "fan"} and snap["lamp"]["online"] is True
        hub.remove("fan")
        mon.poll_all()
        assert set(mon.snapshot()) == {"lamp"} and mon.get("fan") is None

    def test_status_to_dict(self):
        assert DeviceStatus().to_dict() == {"online": None, "failures": 0, "last_seen": None,
                                            "last_error": None, "changed_at": None}


class TestBackground:
    def test_background_loop_detects_offline_and_stops(self, make_monitor, lamp):
        mon = make_monitor(interval=0.02, offline_after=2)
        mon.start_background()
        wait_for(lambda: mon.get("lamp") is not None and mon.get("lamp").online is True)
        fail(lamp)
        wait_for(lambda: mon.offline_devices() == ["lamp"])
        t0 = time.monotonic()
        mon.stop()
        assert time.monotonic() - t0 < 3 and mon._thread is None

    def test_loop_survives_poll_errors(self, make_monitor, monkeypatch):
        mon = make_monitor(interval=0.02)
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("hub exploded")

        monkeypatch.setattr(mon.hub, "devices", boom)
        mon.start_background()
        wait_for(lambda: len(calls) >= 3)
        mon.stop()
