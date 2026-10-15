"""CLI home-automation commands: validate, schedules, notify test, user management, discover --yaml."""
import json

import pytest
import responses
import yaml
from click.testing import CliRunner

from fuseiot.auth import UserStore
from fuseiot.cli.main import cli
from fuseiot.discovery.mdns import DiscoveryResult, MDNSDiscovery

FAST = {"timeout": 0.5, "retry_attempts": 1, "retry_delay": 0, "poll_interval": 0.01}
PW = "correct horse"

CONFIG = {
    "circuit_breaker_enabled": False,
    "devices": [
        {"id": "lamp", "capability": "switchable", "protocol": "virtual", "command": FAST},
        {"id": "temp", "capability": "sensor", "protocol": "virtual"},
    ],
    "rules": [{"name": "cold", "when": {"device": "temp", "property": "value", "op": "<", "value": 5},
               "then": {"device": "lamp", "command": "on"}}],
    "location": {"latitude": 51.5074, "longitude": -0.1278, "timezone": "Europe/London"},
    "schedules": [
        {"name": "porch_on", "at": "sunset-30m", "then": {"device": "lamp", "command": "on"}},
        {"name": "porch_off", "at": "23:30", "days": "weekdays", "then": {"device": "lamp", "command": "off"}},
        {"name": "hourly", "cron": "0 * * * *", "then": {"notify": "log", "message": "tick"}},
        {"name": "paused", "at": "12:00", "enabled": False, "then": {"device": "lamp", "command": "on"}},
    ],
    "notifications": {
        "channels": {"log": {"type": "log"}, "hook": {"type": "webhook", "url": "https://hooks.example/x"}},
        "alerts": {"device_offline": ["log"]},
    },
}


@pytest.fixture
def write_config(tmp_path):
    def _write(data=None, name="fuseiot.yaml", **extra):
        cfg = dict(CONFIG if data is None else data)
        cfg.update(extra)
        path = tmp_path / name
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return str(path)

    return _write


@pytest.fixture
def run(write_config):
    runner = CliRunner()
    default = write_config()

    def _run(*args, config=None, env=None, input=None):
        return runner.invoke(cli, ["-c", config or default, *args], env=env, input=input, catch_exceptions=False)

    return _run


@pytest.fixture
def users_config(write_config, tmp_path, fast_scrypt):
    return write_config(name="with_users.yaml", users_db=str(tmp_path / "data" / "users.db"))


def users_at(config_path):
    db = yaml.safe_load(open(config_path, encoding="utf-8"))["users_db"]
    return UserStore(db)


class TestValidate:
    def test_summary_counts(self, run):
        result = run("validate")
        assert result.exit_code == 0
        assert ("Configuration OK: 2 device(s), 1 rule(s), 4 schedule(s), 2 notification channel(s)."
                in result.output)

    def test_reports_schedule_and_channel_problems(self, run, write_config):
        bad = write_config(name="bad.yaml", schedules=[
            {"name": "a", "at": "sunrise", "then": {"notify": "sms", "message": "x"}},
            {"name": "b", "at": "10:00", "then": {"device": "ghost", "command": "on"}},
        ], location={"timezone": "UTC"})
        result = run("validate", config=bad)
        assert result.exit_code == 2
        assert "Schedule 'a' uses sunrise/sunset" in result.output
        assert "Schedule 'a' uses unknown notification channel 'sms'" in result.output
        assert "Schedule 'b' references unknown device 'ghost'" in result.output
        assert "3 problem(s) in configuration" in result.output


class TestSchedulesCommand:
    def test_table(self, run):
        result = run("schedules")
        assert result.exit_code == 0, result.output
        assert "Today (Europe/London): sunrise " in result.output and "sunset " in result.output
        lines = {line.split()[0]: line for line in result.output.splitlines()[1:] if line.strip()}
        assert "at sunset-30m" in lines["porch_on"] and "daily" in lines["porch_on"]
        assert "mon,tue,wed,thu,fri" in lines["porch_off"]
        assert "cron 0 * * * *" in lines["hourly"]
        assert "next: disabled" in lines["paused"]

    def test_json(self, run):
        result = run("--json", "schedules")
        assert result.exit_code == 0
        items = json.loads(result.stdout)
        assert [s["name"] for s in items] == ["porch_on", "porch_off", "hourly", "paused"]
        assert items[0]["next_run"] and items[3]["next_run"] is None and items[3]["enabled"] is False
        assert items[2]["cron"] == "0 * * * *"

    def test_no_schedules_without_coordinates(self, run, write_config):
        path = write_config(name="none.yaml", schedules=[], location={"timezone": "UTC"})
        result = run("schedules", config=path)
        assert result.exit_code == 0
        assert "No schedules" in result.output and "sunrise" not in result.output

    def test_invalid_schedule_exit_2(self, run, write_config):
        path = write_config(name="bad.yaml", schedules=[{"name": "x", "at": "10:00",
                                                          "then": {"device": "ghost", "command": "on"}}])
        result = run("schedules", config=path)
        assert result.exit_code == 2 and "unknown device 'ghost'" in result.output


