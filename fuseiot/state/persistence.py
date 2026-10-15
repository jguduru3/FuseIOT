import json
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from fuseiot.exceptions import ConfigurationError
from fuseiot.logging_config import get_logger

logger = get_logger("state.persistence")


class PersistenceBackend(ABC):
    """Abstract base for state persistence."""

    @abstractmethod
    def store(self, key: str, data: Dict[str, Any]) -> None:
        """Store state data."""
        pass

    @abstractmethod
    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve state data."""
        pass

    @abstractmethod
    def get_all(self, prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get all entries, optionally filtered by prefix."""
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """Delete entry. Returns True if existed."""
        pass

    @abstractmethod
    def cleanup(self, max_age_seconds: float) -> int:
        """Remove entries older than max_age. Returns count removed."""
        pass


@dataclass
class SQLiteBackend(PersistenceBackend):
    """SQLite-based state persistence."""

    db_path: str = "fuseiot_state.db"
    _lock: Lock = None

    def __post_init__(self):
        if self._lock is None:
            self._lock = Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS device_states (
                    device_id TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    source TEXT,
                    created_at REAL,
                    updated_at REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp
                ON device_states(timestamp)
            """)
            conn.commit()

        logger.info("sqlite_backend_initialized", path=self.db_path)

    def store(self, key: str, data: Dict[str, Any]) -> None:
        """Store or update state."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                    INSERT INTO device_states (device_id, value, timestamp, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        value=excluded.value,
                        timestamp=excluded.timestamp,
                        source=excluded.source,
                        updated_at=excluded.updated_at
                """, (
                key,
                json.dumps(data["value"], default=str),
                data["timestamp"],
                data.get("source", "unknown"),
                time.time(),
                time.time(),
            ))
            conn.commit()

        logger.debug("sqlite_store", key=key, timestamp=data["timestamp"])

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve state."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT value, timestamp, source FROM device_states WHERE device_id = ?",
                (key,)
            ).fetchone()

            if row:
                return {
                    "value": json.loads(row[0]),
                    "timestamp": row[1],
                    "source": row[2]
                }
            return None

    def get_all(self, prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get all entries."""
        with sqlite3.connect(self.db_path) as conn:
            if prefix:
                rows = conn.execute(
                    "SELECT device_id, value, timestamp, source FROM device_states "
                    "WHERE device_id LIKE ? ESCAPE '!'",
                    (prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%",)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT device_id, value, timestamp, source FROM device_states"
                ).fetchall()

            return {
                row[0]: {
                    "value": json.loads(row[1]),
                    "timestamp": row[2],
                    "source": row[3]
                }
                for row in rows
            }

    def delete(self, key: str) -> bool:
        """Delete entry."""
        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM device_states WHERE device_id = ?",
                (key,)
            )
            conn.commit()
            return cursor.rowcount > 0

    def cleanup(self, max_age_seconds: float) -> int:
        """Remove old entries."""
        cutoff = time.time() - max_age_seconds

        with self._lock, sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM device_states WHERE timestamp < ?",
                (cutoff,)
            )
            conn.commit()
            removed = cursor.rowcount

        logger.info("sqlite_cleanup", removed=removed, cutoff=cutoff)
        return removed

    def get_history(
        self,
        key: str,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Get state history (if using history table)."""
        # This would require a separate history table
        # Simplified implementation returns current state only
        current = self.get(key)
        return [current] if current else []


class RedisBackend(PersistenceBackend):
    """Redis-based state persistence."""

    def __init__(self, redis_url: str = "redis://localhost:6379/0", ttl: int = 86400):
        try:
            import redis
        except ImportError as e:
            raise ConfigurationError("redis package required: pip install 'fuseiot[redis]'") from e

        self._client = redis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl
        self._prefix = "fuseiot:state:"

        logger.info("redis_backend_initialized", url=redis_url)

    def store(self, key: str, data: Dict[str, Any]) -> None:
        """Store with TTL."""
        full_key = f"{self._prefix}{key}"
        self._client.setex(
            full_key,
            self._ttl,
            json.dumps(data)
        )

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Retrieve state."""
        full_key = f"{self._prefix}{key}"
        data = self._client.get(full_key)
        if data:
            return json.loads(data)
        return None

    def get_all(self, prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Get all entries."""
        pattern = f"{self._prefix}{prefix or ''}*"
        keys = self._client.keys(pattern)

        result = {}
        for key in keys:
            data = self._client.get(key)
            if data:
                short_key = key[len(self._prefix):]
                result[short_key] = json.loads(data)

        return result

    def delete(self, key: str) -> bool:
        """Delete entry."""
        full_key = f"{self._prefix}{key}"
        return self._client.delete(full_key) > 0

    def cleanup(self, max_age_seconds: float) -> int:
        """Redis handles TTL automatically."""
        return 0  # Redis auto-expires

    def publish_event(self, channel: str, message: Dict[str, Any]) -> None:
        """Publish state change event."""
        self._client.publish(f"fuseiot:events:{channel}", json.dumps(message))
