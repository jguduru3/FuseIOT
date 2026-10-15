"""Cloud connectors."""
from typing import Any, Dict

from fuseiot.cloud.aws_iot import AWSIoT
from fuseiot.cloud.azure_iot import AzureIoT
from fuseiot.cloud.base import CloudConnector
from fuseiot.cloud.gcp_iot import GCPIoT
from fuseiot.cloud.mqtt_bridge import MQTTBridge
from fuseiot.exceptions import ConfigurationError


def connector_from_config(hub: Any, provider: str, options: Dict[str, Any]) -> CloudConnector:
    """Create a connector from ``cloud_provider`` / ``cloud_options`` config values."""
    providers = {"aws": AWSIoT, "azure": AzureIoT, "mqtt": MQTTBridge, "gcp": GCPIoT}
    try:
        cls = providers[(provider or "").lower()]
    except KeyError:
        raise ConfigurationError(f"Unknown cloud_provider {provider!r}; use aws, azure or mqtt") from None
    try:
        return cls(hub, **options)
    except TypeError as e:
        raise ConfigurationError(f"Invalid cloud_options for {provider}: {e}") from e


__all__ = ["CloudConnector", "MQTTBridge", "AWSIoT", "AzureIoT", "GCPIoT", "connector_from_config"]
