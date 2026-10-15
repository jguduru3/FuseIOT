"""
Capability base classes.

A capability describes *what* a device can do (switch, dim, move, ...). It
turns friendly method calls into protocol payloads and runs every command
through the same pipeline:

    rate limit -> circuit breaker -> send (with retries) -> confirm -> cache/event
"""
import asyncio
import copy
import functools
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from ..exceptions import StateError
from ..logging_config import get_logger
from ..result import CommandResult, CommandStatus
from ..state.confirm import ConfirmationEngine, state_matches
from ..utils.circuit_breaker import CircuitBreaker
from ..utils.rate_limiter import RateLimiter, TokenBucket

if TYPE_CHECKING:
    from ..protocols.base import Protocol
    from ..state.cache import StateCache

logger = get_logger("capabilities.base")

ResultListener = Callable[["Capability", CommandResult], None]


@dataclass
class CapabilityConfig:
    """
    Per-device command behaviour.

    Attributes:
        name: Optional human-readable name.
        timeout: Seconds allowed for send + confirmation.
        confirm: Verify state after each command (overridable per call).
        retry_attempts: Maximum send attempts when the transport fails (>= 1).
        retry_delay: Base delay between send attempts (doubles each retry).
        poll_interval: Seconds between confirmation polls.
        tolerance: Absolute tolerance when comparing numeric state values.
        circuit_breaker: Optional breaker; opens after repeated failures.
        rate_limiter: Optional :class:`RateLimiter` or :class:`TokenBucket`.
        metrics_enabled: Track per-device command metrics.
    """

    name: Optional[str] = None
    timeout: float = 2.0
    confirm: bool = True
    retry_attempts: int = 3
    retry_delay: float = 0.1
    poll_interval: float = 0.1
    tolerance: float = 1e-6
    circuit_breaker: Optional[CircuitBreaker] = None
    rate_limiter: Optional[Any] = None
    metrics_enabled: bool = True


