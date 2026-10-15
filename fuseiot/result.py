"""
Result types for device commands and batch operations.

Every capability command returns a :class:`CommandResult`, so callers always
know whether a command was sent, acknowledged and verified on the device.
"""
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Union


class CommandStatus(Enum):
    """
    Lifecycle states of a device command.

    PENDING -> SENT -> [ACKNOWLEDGED | FAILED]
    With confirmation: ACKNOWLEDGED -> CONFIRMED | TIMEOUT
    Rejected before sending: RATE_LIMITED | CIRCUIT_OPEN
    """
    PENDING = auto()
    SENT = auto()
    ACKNOWLEDGED = auto()
    CONFIRMED = auto()
    FAILED = auto()
    TIMEOUT = auto()
    CANCELLED = auto()
    RATE_LIMITED = auto()
    CIRCUIT_OPEN = auto()
    RETRYING = auto()


@dataclass
class CommandResult:
    """
    Complete result of a device command execution.

    Attributes:
        success: True if the command achieved its intended effect. With
            confirmation enabled this means the device state was verified.
        confirmed: True if the device state was read back and matched.
        status: Detailed lifecycle status.
        device_id: Device the command targeted.
        command: Command name (e.g. "on", "move_to").
        state_before: Last known state before the command (if known).
        state_after: Verified state after the command (if confirmed).
        latency_ms: Total time including confirmation.
        error: Human-readable error description if failed.

    Example:
        >>> result = relay.on(confirm=True)
        >>> if result.confirmed:
        ...     print(f"Light on! Took {result.latency_ms:.0f}ms")
    """

    success: bool
    confirmed: bool
    status: CommandStatus
    device_id: Optional[str] = None
    command: Optional[str] = None
    state_before: Optional[Dict[str, Any]] = None
    state_after: Optional[Dict[str, Any]] = None
    latency_ms: float = 0.0
    network_latency_ms: Optional[float] = None
    confirmation_latency_ms: Optional[float] = None
    raw_response: Optional[Union[Dict, str, bytes]] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    attempts: int = 1
    retries: int = 0
    timestamp: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.timestamp is None:
            self.timestamp = time.time()

    def __bool__(self) -> bool:
        """Truthiness based on success: ``if result: ...``."""
        return self.success

    @property
    def failed(self) -> bool:
        """Convenience: check if command failed."""
        return not self.success

    @property
    def state_changed(self) -> Optional[bool]:
        """True/False if before and after states differ, None if unknown."""
        if self.state_before is None or self.state_after is None:
            return None
        return self.state_before != self.state_after

    def raise_for_error(self) -> "CommandResult":
        """
        Raise an exception if the command did not succeed.

        Returns the result itself so it can be chained:
        ``plug.on().raise_for_error()``.
        """
        from fuseiot.exceptions import (
            CircuitOpenError,
            CommandError,
            ConfirmationError,
            RateLimitError,
        )

        if self.status == CommandStatus.RATE_LIMITED:
            raise RateLimitError(
                limit=self.metadata.get("limit", 0),
                window_seconds=self.metadata.get("window_seconds", 0.0),
                retry_after=self.metadata.get("retry_after", 0.0),
                device_id=self.device_id,
            )

        if self.status == CommandStatus.CIRCUIT_OPEN:
            raise CircuitOpenError(
                device_id=self.device_id or "unknown",
                open_until=self.metadata.get("open_until", time.time()),
            )

        if self.status == CommandStatus.TIMEOUT:
            raise ConfirmationError(
                message=self.error or "Command not confirmed within timeout",
                device_id=self.device_id or "unknown",
                command=self.command or "unknown",
                result=self,
            )

        if not self.success:
            raise CommandError(
                message=self.error or "Command failed",
                device_id=self.device_id or "unknown",
                command=self.command or "unknown",
                result=self,
            )
        return self

    def to_dict(self) -> Dict[str, Any]:
        """Convert to a JSON-serialisable dictionary."""
        raw = self.raw_response
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return {
            "success": self.success,
            "confirmed": self.confirmed,
            "status": self.status.name,
            "device_id": self.device_id,
            "command": self.command,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "latency_ms": round(self.latency_ms, 3),
            "network_latency_ms": self.network_latency_ms,
            "confirmation_latency_ms": self.confirmation_latency_ms,
            "raw_response": raw,
            "error": self.error,
            "error_code": self.error_code,
            "attempts": self.attempts,
            "retries": self.retries,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    def __repr__(self) -> str:
        device = self.device_id or "unknown"
        status = self.status.name
        if self.success and self.confirmed:
            return f"<CommandResult {device} SUCCESS {status} {self.latency_ms:.1f}ms>"
        if self.success:
            return f"<CommandResult {device} UNCONFIRMED {status}>"
        return f"<CommandResult {device} FAILED {status}: {self.error}>"


@dataclass
class BatchResult:
    """Results from :meth:`Hub.batch`."""

    results: List[CommandResult]
    total: int
    successful: int
    failed: int
    confirmed: int
    total_latency_ms: float
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.completed_at is None:
            self.completed_at = time.time()

    @property
    def success_rate(self) -> float:
        """Percentage (0-100) of successful commands."""
        if self.total == 0:
            return 0.0
        return (self.successful / self.total) * 100

    @property
    def all_succeeded(self) -> bool:
        return self.failed == 0 and len(self.results) == self.total

    @property
    def any_failed(self) -> bool:
        return self.failed > 0

    def get_failures(self) -> List[CommandResult]:
        return [r for r in self.results if not r.success]

    def get_by_device(self, device_id: str) -> Optional[CommandResult]:
        for result in self.results:
            if result.device_id == device_id:
                return result
        return None

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "successful": self.successful,
            "failed": self.failed,
            "confirmed": self.confirmed,
            "success_rate": self.success_rate,
            "total_latency_ms": self.total_latency_ms,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "results": [r.to_dict() for r in self.results],
        }

    def __repr__(self) -> str:
        return (
            f"<BatchResult {self.successful}/{self.total} succeeded "
            f"({self.success_rate:.1f}%)>"
        )
