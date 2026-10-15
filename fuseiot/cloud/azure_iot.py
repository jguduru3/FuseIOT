"""Azure IoT Hub connector (requires ``pip install 'fuseiot[cloud-azure]'``)."""
import json
from typing import Any, Callable, Dict, Optional

from ..exceptions import CloudError
from ..logging_config import get_logger
from .base import CloudConnector

logger = get_logger("cloud.azure")


class AzureIoT(CloudConnector):
    """
    Connect a hub to Azure IoT Hub as one device.

    * State changes are sent as device-to-cloud messages
      ``{"device_id", "state"}`` with ``type=state``.
    * Cloud-to-device messages ``{"device_id","command","params"}`` are
      executed; results are sent back with ``type=result``.
    """

    def __init__(self, hub: Any, connection_string: str):
        super().__init__(hub)
        self.connection_string = connection_string
        self._client: Optional[Any] = None

    def _sdk(self) -> Any:
        try:
            from azure.iot.device import IoTHubDeviceClient, Message
        except ImportError as e:
            raise CloudError("azure-iot-device is required: pip install 'fuseiot[cloud-azure]'") from e
        return IoTHubDeviceClient, Message

    def connect(self) -> bool:
        client_cls, _ = self._sdk()
        try:
            self._client = client_cls.create_from_connection_string(self.connection_string)
            self._client.connect()
        except Exception as e:
            logger.error("azure_connect_failed", error=str(e))
            self._connected = False
            return False
        self._connected = True
        logger.info("azure_connected")
        return True

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.shutdown()
            finally:
                self._client = None
        self._connected = False

    def _send(self, body: Dict[str, Any], msg_type: str) -> bool:
        if not self._connected or self._client is None:
            return False
        _, message_cls = self._sdk()
        msg = message_cls(json.dumps(body, default=str))
        msg.content_type = "application/json"
        msg.content_encoding = "utf-8"
        msg.custom_properties["type"] = msg_type
        try:
            self._client.send_message(msg)
            return True
        except Exception as e:
            logger.warning("azure_send_failed", error=str(e))
            return False

    def publish_state(self, device_id: str, state: Dict[str, Any]) -> bool:
        return self._send({"device_id": device_id, "state": state}, "state")

    def subscribe_commands(self, callback: Callable[[Dict[str, Any]], Any]) -> None:
        if self._client is None:
            raise CloudError("Cannot subscribe: not connected")

        def on_message(message: Any) -> None:
            try:
                payload = json.loads(message.data)
            except (TypeError, ValueError):
                logger.warning("azure_message_invalid")
                return
            result = callback(payload)
            if isinstance(result, dict):
                self._send(result, "result")

        self._client.on_message_received = on_message
