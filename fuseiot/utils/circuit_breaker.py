import functools
import time
from dataclasses import dataclass
from enum import Enum, auto
from threading import Lock
from typing import Any, Callable, Optional, TypeVar

from fuseiot.exceptions import CircuitOpenError
from fuseiot.logging_config import get_logger

logger = get_logger("utils.circuit_breaker")

F = TypeVar("F", bound=Callable[..., Any])


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = auto()      # Normal operation
    OPEN = auto()        # Failing, rejecting requests
    HALF_OPEN = auto()   # Testing if recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for fault tolerance.

    After threshold failures, opens circuit to prevent cascade.
    Periodically tries half-open to test recovery.
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3

    def __post_init__(self):
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._half_open_calls = 0
        self._lock = Lock()

    @property
    def state(self) -> CircuitState:
        """Current circuit state."""
        with self._lock:
            # Check if we should transition from OPEN to HALF_OPEN
            if self._state == CircuitState.OPEN and self._last_failure_time:
                elapsed = time.time() - self._last_failure_time
                if elapsed >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_calls = 0
                    self._success_count = 0
                    logger.info("circuit_half_open",
                              failure_count=self._failure_count)

            return self._state

    @property
    def open_until(self) -> float:
        """Wall-clock time when an open circuit will allow a trial call."""
        return (self._last_failure_time or time.time()) + self.recovery_timeout

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def reset(self) -> None:
        """Force the circuit closed."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0

    def can_execute(self) -> bool:
        """Check if request can proceed."""
        state = self.state  # Trigger state transition check

        with self._lock:
            if state == CircuitState.OPEN:
                return False

            if state == CircuitState.HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    return False
                self._half_open_calls += 1

            return True

    def record_success(self) -> None:
        """Record successful call."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.half_open_max_calls:
                    logger.info("circuit_closed",
                              previous_failures=self._failure_count)
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            else:
                self._failure_count = max(0, self._failure_count - 1)

    def record_failure(self) -> None:
        """Record failed call."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                logger.warning("circuit_reopened",
                               failure_count=self._failure_count)
                self._state = CircuitState.OPEN
                self._success_count = 0
            elif self._failure_count >= self.failure_threshold:
                if self._state != CircuitState.OPEN:
                    logger.error("circuit_opened",
                               failure_count=self._failure_count,
                               threshold=self.failure_threshold)
                    self._state = CircuitState.OPEN

    def __call__(self, func: F) -> F:
        """Decorator to wrap function with circuit breaker."""
        def wrapper(*args, **kwargs):
            if not self.can_execute():
                raise CircuitOpenError(
                    device_id=getattr(args[0], "id", "unknown") if args else "unknown",
                    open_until=self.open_until,
                )

            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except Exception:
                self.record_failure()
                raise

        functools.update_wrapper(wrapper, func)
        return wrapper  # type: ignore

    def get_stats(self) -> dict:
        """Get circuit breaker statistics."""
        state = self.state
        return {
            "state": state.name,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time,
            "recovery_in": max(0.0, self.open_until - time.time())
            if state == CircuitState.OPEN else 0.0,
        }
