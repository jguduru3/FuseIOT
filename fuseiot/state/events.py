import fnmatch
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Set

from fuseiot.logging_config import get_logger

logger = get_logger("state.events")


@dataclass(frozen=True)
class StateChangeEvent:
    """Immutable state change notification."""

    device_id: str
    timestamp: float
    old_state: Optional[Dict[str, Any]]
    new_state: Dict[str, Any]
    source: str  # "command", "poll", "event", "cache"
    latency_ms: Optional[float] = None

    @property
    def changed_keys(self) -> Set[str]:
        """Keys that changed."""
        old = set(self.old_state.keys()) if self.old_state else set()
        new = set(self.new_state.keys())
        return old.symmetric_difference(new) | {
            k for k in new
            if self.old_state and self.old_state.get(k) != self.new_state.get(k)
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "device_id": self.device_id,
            "timestamp": self.timestamp,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "source": self.source,
            "changed_keys": list(self.changed_keys),
            "latency_ms": self.latency_ms,
        }


class EventBus:
    """
    Central event bus for state changes and device notifications.

    Supports:
    - Synchronous callbacks
    - Async callbacks (scheduled in event loop)
    - Pattern-based subscriptions (wildcards)
    - Event filtering
    """

    def __init__(self):
        self._subscribers: Dict[str, Set[Callable]] = {}
        self._pattern_subscribers: List[tuple] = []  # (pattern, callback)
        self._global_subscribers: Set[Callable] = set()
        self._lock = Lock()
        self._event_count = 0
        self._dropped_count = 0

    def subscribe(
        self,
        device_id: str,
        callback: Callable[[StateChangeEvent], None]
    ) -> Callable:
        """
        Subscribe to specific device events.

        Returns unsubscribe function.
        """
        with self._lock:
            if device_id not in self._subscribers:
                self._subscribers[device_id] = set()
            self._subscribers[device_id].add(callback)

        logger.debug("event_subscribe", device_id=device_id,
                     callback=getattr(callback, "__name__", repr(callback)))

        def unsubscribe():
            with self._lock:
                if device_id in self._subscribers:
                    self._subscribers[device_id].discard(callback)

        return unsubscribe

    def subscribe_pattern(
        self,
        pattern: str,
        callback: Callable[[StateChangeEvent], None]
    ) -> Callable:
        """
        Subscribe to devices matching pattern (e.g., "living_room.*").

        Simple wildcard: * matches any characters.
        """
        with self._lock:
            self._pattern_subscribers.append((pattern, callback))

        logger.debug("event_subscribe_pattern", pattern=pattern)

        def unsubscribe():
            with self._lock:
                self._pattern_subscribers = [
                    (p, c) for p, c in self._pattern_subscribers
                    if c != callback or p != pattern
                ]

        return unsubscribe

    def subscribe_all(self, callback: Callable[[StateChangeEvent], None]) -> Callable:
        """Subscribe to all state changes."""
        with self._lock:
            self._global_subscribers.add(callback)

        def unsubscribe():
            with self._lock:
                self._global_subscribers.discard(callback)

        return unsubscribe

    def emit(self, event: StateChangeEvent) -> None:
        """Emit event to all matching subscribers."""
        self._event_count += 1

        callbacks_to_notify: Set[Callable] = set()

        with self._lock:
            # Direct subscribers
            callbacks_to_notify.update(
                self._subscribers.get(event.device_id, set())
            )

            # Pattern subscribers
            for pattern, callback in self._pattern_subscribers:
                if self._match_pattern(pattern, event.device_id):
                    callbacks_to_notify.add(callback)

            # Global subscribers
            callbacks_to_notify.update(self._global_subscribers)

        # Notify outside lock to prevent deadlocks
        for callback in callbacks_to_notify:
            try:
                callback(event)
            except Exception as e:
                logger.error("event_callback_error",
                           device_id=event.device_id,
                           error=str(e),
                           callback=getattr(callback, "__name__", repr(callback)))
                self._dropped_count += 1

    def _match_pattern(self, pattern: str, device_id: str) -> bool:
        """Simple pattern matching with * wildcard."""
        if pattern == "*":
            return True

        if "*" in pattern:
            # Convert pattern to simple wildcard match
            return fnmatch.fnmatch(device_id, pattern)

        return pattern == device_id

    def stats(self) -> Dict[str, Any]:
        """Event bus statistics."""
        with self._lock:
            return {
                "total_events": self._event_count,
                "dropped_events": self._dropped_count,
                "direct_subscriptions": len(self._subscribers),
                "pattern_subscriptions": len(self._pattern_subscribers),
                "global_subscriptions": len(self._global_subscribers),
            }
