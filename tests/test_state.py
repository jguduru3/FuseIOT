"""State cache, persistence, event bus, history and confirmation."""
import functools
import time

import pytest

from fuseiot.state.cache import CacheEntry, StateCache
from fuseiot.state.confirm import ConfirmationEngine, confirm_state, state_matches, values_match
from fuseiot.state.events import EventBus, StateChangeEvent
from fuseiot.state.history import StateHistory
from fuseiot.state.persistence import PersistenceBackend, SQLiteBackend


def event(device_id="lamp", old=None, new=None, source="command"):
    return StateChangeEvent(device_id=device_id, timestamp=time.time(), old_state=old,
                            new_state=new if new is not None else {"power": True}, source=source)


class TestStateCache:
    def test_set_get(self):
        c = StateCache()
        c.set("a", {"x": 1}, source="command")
        assert c.get("a") == {"x": 1}
        assert c.get("missing", default="dflt") == "dflt"
        entry = c.peek("a")
        assert isinstance(entry, CacheEntry) and entry.source == "command" and entry.access_count == 1

    def test_ttl_expiry_keeps_last_known(self):
        c = StateCache(default_ttl=0.02)
        c.set("a", 1)
        time.sleep(0.04)
        assert c.get("a") is None
        assert c.peek("a").value == 1 and not c.peek("a").is_valid
        assert c.stats()["stale_entries"] == 1

    def test_per_key_ttl_and_max_age(self):
        c = StateCache(default_ttl=0.01)
        c.set("long", 1, ttl=60)
        time.sleep(0.03)
        assert c.get("long") == 1
        assert c.get("long", max_age=0.0) is None

    def test_lru_eviction(self):
        c = StateCache(max_size=2)
        c.set("a", 1)
        c.set("b", 2)
        c.get("a")  # a becomes most recently used
        c.set("c", 3)
        assert c.keys() == {"a", "c"}
        assert c.stats()["evictions"] == 1

    def test_change_callbacks(self):
        c = StateCache()
        seen = []
        unregister = c.on_change(lambda k, old, new: seen.append((k, old, new)))
        c.on_change(lambda k, old, new: 1 / 0)  # errors are logged, not raised
        c.set("a", 1)
        c.set("a", 1)  # unchanged -> no callback
        c.set("a", 2)
        unregister()
        c.set("a", 3)
        assert seen == [("a", None, 1), ("a", 1, 2)]

    def test_invalidation(self):
        c = StateCache()
        for k in ("room1.lamp", "room1.fan", "room2.lamp"):
            c.set(k, 1)
        assert c.invalidate("room2.lamp") is True and c.invalidate("room2.lamp") is False
        assert c.invalidate_pattern("room1") == 2
        assert c.keys() == set()
        c.set("x", 1)
        c.invalidate_all()
        assert c.keys() == set()

    def test_stats(self):
        c = StateCache(max_size=10)
        c.set("a", 1)
        c.get("a")
        c.get("b")
        s = c.stats()
        assert (s["hits"], s["misses"], s["hit_rate_percent"]) == (1, 1, 50.0)
        assert s["utilization_percent"] == 10.0 and s["persistence_enabled"] is False


