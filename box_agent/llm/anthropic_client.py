"""Anthropic LLM client implementation."""

import inspect
import logging
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any

import anthropic

from ..retry import RetryConfig, StreamInterrupted, async_retry, is_retryable_stream_error
from ..schema import FunctionCall, LLMResponse, Message, StreamEvent, TokenUsage, ToolCall
from ..tools.argument_limits import (
    PROVIDER_STREAM_ACTIVITY_INTERVAL_SECONDS,
    TOOL_ARGUMENT_ACTIVITY_BUCKET_CHARS,
    streamed_argument_limit,
)
from .base import LLMClientBase
from .error_messages import is_retryable_llm_error
from .debug_logging import (
    log_llm_error_meta,
    log_llm_request,
    log_llm_response_meta,
    request_id_from_headers,
)

logger = logging.getLogger(__name__)

# Hard-coded budget for extended thinking. Kept intentionally low — budgets
# larger than this rarely improve answer quality for agentic workflows and
# waste tokens. Tune here if we ever expose it as config.
_THINKING_BUDGET = 8000


async def _await_if_needed(value: Any) -> Any:
    """Return awaitable SDK values and direct SDK values through one path."""
    if inspect.isawaitable(value):
        return await value
    return value


class AnthropicClient(LLMClientBase):
    """LLM client using Anthropic's protocol.

    This client uses the official Anthropic SDK and supports:
    - Extended thinking content
    - Tool calling
    - Retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-20250514",
        retry_config: RetryConfig | None = None,
        max_output_tokens: int = 64000,
        auth_token: str = "",
        auth_file: str = "",
        timeout: float = 600.0,
    ):
        """Initialize Anthropic client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
            max_output_tokens: Per-request ``max_tokens`` value sent to the API.
            auth_token: Optional in-memory product login token.
            auth_file: Optional auth.json path read before every request.
            timeout: Wall-clock cap (seconds) for each request to the API.
        """
        super().__init__(
            api_key, api_base, model, retry_config,
            auth_token=auth_token, auth_file=auth_file, timeout=timeout,
        )
        self.max_output_tokens = max_output_tokens

        # Initialize Anthropic async client
        self.client = anthropic.AsyncAnthropic(
            base_url=api_base,
            api_key=api_key,
            timeout=timeout,
        )

    async def _make_api_request(
        self,
        system_message: str | None,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> anthropic.types.Message:
        """Execute API request (core method that can be retried).

        Args:
            system_message: Optional system message
            api_messages: List of messages in Anthropic format
            tools: Optional list of tools
            thinking_enabled: When True, add ``thinking`` config with an
                8000-token budget (Anthropic native extended thinking).

        Returns:
            Anthropic Message response

        Raises:
            Exception: API call failed
        """
        params: dict[str, Any] = {
            "max_tokens": self.max_output_tokens,
            "messages": api_messages,
        }
        if self.model:
            params["model"] = self.model

        if system_message:
            params["system"] = system_message

        if tools:
            params["tools"] = self._convert_tools(tools)

        if thinking_enabled:
            params["thinking"] = {"type": "enabled", "budget_tokens": _THINKING_BUDGET}

        auth_headers = await self._auth_headers(
            self._request_headers(session_id, turn_id, title, call_kind)
        )
        if auth_headers:
            params["extra_headers"] = auth_headers

        log_llm_request(provider="anthropic", mode="completion", api_base=self.api_base, params=params)

        try:
            raw_response = await _await_if_needed(
                self.client.messages.with_raw_response.create(**params)
            )
            log_llm_response_meta(
                provider="anthropic",
                mode="completion",
                request_id=getattr(raw_response, "request_id", None),
                headers=getattr(raw_response, "headers", None),
            )
            response = await _await_if_needed(raw_response.parse())
        except AttributeError:
            # Test doubles and older SDK-compatible clients may not expose
            # ``with_raw_response``. Keep the request log and fall back to the
            # existing behavior, but request-id metadata will be unavailable.
            response = await _await_if_needed(self.client.messages.create(**params))
        except Exception as exc:
            log_llm_error_meta(provider="anthropic", mode="completion", exc=exc)
            raise

        return response

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """Convert tools to Anthropic format.

        Anthropic tool format:
        {
            "name": "tool_name",
            "description": "Tool description",
            "input_schema": {
                "type": "object",
                "properties": {...},
                "required": [...]
            }
        }

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in Anthropic dict format
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                result.append(tool)
            elif hasattr(tool, "to_schema"):
                # Tool object with to_schema method
                result.append(tool.to_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")
        return result

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to Anthropic format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
        """
        system_message = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_message = msg.content
                continue

            # For user and assistant messages
            if msg.role in ["user", "assistant"]:
                # Handle assistant messages with thinking or tool calls
                if msg.role == "assistant" and (msg.thinking or msg.tool_calls):
                    # Build content blocks for assistant with thinking and/or tool calls
                    content_blocks = []

                    # Add thinking block if present
                    if msg.thinking:
                        content_blocks.append({"type": "thinking", "thinking": msg.thinking})

                    # Add text content if present
                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})

                    # Add tool use blocks
                    if msg.tool_calls:
                        for tool_call in msg.tool_calls:
                            content_blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": tool_call.id,
                                    "name": tool_call.function.name,
                                    "input": tool_call.function.arguments,
                                }
                            )

                    api_messages.append({"role": "assistant", "content": content_blocks})
                else:
                    api_messages.append(
                        {
                            "role": msg.role,
                            "content": self._convert_input_content(msg.content),
                        }
                    )

            # For tool result messages
            elif msg.role == "tool":
                # Anthropic uses user role with tool_result content blocks
                api_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_call_id,
                                "content": msg.content,
                            }
                        ],
                    }
                )

        # Anthropic requires strict user/assistant alternation. A tool result
        # is represented as a user message, and a request-only image overlay
        # may immediately follow it, so coalesce adjacent user turns.
        merged_messages: list[dict[str, Any]] = []
        for api_message in api_messages:
            if (
                merged_messages
                and api_message["role"] == "user"
                and merged_messages[-1]["role"] == "user"
            ):
                previous_content = merged_messages[-1]["content"]
                current_content = api_message["content"]
                if isinstance(previous_content, str):
                    previous_content = [{"type": "text", "text": previous_content}]
                if isinstance(current_content, str):
                    current_content = [{"type": "text", "text": current_content}]
                merged_messages[-1]["content"] = [
                    *previous_content,
                    *current_content,
                ]
            else:
                merged_messages.append(api_message)

        return system_message, merged_messages

    @staticmethod
    def _convert_input_content(content: Any) -> Any:
        """Translate canonical multimodal blocks to Anthropic wire blocks."""
        if not isinstance(content, list):
            return content
        converted: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "input_image":
                media_type = block.get("media_type")
                data = block.get("data")
                if media_type not in {"image/png", "image/jpeg"} or not isinstance(
                    data, str
                ) or not data:
                    raise ValueError("invalid canonical input_image block")
                converted.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    }
                )
            else:
                converted.append(block)
        return converted

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request for Anthropic API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing request parameters
        """
        system_message, api_messages = self._convert_messages(messages)

        return {
            "system_message": system_message,
            "api_messages": api_messages,
            "tools": tools,
        }

    def _parse_response(self, response: anthropic.types.Message) -> LLMResponse:
        """Parse Anthropic response into LLMResponse.

        Args:
            response: Anthropic Message response

        Returns:
            LLMResponse object
        """
        # Extract text content, thinking, and tool calls
        text_content = ""
        thinking_content = ""
        tool_calls = []

        for block in (response.content or []):
            if block.type == "text":
                text_content += block.text
            elif block.type == "thinking":
                thinking_content += block.thinking
            elif block.type == "tool_use":
                # Parse Anthropic tool_use block
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        type="function",
                        function=FunctionCall(
                            name=block.name,
                            arguments=block.input,
                        ),
                    )
                )

        # Extract token usage from response
        # Anthropic usage includes: input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens
        usage = None
        if hasattr(response, "usage") and response.usage:
            input_tokens = response.usage.input_tokens or 0
            output_tokens = response.usage.output_tokens or 0
            cache_read_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_creation_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            total_input_tokens = input_tokens + cache_read_tokens + cache_creation_tokens
            usage = TokenUsage(
                prompt_tokens=total_input_tokens,
                completion_tokens=output_tokens,
                total_tokens=total_input_tokens + output_tokens,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_creation_input_tokens=cache_creation_tokens,
                cache_read_input_tokens=cache_read_tokens,
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=response.stop_reason or "stop",
            usage=usage,
            provider_response_id=str(response.id) if getattr(response, "id", None) else None,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> LLMResponse:
        """Generate response from Anthropic LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            thinking_enabled: Enable Anthropic extended thinking.
            session_id: Optional caller-owned session id.
            turn_id: Optional caller-owned turn id.
            title: Optional trace title.

        Returns:
            LLMResponse containing the generated content
        """
        # Prepare request
        request_params = self._prepare_request(messages, tools)

        # Make API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(
                config=self.retry_config,
                on_retry=self.retry_callback,
                should_retry=is_retryable_llm_error,
            )
            api_call = retry_decorator(self._make_api_request)
            response = await api_call(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
                session_id=session_id,
                turn_id=turn_id,
                title=title,
                call_kind=call_kind,
            )

        # Parse and return response
        return self._parse_response(response)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
        turn_id: str = "",
        title: str = "",
        call_kind: str = "",
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from Anthropic LLM.

        Yields thinking/text deltas as they arrive. Tool calls are accumulated
        and emitted in the final "finish" event along with token usage.
        """
        request_params = self._prepare_request(messages, tools)

        params: dict[str, Any] = {
            "max_tokens": self.max_output_tokens,
            "messages": request_params["api_messages"],
        }
        if self.model:
            params["model"] = self.model
        if request_params["system_message"]:
            params["system"] = request_params["system_message"]
        if request_params["tools"]:
            params["tools"] = self._convert_tools(request_params["tools"])
        if thinking_enabled:
            params["thinking"] = {"type": "enabled", "budget_tokens": _THINKING_BUDGET}

        auth_headers = await self._auth_headers(
            self._request_headers(session_id, turn_id, title, call_kind)
        )
        if auth_headers:
            params["extra_headers"] = auth_headers

        log_llm_request(provider="anthropic", mode="stream", api_base=self.api_base, params=params)

        # Accumulators for the finish event (reset per retry attempt below)
        text_content = ""
        thinking_content = ""
        tool_calls: list[ToolCall] = []
        finish_reason = "stop"

        # Token tracking
        input_tokens = 0
        output_tokens = 0
        cache_read_tokens = 0
        cache_create_tokens = 0

        # Track current tool_use block being streamed
        current_tool_id: str | None = None
        current_tool_name: str | None = None
        current_tool_json = ""
        current_activity_bucket = -1
        oversized_info: list[dict[str, Any]] = []
        provider_request_id: str | None = None
        provider_response_id: str | None = None

        import asyncio as _asyncio

        max_attempts = max(1, self.retry_config.max_retries + 1) if self.retry_config.enabled else 1
        any_user_yield = False

        for attempt in range(max_attempts):
            # Reset per-attempt accumulators
            text_content = ""
            thinking_content = ""
            tool_calls = []
            finish_reason = "stop"
            input_tokens = 0
            output_tokens = 0
            cache_read_tokens = 0
            cache_create_tokens = 0
            current_tool_id = None
            current_tool_name = None
            current_tool_json = ""
            current_activity_bucket = -1
            oversized_info = []
            provider_response_id = None
            last_provider_activity_at: float | None = None

            try:
                stream_context = self.client.messages.stream(**params)
            except Exception as exc:
                log_llm_error_meta(provider="anthropic", mode="stream", exc=exc)
                # Detect third-party API event order compatibility issues
                if isinstance(exc, RuntimeError) and "Unexpected event order" in str(exc):
                    raise RuntimeError(
                        f"API 返回的事件顺序不符合 Anthropic 协议规范: {exc}\n"
                        f"这通常表示第三方 API 的兼容性问题。请检查:\n"
                        f"1. API 端点是否正确实现了 Anthropic 流式协议\n"
                        f"2. 是否应该使用 OpenAI 兼容模式（provider: openai）而不是 Anthropic 模式"
                    ) from exc
                raise

            try:
                async with stream_context as stream:
                    response_headers = getattr(getattr(stream, "response", None), "headers", None)
                    provider_request_id = request_id_from_headers(response_headers)
                    log_llm_response_meta(
                        provider="anthropic",
                        mode="stream",
                        request_id=provider_request_id,
                        headers=response_headers,
                    )
                    async for event in stream:
                        now = monotonic()
                        if (
                            last_provider_activity_at is None
                            or now - last_provider_activity_at
                            >= PROVIDER_STREAM_ACTIVITY_INTERVAL_SECONDS
                        ):
                            last_provider_activity_at = now
                            yield StreamEvent(
                                type="activity",
                                activity={
                                    "protocol": "agent_activity_v1",
                                    "phase": "provider_stream",
                                },
                            )
                        # ── Message start (input token usage) ──
                        if event.type == "message_start":
                            msg = event.message
                            if provider_response_id is None and getattr(msg, "id", None):
                                provider_response_id = str(msg.id)
                            if hasattr(msg, "usage") and msg.usage:
                                input_tokens = msg.usage.input_tokens or 0
                                cache_read_tokens = getattr(msg.usage, "cache_read_input_tokens", 0) or 0
                                cache_create_tokens = getattr(msg.usage, "cache_creation_input_tokens", 0) or 0

                        # ── Content block start ──
                        elif event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                current_tool_id = block.id
                                current_tool_name = block.name
                                current_tool_json = ""
                                current_activity_bucket = -1

                        # ── Content block delta ──
                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "thinking_delta":
                                thinking_content += delta.thinking
                                any_user_yield = True
                                yield StreamEvent(type="thinking", delta=delta.thinking)
                            elif delta.type == "text_delta":
                                text_content += delta.text
                                any_user_yield = True
                                yield StreamEvent(type="text", delta=delta.text)
                            elif delta.type == "input_json_delta":
                                current_tool_json += delta.partial_json
                                arguments_len = len(current_tool_json)
                                activity_bucket = (
                                    arguments_len // TOOL_ARGUMENT_ACTIVITY_BUCKET_CHARS
                                )
                                if activity_bucket > current_activity_bucket:
                                    current_activity_bucket = activity_bucket
                                    yield StreamEvent(
                                        type="activity",
                                        activity={
                                            "protocol": "agent_activity_v1",
                                            "phase": "tool_arguments",
                                            "tool_name": current_tool_name or "",
                                            "argument_chars": arguments_len,
                                        },
                                    )
                                limit = streamed_argument_limit(current_tool_name)
                                if limit is not None and arguments_len > limit:
                                    oversized_info.append(
                                        {
                                            "name": current_tool_name or "",
                                            "arguments_len": arguments_len,
                                            "limit": limit,
                                        }
                                    )
                                    finish_reason = "tool_argument_limit"
                                    break

                        # ── Content block stop ──
                        elif event.type == "content_block_stop":
                            if current_tool_id is not None:
                                import json

                                try:
                                    arguments = json.loads(current_tool_json) if current_tool_json else {}
                                except json.JSONDecodeError:
                                    arguments = {}
                                tool_calls.append(
                                    ToolCall(
                                        id=current_tool_id,
                                        type="function",
                                        function=FunctionCall(
                                            name=current_tool_name or "",
                                            arguments=arguments,
                                        ),
                                    )
                                )
                                current_tool_id = None
                                current_tool_name = None
                                current_tool_json = ""

                        # ── Message delta (stop reason + output tokens) ──
                        elif event.type == "message_delta":
                            if hasattr(event, "delta") and hasattr(event.delta, "stop_reason"):
                                finish_reason = event.delta.stop_reason or "stop"
                            if hasattr(event, "usage") and event.usage:
                                output_tokens = getattr(event.usage, "output_tokens", 0) or 0
            except Exception as exc:
                log_llm_error_meta(provider="anthropic", mode="stream", exc=exc)
                # Detect third-party API event order compatibility issues
                if isinstance(exc, RuntimeError) and "Unexpected event order" in str(exc):
                    raise RuntimeError(
                        f"API 返回的事件顺序不符合 Anthropic 协议规范: {exc}\n"
                        f"这通常表示第三方 API 的兼容性问题。请检查:\n"
                        f"1. API 端点是否正确实现了 Anthropic 流式协议\n"
                        f"2. 是否应该使用 OpenAI 兼容模式（provider: openai）而不是 Anthropic 模式"
                    ) from exc
                if is_retryable_stream_error(exc):
                    if any_user_yield:
                        logger.warning(
                            "anthropic stream interrupted after partial yield "
                            "(text=%d chars, thinking=%d chars): %s",
                            len(text_content), len(thinking_content), exc,
                        )
                        raise StreamInterrupted(
                            last_exception=exc,
                            partial_text=text_content,
                            partial_thinking=thinking_content,
                            provider_request_id=provider_request_id,
                        ) from exc
                    if attempt < max_attempts - 1:
                        delay = self.retry_config.calculate_delay(attempt)
                        logger.warning(
                            "anthropic generate_stream attempt %d/%d dropped before any yield: %s; "
                            "retrying from scratch in %.2fs",
                            attempt + 1, max_attempts, exc, delay,
                        )
                        if self.retry_callback:
                            try:
                                self.retry_callback(exc, attempt + 1)
                            except Exception:  # pragma: no cover - callback safety
                                logger.exception("retry_callback raised")
                        await _asyncio.sleep(delay)
                        continue
                raise
            else:
                break

        # Build final usage
        total_input = input_tokens + cache_read_tokens + cache_create_tokens
        usage = TokenUsage(
            prompt_tokens=total_input,
            completion_tokens=output_tokens,
            total_tokens=total_input + output_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_create_tokens,
            cache_read_input_tokens=cache_read_tokens,
        )

        # Always-on diagnostics, symmetric with the OpenAI client, so the
        # upstream stop_reason is visible in box-agent-stderr.log for
        # truncation triage regardless of provider.
        logger.info(
            "anthropic stream finished: finish_reason=%r completion_tokens=%s "
            "text_len=%d request_id=%s",
            finish_reason,
            usage.completion_tokens,
            len(text_content),
            provider_request_id,
        )

        yield StreamEvent(
            type="finish",
            finish_reason=("tool_argument_limit" if oversized_info else finish_reason),
            usage=usage,
            tool_calls=None if oversized_info else (tool_calls if tool_calls else None),
            provider_response_id=provider_response_id,
            provider_request_id=provider_request_id,
            oversized_tool_calls=oversized_info or None,
            raw_finish_reason=None if oversized_info else finish_reason,
        )
