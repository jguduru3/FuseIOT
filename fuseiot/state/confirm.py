"""
State confirmation: poll a device until its state matches an expectation.

Simple, deterministic polling with a bounded number of attempts and a hard
deadline. No guessing: a command is only confirmed if a fresh read of the
device returns the expected values.
"""
import math
import time
from typing import Any, Callable, Dict, Optional, Tuple

ConfirmOutcome = Tuple[bool, Optional[Dict[str, Any]], float]
StateFetcher = Callable[[], Dict[str, Any]]
StateMatcher = Callable[[Dict[str, Any], Dict[str, Any]], bool]


def values_match(actual: Any, expected: Any, tolerance: float = 1e-6) -> bool:
    """Compare two state values, allowing a tolerance for numbers."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual == expected
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=tolerance)
    return actual == expected


def state_matches(
    actual: Dict[str, Any],
    expected: Dict[str, Any],
    tolerance: float = 1e-6,
) -> bool:
    """True if every expected key is present in ``actual`` with a matching value."""
    if not isinstance(actual, dict):
        return False
    return all(
        key in actual and values_match(actual[key], value, tolerance)
        for key, value in expected.items()
    )


class ConfirmationEngine:
    """
    Polls device state until it matches the expectation or the timeout expires.

    The first poll happens immediately; subsequent polls wait ``poll_interval``.
    """

    def __init__(self, poll_interval: float = 0.1, max_attempts: int = 50):
        self.poll_interval = poll_interval
        self.max_attempts = max_attempts

    def confirm(
        self,
        fetch_state: StateFetcher,
        expected_state: Dict[str, Any],
        timeout: float = 2.0,
        matcher: Optional[StateMatcher] = None,
    ) -> ConfirmOutcome:
        """
        Poll ``fetch_state`` until it matches ``expected_state``.

        Returns:
            (confirmed, last_observed_state_or_None, elapsed_ms)
        """
        match = matcher or state_matches
        start = time.monotonic()
        deadline = start + max(0.0, timeout)
        attempts = 0
        last_state: Optional[Dict[str, Any]] = None

        while attempts < self.max_attempts:
            attempts += 1
            try:
                last_state = fetch_state()
                if match(last_state, expected_state):
                    return True, last_state, (time.monotonic() - start) * 1000
            except Exception:  # a failed poll is not fatal; retry until the deadline
                last_state = None

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(self.poll_interval, remaining))

        return False, last_state, (time.monotonic() - start) * 1000


_default_engine = ConfirmationEngine()


def confirm_state(
    fetch_state: StateFetcher,
    expected: Dict[str, Any],
    timeout: float = 2.0,
    matcher: Optional[StateMatcher] = None,
) -> ConfirmOutcome:
    """Convenience wrapper around the default :class:`ConfirmationEngine`."""
    return _default_engine.confirm(fetch_state, expected, timeout, matcher)
