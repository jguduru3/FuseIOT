"""The Hub: registry of devices plus shared cache, events, metrics and batching."""
import asyncio
import copy
import time
from dataclasses import dataclass
from threading import RLock
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Type,
    Union,
)

from fuseiot.__version__ import __version__
from fuseiot.capabilities.base import Capability, CapabilityConfig
from fuseiot.config import Config
from fuseiot.exceptions import ConfigurationError, DeviceError
from fuseiot.logging_config import configure_logging, get_logger
from fuseiot.monitoring.metrics import MetricsCollector
from fuseiot.protocols.base import Protocol
from fuseiot.result import BatchResult, CommandResult, CommandStatus
from fuseiot.state.cache import StateCache
from fuseiot.state.events import EventBus, StateChangeEvent
from fuseiot.state.persistence import PersistenceBackend, RedisBackend, SQLiteBackend
from fuseiot.utils.circuit_breaker import CircuitBreaker
from fuseiot.utils.rate_limiter import RateLimiter

logger = get_logger("hub")

Operation = Tuple[Capability, str, Dict[str, Any]]


@dataclass
class HubConfig:
    """
    Hub configuration.

    ``capability_defaults`` is copied for every device that is added without
    an explicit :class:`CapabilityConfig`. When ``circuit_breaker_enabled`` or
    ``rate_limit_enabled`` is set, each device gets its *own* breaker/limiter.
    """

    default_ttl: float = 5.0
    max_devices: int = 1000
    persistence: Optional[PersistenceBackend] = None
    enable_events: bool = True
    metrics_enabled: bool = True
    capability_defaults: Optional[CapabilityConfig] = None
    circuit_breaker_enabled: bool = False
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout: float = 30.0
    rate_limit_enabled: bool = False
    rate_limit_requests: int = 100
    rate_limit_window: float = 60.0
    history_path: Optional[str] = None

    @classmethod
    def from_config(cls, config: Config) -> "HubConfig":
        """Translate a file/env :class:`Config` into hub settings."""
        persistence: Optional[PersistenceBackend] = None
        if config.persistence_enabled:
            backend = (config.persistence_backend or "sqlite").lower()
            if backend == "sqlite":
                persistence = SQLiteBackend(config.persistence_path or "fuseiot.db")
            elif backend == "redis":
                persistence = RedisBackend(config.redis_url or "redis://localhost:6379/0")
            elif backend != "memory":
                raise ConfigurationError(f"Unknown persistence_backend: {config.persistence_backend!r}")

        return cls(
            default_ttl=config.default_ttl,
            max_devices=config.max_devices,
            persistence=persistence,
            enable_events=True,
            metrics_enabled=config.metrics_enabled,
            capability_defaults=CapabilityConfig(
                timeout=config.confirm_timeout,
                confirm=config.confirm_default,
                retry_attempts=config.default_retry_attempts,
                poll_interval=config.confirm_poll_interval,
            ),
            circuit_breaker_enabled=config.circuit_breaker_enabled,
            circuit_failure_threshold=config.circuit_failure_threshold,
            circuit_recovery_timeout=config.circuit_recovery_timeout,
            rate_limit_enabled=config.rate_limit_enabled,
            rate_limit_requests=config.rate_limit_requests,
            rate_limit_window=config.rate_limit_window,
            history_path=config.history_path if config.history_enabled else None,
        )


