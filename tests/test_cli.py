"""`fuseiot` command-line interface (click CliRunner, virtual devices from a temp config)."""
import json

import pytest
import yaml
from click.testing import CliRunner

from fuseiot.__version__ import __version__
from fuseiot.cli.main import cli
from fuseiot.discovery.mdns import DiscoveryResult, MDNSDiscovery
from fuseiot.exceptions import DiscoveryError

FAST = {"timeout": 0.5, "retry_attempts": 1, "retry_delay": 0, "poll_interval": 0.01}

CONFIG = {
    "circuit_breaker_enabled": False,
    "devices": [
        {"id": "lamp", "name": "Lamp", "capability": "switchable", "protocol": "virtual", "command": FAST},
        {"id": "dimmer", "capability": "dimmable", "protocol": "virtual", "command": FAST},
        {"id": "temp", "capability": "sensor", "protocol": "virtual",
         "options": {"initial_state": {"value": 19.5}}, "capability_options": {"unit": "celsius"}},
        {"id": "broken", "capability": "switchable", "protocol": "virtual",
         "options": {"failure_rate": 1.0}, "command": FAST},
        {"id": "bad_opts", "capability": "switchable", "protocol": "virtual", "options": {"warp": 1}},
    ],
    "rules": [
        {"name": "cold", "when": {"device": "temp", "property": "value", "op": "<", "value": 18},
         "then": [{"device": "lamp", "command": "on"}]},
    ],
}


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "fuseiot.yaml"
    path.write_text(yaml.safe_dump(CONFIG), encoding="utf-8")
    return str(path)


@pytest.fixture
def run(config_file):
    runner = CliRunner()

    def _run(*args, config=config_file, env=None):
        base = ["-c", config] if config else []
        return runner.invoke(cli, base + list(args), env=env, catch_exceptions=False)

    return _run


def test_version(run):
    result = run("--version", config=None)
    assert result.exit_code == 0 and __version__ in result.output


def test_devices_table_and_json(run):
    result = run("devices")
    assert result.exit_code == 0
    assert "lamp" in result.output and "switchable" in result.output and "virtual" in result.output
    rows = json.loads(run("--json", "devices").stdout)
    assert [r["id"] for r in rows] == ["lamp", "dimmer", "temp", "broken", "bad_opts"]


