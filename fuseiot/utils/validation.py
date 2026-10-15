"""Pydantic models validating user-supplied device, command and state data."""
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOLS = ("http", "https", "mqtt", "mqtts", "websocket", "serial", "virtual")
CAPABILITIES = ("switchable", "dimmable", "sensor", "motor", "thermostat", "lock",
                "rgb_light", "energy_monitor", "smart_plug")


class CommandOptions(BaseModel):
    """Per-device command behaviour (maps onto CapabilityConfig)."""
    model_config = ConfigDict(extra="forbid")

    timeout: Optional[float] = Field(default=None, gt=0, le=300)
    confirm: Optional[bool] = None
    retry_attempts: Optional[int] = Field(default=None, ge=1, le=10)
    retry_delay: Optional[float] = Field(default=None, ge=0, le=30)
    poll_interval: Optional[float] = Field(default=None, gt=0, le=10)
    tolerance: Optional[float] = Field(default=None, ge=0)


class DeviceModel(BaseModel):
    """One entry of the ``devices:`` list in a config file."""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    device_id: str = Field(..., alias="id", min_length=1, max_length=128,
                           pattern=r"^[A-Za-z0-9_.\-]+$")
    name: Optional[str] = None
    protocol: str
    capability: str
    endpoint: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)
    capability_options: Dict[str, Any] = Field(default_factory=dict)
    command: CommandOptions = Field(default_factory=CommandOptions)
    tags: Dict[str, str] = Field(default_factory=dict)

    @field_validator("protocol", "capability")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("protocol")
    @classmethod
    def _known_protocol(cls, v: str) -> str:
        if v not in PROTOCOLS:
            raise ValueError(f"unknown protocol {v!r}; expected one of {', '.join(PROTOCOLS)}")
        return v

    @field_validator("capability")
    @classmethod
    def _known_capability(cls, v: str) -> str:
        if v not in CAPABILITIES:
            raise ValueError(f"unknown capability {v!r}; expected one of {', '.join(CAPABILITIES)}")
        return v

    @model_validator(mode="after")
    def _endpoint_required(self) -> "DeviceModel":
        if self.protocol != "virtual" and not (self.endpoint or self.options):
            raise ValueError(f"device {self.device_id!r}: 'endpoint' is required for {self.protocol}")
        if self.protocol in ("http", "https") and self.endpoint and not self.endpoint.startswith(
            ("http://", "https://")
        ):
            raise ValueError("HTTP endpoint must start with http:// or https://")
        if self.protocol == "websocket" and self.endpoint and not self.endpoint.startswith(("ws://", "wss://")):
            raise ValueError("WebSocket endpoint must start with ws:// or wss://")
        return self


class CommandModel(BaseModel):
    """A remote command request (REST API / cloud)."""
    model_config = ConfigDict(extra="forbid")

    device_id: str
    command: str = Field(..., pattern=r"^[a-z][a-z0-9_]*$")
    params: Dict[str, Any] = Field(default_factory=dict)


class StateModel(BaseModel):
    """State data validation."""

    device_id: str
    timestamp: float
    values: Dict[str, Any]
    source: str = "unknown"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
