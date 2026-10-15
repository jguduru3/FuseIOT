"""Bridge a hub to any MQTT broker (HiveMQ, EMQX, Mosquitto, AWS IoT, ...)."""
import json
from typing import Any, Callable, Dict, Optional

from ..exceptions import CloudError
from ..logging_config import get_logger
from ..protocols.mqtt import MQTT
from ..protocols.mqtts import MQTTS
from .base import CloudConnector

logger = get_logger("cloud.mqtt")


class MQTTBridge(CloudConnector):
    """
    Topics (``base_topic`` defaults to ``fuseiot/<client_id>``):

    * ``<base>/state/<device_id>``  - retained JSON state, published on change
    * ``<base>/commands``           - incoming ``{"device_id","command","params"}``
    * ``<base>/results``            - command results
    """

    def __init__(
        self,
        hub: Any,
        broker: str,
        port: Optional[int] = None,
        client_id: str = "fuseiot-hub",
        base_topic: Optional[str] = None,
        tls: bool = True,
        username: Optional[str] = None,
        password: Optional[str] = None,
        ca_certs: Optional[str] = None,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
        qos: int = 1,
    ):
        super().__init__(hub)
        self.base_topic = (base_topic or f"fuseiot/{client_id}").strip("/")
        common: Dict[str, Any] = dict(client_id=client_id, username=username, password=password,
                                      topic_prefix=self.base_topic, qos=qos)
        if tls:
            self._mqtt: MQTT = MQTTS(broker, port=port or 8883, ca_certs=ca_certs,
                                     certfile=certfile, keyfile=keyfile, **common)
        else:
            self._mqtt = MQTT(broker, port=port or 1883, **common)

    def connect(self) -> bool:
        self._connected = self._mqtt.connect()
        if not self._connected:
            logger.error("cloud_connect_failed", endpoint=self._mqtt.endpoint, error=self._mqtt.last_error)
        return self._connected

    def disconnect(self) -> None:
        self._mqtt.disconnect()
        self._connected = False

    def publish_state(self, device_id: str, state: Dict[str, Any]) -> bool:
        if not self._connected:
            return False
        try:
            self._mqtt.publish(f"{self.base_topic}/state/{device_id}",
                               json.dumps(state, default=str), retain=True)
            return True
        except Exception as e:
            logger.warning("cloud_publish_failed", device=device_id, error=str(e))
            return False

    def subscribe_commands(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        if not self._connected and not self.connect():
            raise CloudError("Cannot subscribe: not connected")

        def on_command(topic: str, payload: Any) -> None:
            result = callback(payload)
            if isinstance(result, dict):
                try:
                    self._mqtt.publish(f"{self.base_topic}/results", json.dumps(result, default=str))
                except Exception as e:
                    logger.warning("cloud_result_publish_failed", error=str(e))

        self._mqtt.subscribe("commands", on_command)