class TestNotifyCommand:
    def test_log_channel(self, run):
        result = run("notify", "test", "log")
        assert result.exit_code == 0 and "Sent via log." in result.output

    def test_webhook_success_and_failure(self, run):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, "https://hooks.example/x", status=200)
            result = run("notify", "test", "hook", "-m", "hello there")
            assert result.exit_code == 0
            body = json.loads(rsps.calls[0].request.body)
            assert body["message"] == "hello there" and body["title"] == "FuseIoT test"
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, "https://hooks.example/x", status=500)
            result = run("notify", "test", "hook")
            assert result.exit_code == 1 and "Sending via hook failed" in result.output

    def test_unknown_channel(self, run):
        result = run("notify", "test", "sms")
        assert result.exit_code == 2 and "Unknown channel 'sms' (configured: hook, log)" in result.output

    def test_no_channels_configured(self, run, write_config):
        path = write_config(name="n.yaml", notifications={}, schedules=[])
        result = run("notify", "test", "log", config=path)
        assert result.exit_code == 2 and "none configured" in result.output

    def test_bad_notification_config(self, run, write_config):
        path = write_config(name="n.yaml", notifications={"channels": {"x": {"type": "fax"}}}, schedules=[])
        result = run("notify", "test", "x", config=path)
        assert result.exit_code == 2 and "Unknown notification type" in result.output


class TestUserCommands:
    def test_requires_users_db(self, run):
        for args in (["user", "list"], ["user", "add", "bob", "--password", PW]):
            result = run(*args)
            assert result.exit_code == 2 and "users_db" in result.output

    def test_add_list_and_json(self, run, users_config):
        result = run("user", "add", "alice", "--role", "admin", config=users_config,
                     env={"FUSEIOT_NEW_PASSWORD": PW})
        assert result.exit_code == 0 and "User 'alice' created with role admin." in result.output
        assert run("user", "add", "bob", "--password", PW, config=users_config).exit_code == 0
        listed = run("user", "list", config=users_config).output
        assert "alice" in listed and "admin" in listed and "bob" in listed and "operator" in listed
        data = json.loads(run("--json", "user", "list", config=users_config).stdout)
        assert [(u["username"], u["role"]) for u in data] == [("alice", "admin"), ("bob", "operator")]

    def test_empty_list_hint(self, run, users_config):
        assert "No users yet" in run("user", "list", config=users_config).output

    def test_prompted_password(self, run, users_config):
        result = run("user", "add", "carol", config=users_config, input=f"{PW}\n{PW}\n")
        assert result.exit_code == 0
        store = users_at(users_config)
        try:
            assert store.authenticate("carol", PW).role == "operator"
        finally:
            store.close()

    @pytest.mark.parametrize("args,fragment", [
        (["user", "add", "bad name", "--password", PW], "Username may contain"),
        (["user", "add", "dave", "--password", "short"], "at least 8"),
    ])
    def test_add_errors(self, run, users_config, args, fragment):
        result = run(*args, config=users_config)
        assert result.exit_code == 2 and fragment in result.output

    def test_duplicate_user(self, run, users_config):
        run("user", "add", "eve", "--password", PW, config=users_config)
        result = run("user", "add", "eve", "--password", PW, config=users_config)
        assert result.exit_code == 2 and "already exists" in result.output

    def test_passwd_role_disable_enable_remove(self, run, users_config):
        run("user", "add", "admin", "--role", "admin", "--password", PW, config=users_config)
        run("user", "add", "bob", "--role", "viewer", "--password", PW, config=users_config)
        result = run("user", "passwd", "bob", config=users_config, env={"FUSEIOT_NEW_PASSWORD": "new password"})
        assert result.exit_code == 0 and "Password updated." in result.output
        assert run("user", "role", "bob", "operator", config=users_config).output.strip().endswith(
            "bob is now operator.")
        assert "bob disabled." in run("user", "disable", "bob", config=users_config).output
        store = users_at(users_config)
        try:
            users = {u["username"]: u for u in store.list_users()}
            assert users["bob"]["role"] == "operator" and users["bob"]["disabled"] is True
        finally:
            store.close()
        assert "disabled" in run("user", "list", config=users_config).output
        assert "bob enabled." in run("user", "enable", "bob", config=users_config).output
        store = users_at(users_config)
        try:
            assert store.authenticate("bob", "new password").username == "bob"
        finally:
            store.close()
        result = run("user", "remove", "bob", "--yes", config=users_config)
        assert result.exit_code == 0 and "bob removed." in result.output
        assert "bob" not in run("user", "list", config=users_config).output

    def test_remove_requires_confirmation(self, run, users_config):
        run("user", "add", "bob", "--password", PW, config=users_config)
        result = run("user", "remove", "bob", config=users_config, input="n\n")
        assert result.exit_code == 1
        assert "bob" in run("user", "list", config=users_config).output

    def test_last_admin_protections(self, run, users_config):
        run("user", "add", "root", "--role", "admin", "--password", PW, config=users_config)
        for args, fragment in ((["user", "role", "root", "viewer"], "demote the last admin"),
                               (["user", "disable", "root"], "disable the last admin"),
                               (["user", "remove", "root", "--yes"], "remove the last admin")):
            result = run(*args, config=users_config)
            assert result.exit_code == 2 and fragment in result.output

    def test_unknown_user(self, run, users_config):
        for args in (["user", "passwd", "ghost", "--password", PW], ["user", "role", "ghost", "admin"],
                     ["user", "disable", "ghost"], ["user", "enable", "ghost"], ["user", "token", "ghost"]):
            result = run(*args, config=users_config)
            assert result.exit_code == 2 and "No such user: ghost" in result.output

    def test_invalid_role_choice(self, run, users_config):
        assert run("user", "role", "x", "superuser", config=users_config).exit_code == 2

    def test_token(self, run, users_config):
        run("user", "add", "bot", "--role", "viewer", "--password", PW, config=users_config)
        result = run("user", "token", "bot", "--name", "ha", "--days", "2", config=users_config)
        assert result.exit_code == 0
        token = result.output.strip()
        assert token.startswith("fio_")
        store = users_at(users_config)
        try:
            principal = store.resolve_token(token)
            assert (principal.username, principal.token_kind) == ("bot", "api")
            listed = store.list_tokens("bot")
            assert listed[0]["name"] == "ha" and listed[0]["expires_at"] is not None
        finally:
            store.close()
        result = run("user", "token", "bot", config=users_config)
        store = users_at(users_config)
        try:
            assert store.list_tokens("bot")[1]["name"] == "cli" and store.list_tokens("bot")[1]["expires_at"] is None
        finally:
            store.close()

    def test_serve_accepts_public_bind_when_users_exist(self, run, users_config, monkeypatch):
        # Users satisfy the insecure-bind check; stop before actually binding.
        import fuseiot.runtime as runtime_module

        def boom(*a, **kw):
            raise runtime_module.ConfigurationError("stop here")

        monkeypatch.setattr(runtime_module.Runtime, "from_config", classmethod(lambda cls, *a, **kw: boom()))
        result = run("serve", "--host", "0.0.0.0", "--port", "0", config=users_config)
        assert result.exit_code == 2 and "Refusing" in result.output
        run("user", "add", "root", "--role", "admin", "--password", PW, config=users_config)
        result = run("serve", "--host", "0.0.0.0", "--port", "0", config=users_config)
        assert result.exit_code == 2 and "stop here" in result.output and "Refusing" not in result.output