class Hub:
    """
    Central registry for device capabilities.

    Example:
        >>> hub = Hub()
        >>> plug = hub.add_device(HTTP("http://192.168.1.45/api"), Switchable, device_id="plug")
        >>> plug.on().confirmed
        True
    """

    def __init__(
        self,
        config: Optional[Union[HubConfig, Config]] = None,
        default_ttl: Optional[float] = None,
    ):
        #: The file/env :class:`Config` this hub was built from, if any.
        self.source_config: Optional[Config] = None
        if config is None:
            config = HubConfig()
        elif isinstance(config, Config):
            configure_logging(level=config.log_level, json_format=config.log_json,
                              log_file=config.log_file)
            self.source_config = config
            config = HubConfig.from_config(config)
        if default_ttl is not None:
            config.default_ttl = default_ttl

        self._config: HubConfig = config
        self._devices: Dict[str, Capability] = {}
        self._cache = StateCache(default_ttl=config.default_ttl, persistence=config.persistence)
        self._event_bus: Optional[EventBus] = EventBus() if config.enable_events else None
        self.metrics = MetricsCollector()
        self._lock = RLock()
        self._closed = False
        self._metadata = {
            "version": __version__,
            "created_at": time.time(),
            "devices_registered": 0,
            "devices_removed": 0,
        }
        self._metrics = {"commands_total": 0, "commands_success": 0, "commands_failed": 0}

        #: Optional :class:`~fuseiot.notify.NotificationManager` used by rules/schedules.
        self.notifier: Any = None
        self.history = None
        if config.history_path and self._event_bus is not None:
            from fuseiot.state.history import StateHistory

            self.history = StateHistory(config.history_path)
            self.history.attach(self._event_bus)

        logger.info("hub_initialized", version=__version__,
                    persistence_enabled=config.persistence is not None,
                    events_enabled=config.enable_events)

    # ------------------------------------------------------------- properties

    @property
    def config(self) -> HubConfig:
        return self._config

    @property
    def cache(self) -> StateCache:
        return self._cache

    @property
    def event_bus(self) -> Optional[EventBus]:
        return self._event_bus

    # ----------------------------------------------------------- registration

    def default_capability_config(self) -> CapabilityConfig:
        cfg = copy.copy(self._config.capability_defaults or CapabilityConfig())
        if self._config.circuit_breaker_enabled and cfg.circuit_breaker is None:
            cfg.circuit_breaker = CircuitBreaker(
                failure_threshold=self._config.circuit_failure_threshold,
                recovery_timeout=self._config.circuit_recovery_timeout,
            )
        if self._config.rate_limit_enabled and cfg.rate_limiter is None:
            cfg.rate_limiter = RateLimiter(
                requests=self._config.rate_limit_requests,
                window=self._config.rate_limit_window,
            )
        return cfg

    def add_device(
        self,
        protocol: Protocol,
        capability_class: Union[Type[Capability], str],
        device_id: Optional[str] = None,
        config: Optional[CapabilityConfig] = None,
        connect: bool = True,
        **capability_kwargs: Any,
    ) -> Capability:
        """
        Register a device.

        Args:
            protocol: Transport used to reach the device.
            capability_class: Capability class (or its registry name, e.g. ``"switchable"``).
            device_id: Unique ID; generated if omitted.
            config: Per-device :class:`CapabilityConfig` (defaults from the hub).
            connect: Connect now and fail fast if unreachable. With ``False`` the
                protocol connects lazily on first use.
            **capability_kwargs: Extra capability options (e.g. ``unit="celsius"``).

        Raises:
            ConfigurationError: duplicate ID, too many devices, or connection failure.
        """
        if isinstance(capability_class, str):
            from fuseiot.registry import resolve_capability

            capability_class = resolve_capability(capability_class)

        with self._lock:
            if device_id is None:
                base = capability_class.__name__.lower()
                n = 1
                while f"{base}_{n}" in self._devices:
                    n += 1
                device_id = f"{base}_{n}"
            if device_id in self._devices:
                raise ConfigurationError(
                    f"Device ID already exists: {device_id}. "
                    "Use unique identifiers or remove the existing device first."
                )
            if len(self._devices) >= self._config.max_devices:
                raise ConfigurationError(f"Max devices reached: {self._config.max_devices}")

        if connect and not protocol.is_connected and not protocol.connect():
            raise ConfigurationError(
                f"Cannot connect to {getattr(protocol, 'endpoint', protocol)} "
                f"for device {device_id!r}: {protocol.last_error or 'unreachable'}. "
                "Check network and device availability, or pass connect=False."
            )

        if self._event_bus is not None:
            capability_kwargs.setdefault("event_bus", self._event_bus)
        try:
            capability = capability_class(
                protocol=protocol,
                cache=self._cache,
                config=config if config is not None else self.default_capability_config(),
                device_id=device_id,
                **capability_kwargs,
            )
        except Exception as e:
            logger.error("capability_creation_failed", device_id=device_id, error=str(e))
            raise ConfigurationError(
                f"Failed to create {capability_class.__name__} {device_id!r}: {e}"
            ) from e

        capability.add_result_listener(self._record_result)

        with self._lock:
            if device_id in self._devices:  # lost a race with another thread
                raise ConfigurationError(f"Device ID already exists: {device_id}")
            self._devices[device_id] = capability
            self._metadata["devices_registered"] += 1

        self.metrics.gauge("fuseiot_devices", len(self._devices))
        logger.info("device_registered", device_id=device_id, category=capability.category,
                    protocol=protocol.name)
        return capability

    def _record_result(self, capability: Capability, result: CommandResult) -> None:
        with self._lock:
            self._metrics["commands_total"] += 1
            key = "commands_success" if result.success else "commands_failed"
            self._metrics[key] += 1
        if self._config.metrics_enabled:
            labels = {"device": capability.id, "command": result.command or "unknown",
                      "status": result.status.name.lower()}
            self.metrics.increment("fuseiot_commands_total", labels=labels)
            self.metrics.timer("fuseiot_command_latency_ms", result.latency_ms,
                               labels={"device": capability.id})

    # ---------------------------------------------------------------- lookup

    def get(self, device_id: str) -> Capability:
        """Retrieve a device by ID. Raises :class:`DeviceError` if missing."""
        with self._lock:
            try:
                return self._devices[device_id]
            except KeyError:
                raise DeviceError(f"Device not found: {device_id}",
                                  details={"device_id": device_id}) from None

    def find(self, **criteria: Any) -> List[Capability]:
        """Find devices by ``category`` and/or ``protocol`` name."""
        with self._lock:
            devices = list(self._devices.values())
        results = []
        for device in devices:
            if "category" in criteria and device.category != criteria["category"]:
                continue
            if "protocol" in criteria and device.protocol.name != criteria["protocol"]:
                continue
            results.append(device)
        return results

    def list_devices(
        self, category: Optional[str] = None, as_dict: bool = False
    ) -> Union[List[str], Dict[str, str]]:
        """List device IDs (optionally ``{id: category}``)."""
        with self._lock:
            items = [(did, dev.category) for did, dev in self._devices.items()
                     if category is None or dev.category == category]
        return dict(items) if as_dict else [did for did, _ in items]

    def devices(self) -> List[Capability]:
        with self._lock:
            return list(self._devices.values())

    # ---------------------------------------------------------------- removal

    def remove(self, device_id: str) -> bool:
        """Unregister a device and disconnect its protocol."""
        with self._lock:
            device = self._devices.pop(device_id, None)
            if device is None:
                return False
            self._metadata["devices_removed"] += 1
            self._cache.invalidate(device_id)
        self._disconnect(device)
        self.metrics.gauge("fuseiot_devices", len(self._devices))
        logger.info("device_removed", device_id=device_id)
        return True

    def _disconnect(self, device: Capability) -> None:
        # Several devices may share one protocol; only disconnect when unused.
        with self._lock:
            still_used = any(d.protocol is device.protocol for d in self._devices.values())
        if still_used:
            return
        try:
            device.protocol.disconnect()
        except Exception as e:
            logger.warning("protocol_disconnect_failed", device_id=device.id, error=str(e))

    def clear(self) -> None:
        """Remove all devices and clear the cache."""
        with self._lock:
            devices = list(self._devices.values())
            self._metadata["devices_removed"] += len(devices)
            self._devices.clear()
            self._cache.invalidate_all()
        seen = set()
        for device in devices:
            if id(device.protocol) in seen:
                continue
            seen.add(id(device.protocol))
            try:
                device.protocol.disconnect()
            except Exception as e:
                logger.warning("protocol_disconnect_failed", device_id=device.id, error=str(e))
        logger.info("hub_cleared")

    def close(self) -> None:
        """Disconnect everything and release resources. Safe to call twice."""
        if self._closed:
            return
        self.clear()
        if self.history is not None:
            self.history.close()
        self._closed = True

    # ---------------------------------------------------------------- batching

    async def batch(
        self,
        operations: List[Operation],
        max_concurrent: int = 10,
        continue_on_error: bool = True,
    ) -> BatchResult:
        """
        Run many commands concurrently.

        Args:
            operations: ``[(device, "method", {kwargs}), ...]``
            max_concurrent: Maximum commands in flight.
            continue_on_error: If False, cancel remaining work on first failure.
        """
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        semaphore = asyncio.Semaphore(max_concurrent)

        async def execute_one(cap: Capability, method: str, kwargs: Dict[str, Any]) -> CommandResult:
            async with semaphore:
                try:
                    # Native ``<method>_async`` if defined, else the automatic
                    # thread-pool wrapper provided by Capability.__getattr__.
                    outcome = await getattr(cap, f"{method}_async")(**kwargs)
                    if isinstance(outcome, CommandResult):
                        return outcome
                    return CommandResult(success=True, confirmed=False,
                                         status=CommandStatus.ACKNOWLEDGED,
                                         device_id=cap.id, command=method,
                                         metadata={"return_value": outcome})
                except Exception as e:
                    return CommandResult(success=False, confirmed=False,
                                         status=CommandStatus.FAILED, device_id=cap.id,
                                         command=method, error=str(e))

        start = time.monotonic()
        tasks = [asyncio.ensure_future(execute_one(c, m, k or {})) for c, m, k in operations]
        results: List[CommandResult] = []
        try:
            if continue_on_error:
                results = list(await asyncio.gather(*tasks))
            else:
                for next_done in asyncio.as_completed(tasks):
                    result = await next_done
                    results.append(result)
                    if not result.success:
                        break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        successful = sum(1 for r in results if r.success)
        return BatchResult(
            results=results,
            total=len(operations),
            successful=successful,
            failed=len(operations) - successful,
            confirmed=sum(1 for r in results if r.confirmed),
            total_latency_ms=(time.monotonic() - start) * 1000,
        )

    def batch_sync(self, operations: List[Operation], max_concurrent: int = 10,
                   continue_on_error: bool = True) -> BatchResult:
        """Blocking wrapper around :meth:`batch` for non-async code."""
        return asyncio.run(self.batch(operations, max_concurrent, continue_on_error))

    # ------------------------------------------------------------------ events

    def on_state_change(
        self,
        callback: Callable[[StateChangeEvent], None],
        device_id: Optional[str] = None,
        pattern: Optional[str] = None,
    ) -> Callable[[], None]:
        """
        Subscribe to state changes. Returns an unsubscribe function.

        Args:
            device_id: Only this device.
            pattern: Wildcard pattern, e.g. ``"living_room_*"``.
        """
        if self._event_bus is None:
            raise ConfigurationError("Event bus not enabled")
        if device_id:
            return self._event_bus.subscribe(device_id, callback)
        if pattern:
            return self._event_bus.subscribe_pattern(pattern, callback)
        return self._event_bus.subscribe_all(callback)

    # ------------------------------------------------------------------- stats

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            devices = list(self._devices.values())
            metrics = dict(self._metrics)
            meta = dict(self._metadata)
        return {
            "version": meta["version"],
            "devices_active": len(devices),
            "devices_connected": sum(1 for d in devices if d.protocol.is_connected),
            "devices_registered_total": meta["devices_registered"],
            "devices_removed_total": meta["devices_removed"],
            "cache_stats": self._cache.stats(),
            "event_stats": self._event_bus.stats() if self._event_bus else None,
            "uptime_seconds": round(time.time() - meta["created_at"], 2),
            "metrics": metrics,
        }

    # ------------------------------------------------------------------ dunder

    def __getitem__(self, device_id: str) -> Capability:
        return self.get(device_id)

    def __contains__(self, device_id: object) -> bool:
        with self._lock:
            return device_id in self._devices

    def __len__(self) -> int:
        with self._lock:
            return len(self._devices)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._devices.keys()))

    def __repr__(self) -> str:
        return f"<Hub devices={len(self)} version={self._metadata['version']}>"

    def __enter__(self) -> "Hub":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    async def __aenter__(self) -> "Hub":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.close)
