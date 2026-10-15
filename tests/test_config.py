"""Config dataclass and the YAML / JSON / environment loaders."""
import json
from pathlib import Path

import pytest

from fuseiot.config import Config, auto_config, from_env, from_json, from_yaml
from fuseiot.exceptions import ConfigurationError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


class TestConfigValidation:
    def test_defaults(self):
        c = Config()
        assert c.default_ttl == 5.0 and c.confirm_default is True
        assert c.api_token is None and c.devices == [] and c.rules == []
        assert c.server_host == "127.0.0.1"

    def test_empty_token_means_none(self):
        assert Config(api_token="").api_token is None
        assert Config(api_token="abc").api_token == "abc"

    def test_log_level_normalised(self):
        assert Config(log_level="debug").log_level == "DEBUG"
        with pytest.raises(ConfigurationError, match="log_level"):
            Config(log_level="LOUD")

    @pytest.mark.parametrize("kwargs", [
        {"default_ttl": -1}, {"persistence_backend": "mongo"}, {"devices": {"a": 1}}, {"rules": "x"},
    ])
    def test_invalid_values(self, kwargs):
        with pytest.raises(ConfigurationError):
            Config(**kwargs)

    def test_to_dict_redacts_token(self):
        c = Config(api_token="secret")
        assert c.to_dict()["api_token"] == "secret"
        assert c.to_dict(redact=True)["api_token"] == "***"
        assert Config().to_dict(redact=True)["api_token"] is None

    def test_from_dict_ignores_unknown_keys(self):
        c = Config.from_dict({"default_ttl": 2, "bogus": True})
        assert c.default_ttl == 2 and not hasattr(c, "bogus")
        assert Config.from_dict(None).default_ttl == 5.0


class TestEnvExpansion:
    def test_expansion(self, monkeypatch):
        monkeypatch.setenv("FIOT_TEST_HOST", "10.0.0.5")
        monkeypatch.delenv("FIOT_TEST_MISSING", raising=False)
        c = Config.from_dict({
            "server_host": "${FIOT_TEST_HOST}",
            "environment": "${FIOT_TEST_MISSING:-staging}",
            "api_token": "${FIOT_TEST_MISSING:-}",
            "tags": {"site": "plant-${FIOT_TEST_HOST}"},
            "devices": [{"id": "a", "endpoint": "http://${FIOT_TEST_HOST}/api"}],
            "log_file": "${FIOT_TEST_MISSING}",
        })
        assert c.server_host == "10.0.0.5"
        assert c.environment == "staging"
        assert c.api_token is None
        assert c.tags == {"site": "plant-10.0.0.5"}
        assert c.devices[0]["endpoint"] == "http://10.0.0.5/api"
        assert c.log_file == ""


class TestFileLoaders:
    def test_from_yaml(self, tmp_path):
        p = tmp_path / "c.yaml"
        p.write_text("default_ttl: 1.5\nrate_limit_enabled: true\ndevices:\n"
                     "  - {id: lamp, capability: switchable, protocol: virtual}\n", encoding="utf-8")
        c = from_yaml(p)
        assert c.default_ttl == 1.5 and c.rate_limit_enabled is True
        assert c.devices[0]["id"] == "lamp"

    def test_empty_yaml_gives_defaults(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        assert from_yaml(p).default_ttl == 5.0

    @pytest.mark.parametrize("content,match", [
        ("default_ttl: [1, 2\n", "Invalid config"),
        ("- just\n- a list\n", "mapping"),
    ])
    def test_bad_yaml(self, tmp_path, content, match):
        p = tmp_path / "bad.yaml"
        p.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigurationError, match=match):
            from_yaml(p)

    def test_missing_files(self, tmp_path):
        with pytest.raises(ConfigurationError, match="not found"):
            from_yaml(tmp_path / "nope.yaml")
        with pytest.raises(ConfigurationError, match="not found"):
            from_json(tmp_path / "nope.json")

    def test_from_json(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"max_devices": 7, "tags": {"a": "b"}}), encoding="utf-8")
        c = from_json(p)
        assert c.max_devices == 7 and c.tags == {"a": "b"}
        (tmp_path / "e.json").write_text("  ", encoding="utf-8")
        assert from_json(tmp_path / "e.json").max_devices == 1000

    @pytest.mark.parametrize("content,match", [("{nope", "Invalid JSON"), ("[1]", "object")])
    def test_bad_json(self, tmp_path, content, match):
        p = tmp_path / "bad.json"
        p.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigurationError, match=match):
            from_json(p)

    @pytest.mark.parametrize("name", ["config.yaml", "demo.yaml"])
    def test_shipped_example_configs_load(self, name):
        c = from_yaml(EXAMPLES / name)
        assert c.devices and all("id" in d for d in c.devices)
        assert c.api_token is None  # ${FUSEIOT_API_TOKEN:-} with the variable unset


class TestEnvLoader:
    def test_from_env_coercion(self, monkeypatch):
        monkeypatch.setenv("FUSEIOT_DEFAULT_TTL", "2.5")
        monkeypatch.setenv("FUSEIOT_MAX_DEVICES", "12")
        monkeypatch.setenv("FUSEIOT_RATE_LIMIT_ENABLED", "yes")
        monkeypatch.setenv("FUSEIOT_CONFIRM_DEFAULT", "off")
        monkeypatch.setenv("FUSEIOT_TAGS", '{"site": "plant-1"}')
        monkeypatch.setenv("FUSEIOT_LOG_FILE", "null")
        monkeypatch.setenv("FUSEIOT_ENVIRONMENT", "prod")
        monkeypatch.setenv("FUSEIOT_NOT_A_FIELD", "1")
        c = from_env()
        assert c.default_ttl == 2.5 and c.max_devices == 12
        assert c.rate_limit_enabled is True and c.confirm_default is False
        assert c.tags == {"site": "plant-1"} and c.log_file is None
        assert c.environment == "prod"

    def test_custom_prefix(self, monkeypatch):
        monkeypatch.setenv("ACME_DEFAULT_TTL", "9")
        assert from_env("ACME_").default_ttl == 9

    def test_invalid_env_value_is_reported(self, monkeypatch):
        monkeypatch.setenv("FUSEIOT_LOG_LEVEL", "chatty")
        with pytest.raises(ConfigurationError):
            from_env()

    def test_numeric_api_token_stays_a_string(self, monkeypatch):
        monkeypatch.setenv("FUSEIOT_API_TOKEN", "123456")
        assert from_env().api_token == "123456"


class TestAutoConfig:
    def test_file_then_env(self, tmp_path, monkeypatch):
        p = tmp_path / "c.yaml"
        p.write_text("default_ttl: 3\nmax_devices: 4\n", encoding="utf-8")
        monkeypatch.setenv("FUSEIOT_MAX_DEVICES", "40")
        c = auto_config(p)
        assert c.default_ttl == 3 and c.max_devices == 40

    def test_config_path_from_env(self, tmp_path, monkeypatch):
        p = tmp_path / "c.json"
        p.write_text('{"environment": "lab"}', encoding="utf-8")
        monkeypatch.setenv("FUSEIOT_CONFIG", str(p))
        assert auto_config().environment == "lab"

    def test_no_file(self):
        assert auto_config().default_ttl == 5.0
