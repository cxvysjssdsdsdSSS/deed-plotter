"""Retry helpers for transient Cursor agent failures.

Retries rate limits, timeouts, and overloaded/5xx-style failures only.
Auth, permission, and bad-request errors fail immediately.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BASE_DELAY_SECONDS = 2.0
DEFAULT_MAX_DELAY_SECONDS = 30.0

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_RETRYABLE_EXC_NAMES = frozenset(
    {"RateLimitError", "NetworkError", "APITimeoutError", "InternalServerError"}
)
_NON_RETRYABLE_FRAGMENTS = (
    "unauthorized",
    "forbidden",
    "api key",
    "invalid api",
    "authentication",
    "permission",
    "not found",
    "invalid model",
    "bad request",
)
_RETRYABLE_FRAGMENTS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "429",
    "timeout",
    "timed out",
    "temporar",
    "unavailable",
    "overloaded",
    "502",
    "503",
    "504",
    "500",
    "connection reset",
    "connection aborted",
    "network",
    # Windows Cursor bridge / launch flakes (common CursorAgentError text).
    "bridge",
    "launch",
    "failed to start",
)


def _blob(*parts: object) -> str:
    return " ".join(str(p) for p in parts if p is not None).lower()


def _has_transient_signal(text: str) -> bool:
    return any(frag in text for frag in _RETRYABLE_FRAGMENTS)


def is_retryable_error(exc: BaseException) -> bool:
    """True when *exc* looks like a transient transport / capacity failure."""
    if getattr(exc, "is_retryable", False):
        return True
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status in _RETRYABLE_STATUS_CODES:
        return True
    if type(exc).__name__ in _RETRYABLE_EXC_NAMES:
        return True
    text = _blob(exc, getattr(exc, "message", None), getattr(exc, "result", None))
    if any(frag in text for frag in _NON_RETRYABLE_FRAGMENTS):
        return False
    return _has_transient_signal(text)


def is_retryable_run_status(status: str, detail: str = "") -> bool:
    """True when an agent run should be retried (transient only).

    ``error`` / ``failed`` / ``expired`` alone are not enough — the detail (or
    status string) must also look transient. Empty finished responses are
    handled separately by the caller via ``is_retryable`` on the exception.
    """
    text = _blob(status, detail)
    if any(frag in text for frag in _NON_RETRYABLE_FRAGMENTS):
        return False
    if status in ("finished", "cancelled", "canceled"):
        return False
    # Unknown/blank status with no detail: treat as flaky bridge noise.
    if not status and not (detail or "").strip():
        return True
    return _has_transient_signal(text)


def retry_delay_seconds(
    attempt: int,
    *,
    exc: BaseException | None = None,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    rng: Callable[[], float] = random.random,
) -> float:
    """Backoff before retry *attempt* (1-based), with equal jitter."""
    backoff = min(base_delay * (2 ** (attempt - 1)), max_delay)
    retry_after = None
    if exc is not None:
        raw = getattr(exc, "retry_after", None)
        if raw is not None:
            try:
                retry_after = float(raw)
            except (TypeError, ValueError):
                retry_after = None
    if retry_after is not None and retry_after >= 0:
        backoff = min(max(retry_after, backoff), max_delay)
    half = backoff / 2.0
    return half + (rng() * half)


def call_with_retry(
    func: Callable[[], T],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> T:
    """Call *func*, retrying transient failures with jittered exponential backoff."""
    for attempt in range(1, max_attempts + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 — re-raised when not retryable
            if attempt >= max_attempts or not is_retryable_error(exc):
                raise
            delay = retry_delay_seconds(
                attempt, exc=exc, base_delay=base_delay, max_delay=max_delay, rng=rng
            )
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep(delay)
    raise AssertionError("call_with_retry exhausted its loop without returning")
