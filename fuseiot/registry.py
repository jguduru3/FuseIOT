"""
Build devices from declarative configuration.

Used by the CLI, the server and :func:`load_devices` in user code::

    config = from_yaml("fuseiot.yaml")
    hub = Hub(config)
    load_devices(hub, config.devices)
"""
from typing import Any, Dict, List, Optional, Type
from urllib.parse import unquote, urlparse

from pydantic import ValidationError as PydanticValidationError

from .capabilities.base import Capability
from .exceptions import ConfigurationError
from .logging_config import get_logger
from .protocols.base import Protocol
from .utils.validation import DeviceModel

logger = get_logger("registry")


def capability_classes() -> Dict[str, Type[Capability]]:
    from .capabilities import (
        Dimmable,
        EnergyMonitor,
        Lock,
        Motor,
        RGBLight,
        Sensor,
        SmartPlug,
        Switchable,
        Thermostat,
    )

    return {
        "switchable": Switchable,
        "dimmable": Dimmable,
        "sensor": Sensor,
        "motor": Motor,
        "thermostat": Thermostat,
        "lock": Lock,
        "rgb_light": RGBLight,
        "energy_monitor": EnergyMonitor,
        "smart_plug": SmartPlug,
    }


def resolve_capability(name: str) -> Type[Capability]:
    try:
        return capability_classes()[name.strip().lower()]
    except KeyError:
        raise ConfigurationError(
            f"Unknown capability {name!r}; expected one of {', '.join(capability_classes())}"
        ) from None


def _mqtt_kwargs(endpoint: Optional[str], options: Dict[str, Any], tls: bool) -> Dict[str, Any]:
    kwargs = dict(options)
    if endpoint:
        url = urlparse(endpoint if "://" in endpoint else f"{'mqtts' if tls else 'mqtt'}://{endpoint}")
        if url.hostname:
            kwargs.setdefault("broker", url.hostname)
        if url.port:
            kwargs.setdefault("port", url.port)
        if url.username:
            kwargs.setdefault("username", unquote(url.username))
        if url.password:
            kwargs.setdefault("password", unquote(url.password))
        if url.path.strip("/"):
            kwargs.setdefault("topic_prefix", url.path.strip("/"))
    if "broker" not in kwargs:
        raise ConfigurationError("MQTT device needs an endpoint like mqtt://broker:1883/home/plug1")
    return kwargs


def build_protocol(spec: DeviceModel) -> Protocol:
    """Instantiate the transport described by a device spec."""
    options = dict(spec.options)
    p = spec.protocol
    try:
        if p == "http":
            from .protocols.http import HTTP

            return HTTP(spec.endpoint or options.pop("base_url"), **options)
        if p == "https":
            from .protocols.https import HTTPS

            return HTTPS(spec.endpoint or options.pop("base_url"), **options)
        if p == "mqtt":
            from .protocols.mqtt import MQTT

            return MQTT(**_mqtt_kwargs(spec.endpoint, options, tls=False))
        if p == "mqtts":
            from .protocols.mqtts import MQTTS

            return MQTTS(**_mqtt_kwargs(spec.endpoint, options, tls=True))
        if p == "websocket":
            from .protocols.web_socket import WebSocket

            return WebSocket(spec.endpoint or options.pop("uri"), **options)
        if p == "serial":
            from .protocols.serial import Serial

            return Serial(spec.endpoint or options.pop("port"), **options)
        if p == "virtual":
            from .simulation.virtual_device import VirtualDevice

            device_type = options.pop("device_type", None) or spec.endpoint or spec.capability
            return VirtualDevice(device_type, **options)
    except ConfigurationError:
        raise
    except (TypeError, KeyError, ValueError, ImportError) as e:
        raise ConfigurationError(f"Invalid {p} options for device {spec.device_id!r}: {e}") from e
    raise ConfigurationError(f"Unsupported protocol {p!r}")  # pragma: no cover


def parse_device(entry: Dict[str, Any]) -> DeviceModel:
    if not isinstance(entry, dict):
        raise ConfigurationError(f"Each device must be a mapping with id/capability/protocol, got {entry!r}")
    try:
        return DeviceModel.model_validate(entry)
    except PydanticValidationError as e:
        ident = entry.get("id") or entry.get("device_id") or "<missing id>"
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'device'}: {err['msg']}" for err in e.errors()
        )
        raise ConfigurationError(f"Invalid device {ident!r}: {problems}") from None


def add_device_from_spec(hub: Any, entry: Dict[str, Any], connect: bool = False) -> Capability:
    """
    Create and register one device from a config entry.

    With ``connect=False`` an unreachable device is still registered (and
    reconnects lazily); a warning is logged.
    """
    spec = parse_device(entry)
    return _register(hub, spec, build_protocol(spec), connect)


def _register(hub: Any, spec: DeviceModel, protocol: Protocol, connect: bool) -> Capability:
    cls = resolve_capability(spec.capability)
    cap_config = hub.default_capability_config()
    if spec.name:
        cap_config.name = spec.name
    for key, value in spec.command.model_dump(exclude_none=True).items():
        setattr(cap_config, key, value)

    device = hub.add_device(protocol, cls, device_id=spec.device_id, config=cap_config,
                            connect=connect, **spec.capability_options)
    if not connect and not protocol.is_connected:
        try:
            if not protocol.connect():
                logger.warning("device_unreachable", device_id=spec.device_id,
                               endpoint=protocol.endpoint, error=protocol.last_error)
        except Exception as e:
            logger.warning("device_unreachable", device_id=spec.device_id, error=str(e))
    return device


def load_devices(hub: Any, entries: List[Dict[str, Any]], connect: bool = False) -> List[Capability]:
    """
    Register every device in ``entries``.

    All entries are validated and their protocols built before any device is
    registered, so a mistake cannot leave a half-loaded hub. If registration
    itself fails, devices added by this call are removed again.
    """
    specs = [parse_device(e) for e in entries]
    ids = [s.device_id for s in specs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ConfigurationError(f"Duplicate device ids in config: {', '.join(duplicates)}")
    clashes = sorted(i for i in ids if i in hub)
    if clashes:
        raise ConfigurationError(f"Device ids already registered: {', '.join(clashes)}")
    protocols = [build_protocol(s) for s in specs]
    devices: List[Capability] = []
    try:
        for spec, protocol in zip(specs, protocols):
            devices.append(_register(hub, spec, protocol, connect))
    except Exception:
        for device in devices:
            hub.remove(device.id)
        raise
    logger.info("devices_loaded", count=len(devices))
    return devices
