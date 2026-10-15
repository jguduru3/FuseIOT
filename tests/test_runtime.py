"""Runtime assembly and validate_config."""
import pytest

from fuseiot.config import Config
from fuseiot.exceptions import ConfigurationError
from fuseiot.runtime import Runtime, validate_config
from tests.test_server import call_json

FAST = {"timeout": 0.5, "retry_attempts": 1, "retry_delay": 0, "poll_interval": 0.01}


def base(**extra):
    data = {
        "circuit_breaker_enabled": False,
        "devices": [
            {"id": "lamp", "capability": "switchable", "protocol": "virtual", "command": FAST},
            {"id": "temp", "capability": "sensor", "protocol": "virtual"},
        ],
    }
    data.update(extra)
    return Config.from_dict(data)


GOOD = dict(
    rules=[{"name": "cold", "when": {"device": "temp", "property": "value", "op": "<", "value": 5},
            "then": [{"device": "lamp", "command": "on"}, {"notify": "log", "message": "cold"}]}],
    schedules=[{"name": "eve", "at": "sunset", "then": {"device": "lamp", "command": "on"}},
               {"name": "ping", "cron": "0 * * * *", "then": {"notify": "log", "message": "{time}"}}],
    location={"latitude": 51.5, "longitude": -0.12, "timezone": "Europe/London"},
    notifications={"channels": {"log": {"type": "log"}}, "alerts": {"device_offline": ["log"]}},
)


@pytest.fixture
def runtimes():
    made = []
    yield made
    for rt in made:
        rt.stop()


class TestValidateConfig:
    def test_valid(self):
        assert validate_config(base(**GOOD)) == []

    def test_unknown_device_refs(self):
        errors = validate_config(base(
            rules=[{"name": "r", "when": {"device": "ghost", "property": "value"},
                    "then": {"device": "lamp", "command": "on"}, "else": {"device": "phantom", "command": "off"}}],
            schedules=[{"name": "s", "at": "10:00", "then": {"device": "spook", "command": "on"}}],
        ))
        assert "Rule 'r' references unknown device 'ghost'" in errors
        assert "Rule 'r' references unknown device 'phantom'" in errors
        assert "Schedule 's' references unknown device 'spook'" in errors

    def test_unknown_notify_channel(self):
        errors = validate_config(base(
            notifications={"channels": {"log": {"type": "log"}}},
            rules=[{"name": "r", "when": {"device": "temp", "property": "value"},
                    "then": {"notify": "sms", "message": "x"}}],
            schedules=[{"name": "s", "at": "10:00", "then": {"notify": "pager", "message": "x"}}],
        ))
        assert errors == ["Rule 'r' uses unknown notification channel 'sms'",
                          "Schedule 's' uses unknown notification channel 'pager'"]

    def test_notify_without_any_channels(self):
        errors = validate_config(base(schedules=[{"name": "s", "at": "10:00",
                                                  "then": {"notify": "log", "message": "x"}}]))
        assert errors == ["Schedule 's' uses unknown notification channel 'log'"]

    def test_sun_without_coordinates(self):
        errors = validate_config(base(location={"timezone": "UTC"},
                                      schedules=[{"name": "s", "at": "sunrise-10m",
                                                  "then": {"device": "lamp", "command": "on"}}]))
        assert errors == ["Schedule 's' uses sunrise/sunset; set location.latitude/longitude"]

    def test_bad_schedules_location_and_notifications(self):
        errors = validate_config(base(
            location={"timezone": "Atlantis/City"},
            notifications={"channels": {"x": {"type": "carrier-pigeon"}}},
            schedules=[{"name": "bad", "at": "25:00", "then": {"device": "lamp", "command": "on"}},
                       {"name": "bad2", "cron": "* *", "then": {"device": "lamp", "command": "on"}}],
        ))
        assert any("Unknown timezone" in e for e in errors)
        assert any("Unknown notification type" in e for e in errors)
        assert any("invalid time" in e for e in errors)
        assert any("5 fields" in e for e in errors)

    def test_bad_device_entry_reported(self):
        cfg = base()
        cfg.devices.append({"id": "bad id", "capability": "switchable", "protocol": "virtual"})
        assert any("bad id" in e for e in validate_config(cfg))


class TestRuntime:
    def test_from_config_builds_everything(self, tmp_path, runtimes):
        cfg = base(**GOOD, users_db=str(tmp_path / "users.db"), monitor_interval=60)
        rt = Runtime.from_config(cfg)
        runtimes.append(rt)
        assert len(rt.hub) == 2
        assert rt.notifier is not None and rt.hub.notifier is rt.notifier
        assert [r.name for r in rt.rules.rules] == ["cold"]
        assert [s.name for s in rt.scheduler.schedules] == ["eve", "ping"]
        assert rt.monitor is not None and rt.monitor.notifier is rt.notifier
        assert rt.users is not None and not rt.users.has_users()

    def test_invalid_config_raises(self):
        with pytest.raises(ConfigurationError, match="Invalid configuration:\n  - Schedule 's' references"):
            Runtime.from_config(base(schedules=[{"name": "s", "at": "10:00",
                                                 "then": {"device": "ghost", "command": "on"}}]))

    def test_minimal_runtime(self, runtimes):
        rt = Runtime.from_config(base(monitor_interval=0))
        runtimes.append(rt)
        assert rt.notifier is None and rt.rules is None and rt.scheduler is None
        assert rt.monitor is None and rt.users is None and rt.hub.notifier is None

    def test_without_rules_flag(self, runtimes):
        rt = Runtime.from_config(base(**GOOD), with_rules=False)
        runtimes.append(rt)
        assert rt.rules is None and rt.scheduler is not None

    def test_location_alone_creates_scheduler(self, runtimes):
        rt = Runtime.from_config(base(location={"timezone": "UTC"}))
        runtimes.append(rt)
        assert rt.scheduler is not None and rt.scheduler.schedules == []

    def test_start_stop_lifecycle(self, runtimes):
        rt = Runtime.from_config(base(**GOOD, monitor_interval=0.05, rules_interval=0.05))
        runtimes.append(rt)
        assert rt.start() is rt
        assert rt.start() is rt  # idempotent
        assert rt.monitor._thread.is_alive() and rt.scheduler._thread.is_alive()
        rt.stop()
        assert rt.monitor._thread is None and rt.scheduler._thread is None and not rt._started

    def test_context_manager_and_server(self, tmp_path, runtimes, fast_scrypt):
        cfg = base(**GOOD, users_db=str(tmp_path / "users.db"), api_token="svc-token", monitor_interval=60)
        with Runtime.from_config(cfg) as rt:
            server = rt.make_server(host="127.0.0.1", port=0)
            assert server.scheduler is rt.scheduler and server.monitor is rt.monitor
            assert server.notifier is rt.notifier and server.rule_engine is rt.rules and server.users is rt.users
            server.start(poll_interval=0.02)
            status, body = call_json(server, "GET", "/api/schedules", token="svc-token")
            assert status == 200 and [s["name"] for s in body["schedules"]] == ["eve", "ping"]
            assert call_json(server, "GET", "/api/rules", token="svc-token")[0] == 200
        assert rt.server is None and not rt._started

    def test_make_server_refuses_public_bind_without_auth(self, runtimes):
        rt = Runtime.from_config(base())
        runtimes.append(rt)
        with pytest.raises(ConfigurationError, match="Refusing"):
            rt.make_server(host="0.0.0.0", port=0)
