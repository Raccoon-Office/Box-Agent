"""LLM client wrapper that supports multiple providers.

This module provides a unified interface for different LLM providers
(Anthropic and OpenAI) through a single LLMClient class.
"""

import logging
from collections.abc import AsyncIterator, Iterable
from copy import copy
from time import perf_counter
from typing import Any
from uuid import uuid4

from ..client_info import ClientInfo, scoped_client_info
from ..retry import RetryConfig
from ..schema import LLMProvider, LLMResponse, Message, StreamEvent
from ..session_trace import emit_session_trace
from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .openai_client import OpenAIClient
from .think_tag_splitter import split_inline_think, unwrap_think_tags
from .token_meter import record_usage

logger = logging.getLogger(__name__)


def _trace_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _trace_request_message(message: Message) -> dict[str, Any]:
    payload = message.model_dump(exclude_none=True)
    if not message.trace_redact_content or not isinstance(message.content, list):
        return payload

    redacted_blocks: list[dict[str, Any]] = []
    for block in message.content:
        block_type = str(block.get("type") or "unknown")
        if block_type == "text":
            redacted_blocks.append(
                {
                    "type": "text",
                    "text": str(block.get("text") or ""),
                }
            )
            continue
        if block_type == "input_image":
            redacted_blocks.append(
                {
                    "type": "input_image",
                    "media_type": str(block.get("media_type") or ""),
                    "width": _trace_int(block.get("width")),
                    "height": _trace_int(block.get("height")),
                    "source_bytes": _trace_int(block.get("source_bytes")),
                    "sha256": str(block.get("sha256") or ""),
                    "redacted": True,
                }
            )
            continue
        redacted_blocks.append({"type": block_type, "redacted": True})
    payload["content"] = redacted_blocks
    return payload


def _trace_llm_request(
    *,
    llm_call_id: str,
    provider: str,
    model: str,
    messages: list[Message],
    tools: list | None,
    thinking_enabled: bool,
    session_id: str,
    turn_id: str,
    title: str,
    streaming: bool,
    call_kind: str,
) -> None:
    emit_session_trace(
        "llm.request",
        turn_id=turn_id,
        llm_call_id=llm_call_id,
        data={
            "provider": provider,
            "model": model,
            "messages": [_trace_request_message(message) for message in messages],
            "tools": tools or [],
            "thinking_enabled": thinking_enabled,
            "request_session_id": session_id,
            "title": title,
            "streaming": streaming,
            "call_kind": call_kind,
        },
    )


def _trace_timing(
    *,
    started_at: float,
    first_event_at: float | None,
    first_content_at: float | None,
) -> dict[str, int | None]:
    completed_at = perf_counter()
    first_event_ms = (
        max(0, int((first_event_at - started_at) * 1000))
        if first_event_at is not None
        else None
    )
    first_content_ms = (
        max(0, int((first_content_at - started_at) * 1000))
        if first_content_at is not None
        else None
    )
    return {
        "first_event_ms": first_event_ms,
        "first_content_ms": first_content_ms,
        "ttfb_ms": first_content_ms if first_content_ms is not None else first_event_ms,
        "duration_ms": max(0, int((completed_at - started_at) * 1000)),
    }


def _is_provider_wait_activity(event: StreamEvent) -> bool:
    return bool(
        event.type == "activity"
        and event.activity
        and event.activity.get("phase") == "provider_wait"
    )


def _is_meaningful_stream_event(event: StreamEvent) -> bool:
    if event.type in {"text", "thinking"}:
        return bool(event.delta)
    if event.type == "activity":
        return not _is_provider_wait_activity(event)
    return event.type == "finish"


def _trace_client_identity(client: "LLMClient") -> tuple[str, str]:
    provider = getattr(client, "provider", "")
    model = getattr(client, "model", "")
    underlying = getattr(client, "_client", None)
    if not provider and underlying is not None:
        provider = type(underlying).__name__
    if not model and underlying is not None:
        model = str(getattr(underlying, "model", ""))
    provider_name = provider.value if isinstance(provider, LLMProvider) else str(provider)
    return provider_name, model


