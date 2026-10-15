"""Circuit breaker, rate limiting, retry helpers and async utilities, alone and in the pipeline."""
import asyncio
import time

import pytest

from fuseiot import Switchable, VirtualDevice
from fuseiot.exceptions import CircuitOpenError, RateLimitError
from fuseiot.result import CommandStatus
from fuseiot.utils.async_tools import AsyncLock, create_task_safely, run_async, run_sync
from fuseiot.utils.circuit_breaker import CircuitBreaker, CircuitState
from fuseiot.utils.rate_limiter import RateLimiter, TokenBucket
from fuseiot.utils.retry import AsyncRetryConfig, RetryConfig, async_retry, retry
from tests.helpers import fast_config


class TestCircuitBreaker:
    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        assert cb.can_execute()
        cb.record_failure()
        assert cb.state is CircuitState.CLOSED
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        assert not cb.can_execute()
        stats = cb.get_stats()
        assert stats["state"] == "OPEN" and stats["failure_count"] == 2
        assert 59 < stats["recovery_in"] <= 60
        assert cb.open_until > time.time()

    def test_success_decays_failures(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.state is CircuitState.CLOSED and cb.failure_count == 1

    def test_half_open_then_close(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=2)
        cb.record_failure()
        assert cb.state is CircuitState.OPEN
        time.sleep(0.07)
        assert cb.state is CircuitState.HALF_OPEN
        assert cb.can_execute() and cb.can_execute()
        assert not cb.can_execute(), "half-open allows only a limited number of trial calls"
        cb.record_success()
        assert cb.state is CircuitState.HALF_OPEN
        cb.record_success()
        assert cb.state is CircuitState.CLOSED and cb.failure_count == 0

    def test_failure_in_half_open_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
        cb.record_failure()
        time.sleep(0.07)
        assert cb.can_execute()
        cb.record_failure()
        assert cb._state is CircuitState.OPEN

    def test_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure()
        cb.reset()
        assert cb.state is CircuitState.CLOSED and cb.can_execute()

    def test_decorator(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
        calls = []

        @cb
        def flaky(x):
            """docstring kept"""
            calls.append(x)
            raise OSError("nope")

        with pytest.raises(OSError):
            flaky(1)
        with pytest.raises(CircuitOpenError):
            flaky(2)
        assert calls == [1]
        assert flaky.__doc__ == "docstring kept"


class TestRateLimiters:
    def test_sliding_window(self):
        rl = RateLimiter(requests=2, window=0.1)
        assert rl.allow() and rl.allow()
        assert not rl.allow()
        assert rl.current_count == 2
        assert 0 < rl.time_until_next() <= 0.1
        time.sleep(0.12)
        assert rl.time_until_next() == 0 or rl.current_count == 0
        assert rl.allow()

    def test_token_bucket(self):
        tb = TokenBucket(rate=20.0, capacity=2)
        assert tb.allow() and tb.allow()
        assert not tb.allow()
        assert tb.time_until_next() > 0
        time.sleep(0.07)
        assert tb.allow()
        assert not tb.allow(tokens=5)


class TestPipelineResilience:
    def test_circuit_opens_and_blocks_sends(self, hub):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60)
        dev = VirtualDevice(failure_rate=1.0)
        lamp = hub.add_device(dev, Switchable, device_id="lamp", config=fast_config(circuit_breaker=breaker))
        assert lamp.on().status is CommandStatus.FAILED
        assert lamp.on().status is CommandStatus.FAILED
        sent = len(dev.requests)
        r = lamp.on()
        assert r.status is CommandStatus.CIRCUIT_OPEN
        assert r.attempts == 0 and r.error_code == "CIRCUIT_OPEN"
        assert len(dev.requests) == sent, "no request reaches an open circuit"
        assert r.metadata["open_until"] == pytest.approx(breaker.open_until)
        with pytest.raises(CircuitOpenError):
            r.raise_for_error()

    def test_confirmation_timeout_counts_as_failure(self, hub):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60)
        dev = VirtualDevice()
        dev.on_request("/on", lambda p: {})
        lamp = hub.add_device(dev, Switchable, device_id="lamp",
                              config=fast_config(circuit_breaker=breaker, timeout=0.05))
        assert lamp.on().status is CommandStatus.TIMEOUT
        assert breaker.state is CircuitState.OPEN

    def test_circuit_recovers(self, hub):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=1)
        dev = VirtualDevice(failure_rate=1.0)
        lamp = hub.add_device(dev, Switchable, device_id="lamp", config=fast_config(circuit_breaker=breaker))
        lamp.on()
        assert lamp.on().status is CommandStatus.CIRCUIT_OPEN
        dev.sim.failure_rate = 0.0
        time.sleep(0.07)
        assert lamp.on().confirmed
        assert breaker.state is CircuitState.CLOSED

    def test_rate_limited(self, hub):
        dev = VirtualDevice()
        lamp = hub.add_device(dev, Switchable, device_id="lamp",
                              config=fast_config(rate_limiter=RateLimiter(requests=2, window=60)))
        assert lamp.on().confirmed and lamp.off().confirmed
        sent = len(dev.requests)
        r = lamp.on()
        assert r.status is CommandStatus.RATE_LIMITED and r.attempts == 0
        assert r.metadata["limit"] == 2 and r.metadata["window_seconds"] == 60
        assert r.metadata["retry_after"] > 0
        assert len(dev.requests) == sent
        with pytest.raises(RateLimitError) as exc:
            r.raise_for_error()
        assert exc.value.retry_after > 0

    def test_token_bucket_metadata(self, hub):
        lamp = hub.add_device(VirtualDevice(), Switchable, device_id="lamp",
                              config=fast_config(rate_limiter=TokenBucket(rate=0.5, capacity=1)))
        assert lamp.on().confirmed
        r = lamp.off()
        assert r.status is CommandStatus.RATE_LIMITED
        assert r.metadata["limit"] == 1 and r.metadata["window_seconds"] == 2.0


