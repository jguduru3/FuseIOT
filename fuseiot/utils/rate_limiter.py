import time
from dataclasses import dataclass
from threading import Lock

from fuseiot.logging_config import get_logger

logger = get_logger("utils.rate_limiter")


@dataclass
class TokenBucket:
    """
    Token bucket rate limiter.

    Allows bursts up to bucket size, then rate-limited.
    """

    rate: float  # tokens per second
    capacity: int  # maximum bucket size

    def __post_init__(self):
        self._tokens = float(self.capacity)
        self._last_update = time.monotonic()
        self._lock = Lock()

    def allow(self, tokens: int = 1) -> bool:
        """Check if request can proceed."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_update

            # Add tokens based on elapsed time
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.rate
            )
            self._last_update = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True

            return False

    def time_until_next(self) -> float:
        """Seconds until next token available."""
        with self._lock:
            if self._tokens >= 1:
                return 0.0

            needed = 1 - self._tokens
            return needed / self.rate


@dataclass
class RateLimiter:
    """Request rate limiter with sliding window."""

    requests: int  # max requests
    window: float  # in seconds

    def __post_init__(self):
        self._timestamps: list = []
        self._lock = Lock()

    def allow(self) -> bool:
        """Check if request can proceed."""
        with self._lock:
            now = time.monotonic()

            # Remove old timestamps outside window
            cutoff = now - self.window
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            if len(self._timestamps) < self.requests:
                self._timestamps.append(now)
                return True

            return False

    def time_until_next(self) -> float:
        """Seconds until next request allowed."""
        with self._lock:
            if len(self._timestamps) < self.requests:
                return 0.0

            oldest = min(self._timestamps)
            return max(0, (oldest + self.window) - time.monotonic())

    @property
    def current_count(self) -> int:
        """Current request count in window."""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            return len(self._timestamps)
