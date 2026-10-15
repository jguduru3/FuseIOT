"""
Configuration loading (YAML / JSON / environment variables).

A config file can hold hub settings *and* the devices and automation rules a
deployment runs::

    default_ttl: 5.0
    circuit_breaker_enabled: true
    devices:
      - id: living_room_light
        capability: switchable
        protocol: http
        endpoint: http://192.168.1.45/api
    rules:
      - name: night_light
        when: {device: hallway_motion, property: value, op: ">", value: 0}
        then: [{device: living_room_light, command: "on"}]
"""
import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, get_type_hints

import yaml

from fuseiot.exceptions import ConfigurationError
from fuseiot.logging_config import get_logger

logger = get_logger("config")

_ENV_PATTERN_START = "${"


@dataclass
class Config:
    """FuseIoT configuration. Unknown keys are ignored with a warning."""

    # Hub
    default_ttl: float = 5.0
    max_devices: int = 1000
    metrics_enabled: bool = True

    # Protocol defaults
    default_timeout: float = 5.0
    default_retry_attempts: int = 3

    # Confirmation
    confirm_default: bool = True
    confirm_timeout: float = 2.0
    confirm_poll_interval: float = 0.1
    confirm_max_attempts: int = 50

    # Resilience
    circuit_breaker_enabled: bool = True
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout: float = 30.0
    rate_limit_enabled: bool = False
    rate_limit_requests: int = 100
    rate_limit_window: float = 60.0

    # Persistence
    persistence_enabled: bool = False
    persistence_backend: str = "sqlite"  # sqlite | redis | memory
    persistence_path: Optional[str] = "fuseiot.db"
    redis_url: Optional[str] = None
    history_enabled: bool = False
    history_path: Optional[str] = "fuseiot_history.db"

    # Logging
    log_level: str = "INFO"
    log_json: bool = False
    log_file: Optional[str] = None

    # Security
    tls_verify: bool = True
    tls_ca_bundle: Optional[str] = None

    # Server (``fuseiot serve``)
    server_host: str = "127.0.0.1"
    server_port: int = 8080
    api_token: Optional[str] = None
    rules_interval: float = 5.0

    # Home automation
    location: Dict[str, Any] = field(default_factory=dict)       # latitude, longitude, timezone
    schedules: List[Dict[str, Any]] = field(default_factory=list)
    notifications: Dict[str, Any] = field(default_factory=dict)  # channels, alerts, cooldown
    monitor_interval: float = 30.0   # seconds between device polls in `serve`; 0 disables
    offline_after: int = 2           # failed polls before a device counts as offline

    # User accounts (`fuseiot user add ...`); enables logins and roles in `serve`
    users_db: Optional[str] = None
    session_days: float = 30.0

    # Cloud
    cloud_enabled: bool = False
    cloud_provider: Optional[str] = None  # aws | azure
    cloud_options: Dict[str, Any] = field(default_factory=dict)

    # Deployment content
    devices: List[Dict[str, Any]] = field(default_factory=list)
    rules: List[Dict[str, Any]] = field(default_factory=list)

    # Metadata
    environment: str = "development"
    tags: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.api_token:
            self.api_token = None  # treat "" (e.g. unset ${FUSEIOT_API_TOKEN:-}) as no token
        self.log_level = str(self.log_level).upper()
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ConfigurationError(f"Invalid log_level: {self.log_level}")
        if self.default_ttl < 0:
            raise ConfigurationError("default_ttl must be >= 0")
        if self.persistence_backend not in ("sqlite", "redis", "memory"):
            raise ConfigurationError(
                f"persistence_backend must be sqlite, redis or memory, got {self.persistence_backend!r}"
            )
        if not isinstance(self.devices, list) or not isinstance(self.rules, list):
            raise ConfigurationError("'devices' and 'rules' must be lists")
        if not isinstance(self.schedules, list):
            raise ConfigurationError("'schedules' must be a list")
        if not isinstance(self.location, dict) or not isinstance(self.notifications, dict):
            raise ConfigurationError("'location' and 'notifications' must be mappings")
        if self.monitor_interval < 0:
            raise ConfigurationError("monitor_interval must be >= 0 (0 disables monitoring)")

    @classmethod
    def field_names(cls) -> List[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        """Create from a dictionary, expanding ``${ENV_VAR}`` references in strings."""
        data = _expand_env(data or {})
        valid = set(cls.field_names())
        unknown = sorted(k for k in data if k not in valid)
        if unknown:
            logger.warning("config_unknown_keys", keys=unknown)
        return cls(**{k: v for k, v in data.items() if k in valid})

    def to_dict(self, redact: bool = False) -> Dict[str, Any]:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        if redact and out.get("api_token"):
            out["api_token"] = "***"  # noqa: S105 - redaction marker, not a secret
        return out


def _expand_env(value: Any) -> Any:
    """Recursively expand ``${VAR}`` / ``${VAR:-default}`` in strings."""
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    if isinstance(value, str) and _ENV_PATTERN_START in value:
        import re

        def repl(match: "re.Match[str]") -> str:
            name, _, default = match.group(1).partition(":-")
            return os.environ.get(name, default)

        return re.sub(r"\$\{([^}]+)\}", repl, value)
    return value


def _read_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Config file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(text) if text.strip() else {}
        else:
            data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, json.JSONDecodeError) as e:
        raise ConfigurationError(f"Invalid config in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigurationError(f"Config root must be a mapping in {path}")
    return data


def from_yaml(path: Union[str, Path]) -> Config:
    """Load configuration from a YAML file."""
    path = Path(path)
    config = Config.from_dict(_read_file(path))
    logger.info("config_loaded", source=str(path), format="yaml")
    return config


def from_json(path: Union[str, Path]) -> Config:
    """Load configuration from a JSON file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if path.exists() else None
    if text is None:
        raise ConfigurationError(f"Config file not found: {path}")
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise ConfigurationError(f"Invalid JSON in {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigurationError(f"Config root must be an object in {path}")
    config = Config.from_dict(data)
    logger.info("config_loaded", source=str(path), format="json")
    return config


def _coerce(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", ""):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    if raw.strip().startswith(("{", "[")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


def _is_text_field(hint: Any) -> bool:
    """True for ``str`` and ``Optional[str]`` fields, which must not be coerced."""
    if hint is str:
        return True
    args = [a for a in getattr(hint, "__args__", ()) if a is not type(None)]
    return getattr(hint, "__origin__", None) is Union and args == [str]


def _env_values(prefix: str) -> Dict[str, Any]:
    hints = get_type_hints(Config)
    values: Dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):].lower()
        if name not in hints:
            continue
        if _is_text_field(hints[name]):
            # Tokens, paths and URLs stay strings even when they look numeric.
            if hints[name] is not str and raw.strip().lower() in ("", "null", "none"):
                values[name] = None  # clears an Optional[str] field
            else:
                values[name] = raw
        else:
            values[name] = _coerce(raw)
    return values


def from_env(prefix: str = "FUSEIOT_") -> Config:
    """
    Load configuration from environment variables.

    ``FUSEIOT_DEFAULT_TTL=2`` -> ``default_ttl=2``. JSON values are accepted for
    lists/dicts (e.g. ``FUSEIOT_TAGS='{"site": "plant-1"}'``).
    """
    values = _env_values(prefix)
    logger.info("config_loaded", source="environment", vars_count=len(values))
    return Config.from_dict(values)


def auto_config(config_path: Optional[Union[str, Path]] = None, env_prefix: str = "FUSEIOT_") -> Config:
    """
    Resolve configuration: defaults < file < environment variables.

    If ``config_path`` is None, ``FUSEIOT_CONFIG`` is used when set.
    """
    config_path = config_path or os.environ.get(f"{env_prefix}CONFIG")
    data: Dict[str, Any] = {}
    if config_path:
        data.update(_read_file(Path(config_path)))
    data.update(_env_values(env_prefix))
    config = Config.from_dict(data)
    logger.info("config_resolved", file=str(config_path) if config_path else None,
                environment=config.environment, devices=len(config.devices))
    return config
