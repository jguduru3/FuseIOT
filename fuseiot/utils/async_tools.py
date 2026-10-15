import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Coroutine, Optional, TypeVar

from fuseiot.utils.retry import RetryConfig

F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")

# Global thread pool for sync->async bridging
_global_executor: Optional[ThreadPoolExecutor] = None


def _get_executor() -> ThreadPoolExecutor:
    """Get or create global thread pool."""
    global _global_executor
    if _global_executor is None:
        _global_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="fuseiot_"
        )
    return _global_executor


def run_sync(coro: Coroutine[Any, Any, T], timeout: Optional[float] = None) -> T:
    """
    Run coroutine from synchronous code.

    Creates new event loop if none exists in current thread.
    Uses existing loop if already in async context (creates task).

    Args:
        coro: Coroutine to execute
        timeout: Maximum seconds to wait

    Returns:
        Coroutine result

    Raises:
        TimeoutError: If timeout exceeded
        RuntimeError: If event loop handling fails

    Example:
        >>> async def fetch():
        ...     return await aiohttp.get(url)
        >>> result = run_sync(fetch())
    """
    try:
        # Check if we're already in an event loop
        loop = asyncio.get_running_loop()
        # Already in async context—create task but don't await
        # Caller must await this, or we schedule it
        return loop.create_task(coro)  # type: ignore

    except RuntimeError:
        # No loop running—create one
        try:
            return asyncio.run(asyncio.wait_for(coro, timeout=timeout))
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"Coroutine timed out after {timeout}s") from e


def run_async(
    func: Callable[..., T],
    *args,
    executor: Optional[ThreadPoolExecutor] = None,
    **kwargs
) -> Coroutine[Any, Any, T]:
    """
    Run synchronous function from async code.

    Args:
        func: Blocking function to execute
        *args, **kwargs: Function arguments
        executor: Optional custom executor

    Returns:
        Coroutine that resolves to function result

    Example:
        >>> def blocking_read():
        ...     return serial.read()
        >>> data = await run_async(blocking_read)
    """
    loop = asyncio.get_event_loop()
    exec_pool = executor or _get_executor()

    return loop.run_in_executor(
        exec_pool,
        functools.partial(func, *args, **kwargs)
    )


def async_retry(config: Optional[RetryConfig] = None) -> Callable[[F], F]:
    """
    Decorator: retry async function with exponential backoff.

    Mirrors sync retry decorator behavior for async functions.

    Args:
        config: Retry configuration

    Returns:
        Decorated async function

    Example:
        >>> @async_retry(RetryConfig(max_attempts=3))
        ... async def fetch_data():
        ...     return await aiohttp.get(url)
    """
    cfg = config or RetryConfig()

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
                        # Calculate backoff
                        delay = min(
                            cfg.base_delay * (cfg.exponential_base ** attempt),
                            cfg.max_delay
                        )
                        await asyncio.sleep(delay)
                    else:
                        # Final attempt failed
                        raise

            # Should never reach here
            raise last_exception or RuntimeError("Async retry failed")

        return wrapper  # type: ignore

    return decorator


class AsyncLock:
    """
    Async-aware lock with timeout.

    Wrapper around asyncio.Lock with convenience methods.
    """

    def __init__(self):
        self._lock = asyncio.Lock()

    async def acquire(self, timeout: Optional[float] = None) -> bool:
        """Acquire lock with optional timeout."""
        if timeout is None:
            await self._lock.acquire()
            return True

        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        """Release lock."""
        self._lock.release()

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._lock.release()


def create_task_safely(
    coro: Coroutine[Any, Any, T],
    name: Optional[str] = None
) -> asyncio.Task[T]:
    """
    Create asyncio task with exception handling.

    Unlike raw create_task(), this ensures exceptions are logged
    and not silently swallowed.

    Args:
        coro: Coroutine to taskify
        name: Optional task name (Python 3.8+)

    Returns:
        Task handle
    """
    loop = asyncio.get_event_loop()

    if name and hasattr(asyncio, 'create_task'):
        # Python 3.8+ supports name
        task = loop.create_task(coro, name=name)
    else:
        task = loop.create_task(coro)

    # Add exception handler
    def on_task_done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            pass  # Expected
        except Exception as e:
            # Log but don't crash
            import logging
            logging.getLogger('fuseiot').exception(f"Task failed: {e}")

    task.add_done_callback(on_task_done)
    return task


# Backwards compatibility
asyncio_run = run_sync
asyncio_wrap = run_async
