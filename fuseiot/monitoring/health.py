"""Health checks for embedding in your own application."""
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

from ..logging_config import get_logger

logger = get_logger("monitoring.health")


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthCheck:
    status: HealthStatus
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


class HealthServer:
    """
    Serve ``/health``, ``/ready`` and ``/metrics`` for a hub.

    The device API is not exposed (use ``fuseiot serve`` for that).

    Example:
        >>> health = HealthServer(hub, port=8081)
        >>> health.start()
    """

    def __init__(self, hub: Any, port: int = 8080, host: str = "127.0.0.1"):
        self.hub = hub
        self.port = port
        self.host = host
        self._checks: Dict[str, Callable[[], bool]] = {}
        self._server: Optional[Any] = None

    def add_check(self, name: str, check_func: Callable[[], bool]) -> None:
        """Add a custom check; returning False (or raising) marks the hub unhealthy."""
        self._checks[name] = check_func

    def check(self) -> HealthCheck:
        stats = self.hub.stats()
        for name, func in self._checks.items():
            try:
                ok = bool(func())
            except Exception as e:
                logger.error("health_check_error", check=name, error=str(e))
                ok = False
            if not ok:
                return HealthCheck(HealthStatus.UNHEALTHY, f"Custom check failed: {name}", stats)
        total, connected = stats["devices_active"], stats["devices_connected"]
        if total and connected == 0:
            return HealthCheck(HealthStatus.UNHEALTHY, "No devices reachable", stats)
        if connected < total:
            return HealthCheck(HealthStatus.DEGRADED, f"{total - connected} device(s) disconnected", stats)
        return HealthCheck(HealthStatus.HEALTHY, "All systems operational", stats)

    def start(self) -> None:
        from ..server.app import FuseIoTServer

        # A random token keeps /api/* closed; only health endpoints are usable.
        server = FuseIoTServer(self.hub, host=self.host, port=self.port,
                               api_token=secrets.token_urlsafe(32), enable_dashboard=False)
        original = server.health

        def health() -> Any:
            _code, payload = original()
            result = self.check()
            payload["status"] = result.status.value
            payload["message"] = result.message
            return (503 if result.status == HealthStatus.UNHEALTHY else 200), payload

        server.health = health  # type: ignore[method-assign]
        self._server = server.start()
        self.port = server.port
        logger.info("health_server_started", host=self.host, port=self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
