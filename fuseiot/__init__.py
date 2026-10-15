"""FuseIoT - deterministic device control SDK for Python."""

from fuseiot.__version__ import __version__, __version_info__
from fuseiot.auth import UserStore
from fuseiot.automation.actions import NotifyAction

# Home automation
from fuseiot.automation.schedule import Location, Schedule, Scheduler

# Capabilities
from fuseiot.capabilities.base import AsyncCapability, Capability, CapabilityConfig
from fuseiot.capabilities.composite import CompositeCapability, SmartPlug
from fuseiot.capabilities.dimmable import Dimmable
from fuseiot.capabilities.energy_monitor import EnergyMonitor
from fuseiot.capabilities.lock import Lock, LockState
from fuseiot.capabilities.motor import Motor
from fuseiot.capabilities.rgb_light import ColorMode, RGBLight
from fuseiot.capabilities.sensor import Sensor
from fuseiot.capabilities.switchable import Switchable
from fuseiot.capabilities.thermostat import Thermostat, ThermostatMode

# Cloud connectors (their SDK dependencies are only needed when connecting)
from fuseiot.cloud import AWSIoT, AzureIoT, CloudConnector, GCPIoT, MQTTBridge
from fuseiot.config import Config, auto_config, from_env, from_json, from_yaml
from fuseiot.discovery.mdns import DiscoveryResult, MDNSDiscovery

# Edge computing, discovery, monitoring, simulation, server (stdlib-only)
from fuseiot.edge.rules import Action, Condition, Rule, RuleEngine, Trigger

# Exceptions
from fuseiot.exceptions import (
    AuthenticationError,
    AuthorizationError,
    CacheError,
    CircuitOpenError,
    CloudError,
    CommandError,
    ConcurrencyError,
    ConfigurationError,
    ConfirmationError,
    DeviceError,
    DiscoveryError,
    FuseIoTError,
    ProtocolError,
    RateLimitError,
    ResourceExhaustedError,
    StateError,
    TimeoutError,
    ValidationError,
)

# Core classes
from fuseiot.hub import Hub, HubConfig
from fuseiot.logging_config import configure_logging, get_logger
from fuseiot.monitoring.device_monitor import DeviceMonitor
from fuseiot.monitoring.health import HealthServer
from fuseiot.monitoring.health import HealthStatus as ServerHealth
from fuseiot.monitoring.metrics import MetricsCollector, PrometheusMetrics
from fuseiot.notify import NotificationManager

# Protocols
from fuseiot.protocols.base import AsyncProtocol, Protocol, ProtocolConfig
from fuseiot.protocols.http import HTTP, HTTPConfig
from fuseiot.protocols.https import HTTPS, HTTPSConfig
from fuseiot.protocols.mqtt import MQTT, MQTTConfig
from fuseiot.protocols.mqtts import MQTTS, MQTTSConfig
from fuseiot.protocols.serial import Serial, SerialConfig

# Optional dependencies (websockets, pyserial) are checked on instantiation
from fuseiot.protocols.web_socket import WebSocket, WebSocketConfig
from fuseiot.registry import load_devices
from fuseiot.result import BatchResult, CommandResult, CommandStatus
from fuseiot.runtime import Runtime, validate_config
from fuseiot.server.app import FuseIoTServer, run_server
from fuseiot.simulation.http_device import SimulatedHTTPDevice
from fuseiot.simulation.virtual_device import VirtualDevice, VirtualDeviceConfig
from fuseiot.state.cache import CacheEntry, StateCache
from fuseiot.state.events import EventBus, StateChangeEvent
from fuseiot.state.history import StateHistory
from fuseiot.state.persistence import PersistenceBackend, RedisBackend, SQLiteBackend

# Types
from fuseiot.types import (
    Brightness,
    CapabilityType,
    Color,
    DeviceID,
    DeviceInfo,
    DimmableProtocol,
    Handler,
    HealthStatus,
    LockProtocol,
    MotorProtocol,
    Payload,
    Position,
    ProtocolType,
    Readable,
    RGBLightProtocol,
    SensorProtocol,
    StateVector,
    SwitchableProtocol,
)
from fuseiot.types import (
    CommandStatus as CommandStatusEnum,
)
from fuseiot.utils.async_tools import AsyncLock, create_task_safely, run_async, run_sync
from fuseiot.utils.circuit_breaker import CircuitBreaker, CircuitState
from fuseiot.utils.rate_limiter import RateLimiter, TokenBucket

