from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from fuseiot.result import CommandResult


class FuseIoTError(Exception):
    """
    Base for all FuseIoT errors.

    All exceptions carry:
    - message: Human-readable description
    - details: Structured context for programmatic handling
    - error_code: Machine-readable error identifier
    """

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None
    ):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.error_code = error_code or self.__class__.__name__

    def __str__(self) -> str:
        if self.details:
            return f"[{self.error_code}] {self.message} (details: {self.details})"
        return f"[{self.error_code}] {self.message}"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, error_code={self.error_code!r})"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for logging/API responses."""
        return {
            "error_type": self.__class__.__name__,
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details,
        }


# Device-level errors

class DeviceError(FuseIoTError):
    """Base for device-related errors."""
    pass


class ProtocolError(DeviceError):
    """Transport layer failure."""

    def __init__(
        self,
        message: str,
        protocol: str,
        endpoint: str,
        original_error: Optional[Exception] = None,
        **kwargs
    ):
        super().__init__(
            message,
            details={
                "protocol": protocol,
                "endpoint": endpoint,
                "original_error": str(original_error) if original_error else None,
                **kwargs
            },
            error_code=f"PROTOCOL_{protocol.upper()}_ERROR"
        )
        self.protocol = protocol
        self.endpoint = endpoint
        self.original_error = original_error


class ConnectionError(DeviceError):
    """Raised when a connection cannot be established or is lost."""
    pass


class CommandError(DeviceError):
    """Command execution failed."""

    def __init__(
        self,
        message: str,
        device_id: str,
        command: str,
        result: Optional["CommandResult"] = None,
        **kwargs
    ):
        super().__init__(
            message,
            details={
                "device_id": device_id,
                "command": command,
                "result": result.to_dict() if result is not None else None,
                **kwargs
            },
            error_code="COMMAND_FAILED"
        )
        self.device_id = device_id
        self.command = command
        self.result = result


class ConfirmationError(CommandError):
    """Command sent but state change not verified."""
    pass


class StateError(DeviceError):
    """State inconsistent or unreadable."""
    pass


class CacheError(StateError):
    """State cache operation failed."""
    pass


# Configuration errors

class ConfigurationError(FuseIoTError):
    """Setup or configuration mistake."""
    pass


class ValidationError(ConfigurationError):
    """Parameter validation failed."""

    def __init__(self, message: str, field: Optional[str] = None, **kwargs):
        super().__init__(
            message,
            details={"field": field, **kwargs} if field else kwargs,
            error_code="VALIDATION_ERROR"
        )
        self.field = field


# Runtime errors

class TimeoutError(FuseIoTError):
    """Operation exceeded time limit."""

    def __init__(
        self,
        message: str,
        operation: str,
        timeout_seconds: float,
        **kwargs
    ):
        super().__init__(
            message,
            details={
                "operation": operation,
                "timeout_seconds": timeout_seconds,
                **kwargs
            },
            error_code="TIMEOUT"
        )
        self.operation = operation
        self.timeout_seconds = timeout_seconds


class ConcurrencyError(FuseIoTError):
    """Threading/async conflict."""
    pass


class ResourceExhaustedError(FuseIoTError):
    """Out of memory, file handles, connections, etc."""
    pass


# NEW: Resilience errors

class CircuitOpenError(FuseIoTError):
    """Circuit breaker is open - device temporarily disabled."""

    def __init__(self, device_id: str, open_until: float, **kwargs):
        super().__init__(
            f"Circuit breaker open for {device_id}",
            details={"device_id": device_id, "open_until": open_until, **kwargs},
            error_code="CIRCUIT_OPEN"
        )
        self.device_id = device_id
        self.open_until = open_until


class RateLimitError(FuseIoTError):
    """Rate limit exceeded."""

    def __init__(self, limit: int, window_seconds: float, retry_after: float, **kwargs):
        super().__init__(
            f"Rate limit exceeded: {limit} requests per {window_seconds}s",
            details={
                "limit": limit,
                "window_seconds": window_seconds,
                "retry_after": retry_after,
                **kwargs
            },
            error_code="RATE_LIMITED"
        )
        self.limit = limit
        self.window_seconds = window_seconds
        self.retry_after = retry_after


# NEW: Security errors

class AuthenticationError(FuseIoTError):
    """Failed to authenticate with device or service."""
    pass


class AuthorizationError(FuseIoTError):
    """Authenticated but not authorized for operation."""
    pass


# NEW: Discovery errors

class DiscoveryError(FuseIoTError):
    """Device discovery failed."""
    pass


# NEW: Cloud errors

class CloudError(FuseIoTError):
    """Cloud connector operation failed."""
    pass
