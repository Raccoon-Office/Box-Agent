"""Regression tests for ``async_retry(should_retry=...)`` fail-fast predicate.

Covers B3: the non-streaming ``generate()`` path used to retry *all* exceptions
(including non-recoverable 4xx client errors) because ``RetryConfig`` defaults
``retryable_exceptions`` to ``(Exception,)``. The ``should_retry`` predicate lets
the non-streaming path reuse ``is_retryable_stream_error`` so only transient
network errors are retried; 4xx errors fail fast and are re-raised untouched.
"""

from __future__ import annotations

from box_agent.retry import (
    RetryConfig,
    RetryExhaustedError,
    async_retry,
    is_retryable_stream_error,
)


class _FourOhOne(Exception):
    """Stand-in for a non-recoverable 4xx client error."""


def _config() -> RetryConfig:
    # Zero delay so the test is instant.
    return RetryConfig(max_retries=3, initial_delay=0.0, max_delay=0.0)


async def test_should_retry_false_fails_fast_without_retrying():
    calls = 0

    @async_retry(config=_config(), should_retry=is_retryable_stream_error)
    async def boom():
        nonlocal calls
        calls += 1
        raise _FourOhOne("400 invalid request")

    raised: Exception | None = None
    try:
        await boom()
    except Exception as exc:  # noqa: BLE001 - asserting exact type below
        raised = exc

    # Original error is re-raised untouched (NOT wrapped in RetryExhaustedError),
    # and the function ran exactly once (no retries).
    assert isinstance(raised, _FourOhOne)
    assert not isinstance(raised, RetryExhaustedError)
    assert calls == 1


async def test_should_retry_true_still_retries_transient_errors():
    calls = 0

    @async_retry(config=_config(), should_retry=is_retryable_stream_error)
    async def flaky():
        nonlocal calls
        calls += 1
        # ConnectionError is matched by is_retryable_stream_error.
        raise ConnectionError("connection reset")

    raised: Exception | None = None
    try:
        await flaky()
    except Exception as exc:  # noqa: BLE001
        raised = exc

    # Transient error: retried up to the cap, then surfaced as RetryExhaustedError.
    assert isinstance(raised, RetryExhaustedError)
    assert calls == 4  # initial + 3 retries


async def test_should_retry_recovers_when_transient_error_clears():
    calls = 0

    @async_retry(config=_config(), should_retry=is_retryable_stream_error)
    async def recover():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("server disconnected")
        return "ok"

    assert await recover() == "ok"
    assert calls == 3


async def test_default_behavior_unchanged_without_predicate():
    """Without should_retry, all exceptions are still retried (back-compat)."""
    calls = 0

    @async_retry(config=_config())
    async def boom():
        nonlocal calls
        calls += 1
        raise _FourOhOne("400 invalid request")

    raised: Exception | None = None
    try:
        await boom()
    except Exception as exc:  # noqa: BLE001
        raised = exc

    assert isinstance(raised, RetryExhaustedError)
    assert calls == 4
