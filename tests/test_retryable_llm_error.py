"""Tests for is_retryable_llm_error — the non-streaming LLM retry predicate.

The non-streaming generate() path must retry transient server/network faults
(429 rate limit, 5xx server error, request timeout, connection drop) but fail
fast on deterministic client errors (400/401/403/404, content filter, quota,
context length, unknown model, wrong endpoint).
"""

from __future__ import annotations

import pytest

from box_agent.llm.error_messages import is_retryable_llm_error


class _StatusError(Exception):
    """Provider-style exception exposing an HTTP status code."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _ResponseStatusError(Exception):
    """Provider-style exception exposing status via a .response object."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.response = type("R", (), {"status_code": status_code})()


# ── Retryable: transient server / network conditions ────────────


@pytest.mark.parametrize("status", [429, 408, 500, 502, 503, 504])
def test_transient_http_status_is_retryable(status):
    assert is_retryable_llm_error(_StatusError("boom", status)) is True


def test_status_via_response_object_is_read():
    assert is_retryable_llm_error(_ResponseStatusError("rate limited", 429)) is True
    assert is_retryable_llm_error(_ResponseStatusError("bad request", 400)) is False


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached for requests (429)",
        "Internal server error (500)",
        "503 Service Unavailable",
        "Request timed out",
        "Connection reset by peer",
        "model is currently overloaded",
    ],
)
def test_transient_messages_are_retryable_without_status(message):
    assert is_retryable_llm_error(Exception(message)) is True


def test_network_transport_errors_are_retryable():
    assert is_retryable_llm_error(ConnectionError("connection reset")) is True


# ── Fail-fast: deterministic client errors ──────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_http_status_is_not_retryable(status):
    assert is_retryable_llm_error(_StatusError("nope", status)) is False


@pytest.mark.parametrize(
    "message",
    [
        "invalid api key",
        "permission_denied for this model",
        "insufficient_quota: please check billing",
        "context_length_exceeded: maximum context",
        "model_not_found: no such model",
        "content_filter triggered",
        "404 page not found",
    ],
)
def test_deterministic_messages_are_not_retryable(message):
    assert is_retryable_llm_error(Exception(message)) is False


# ── Unknown errors default to retry (transient assumption) ───────


def test_unknown_error_defaults_to_retryable():
    assert is_retryable_llm_error(Exception("something weird happened")) is True


# ── HTTP status wins over message-token matching (priority order) ─


def test_4xx_status_fails_fast_even_with_transient_message():
    """A structured 400 must fail fast even when its body contains a
    transient-looking substring like 'connection reset' — status code is the
    higher-priority signal."""
    exc = _StatusError("Error 400: upstream connection reset", 400)
    assert is_retryable_llm_error(exc) is False


def test_5xx_status_retries_even_with_deterministic_message():
    """Symmetric case: a 500 retries even if its message mentions an otherwise
    fail-fast token like 'invalid api key'."""
    exc = _StatusError("500: downstream said invalid api key", 500)
    assert is_retryable_llm_error(exc) is True

