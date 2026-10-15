"""mDNS / DNS-SD device discovery (requires ``pip install 'fuseiot[discovery]'``)."""
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..exceptions import DiscoveryError
from ..logging_config import get_logger

logger = get_logger("discovery.mdns")

DEFAULT_SERVICE_TYPES = (
    "_http._tcp.local.",
    "_https._tcp.local.",
    "_mqtt._tcp.local.",
    "_hap._tcp.local.",
    "_fuseiot._tcp.local.",
)

_PROTOCOL_BY_SERVICE = {
    "_http._tcp.local.": "http",
    "_https._tcp.local.": "https",
    "_mqtt._tcp.local.": "mqtt",
    "_hap._tcp.local.": "homekit",
    "_fuseiot._tcp.local.": "http",
}


@dataclass
class DiscoveryResult:
    """One discovered service."""
    name: str
    address: str
    port: int
    device_type: str
    protocol: str
    properties: Dict[str, str] = field(default_factory=dict)
    hostname: Optional[str] = None
    addresses: List[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"{self.protocol}://{host}:{self.port}"


class MDNSDiscovery:
    """Browse the local network for IoT services."""

    def scan(self, service_type: Optional[str] = None, timeout: float = 5.0) -> List[DiscoveryResult]:
        """
        Browse for ``timeout`` seconds and return unique services found.

        Raises:
            DiscoveryError: if zeroconf is not installed or the network fails.
        """
        try:
            from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
        except ImportError as e:
            raise DiscoveryError("zeroconf is required: pip install 'fuseiot[discovery]'") from e

        found: Dict[str, DiscoveryResult] = {}
        lock = threading.Lock()

        def _decode(v: object) -> str:
            return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v)

        class Listener(ServiceListener):
            def _record(self, zc: "Zeroconf", type_: str, name: str) -> None:
                info = zc.get_service_info(type_, name, timeout=2000)
                if info is None:
                    return
                addresses = info.parsed_addresses()
                result = DiscoveryResult(
                    name=name,
                    address=addresses[0] if addresses else "",
                    port=info.port or 0,
                    device_type=type_,
                    protocol=_PROTOCOL_BY_SERVICE.get(type_, "unknown"),
                    properties={_decode(k): _decode(v) for k, v in (info.properties or {}).items()
                                if v is not None},
                    hostname=info.server,
                    addresses=addresses,
                )
                with lock:
                    found[name] = result
                logger.info("device_discovered", name=name, address=result.address, port=result.port)

            def add_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
                self._record(zc, type_, name)

            def update_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
                self._record(zc, type_, name)

            def remove_service(self, zc: "Zeroconf", type_: str, name: str) -> None:
                return

        try:
            zc = Zeroconf()
        except OSError as e:
            raise DiscoveryError(f"mDNS unavailable on this network: {e}") from e
        try:
            listener = Listener()
            types = [service_type] if service_type else list(DEFAULT_SERVICE_TYPES)
            browsers = [ServiceBrowser(zc, t, listener) for t in types]
            time.sleep(timeout)
            for browser in browsers:
                browser.cancel()
        finally:
            zc.close()
        return sorted(found.values(), key=lambda r: r.name)

    def find_by_name(self, name: str, timeout: float = 5.0) -> Optional[DiscoveryResult]:
        for result in self.scan(timeout=timeout):
            if name.lower() in result.name.lower():
                return result
        return None


def config_entries(results: List[DiscoveryResult]) -> List[Dict[str, object]]:
    """
    Turn discovered FuseIoT firmware (``_fuseiot._tcp`` with TXT ``id``/``cap``/``auth``)
    into ``devices:`` config entries. Other services are skipped.
    """
    from ..utils.validation import CAPABILITIES

    entries: List[Dict[str, object]] = []
    seen = set()
    for r in results:
        if r.device_type != "_fuseiot._tcp.local.":
            continue
        cap = r.properties.get("cap", "")
        device_id = r.properties.get("id") or r.name.split(".")[0]
        if cap not in CAPABILITIES or not device_id or device_id in seen:
            continue
        seen.add(device_id)
        host = r.hostname.rstrip(".") if r.hostname else r.address
        if ":" in host:
            host = f"[{host}]"
        port = "" if r.port in (80, 0) else f":{r.port}"
        entry: Dict[str, object] = {
            "id": device_id,
            "name": device_id.replace("_", " ").replace("-", " ").capitalize(),
            "capability": cap,
            "protocol": "http",
            "endpoint": f"http://{host}{port}",
        }
        if r.properties.get("auth") == "1":
            entry["options"] = {"bearer_token": "${" + device_id.upper().replace("-", "_") + "_TOKEN}"}
        entries.append(entry)
    return entries