class TestPersistence:
    def test_sqlite_backend(self, tmp_path):
        db = SQLiteBackend(str(tmp_path / "sub" / "state.db"))
        now = time.time()
        db.store("a", {"value": {"power": True}, "timestamp": now, "source": "command"})
        db.store("a", {"value": {"power": False}, "timestamp": now + 1})
        db.store("b", {"value": 3, "timestamp": now - 1000})
        assert db.get("a") == {"value": {"power": False}, "timestamp": now + 1, "source": "unknown"}
        assert db.get("zzz") is None
        assert set(db.get_all()) == {"a", "b"}
        assert set(db.get_all(prefix="a")) == {"a"}
        assert db.get_history("a") == [db.get("a")] and db.get_history("zzz") == []
        assert db.cleanup(max_age_seconds=500) == 1
        assert db.delete("a") is True and db.delete("a") is False

    def test_sqlite_prefix_is_literal(self, tmp_path):
        db = SQLiteBackend(str(tmp_path / "state.db"))
        db.store("a_1", {"value": 1, "timestamp": time.time()})
        db.store("ab1", {"value": 2, "timestamp": time.time()})
        assert set(db.get_all(prefix="a_")) == {"a_1"}

    def test_cache_writes_through_and_restores(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "state.db"))
        StateCache(persistence=backend).set("lamp", {"power": True}, source="command")
        StateCache(persistence=backend).set("scratch", 1, persist=False)
        assert backend.get("scratch") is None
        restored = StateCache(default_ttl=60, persistence=backend)
        assert restored.get("lamp") == {"power": True}
        assert restored.peek("lamp").source == "persistence"
        assert restored.stats()["hits"] == 1 and restored.stats()["misses"] == 0

    def test_old_persisted_state_is_not_served(self, tmp_path):
        backend = SQLiteBackend(str(tmp_path / "state.db"))
        backend.store("lamp", {"value": {"power": True}, "timestamp": time.time() - 120})
        cache = StateCache(default_ttl=60, persistence=backend)
        assert cache.get("lamp") is None
        assert cache.get("lamp", max_age=300) == {"power": True}
        assert cache.peek("lamp").age_seconds >= 119

    def test_backend_errors_are_swallowed(self):
        class Broken(PersistenceBackend):
            def store(self, key, data):
                raise OSError("disk full")

            def get(self, key):
                raise OSError("disk gone")

            def get_all(self, prefix=None):
                return {}

            def delete(self, key):
                return False

            def cleanup(self, max_age_seconds):
                return 0

        c = StateCache(persistence=Broken())
        c.set("a", 1)
        assert c.get("a") == 1
        assert c.get("b") is None


class TestEventBus:
    def test_subscriptions(self):
        bus = EventBus()
        direct, pattern, everything = [], [], []
        unsub = bus.subscribe("lamp", direct.append)
        bus.subscribe_pattern("room1.*", pattern.append)
        bus.subscribe_all(everything.append)
        bus.emit(event("lamp"))
        bus.emit(event("room1.fan"))
        bus.emit(event("room2.fan"))
        assert [e.device_id for e in direct] == ["lamp"]
        assert [e.device_id for e in pattern] == ["room1.fan"]
        assert len(everything) == 3
        unsub()
        bus.emit(event("lamp"))
        assert len(direct) == 1
        stats = bus.stats()
        assert stats["total_events"] == 4 and stats["pattern_subscriptions"] == 1

    def test_pattern_matching_rules(self):
        bus = EventBus()
        assert bus._match_pattern("*", "anything")
        assert bus._match_pattern("lamp", "lamp") and not bus._match_pattern("lamp", "lamp2")
        assert bus._match_pattern("kitchen_*", "kitchen_light")

    def test_unsubscribe_pattern(self):
        bus = EventBus()
        got = []
        unsub = bus.subscribe_pattern("*", got.append)
        unsub()
        bus.emit(event())
        assert got == []

    def test_failing_callback_is_isolated(self):
        bus = EventBus()
        got = []

        def bad(e):
            raise RuntimeError("nope")

        bus.subscribe_all(bad)
        bus.subscribe_all(got.append)
        bus.emit(event())
        assert len(got) == 1 and bus.stats()["dropped_events"] == 1

    def test_subscribe_partial(self):
        bus = EventBus()
        got = []
        bus.subscribe("lamp", functools.partial(lambda sink, e: sink.append(e), got))
        bus.emit(event())
        assert len(got) == 1

    def test_event_changed_keys(self):
        e = event(old={"power": False, "brightness": 5}, new={"power": True, "brightness": 5, "x": 1})
        assert e.changed_keys == {"power", "x"}
        assert event(old=None, new={"a": 1}).changed_keys == {"a"}
        d = e.to_dict()
        assert sorted(d["changed_keys"]) == ["power", "x"] and d["source"] == "command"


