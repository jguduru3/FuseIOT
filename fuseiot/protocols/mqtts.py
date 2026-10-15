"""MQTT over TLS."""
import ssl
from dataclasses import dataclass
from typing import Any, Optional

from ..logging_config import get_logger
from .mqtt import MQTT, MQTTConfig

logger = get_logger("protocols.mqtts")


@dataclass
class MQTTSConfig(MQTTConfig):
    """MQTTS-specific configuration."""
    tls_enabled: bool = True
    ca_certs: Optional[str] = None
    certfile: Optional[str] = None
    keyfile: Optional[str] = None


class MQTTS(MQTT):
    """
    MQTT over TLS (default port 8883).

    Args:
        ca_certs: CA bundle; system trust store is used when omitted.
        certfile / keyfile: Client certificate for mutual TLS (e.g. AWS IoT).
        tls_insecure: Skip hostname verification. Testing only.
    """

    def __init__(
        self,
        broker: str,
        port: int = 8883,
        ca_certs: Optional[str] = None,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
        tls_insecure: bool = False,
        **kwargs: Any,
    ):
        super().__init__(broker=broker, port=port, **kwargs)
        self.ca_certs = ca_certs
        self.certfile = certfile
        self.keyfile = keyfile
        self.tls_insecure = tls_insecure

    @property
    def scheme(self) -> str:
        return "mqtts"

    def _configure_client(self, client: Any) -> None:
        client.tls_set(
            ca_certs=self.ca_certs,
            certfile=self.certfile,
            keyfile=self.keyfile,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        if self.tls_insecure:
            logger.warning("mqtts_tls_insecure", broker=self.broker)
            client.tls_insecure_set(True)