def test_devices_empty_config(run, tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("default_ttl: 1\n", encoding="utf-8")
    result = run("devices", config=str(empty))
    assert result.exit_code == 0 and "No devices configured" in result.output


def test_status(run):
    result = run("status", "temp")
    assert result.exit_code == 0
    assert "temp  (sensor via virtual virtual://sensor)" in result.output
    assert "19.5" in result.output and "commands: calibrate" in result.output
    info = json.loads(run("--json", "status", "lamp", "--cached").stdout)
    assert info["device_id"] == "lamp" and info["state"] == {"power": False} and info["name"] == "Lamp"


def test_power_commands(run):
    result = run("on", "lamp")
    assert result.exit_code == 0 and "OK lamp on: confirmed" in result.output
    result = run("off", "lamp", "--no-confirm")
    assert result.exit_code == 0 and "SENT lamp off: acknowledged, not verified" in result.output
    result = run("toggle", "lamp", "-t", "1.0")
    assert result.exit_code == 0 and "lamp on" in result.output
    payload = json.loads(run("--json", "on", "lamp").stdout)
    assert payload["status"] == "CONFIRMED" and payload["state_after"] == {"power": True}


def test_failed_command_exit_code(run):
    result = run("on", "broken")
    assert result.exit_code == 1
    assert "FAILED broken on: [FAILED]" in result.output
    payload = json.loads(run("--json", "on", "broken").stdout)
    assert payload["success"] is False


def test_cmd_with_parameters(run):
    payload = json.loads(run("--json", "cmd", "dimmer", "set_brightness", "brightness=40").stdout)
    assert payload["state_after"]["brightness"] == 40
    result = run("cmd", "dimmer", "on", "brightness=0.5", "confirm=true")
    assert result.exit_code == 0 and "OK dimmer on" in result.output


@pytest.mark.parametrize("args,fragment", [
    (("cmd", "lamp", "self_destruct"), "has no command 'self_destruct'"),
    (("cmd", "lamp", "read_state"), "has no command 'read_state'"),
    (("cmd", "lamp", "on", "volume=11"), "Invalid parameters for on"),
    (("cmd", "lamp", "on", "novalue"), "expected key=value"),
    (("on", "ghost"), "Device not found in config: ghost"),
    (("on", "bad_opts"), "Invalid virtual options"),
])
def test_usage_errors_exit_2(run, args, fragment):
    result = run(*args)
    assert result.exit_code == 2
    assert fragment in result.output


def test_status_read_failure_exit_1(run):
    result = run("status", "broken")
    assert result.exit_code == 1 and "Failed to read state of broken" in result.output


def test_missing_config_file_exit_2(run, tmp_path):
    result = run("devices", config=str(tmp_path / "missing.yaml"))
    assert result.exit_code == 2 and "Config file not found" in result.output


def test_config_from_environment(run, config_file):
    result = run("devices", config=None, env={"FUSEIOT_CONFIG": config_file})
    assert result.exit_code == 0 and "dimmer" in result.output


def test_validate(run, tmp_path):
    result = run("validate")
    assert result.exit_code == 0 and "Configuration OK: 5 device(s), 1 rule(s)," in result.output
    bad = dict(CONFIG)
    bad["devices"] = CONFIG["devices"][:2] + [{"id": "x y", "capability": "switchable", "protocol": "virtual"}]
    bad["rules"] = [{"name": "r", "when": {"device": "nobody", "property": "value"},
                     "then": [{"device": "lamp", "command": "on"}]},
                    {"name": "broken_rule", "when": []}]
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    result = run("validate", config=str(path))
    assert result.exit_code == 2
    assert "Invalid device 'x y'" in result.output
    assert "Rule 'r' references unknown device 'nobody'" in result.output
    assert "missing 'then'" in result.output
    assert "3 problem(s) in configuration" in result.output


def test_stats(run):
    stats = json.loads(run("stats").stdout)
    assert stats["version"] == __version__ and stats["devices_active"] == 0


def test_serve_refuses_public_host_without_token(run):
    result = run("serve", "--host", "0.0.0.0", "--port", "0")
    assert result.exit_code == 2 and "Refusing to serve on 0.0.0.0" in result.output


def test_simulate_rejects_unknown_type(run):
    result = run("simulate", "toaster", "--port", "0", config=None)
    assert result.exit_code == 2 and "Unknown virtual device type" in result.output


def test_discover(run, monkeypatch):
    found = [DiscoveryResult(name="Plug._http._tcp.local.", address="192.168.1.9", port=80,
                             device_type="_http._tcp.local.", protocol="http")]
    monkeypatch.setattr(MDNSDiscovery, "scan", lambda self, service_type=None, timeout=5.0: found)
    result = run("discover", "--timeout", "0")
    assert result.exit_code == 0 and "Found 1 device(s)" in result.output
    assert "http://192.168.1.9:80" in result.output
    data = json.loads(run("--json", "discover", "-t", "0").stdout)
    assert data[0]["address"] == "192.168.1.9"
    assert found[0].url == "http://192.168.1.9:80"

    monkeypatch.setattr(MDNSDiscovery, "scan", lambda self, service_type=None, timeout=5.0: [])
    assert "No devices found." in run("discover", "-t", "0").output

    def fail(self, service_type=None, timeout=5.0):
        raise DiscoveryError("mDNS unavailable")

    monkeypatch.setattr(MDNSDiscovery, "scan", fail)
    result = run("discover", "-t", "0")
    assert result.exit_code == 1 and "mDNS unavailable" in result.output


def test_discovery_result_ipv6_url():
    r = DiscoveryResult(name="n", address="fe80::1", port=8080, device_type="t", protocol="http")
    assert r.url == "http://[fe80::1]:8080"