# Utilities
from fuseiot.utils.retry import AsyncRetryConfig, RetryConfig, async_retry, retry
from fuseiot.utils.validation import CommandModel, DeviceModel, StateModel

__all__ = [
    # Version
    "__version__",
    "__version_info__",

    # Core
    "Hub",
    "HubConfig",
    "CommandResult",
    "CommandStatus",
    "CommandStatusEnum",
    "BatchResult",
    "StateCache",
    "CacheEntry",
    "PersistenceBackend",
    "SQLiteBackend",
    "RedisBackend",
    "EventBus",
    "StateChangeEvent",
    "Config",
    "from_yaml",
    "from_json",
    "from_env",
    "auto_config",
    "configure_logging",
    "get_logger",

    # Exceptions
    "FuseIoTError",
    "DeviceError",
    "ProtocolError",
    "CommandError",
    "ConfirmationError",
    "StateError",
    "CacheError",
    "ConfigurationError",
    "ValidationError",
    "TimeoutError",
    "ConcurrencyError",
    "ResourceExhaustedError",
    "CircuitOpenError",
    "RateLimitError",
    "AuthenticationError",
    "AuthorizationError",
    "DiscoveryError",
    "CloudError",

    # Types
    "StateVector",
    "DeviceID",
    "Payload",
    "Handler",
    "Readable",
    "SwitchableProtocol",
    "SensorProtocol",
    "MotorProtocol",
    "DimmableProtocol",
    "LockProtocol",
    "RGBLightProtocol",
    "HealthStatus",
    "DeviceInfo",
    "CapabilityType",
    "ProtocolType",
    "Color",
    "Position",
    "Brightness",

    # Protocols
    "Protocol",
    "AsyncProtocol",
    "ProtocolConfig",
    "HTTP",
    "HTTPConfig",
    "HTTPS",
    "HTTPSConfig",
    "MQTT",
    "MQTTConfig",
    "MQTTS",
    "MQTTSConfig",
    "WebSocket",
    "WebSocketConfig",
    "Serial",
    "SerialConfig",

    # Capabilities
    "Capability",
    "AsyncCapability",
    "CapabilityConfig",
    "Switchable",
    "Dimmable",
    "Sensor",
    "Motor",
    "Thermostat",
    "ThermostatMode",
    "Lock",
    "LockState",
    "RGBLight",
    "ColorMode",
    "EnergyMonitor",
    "CompositeCapability",
    "SmartPlug",

    # Utilities
    "retry",
    "RetryConfig",
    "async_retry",
    "AsyncRetryConfig",
    "run_sync",
    "run_async",
    "AsyncLock",
    "create_task_safely",
    "CircuitBreaker",
    "CircuitState",
    "RateLimiter",
    "TokenBucket",
    "DeviceModel",
    "CommandModel",
    "StateModel",

    # Cloud
    "CloudConnector",
    "MQTTBridge",
    "AWSIoT",
    "AzureIoT",
    "GCPIoT",

    # Edge
    "RuleEngine",
    "Rule",
    "Condition",
    "Action",
    "Trigger",

    # Discovery
    "MDNSDiscovery",
    "DiscoveryResult",

    # Monitoring
    "MetricsCollector",
    "PrometheusMetrics",
    "HealthServer",
    "ServerHealth",

    # Simulation
    "VirtualDevice",
    "VirtualDeviceConfig",
    "SimulatedHTTPDevice",

    # Deployment
    "FuseIoTServer",
    "run_server",
    "load_devices",
    "StateHistory",

    # Home automation
    "Location",
    "Schedule",
    "Scheduler",
    "NotifyAction",
    "NotificationManager",
    "DeviceMonitor",
    "UserStore",
    "Runtime",
    "validate_config",
]
