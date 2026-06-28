"""Elegant retry mechanism module

Provides decorators and utility functions to support retry logic for async functions.

Features:
- Supports exponential backoff strategy
- Configurable retry count and intervals
- Supports specifying retryable exception types
- Detailed logging
- Fully decoupled, non-invasive to business code
"""

import asyncio
import functools
import logging
from typing import Any, Callable, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RetryConfig:
    """Retry configuration class"""

    def __init__(
        self,
        enabled: bool = True,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        retryable_exceptions: tuple[Type[Exception], ...] = (Exception,),
    ):
        """
        Args:
            enabled: Whether to enable retry mechanism
            max_retries: Maximum number of retries
            initial_delay: Initial delay time (seconds)
            max_delay: Maximum delay time (seconds)
            exponential_base: Exponential backoff base
            retryable_exceptions: Tuple of retryable exception types
        """
        self.enabled = enabled
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.retryable_exceptions = retryable_exceptions

    def calculate_delay(self, attempt: int) -> float:
        """Calculate delay time (exponential backoff)

        Args:
            attempt: Current attempt number (starting from 0)

        Returns:
            Delay time (seconds)
        """
        delay = self.initial_delay * (self.exponential_base**attempt)
        return min(delay, self.max_delay)


class RetryExhaustedError(Exception):
    """Retry exhausted exception"""

    def __init__(self, last_exception: Exception, attempts: int):
        self.last_exception = last_exception
        self.attempts = attempts
        super().__init__(f"Retry failed after {attempts} attempts. Last error: {str(last_exception)}")


class StreamInterrupted(Exception):
    """Raised when a streaming LLM call drops mid-stream after partial content was already yielded.

    Carries the partial text/thinking accumulated before the drop so the agent loop can preserve
    it as a partial assistant message and resume on user request instead of restarting from scratch.
    """

    def __init__(
        self,
        last_exception: Exception,
        partial_text: str = "",
        partial_thinking: str = "",
        provider_request_id: str | None = None,
    ):
        self.last_exception = last_exception
        self.partial_text = partial_text
        self.partial_thinking = partial_thinking
        self.provider_request_id = provider_request_id
        super().__init__(
            f"LLM stream interrupted mid-response (partial_text={len(partial_text)} chars, "
            f"partial_thinking={len(partial_thinking)} chars): {last_exception!s}"
        )


def is_retryable_stream_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a transient network/stream error worth retrying."""
    import http.client as _http_client

    # Match by class name so we don't hard-import httpx/openai (they're optional).
    cls_names = {c.__name__ for c in type(exc).__mro__}
    transient_names = {
        "RemoteProtocolError",
        "ReadTimeout",
        "ConnectTimeout",
        "ConnectError",
        "WriteError",
        "ReadError",
        "PoolTimeout",
        "IncompleteRead",
        "ProtocolError",
        "ChunkedEncodingError",
        "APIConnectionError",
        "APITimeoutError",
    }
    if cls_names & transient_names:
        return True
    if isinstance(exc, (ConnectionError, TimeoutError, _http_client.IncompleteRead)):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "peer closed connection",
            "incomplete chunked read",
            "incomplete read",
            "connection reset",
            "server disconnected",
        )
    )


def async_retry(
    config: RetryConfig | None = None,
    on_retry: Callable[[Exception, int], None] | None = None,
    should_retry: Callable[[BaseException], bool] | None = None,
) -> Callable:
    """Async function retry decorator

    Args:
        config: Retry configuration object, uses default config if None
        on_retry: Callback function on retry, receives exception and current attempt number
        should_retry: Optional predicate applied to a caught exception. When
            provided, an exception is retried only if both it matches
            ``config.retryable_exceptions`` AND ``should_retry(exc)`` returns
            True; otherwise it is re-raised immediately (fail-fast). Use this to
            avoid retrying non-recoverable errors such as 4xx client errors.
            When omitted, behavior is unchanged (retry on
            ``config.retryable_exceptions``).

    Returns:
        Decorator function

    Example:
        ```python
        @async_retry(RetryConfig(max_retries=3, initial_delay=1.0))
        async def call_api():
            # API call code
            pass
        ```
    """
    if config is None:
        config = RetryConfig()

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None

            for attempt in range(config.max_retries + 1):
                try:
                    # Try to execute function
                    return await func(*args, **kwargs)

                except config.retryable_exceptions as e:
                    last_exception = e

                    # Fail-fast on exceptions the predicate rejects (e.g. 4xx
                    # client errors) — re-raise the original error untouched
                    # rather than wrapping/retrying it.
                    if should_retry is not None and not should_retry(e):
                        raise

                    # If this is the last attempt, don't retry
                    if attempt >= config.max_retries:
                        logger.error(f"Function {func.__name__} retry failed, reached maximum retry count {config.max_retries}")
                        raise RetryExhaustedError(e, attempt + 1)

                    # Calculate delay time
                    delay = config.calculate_delay(attempt)

                    # Log
                    logger.warning(
                        f"Function {func.__name__} call {attempt + 1} failed: {str(e)}, "
                        f"retrying attempt {attempt + 2} after {delay:.2f} seconds"
                    )

                    # Call callback function
                    if on_retry:
                        on_retry(e, attempt + 1)

                    # Wait before retry
                    await asyncio.sleep(delay)

            # Should not reach here in theory
            if last_exception:
                raise last_exception
            raise Exception("Unknown error")

        return wrapper

    return decorator