class TestRetryDecorators:
    def test_retry_until_success(self):
        attempts = []

        @retry(RetryConfig(max_attempts=3, base_delay=0.001))
        def op():
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionError("flap")
            return "ok"

        assert op() == "ok" and len(attempts) == 3

    def test_retry_exhausted_and_non_retryable(self):
        calls = []

        @retry(RetryConfig(max_attempts=2, base_delay=0.001))
        def always_fails():
            calls.append(1)
            raise OSError("down")

        with pytest.raises(OSError):
            always_fails()
        assert len(calls) == 2

        @retry(RetryConfig(max_attempts=5, base_delay=0.001))
        def bad_value():
            calls.append(2)
            raise ValueError("not transient")

        with pytest.raises(ValueError):
            bad_value()
        assert calls.count(2) == 1

    async def test_async_retry(self):
        attempts = []

        @async_retry(AsyncRetryConfig(max_attempts=3, base_delay=0.001))
        async def op():
            attempts.append(1)
            if len(attempts) < 2:
                raise TimeoutError("slow")
            return 42

        assert await op() == 42 and len(attempts) == 2


class TestAsyncTools:
    def test_run_sync_without_loop(self):
        async def coro():
            return 5

        assert run_sync(coro()) == 5

    def test_run_sync_timeout(self):
        with pytest.raises(TimeoutError):
            run_sync(asyncio.sleep(1), timeout=0.01)

    async def test_run_async_and_lock(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            assert await run_async(lambda a, b=0: a + b, 2, b=3, executor=pool) == 5
        lock = AsyncLock()
        assert await lock.acquire(timeout=0.1)
        assert await lock.acquire(timeout=0.01) is False
        lock.release()
        async with lock:
            pass

    async def test_create_task_safely_swallows_errors(self):
        async def boom():
            raise RuntimeError("logged, not raised")

        task = create_task_safely(boom(), name="boom")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert task.done() and isinstance(task.exception(), RuntimeError)
