"""
Google Cloud IoT Core connector (retired).

Google shut down Cloud IoT Core on 16 August 2023. This class remains so old
imports keep working, but connecting raises :class:`CloudError`. Use
:class:`~fuseiot.cloud.mqtt_bridge.MQTTBridge` with an MQTT broker (and e.g.
Pub/Sub on the Google side) instead.
"""
from typing import Any, Callable, Dict

from ..exceptions import CloudError
from .base import CloudConnector

RETIRED_MESSAGE = (
    "Google Cloud IoT Core was retired on 2023-08-16 and can no longer be used. "
    "Use fuseiot.cloud.MQTTBridge with your MQTT broker instead."
)


class GCPIoT(CloudConnector):
    def __init__(self, hub: Any, *args: Any, **kwargs: Any):
        super().__init__(hub)

    def connect(self) -> bool:
        raise CloudError(RETIRED_MESSAGE)

    def disconnect(self) -> None:
        self._connected = False

    def publish_state(self, device_id: str, state: Dict[str, Any]) -> bool:
        raise CloudError(RETIRED_MESSAGE)

    def subscribe_commands(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        raise CloudError(RETIRED_MESSAGE)