class TestDiscoverYaml:
    def test_yaml_snippet(self, run, monkeypatch):
        seen = {}

        def scan(self, service_type=None, timeout=5.0):
            seen["service_type"] = service_type
            return [
                DiscoveryResult(name="lamp._fuseiot._tcp.local.", address="10.0.0.2", port=80,
                                device_type="_fuseiot._tcp.local.", protocol="http",
                                properties={"id": "desk_lamp", "cap": "dimmable", "auth": "1"},
                                hostname="desk.local."),
                DiscoveryResult(name="printer._http._tcp.local.", address="10.0.0.3", port=80,
                                device_type="_http._tcp.local.", protocol="http"),
            ]

        monkeypatch.setattr(MDNSDiscovery, "scan", scan)
        result = run("discover", "--yaml", "-t", "0")
        assert result.exit_code == 0
        assert seen["service_type"] == "_fuseiot._tcp.local."
        assert "Scanning" not in result.output
        data = yaml.safe_load(result.stdout)
        assert data == {"devices": [{"id": "desk_lamp", "name": "Desk lamp", "capability": "dimmable",
                                     "protocol": "http", "endpoint": "http://desk.local",
                                     "options": {"bearer_token": "${DESK_LAMP_TOKEN}"}}]}

    def test_yaml_respects_explicit_service(self, run, monkeypatch):
        seen = {}

        def scan(self, service_type=None, timeout=5.0):
            seen["service_type"] = service_type
            return []

        monkeypatch.setattr(MDNSDiscovery, "scan", scan)
        result = run("discover", "--yaml", "-s", "_http._tcp.local.", "-t", "0")
        assert result.exit_code == 0 and seen["service_type"] == "_http._tcp.local."
        assert "No FuseIoT devices found" in result.output
