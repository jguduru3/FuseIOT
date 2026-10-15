"""AWS IoT Core connector (MQTT over mutual TLS; no AWS SDK needed)."""
from typing import Any, Optional

from .mqtt_bridge import MQTTBridge


class AWSIoT(MQTTBridge):
    """
    Connect a hub to AWS IoT Core as a Thing.

    Args:
        endpoint: Your ATS endpoint, e.g. ``abc123-ats.iot.eu-west-1.amazonaws.com``.
        client_id: Thing name (must be allowed by the attached IoT policy).
        cert_path / key_path: Device certificate and private key.
        ca_path: Amazon Root CA 1.

    Topics follow :class:`MQTTBridge` with base ``fuseiot/<client_id>``; your IoT
    policy must allow publish/subscribe/receive on ``fuseiot/<client_id>/*``.
    """

    def __init__(
        self,
        hub: Any,
        endpoint: str,
        client_id: str,
        cert_path: str,
        key_path: str,
        ca_path: str,
        port: int = 8883,
        base_topic: Optional[str] = None,
    ):
        super().__init__(
            hub,
            broker=endpoint,
            port=port,
            client_id=client_id,
            base_topic=base_topic,
            tls=True,
            ca_certs=ca_path,
            certfile=cert_path,
            keyfile=key_path,
        )
        self.endpoint = endpoint
        self.client_id = client_id
