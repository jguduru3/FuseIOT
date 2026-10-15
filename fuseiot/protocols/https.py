"""HTTPS transport with TLS options (CA bundle, mutual TLS)."""
from dataclasses import dataclass
from typing import Any, Optional, Tuple, Union

from ..logging_config import get_logger
from .http import HTTP, HTTPConfig

logger = get_logger("protocols.https")


@dataclass
class HTTPSConfig(HTTPConfig):
    """HTTPS-specific configuration."""
    ca_bundle: Optional[str] = None
    client_key: Optional[str] = None


class HTTPS(HTTP):
    """
    HTTP over TLS.

    Args:
        base_url: ``https://...`` (``http://`` is upgraded automatically).
        verify_ssl: Verify the server certificate. Never disable in production.
        ca_bundle: Path to a CA bundle for private/self-signed CAs.
        client_cert: Client certificate file, or ``(cert_file, key_file)``.
        client_key: Client key file when ``client_cert`` is a single path.
    """

    def __init__(
        self,
        base_url: str,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        client_cert: Optional[Union[str, Tuple[str, str]]] = None,
        client_key: Optional[str] = None,
        **kwargs: Any,
    ):
        if base_url.startswith("http://"):
            base_url = "https://" + base_url[len("http://"):]
        elif not base_url.startswith("https://"):
            base_url = "https://" + base_url

        cert: Optional[Tuple[str, str]] = None
        if isinstance(client_cert, str):
            if not client_key:
                raise ValueError("client_key is required when client_cert is a single path")
            cert = (client_cert, client_key)
        elif client_cert:
            cert = tuple(client_cert)  # type: ignore[assignment]

        verify: Union[bool, str] = ca_bundle if (verify_ssl and ca_bundle) else verify_ssl
        if not verify_ssl:
            logger.warning("tls_verification_disabled", url=base_url)

        super().__init__(base_url=base_url, verify_ssl=verify, client_cert=cert, **kwargs)
        self.ca_bundle = ca_bundle
        self.client_key = client_key
