import asyncio
import functools
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple, Type, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class RetryConfig:
    """Configuration for synchronous retry behavior."""

    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 5.0
    exponential_base: float = 2.0
    retryable_exceptions: Tuple[Type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
    )


def retry(config: Optional[RetryConfig] = None) -> Callable[[F], F]:
    """
    Synchronous decorator: retry function with exponential backoff.
    """
    cfg = config or RetryConfig()

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(cfg.max_attempts):
                try:
                    return func(*args, **kwargs)
                except cfg.retryable_exceptions as e:
                    last_exception = e
                    if attempt < cfg.max_attempts - 1:
                        delay = min(
                            cfg.base_delay * (cfg.exponential_base ** attempt),
                            cfg.max_delay
                        )
                        time.sleep(delay)
                    else:
                        raise
            raise last_exception or RuntimeError("Retry failed")

        return wrapper  # type: ignore

    return decorator


# ========== Async version ==========

@dataclass
class AsyncRetryConfig(RetryConfig):
    """Configuration for asynchronous retry behavior."""
    pass


def async_retry(config: Optional[AsyncRetryConfig] = None) -> Callable[[F], F]:
    """
    Asynchronous decorator: retry an async function with exponential backoff.
    """
    cfg = config or AsyncRetryConfig()

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(cfg.max_attempts):
                try:
                    return await func(*args, **kwargs)
                except cfg.retryable_exceptions as e:
                    last_exception = e
                    if attempt < cfg.max_attempts - 1:
                        delay = min(
                            cfg.base_delay * (cfg.exponential_base ** attempt),
                            cfg.max_delay
                        )
                        await asyncio.sleep(delay)
                    else:
                        raise
            raise last_exception or RuntimeError("Async retry failed")

        return wrapper  # type: ignore

    return decorator
