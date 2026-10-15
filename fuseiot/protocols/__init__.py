# fuseiot/protocols/__init__.py
"""Protocol implementations for FuseIoT."""

from fuseiot.protocols.base import AsyncProtocol, ConnectionState, Protocol, ProtocolConfig
from fuseiot.protocols.http import HTTP, HTTPConfig
from fuseiot.protocols.https import HTTPS, HTTPSConfig
from fuseiot.protocols.mqtt import MQTT, MQTTConfig
from fuseiot.protocols.mqtts import MQTTS, MQTTSConfig

# Optional dependencies are checked when a protocol is instantiated.
from fuseiot.protocols.serial import Serial, SerialConfig
from fuseiot.protocols.web_socket import WebSocket, WebSocketConfig

__all__ = [
    "Protocol",
    "ProtocolConfig",
    "AsyncProtocol",
    "ConnectionState",
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
]