class Capability(ABC):
    """
    Abstract base for device capabilities.

    Subclasses implement :attr:`category` and :meth:`_fetch_state` (an uncached
    read of the device that returns a normalised state dict), and declare the
    public command methods they expose in :attr:`commands`.

    Any command method ``foo`` is also available as an awaitable ``foo_async``.
    """

    #: Public command methods that may be invoked remotely (CLI, REST API, rules).
    commands: Tuple[str, ...] = ()

    def __init__(
        self,
        protocol: "Protocol",
        cache: "StateCache",
        config: Optional[CapabilityConfig] = None,
        event_bus: Optional[Any] = None,
        device_id: Optional[str] = None,
        **kwargs: Any,
    ):
        self.protocol = protocol
        self.cache = cache
        # Copy so per-device tweaks (e.g. tolerance) never leak into shared configs.
        self.config = copy.copy(config) if config is not None else CapabilityConfig()
        self.event_bus = event_bus
        self._device_id = device_id or f"{self.__class__.__name__.lower()}_{id(self):x}"
        self._result_listeners: List[ResultListener] = []
        self._confirm_engine = ConfirmationEngine(poll_interval=self.config.poll_interval)
        self._metrics: Dict[str, Any] = {
            "commands_total": 0,
            "commands_success": 0,
            "commands_failed": 0,
            "confirmation_failures": 0,
            "avg_latency_ms": 0.0,
        }
        if kwargs:
            logger.debug("capability_unused_kwargs", device=self._device_id, keys=sorted(kwargs))

    # ------------------------------------------------------------------ identity

    @property
    def id(self) -> str:
        """Device identifier (the ``device_id`` given to :meth:`Hub.add_device`)."""
        return self._device_id

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    @abstractmethod
    def category(self) -> str:
        """Capability type identifier, e.g. ``"switchable"``."""

    def describe(self) -> Dict[str, Any]:
        """Static description used by the CLI and REST API."""
        return {
            "device_id": self.id,
            "name": self.config.name or self.id,
            "category": self.category,
            "protocol": getattr(self.protocol, "name", "unknown"),
            "endpoint": getattr(self.protocol, "endpoint", None),
            "connected": bool(getattr(self.protocol, "is_connected", False)),
            "commands": list(self.commands),
        }

    # --------------------------------------------------------------------- state

    @abstractmethod
    def _fetch_state(self) -> Dict[str, Any]:
        """Read state directly from the device (no cache). Raise on failure."""

    def read_state(self, fresh: bool = False) -> Dict[str, Any]:
        """
        Return current device state.

        Uses the cache unless ``fresh`` is True or the cached value expired.

        Raises:
            StateError: if the device cannot be read.
        """
        if not fresh:
            cached = self.cache.get(self.id)
            if cached is not None:
                return cached

        old = self.last_known_state()
        try:
            state = self._fetch_state()
        except StateError:
            raise
        except Exception as e:
            raise StateError(
                f"Failed to read state of {self.id}: {e}",
                details={"device_id": self.id, "error": str(e)},
            ) from e

        self.cache.set(self.id, state, source="poll")
        if old is not None and old != state:
            self._emit_event(old, state, source="poll")
        return state

    async def read_state_async(self, fresh: bool = False) -> Dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, functools.partial(self.read_state, fresh))

    def last_known_state(self) -> Optional[Dict[str, Any]]:
        """Most recent state seen (possibly stale), without contacting the device."""
        entry = self.cache.peek(self.id)
        return entry.value if entry is not None else None


    def _state_value(self, key: str, default: Any = None) -> Any:
        return self.read_state().get(key, default)

    # -------------------------------------------------------------------- events

    def add_result_listener(self, listener: ResultListener) -> Callable[[], None]:
        """Register a callback invoked with every :class:`CommandResult`."""
        self._result_listeners.append(listener)

        def remove() -> None:
            if listener in self._result_listeners:
                self._result_listeners.remove(listener)

        return remove

    def _emit_event(
        self,
        old_state: Optional[Dict[str, Any]],
        new_state: Dict[str, Any],
        source: str = "command",
        latency_ms: Optional[float] = None,
    ) -> None:
        if not self.event_bus:
            return
        from ..state.events import StateChangeEvent

        self.event_bus.emit(
            StateChangeEvent(
                device_id=self.id,
                timestamp=time.time(),
                old_state=old_state,
                new_state=new_state,
                source=source,
                latency_ms=latency_ms,
            )
        )

    def _finish(self, result: CommandResult) -> CommandResult:
        """Update metrics and notify listeners."""
        if self.config.metrics_enabled:
            m = self._metrics
            m["commands_total"] += 1
            if result.success:
                m["commands_success"] += 1
            else:
                m["commands_failed"] += 1
                if result.status == CommandStatus.TIMEOUT:
                    m["confirmation_failures"] += 1
            total = m["commands_total"]
            m["avg_latency_ms"] = (m["avg_latency_ms"] * (total - 1) + result.latency_ms) / total

        for listener in list(self._result_listeners):
            try:
                listener(self, result)
            except Exception as e:  # never let observers break commands
                logger.warning("result_listener_error", device=self.id, error=str(e))

        log = logger.info if result.success else logger.warning
        log(
            "command_finished",
            device=self.id,
            command=result.command,
            status=result.status.name,
            latency_ms=round(result.latency_ms, 1),
            error=result.error,
        )
        return result

    def get_metrics(self) -> Dict[str, Any]:
        return dict(self._metrics)

    # ------------------------------------------------------------------ commands

    def _check_rate_limit(self) -> Optional[Dict[str, Any]]:
        limiter = self.config.rate_limiter
        if limiter is None or limiter.allow():
            return None
        meta: Dict[str, Any] = {"retry_after": limiter.time_until_next()}
        if isinstance(limiter, RateLimiter):
            meta.update(limit=limiter.requests, window_seconds=limiter.window)
        elif isinstance(limiter, TokenBucket):
            meta.update(limit=limiter.capacity, window_seconds=limiter.capacity / limiter.rate)
        return meta

    def _send_with_retries(self, payload: Dict[str, Any], deadline: float) -> Tuple[Any, int]:
        """Send payload, retrying transport errors. Returns (response, attempts)."""
        attempts = max(1, int(self.config.retry_attempts))
        last_error: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                return self.protocol.send(payload), attempt
            except Exception as e:
                last_error = e
                delay = self.config.retry_delay * (2 ** (attempt - 1))
                if attempt == attempts or time.monotonic() + delay >= deadline:
                    e._fuseiot_attempts = attempt
                    raise
                logger.debug("send_retry", device=self.id, attempt=attempt, error=str(e))
                time.sleep(delay)
        raise last_error  # pragma: no cover - loop always returns or raises

    def _send_and_confirm(
        self,
        command_payload: Dict[str, Any],
        expected_state: Dict[str, Any],
        timeout: Optional[float] = None,
        command_name: Optional[str] = None,
        confirm: Optional[bool] = None,
    ) -> CommandResult:
        """
        Execute one command through the full pipeline.

        Never raises for device problems; the outcome is in the returned result.
        """
        start = time.monotonic()
        timeout_val = timeout if timeout is not None else self.config.timeout
        deadline = start + timeout_val
        use_confirm = self.config.confirm if confirm is None else confirm
        state_before = self.last_known_state()

        def elapsed_ms() -> float:
            return (time.monotonic() - start) * 1000

        def make(**kw: Any) -> CommandResult:
            base = dict(
                device_id=self.id,
                command=command_name,
                state_before=state_before,
                latency_ms=elapsed_ms(),
            )
            base.update(kw)
            return CommandResult(**base)

        # 1. Rate limiting
        limited = self._check_rate_limit()
        if limited is not None:
            return self._finish(make(
                success=False, confirmed=False, status=CommandStatus.RATE_LIMITED,
                error="Rate limit exceeded", error_code="RATE_LIMITED", attempts=0,
                metadata=limited,
            ))

        # 2. Circuit breaker
        breaker = self.config.circuit_breaker
        if breaker is not None and not breaker.can_execute():
            return self._finish(make(
                success=False, confirmed=False, status=CommandStatus.CIRCUIT_OPEN,
                error="Circuit breaker open; device temporarily disabled",
                error_code="CIRCUIT_OPEN", attempts=0,
                metadata={"open_until": breaker.open_until},
            ))

        # 3. Send
        try:
            raw_response, attempts = self._send_with_retries(command_payload, deadline)
        except Exception as e:
            if breaker is not None:
                breaker.record_failure()
            attempts = getattr(e, "_fuseiot_attempts", 1)
            return self._finish(make(
                success=False, confirmed=False, status=CommandStatus.FAILED,
                error=getattr(e, "message", None) or str(e),
                error_code=getattr(e, "error_code", type(e).__name__),
                attempts=attempts, retries=attempts - 1,
            ))
        network_ms = elapsed_ms()

        # 4a. No confirmation requested: acknowledged only, cached state is now unknown.
        if not use_confirm:
            self.cache.invalidate(self.id)
            if breaker is not None:
                breaker.record_success()
            return self._finish(make(
                success=True, confirmed=False, status=CommandStatus.ACKNOWLEDGED,
                raw_response=raw_response, network_latency_ms=network_ms,
                attempts=attempts, retries=attempts - 1,
            ))

        # 4b. Confirm by reading the device back.
        remaining = deadline - time.monotonic()
        confirmed, observed, confirm_ms = self._confirm_engine.confirm(
            self._fetch_state,
            expected_state,
            timeout=max(remaining, 0.0),
            matcher=lambda actual, expected: state_matches(actual, expected, self.config.tolerance),
        )

        if confirmed and observed is not None:
            self.cache.set(self.id, observed, source="command")
            self._emit_event(state_before, observed, source="command", latency_ms=elapsed_ms())
            if breaker is not None:
                breaker.record_success()
            return self._finish(make(
                success=True, confirmed=True, status=CommandStatus.CONFIRMED,
                state_after=observed, raw_response=raw_response,
                network_latency_ms=network_ms, confirmation_latency_ms=confirm_ms,
                attempts=attempts, retries=attempts - 1,
            ))

        # Device acknowledged but never reached the expected state.
        self.cache.invalidate(self.id)
        if breaker is not None:
            breaker.record_failure()
        return self._finish(make(
            success=False, confirmed=False, status=CommandStatus.TIMEOUT,
            state_after=observed, raw_response=raw_response,
            network_latency_ms=network_ms, confirmation_latency_ms=confirm_ms,
            error=(
                f"State not confirmed within {timeout_val:.2f}s "
                f"(expected {expected_state}, last observed {observed})"
            ),
            error_code="CONFIRMATION_TIMEOUT",
            attempts=attempts, retries=attempts - 1,
        ))

    async def _send_and_confirm_async(self, *args: Any, **kwargs: Any) -> CommandResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(self._send_and_confirm, *args, **kwargs)
        )

    # --------------------------------------------------------------- async sugar

    def __getattr__(self, name: str) -> Any:
        """Provide ``<command>_async`` for every public method automatically."""
        if name.endswith("_async") and not name.startswith("_"):
            sync_name = name[: -len("_async")]
            try:
                func = object.__getattribute__(self, sync_name)
            except AttributeError:
                func = None
            if callable(func):
                @functools.wraps(func)
                async def runner(*args: Any, **kwargs: Any) -> Any:
                    loop = asyncio.get_running_loop()
                    return await loop.run_in_executor(
                        None, functools.partial(func, *args, **kwargs)
                    )

                return runner
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r} protocol={getattr(self.protocol, 'name', '?')}>"


class AsyncCapability(Capability, ABC):
    """
    Base for capabilities with a native async implementation.

    Subclasses override :meth:`_send_and_confirm_async`; :meth:`Hub.batch`
    will await it directly instead of using a worker thread.
    """

    @abstractmethod
    async def _send_and_confirm_async(self, *args: Any, **kwargs: Any) -> CommandResult:
        """Native async implementation."""
