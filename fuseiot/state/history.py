"""Append-only state history stored in SQLite."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..logging_config import get_logger

logger = get_logger("state.history")


class StateHistory:
    """
    Records every state change to SQLite.

    Attach it to a hub's event bus (``Hub`` does this when ``history_enabled``)::

        history = StateHistory("history.db")
        history.attach(hub.event_bus)
        history.get_history("living_room_light", limit=10)
    """

    def __init__(self, db_path: str = "fuseiot_history.db", retention_seconds: Optional[float] = None):
        self.db_path = db_path
        self.retention_seconds = retention_seconds
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                source TEXT,
                state TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_device_ts ON history(device_id, timestamp)"
        )
        self._conn.commit()
        self._unsubscribe: Optional[Callable[[], None]] = None

    def attach(self, event_bus: Any) -> None:
        """Record all events from an :class:`~fuseiot.state.events.EventBus`."""
        self._unsubscribe = event_bus.subscribe_all(
            lambda event: self.log_state(event.device_id, event.new_state,
                                         source=event.source, timestamp=event.timestamp)
        )

    def log_state(self, device_id: str, state: Dict[str, Any], source: str = "unknown",
                  timestamp: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO history (device_id, timestamp, source, state) VALUES (?, ?, ?, ?)",
                (device_id, timestamp or time.time(), source, json.dumps(state, default=str)),
            )
            self._conn.commit()

    def get_history(
        self,
        device_id: str,
        limit: int = 100,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Newest first."""
        query = "SELECT timestamp, source, state FROM history WHERE device_id = ?"
        params: List[Any] = [device_id]
        if since is not None:
            query += " AND timestamp >= ?"
            params.append(since)
        if until is not None:
            query += " AND timestamp <= ?"
            params.append(until)
        query += " ORDER BY timestamp DESC, id DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [{"timestamp": ts, "source": src, "state": json.loads(st)} for ts, src, st in rows]

    def cleanup(self, max_age_seconds: Optional[float] = None) -> int:
        """Delete entries older than ``max_age_seconds`` (or the configured retention)."""
        age = max_age_seconds if max_age_seconds is not None else self.retention_seconds
        if age is None:
            return 0
        with self._lock:
            cursor = self._conn.execute("DELETE FROM history WHERE timestamp < ?", (time.time() - age,))
            self._conn.commit()
            return cursor.rowcount

    def close(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        with self._lock:
            self._conn.close()
