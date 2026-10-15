import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Dict, Optional, Set

from fuseiot.logging_config import get_logger

logger = get_logger("state.cache")


@dataclass
class CacheEntry:
    """Single cached value with metadata."""

    value: Any
    timestamp: float
    ttl_seconds: float
    source: str  # "poll", "command", "event", "persistence"
    access_count: int = field(default=0)
    last_accessed: float = field(default_factory=time.monotonic)

    @property
    def is_valid(self) -> bool:
        """Check if entry hasn't expired."""
        return (time.monotonic() - self.timestamp) < self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        """How old is this entry."""
        return time.monotonic() - self.timestamp

    def touch(self) -> None:
        """Update access metadata."""
        self.access_count += 1
        self.last_accessed = time.monotonic()


class StateCache:
    """
    Thread-safe TTL cache for device state with LRU eviction.

    Supports:
    - Per-key TTL
    - LRU eviction when size limit reached
    - Persistence backend integration
    - Event callbacks on state changes
    """

    def __init__(
        self,
        default_ttl: float = 5.0,
        max_size: int = 10000,
        persistence: Optional[Any] = None
    ):
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._persistence = persistence
        self._data: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = RLock()  # Reentrant for nested operations
        self._callbacks: Set[Callable[[str, Any, Any], None]] = set()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

        logger.info("cache_initialized",
                   default_ttl=default_ttl,
                   max_size=max_size,
                   persistence_enabled=persistence is not None)

    def get(
        self,
        key: str,
        max_age: Optional[float] = None,
        default: Any = None
    ) -> Any:
        """
        Retrieve value if fresh enough.

        Args:
            key: Device identifier
            max_age: Maximum acceptable age (None = use TTL)
            default: Value to return if stale/missing

        Returns:
            Cached value or default
        """
        with self._lock:
            entry = self._data.get(key)

            if entry is None:
                self._misses += 1

                # Try persistence fallback
                if self._persistence:
                    try:
                        persisted = self._persistence.get(key)
                    except Exception as e:
                        logger.warning("cache_persistence_read_failed", key=key, error=str(e))
                        persisted = None
                    if persisted is not None:
                        # Persisted timestamps are wall-clock; convert age to monotonic.
                        age = max(0.0, time.time() - float(persisted.get("timestamp", 0)))
                        limit = max_age if max_age is not None else self._default_ttl
                        if age <= limit:
                            logger.debug("cache_persistence_hit", key=key)
                            self._data[key] = CacheEntry(
                                value=persisted["value"],
                                timestamp=time.monotonic() - age,
                                ttl_seconds=self._default_ttl,
                                source="persistence",
                            )
                            self._hits += 1
                            self._misses -= 1
                            return persisted["value"]

                return default

            age_limit = max_age if max_age is not None else entry.ttl_seconds

            if entry.age_seconds > age_limit:
                self._misses += 1
                # Keep the stale entry as "last known state" (see peek()); it is
                # never returned by get() and is evicted by LRU when needed.
                return default

            # Valid hit - update LRU order
            entry.touch()
            self._data.move_to_end(key)
            self._hits += 1

            return entry.value

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[float] = None,
        source: str = "poll",
        persist: bool = True
    ) -> None:
        """
        Store value with freshness tracking.

        Args:
            key: Device identifier
            value: State data
            ttl: Time-to-live (None = use default)
            source: How this state was obtained
            persist: Whether to persist to backend
        """
        entry = CacheEntry(
            value=value,
            timestamp=time.monotonic(),
            ttl_seconds=ttl if ttl is not None else self._default_ttl,
            source=source
        )

        with self._lock:
            # Check for update
            old_value = None
            if key in self._data:
                old_value = self._data[key].value

            # Evict if at capacity and new key
            if len(self._data) >= self._max_size and key not in self._data:
                self._evict_lru()

            self._data[key] = entry
            self._data.move_to_end(key)

            # Notify callbacks if value changed
            if old_value != value:
                self._notify_change(key, old_value, value)

        # Persist asynchronously if configured
        if persist and self._persistence:
            try:
                self._persistence.store(key, {
                    "value": value,
                    "timestamp": time.time(),
                    "source": source
                })
            except Exception as e:
                logger.warning("cache_persist_failed", key=key, error=str(e))

    def invalidate(self, key: str) -> bool:
        """Remove key from cache. Returns True if existed."""
        with self._lock:
            existed = key in self._data
            if existed:
                del self._data[key]
                logger.debug("cache_invalidated", key=key)
            return existed

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate all keys matching pattern (simple substring match)."""
        with self._lock:
            matching = [k for k in self._data if pattern in k]
            for key in matching:
                del self._data[key]
            logger.info("cache_pattern_invalidated",
                       pattern=pattern,
                       count=len(matching))
            return len(matching)

    def invalidate_all(self) -> None:
        """Clear entire cache."""
        with self._lock:
            count = len(self._data)
            self._data.clear()
            logger.info("cache_cleared", entries_removed=count)

    def _evict_lru(self) -> None:
        """Evict least recently used entry."""
        if self._data:
            oldest_key = next(iter(self._data))
            del self._data[oldest_key]
            self._evictions += 1
            logger.debug("cache_eviction", key=oldest_key)

    def on_change(self, callback: Callable[[str, Any, Any], None]) -> Callable:
        """
        Register callback for state changes.

        Returns unregister function.
        """
        self._callbacks.add(callback)

        def unregister():
            self._callbacks.discard(callback)

        return unregister

    def _notify_change(self, key: str, old: Any, new: Any) -> None:
        """Notify all registered callbacks."""
        for callback in list(self._callbacks):
            try:
                callback(key, old, new)
            except Exception as e:
                logger.error("cache_callback_error", key=key, error=str(e))

    def stats(self) -> Dict[str, Any]:
        """Cache statistics."""
        with self._lock:
            total = len(self._data)
            valid = sum(1 for e in self._data.values() if e.is_valid)
            total_accesses = self._hits + self._misses
            hit_rate = (self._hits / total_accesses * 100) if total_accesses > 0 else 0

            return {
                "total_entries": total,
                "valid_entries": valid,
                "stale_entries": total - valid,
                "max_size": self._max_size,
                "utilization_percent": (total / self._max_size * 100) if self._max_size > 0 else 0,
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "hit_rate_percent": round(hit_rate, 2),
                "persistence_enabled": self._persistence is not None,
            }

    def keys(self) -> Set[str]:
        """Get all cached keys."""
        with self._lock:
            return set(self._data.keys())

    def peek(self, key: str) -> Optional[CacheEntry]:
        """Get entry metadata without affecting LRU order."""
        with self._lock:
            return self._data.get(key)