class LLMClient:
    """LLM Client wrapper supporting multiple providers.

    This class provides a unified interface for different LLM providers
    (Anthropic and OpenAI). It automatically instantiates the correct
    underlying client based on the provider parameter.
    """

    def __init__(
        self,
        api_key: str,
        provider: LLMProvider = LLMProvider.ANTHROPIC,
        api_base: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-20250514",
        retry_config: RetryConfig | None = None,
        max_output_tokens: int = 64000,
        auth_token: str = "",
        auth_file: str = "",
        timeout: float = 600.0,
    ):
        """Initialize LLM client with specified provider.

        Args:
            api_key: API key for authentication
            provider: LLM provider (anthropic or openai)
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
            max_output_tokens: Per-request output token cap forwarded to the
                underlying provider as ``max_tokens``.
            auth_token: Optional in-memory product login token.
            auth_file: Optional auth.json path read before every request.
            timeout: Wall-clock cap (seconds) handed to the provider SDK.
        """
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.retry_config = retry_config or RetryConfig()
        self.max_output_tokens = max_output_tokens
        self.auth_token = auth_token
        self.auth_file = auth_file
        self.timeout = timeout

        # Normalize api_base (remove trailing slash)
        api_base = api_base.rstrip("/")
        self.api_base = api_base

        # Instantiate the appropriate client
        self._client: LLMClientBase
        if provider == LLMProvider.ANTHROPIC:
            self._client = AnthropicClient(
                api_key=api_key,
                api_base=api_base,
                model=model,
                retry_config=retry_config,
                max_output_tokens=max_output_tokens,
                auth_token=auth_token,
                auth_file=auth_file,
                timeout=timeout,
            )
        elif provider == LLMProvider.OPENAI:
            self._client = OpenAIClient(
                api_key=api_key,
                api_base=api_base,
                model=model,
                retry_config=retry_config,
                max_output_tokens=max_output_tokens,
                auth_token=auth_token,
                auth_file=auth_file,
                timeout=timeout,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        logger.info("Initialized LLM client with provider: %s, api_base: %s", provider, api_base)

    def for_model(self, model: str, *, max_output_tokens: int | None = None) -> "LLMClient":
        """Return a client with the same endpoint/auth settings for ``model``.

        ACP conversation sessions use this to bind an app-owned session to a
        hosted catalog model without mutating the process-wide default client.
        """
        normalized_model = model.strip()
        if not normalized_model:
            raise ValueError("model must not be empty")

        # The provider wrapper owns per-request mutable state (for example a
        # one-shot max-token override), while the underlying SDK transport is
        # safe to share. Shallow-copy both wrappers so sessions stay isolated
        # without creating an unbounded set of HTTP connection pools.
        client = copy(self)
        client._client = copy(self._client)
        client.model = normalized_model
        client._client.model = normalized_model
        if max_output_tokens is not None:
            if max_output_tokens <= 0:
                raise ValueError("max_output_tokens must be positive")
            client.max_output_tokens = max_output_tokens
            client._client.max_output_tokens = max_output_tokens
        if hasattr(client._client, "_ephemeral_max_output_tokens"):
            client._client._ephemeral_max_output_tokens = None
        return client

    @property
    def retry_callback(self):
        """Get retry callback."""
        return self._client.retry_callback

    @retry_callback.setter
    def retry_callback(self, value):
        """Set retry callback."""
        self._client.retry_callback = value

    async def generate(
        self,
        messages: list[Message],
        tools: list | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "Box-Agent",
        call_kind: str = "",
        retry_enabled: bool | None = None,
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            thinking_enabled: Enable provider-native extended thinking.
            session_id: Optional caller-owned session id.
            turn_id: Optional caller-owned turn id.
            title: Optional trace title.

        Returns:
            LLMResponse containing the generated content
        """
        llm_call_id = uuid4().hex
        trace_provider, trace_model = _trace_client_identity(self)
        _trace_llm_request(
            llm_call_id=llm_call_id,
            provider=trace_provider,
            model=trace_model,
            messages=messages,
            tools=tools,
            thinking_enabled=thinking_enabled,
            session_id=session_id,
            turn_id=turn_id,
            title=title,
            call_kind=call_kind,
            streaming=False,
        )
        started_at = perf_counter()
        try:
            response = await self._client.generate(
                messages,
                tools,
                thinking_enabled=thinking_enabled,
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
                retry_enabled=retry_enabled,
            )
        except BaseException as exc:
            emit_session_trace(
                "llm.error",
                turn_id=turn_id,
                llm_call_id=llm_call_id,
                data={
                    "provider": trace_provider,
                    "model": trace_model,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "timing": _trace_timing(
                        started_at=started_at,
                        first_event_at=None,
                        first_content_at=None,
                    ),
                },
            )
            raise
        record_usage(response.usage)
        if response.content and "<think>" in response.content:
            cleaned, extracted = split_inline_think(response.content)
            if extracted:
                merged_thinking = (response.thinking or "") + extracted
                response = response.model_copy(update={"content": cleaned, "thinking": merged_thinking})
        timing = _trace_timing(
            started_at=started_at,
            first_event_at=None,
            first_content_at=None,
        )
        timing["ttfb_ms"] = timing["duration_ms"]
        emit_session_trace(
            "llm.response",
            turn_id=turn_id,
            llm_call_id=llm_call_id,
            data={
                "provider": trace_provider,
                "model": trace_model,
                "content": response.content,
                "thinking": response.thinking,
                "tool_calls": response.tool_calls,
                "finish_reason": response.finish_reason,
                "usage": response.usage,
                "provider_response_id": response.provider_response_id,
                "timing": timing,
            },
        )
        return response

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "Box-Agent",
        call_kind: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from LLM.

        Yields StreamEvent chunks for thinking/text deltas as they arrive.
        The final event has type="finish" and carries tool_calls + usage.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts
            thinking_enabled: Enable provider-native extended thinking.
            session_id: Optional caller-owned session id.
            turn_id: Optional caller-owned turn id.
            title: Optional trace title.

        Yields:
            StreamEvent chunks
        """
        llm_call_id = uuid4().hex
        trace_provider, trace_model = _trace_client_identity(self)
        _trace_llm_request(
            llm_call_id=llm_call_id,
            provider=trace_provider,
            model=trace_model,
            messages=messages,
            tools=tools,
            thinking_enabled=thinking_enabled,
            session_id=session_id,
            turn_id=turn_id,
            title=title,
            call_kind=call_kind,
            streaming=True,
        )
        started_at = perf_counter()
        first_event_at: float | None = None
        first_content_at: float | None = None
        text_content = ""
        thinking_content = ""
        finish_seen = False
        try:
            upstream = self._client.generate_stream(
                messages,
                tools,
                thinking_enabled=thinking_enabled,
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
            )
            async for event in unwrap_think_tags(upstream):
                observed_at = perf_counter()
                if first_event_at is None and not _is_provider_wait_activity(event):
                    first_event_at = observed_at
                if first_content_at is None and _is_meaningful_stream_event(event):
                    first_content_at = observed_at
                if event.type == "text":
                    text_content += event.delta or ""
                elif event.type == "thinking":
                    thinking_content += event.delta or ""
                elif event.type == "finish":
                    finish_seen = True
                    record_usage(event.usage)
                    emit_session_trace(
                        "llm.response",
                        turn_id=turn_id,
                        llm_call_id=llm_call_id,
                        data={
                            "provider": trace_provider,
                            "model": trace_model,
                            "content": text_content,
                            "thinking": thinking_content or None,
                            "tool_calls": event.tool_calls,
                            "finish_reason": event.finish_reason,
                            "raw_finish_reason": event.raw_finish_reason,
                            "provider_response_id": event.provider_response_id,
                            "provider_request_id": event.provider_request_id,
                            "usage": event.usage,
                            "timing": _trace_timing(
                                started_at=started_at,
                                first_event_at=first_event_at,
                                first_content_at=first_content_at,
                            ),
                        },
                    )
                yield event
        except BaseException as exc:
            emit_session_trace(
                "llm.error",
                turn_id=turn_id,
                llm_call_id=llm_call_id,
                data={
                    "provider": trace_provider,
                    "model": trace_model,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "partial_content": text_content,
                    "partial_thinking": thinking_content or None,
                    "timing": _trace_timing(
                        started_at=started_at,
                        first_event_at=first_event_at,
                        first_content_at=first_content_at,
                    ),
                },
            )
            raise
        if not finish_seen:
            emit_session_trace(
                "llm.error",
                turn_id=turn_id,
                llm_call_id=llm_call_id,
                data={
                    "provider": trace_provider,
                    "model": trace_model,
                    "error_type": "IncompleteStream",
                    "error": "stream ended without a finish event",
                    "partial_content": text_content,
                    "partial_thinking": thinking_content or None,
                    "timing": _trace_timing(
                        started_at=started_at,
                        first_event_at=first_event_at,
                        first_content_at=first_content_at,
                    ),
                },
            )


class SessionBoundLLM:
    """Stable per-session LLM reference with inherited request correlation."""

    def __init__(self, client: LLMClient):
        self._delegate = client
        self._session_id = ""
        self._turn_id = ""
        self._title = "Box-Agent"
        self._call_kind = ""
        self._client_info: ClientInfo | None = None
        self._auto_model_candidates: tuple[dict[str, Any], ...] = ()

    def bind(self, client: LLMClient) -> None:
        self._delegate = client

    @property
    def auto_model_candidates(self) -> tuple[dict[str, Any], ...]:
        return self._auto_model_candidates

    def set_auto_model_candidates(self, candidates: Iterable[dict[str, Any]]) -> None:
        self._auto_model_candidates = tuple(dict(candidate) for candidate in candidates)

    def for_model(
        self,
        model: str,
        *,
        max_output_tokens: int | None = None,
    ) -> "SessionBoundLLM":
        """Clone this session reference for an isolated child-agent model."""

        clone_for_model = getattr(self._delegate, "for_model", None)
        if not callable(clone_for_model):
            raise ValueError("configured LLM client does not support child model binding")
        child = SessionBoundLLM(
            clone_for_model(model, max_output_tokens=max_output_tokens)
        )
        child.set_request_context(
            session_id=self._session_id,
            turn_id=self._turn_id,
            title=self._title,
            call_kind=self._call_kind,
            client_info=self._client_info,
        )
        return child

    def set_request_context(
        self,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        title: str | None = None,
        call_kind: str | None = None,
        client_info: ClientInfo | None = None,
    ) -> None:
        """Set defaults inherited by nested LLM calls in this ACP session."""

        if session_id is not None:
            self._session_id = session_id.strip()
        if turn_id is not None:
            self._turn_id = turn_id.strip()
        if title is not None:
            self._title = title.strip() or "Box-Agent"
        if call_kind is not None:
            self._call_kind = call_kind.strip()
        if client_info is not None:
            self._client_info = client_info

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def generate(
        self,
        messages: list[Message],
        tools: list | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
        retry_enabled: bool | None = None,
    ) -> LLMResponse:
        client = self._delegate
        kwargs = {
            "thinking_enabled": thinking_enabled,
            "session_id": session_id.strip() or self._session_id,
            "turn_id": turn_id.strip() or self._turn_id,
            "title": title.strip() or self._title,
        }
        effective_call_kind = call_kind.strip() or self._call_kind
        if effective_call_kind:
            kwargs["call_kind"] = effective_call_kind
        if retry_enabled is not None:
            kwargs["retry_enabled"] = retry_enabled
        with scoped_client_info(self._client_info):
            return await client.generate(messages, tools, **kwargs)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> AsyncIterator[StreamEvent]:
        client = self._delegate
        kwargs = {
            "thinking_enabled": thinking_enabled,
            "session_id": session_id.strip() or self._session_id,
            "turn_id": turn_id.strip() or self._turn_id,
            "title": title.strip() or self._title,
        }
        effective_call_kind = call_kind.strip() or self._call_kind
        if effective_call_kind:
            kwargs["call_kind"] = effective_call_kind
        stream = client.generate_stream(messages, tools, **kwargs)
        while True:
            try:
                with scoped_client_info(self._client_info):
                    event = await anext(stream)
            except StopAsyncIteration:
                return
            yield event