class TestStateHistory:
    def test_log_and_query(self, tmp_path):
        h = StateHistory(str(tmp_path / "h" / "history.db"))
        try:
            for i in range(5):
                h.log_state("lamp", {"n": i}, source="poll", timestamp=100.0 + i)
            h.log_state("other", {"n": 99}, timestamp=200.0)
            rows = h.get_history("lamp")
            assert [r["state"]["n"] for r in rows] == [4, 3, 2, 1, 0]
            assert rows[0] == {"timestamp": 104.0, "source": "poll", "state": {"n": 4}}
            assert len(h.get_history("lamp", limit=2)) == 2
            assert [r["state"]["n"] for r in h.get_history("lamp", since=101, until=103)] == [3, 2, 1]
            assert h.cleanup() == 0  # no retention configured
            assert h.cleanup(max_age_seconds=60) == 6
        finally:
            h.close()

    def test_attach_to_event_bus(self):
        bus = EventBus()
        h = StateHistory(":memory:", retention_seconds=3600)
        h.attach(bus)
        bus.emit(event("lamp", new={"power": True}))
        bus.emit(event("lamp", new={"power": False}, source="poll"))
        rows = h.get_history("lamp")
        assert [(r["state"]["power"], r["source"]) for r in rows] == [(False, "poll"), (True, "command")]
        assert h.cleanup() == 0
        h.close()
        bus.emit(event("lamp"))  # detached: no error, nothing recorded
        assert bus.stats()["global_subscriptions"] == 0


class TestConfirmation:
    @pytest.mark.parametrize("actual,expected,tol,result", [
        (1.0000001, 1.0, 1e-6, True), (1.1, 1.0, 1e-6, False), (1.4, 1.0, 0.5, True),
        (True, True, 1e-6, True), (False, True, 1e-6, False), ("on", "on", 1e-6, True),
        (5, 5.0, 1e-6, True), (None, 0, 1e-6, False),
    ])
    def test_values_match(self, actual, expected, tol, result):
        assert values_match(actual, expected, tol) is result

    def test_state_matches_subset(self):
        assert state_matches({"power": True, "brightness": 50}, {"power": True})
        assert not state_matches({"power": True}, {"brightness": 50})
        assert not state_matches(None, {"power": True})  # type: ignore[arg-type]
        assert state_matches({}, {})

    def test_engine_polls_until_match(self):
        states = iter([{"v": 0}, {"v": 1}, {"v": 2}])
        ok, observed, ms = ConfirmationEngine(poll_interval=0.001).confirm(lambda: next(states), {"v": 2})
        assert ok and observed == {"v": 2} and ms >= 0

    def test_engine_timeout_returns_last_observation(self):
        ok, observed, ms = ConfirmationEngine(poll_interval=0.01).confirm(lambda: {"v": 0}, {"v": 1},
                                                                          timeout=0.05)
        assert not ok and observed == {"v": 0} and 40 <= ms < 1000

    def test_engine_tolerates_fetch_errors_and_attempt_bound(self):
        calls = []

        def fetch():
            calls.append(1)
            raise OSError("flaky")

        ok, observed, _ = ConfirmationEngine(poll_interval=0.0, max_attempts=3).confirm(fetch, {"v": 1},
                                                                                        timeout=5)
        assert not ok and observed is None and len(calls) == 3

    def test_zero_timeout_polls_once_and_custom_matcher(self):
        calls = []
        ok, _, _ = confirm_state(lambda: calls.append(1) or {"v": 10}, {"v": 0}, timeout=0,
                                 matcher=lambda actual, expected: actual["v"] > 5)
        assert ok and len(calls) == 1
