"""OpenAI LLM client implementation."""

import inspect
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from ..retry import RetryConfig, StreamInterrupted, async_retry, is_retryable_stream_error
from ..schema import FunctionCall, LLMResponse, Message, StreamEvent, TokenUsage, ToolCall
from .base import LLMClientBase
from .error_messages import is_retryable_llm_error
from .debug_logging import (
    log_llm_error_meta,
    log_llm_request,
    log_llm_response_meta,
    request_id_from_headers,
)

logger = logging.getLogger(__name__)

# Fallback completion-token budget when no explicit value is supplied.
# Many OpenAI-protocol relay/proxy gateways default to 4096, which silently
# truncates long tool-call argument streams (we observed `finish_reason="length"`
# cutting JSON mid-string and triggering empty-arguments retry loops). Pin a
# generous default; users can override via ``LLMConfig.max_output_tokens``.
_DEFAULT_MAX_TOKENS = 64000


async def _await_if_needed(value: Any) -> Any:
    """Return awaitable SDK values and direct SDK values through one path."""
    if inspect.isawaitable(value):
        return await value
    return value


class OpenAIClient(LLMClientBase):
    """LLM client using OpenAI's protocol.

    This client uses the official OpenAI SDK and supports:
    - Reasoning content (via reasoning_split=True)
    - Tool calling
    - Retry logic
    """

    def __init__(
        self,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
        retry_config: RetryConfig | None = None,
        max_output_tokens: int = _DEFAULT_MAX_TOKENS,
        auth_token: str = "",
        auth_file: str = "",
        timeout: float = 600.0,
    ):
        """Initialize OpenAI client.

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

        # Initialize OpenAI client
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=timeout,
        )

    async def _make_api_request(
        self,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
    ) -> Any:
        """Execute API request (core method that can be retried).

        Args:
            api_messages: List of messages in OpenAI format
            tools: Optional list of tools
            thinking_enabled: Currently a no-op for OpenAI-compatible
                endpoints to preserve broad third-party gateway compatibility.

        Returns:
            OpenAI ChatCompletion response (full response including usage)

        Raises:
            Exception: API call failed
        """
        params: dict[str, Any] = {
            "messages": api_messages,
            "max_tokens": self.max_output_tokens,
        }
        if self.model:
            params["model"] = self.model

        if tools:
            params["tools"] = self._convert_tools(tools)

        auth_headers = self._auth_headers(self._session_header(session_id))
        if auth_headers:
            params["extra_headers"] = auth_headers

        log_llm_request(provider="openai", mode="completion", api_base=self.api_base, params=params)

        try:
            raw_response = await _await_if_needed(
                self.client.chat.completions.with_raw_response.create(**params)
            )
            log_llm_response_meta(
                provider="openai",
                mode="completion",
                request_id=getattr(raw_response, "request_id", None),
                headers=getattr(raw_response, "headers", None),
            )
            response = await _await_if_needed(raw_response.parse())
        except AttributeError:
            # Test doubles and older SDK-compatible clients may not expose
            # ``with_raw_response``. Keep the request log and fall back to the
            # existing behavior, but request-id metadata will be unavailable.
            response = await _await_if_needed(self.client.chat.completions.create(**params))
        except Exception as exc:
            log_llm_error_meta(provider="openai", mode="completion", exc=exc)
            raise

        # Return full response to access usage info
        return response

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """Convert tools to OpenAI format.

        Args:
            tools: List of Tool objects or dicts

        Returns:
            List of tools in OpenAI dict format
        """
        result = []
        for tool in tools:
            if isinstance(tool, dict):
                # If already a dict, check if it's in OpenAI format
                if "type" in tool and tool["type"] == "function":
                    result.append(tool)
                else:
                    # Assume it's in Anthropic format, convert to OpenAI
                    result.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool["name"],
                                "description": tool["description"],
                                "parameters": tool["input_schema"],
                            },
                        }
                    )
            elif hasattr(tool, "to_openai_schema"):
                # Tool object with to_openai_schema method
                result.append(tool.to_openai_schema())
            else:
                raise TypeError(f"Unsupported tool type: {type(tool)}")
        return result

    def _convert_messages(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        """Convert internal messages to OpenAI format.

        Args:
            messages: List of internal Message objects

        Returns:
            Tuple of (system_message, api_messages)
            Note: OpenAI includes system message in the messages array
        """
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                # OpenAI includes system message in messages array
                api_messages.append({"role": "system", "content": msg.content})
                continue

            # For user messages
            if msg.role == "user":
                api_messages.append({"role": "user", "content": msg.content})

            # For assistant messages
            elif msg.role == "assistant":
                assistant_msg = {"role": "assistant"}

                # Add content if present
                if msg.content:
                    assistant_msg["content"] = msg.content

                # Add tool calls if present
                if msg.tool_calls:
                    tool_calls_list = []
                    for tool_call in msg.tool_calls:
                        tool_calls_list.append(
                            {
                                "id": tool_call.id,
                                "type": "function",
                                "function": {
                                    "name": tool_call.function.name,
                                    "arguments": json.dumps(tool_call.function.arguments),
                                },
                            }
                        )
                    assistant_msg["tool_calls"] = tool_calls_list

                # IMPORTANT: Add reasoning_details if thinking is present
                # This is CRITICAL for Interleaved Thinking to work properly!
                # The complete response_message (including reasoning_details) must be
                # preserved in Message History and passed back to the model in the next turn.
                # This ensures the model's chain of thought is not interrupted.
                if msg.thinking:
                    assistant_msg["reasoning_details"] = [{"text": msg.thinking}]

                api_messages.append(assistant_msg)

            # For tool result messages
            elif msg.role == "tool":
                api_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_call_id,
                        "content": msg.content,
                    }
                )

        return None, api_messages

    def _prepare_request(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare the request for OpenAI API.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools

        Returns:
            Dictionary containing request parameters
        """
        _, api_messages = self._convert_messages(messages)

        return {
            "api_messages": api_messages,
            "tools": tools,
        }

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse OpenAI response into LLMResponse.

        Args:
            response: OpenAI ChatCompletion response (full response object)

        Returns:
            LLMResponse object
        """
        # Get message from response
        message = response.choices[0].message

        # Extract text content
        text_content = message.content or ""

        # Extract thinking content from reasoning_details
        thinking_content = ""
        if hasattr(message, "reasoning_details") and message.reasoning_details:
            # reasoning_details is a list of reasoning blocks
            for detail in message.reasoning_details:
                if hasattr(detail, "text"):
                    thinking_content += detail.text

        # Extract tool calls
        tool_calls = []
        if message.tool_calls:
            for tool_call in message.tool_calls:
                # Parse arguments from JSON string
                arguments = json.loads(tool_call.function.arguments)

                tool_calls.append(
                    ToolCall(
                        id=tool_call.id,
                        type="function",
                        function=FunctionCall(
                            name=tool_call.function.name,
                            arguments=arguments,
                        ),
                    )
                )

        # Extract token usage from response
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason="stop",  # OpenAI doesn't provide finish_reason in the message
            usage=usage,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
        session_id: str = "",
    ) -> LLMResponse:
        """Generate response from OpenAI LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            thinking_enabled: Currently a no-op for OpenAI-compatible endpoints.
            session_id: Optional caller-owned session id forwarded as the
                ``X-RACCOON-Session-ID`` header (empty = gateway default).

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
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
                session_id=session_id,
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
                session_id=session_id,
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
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from OpenAI LLM.

        Yields thinking/text deltas as they arrive. Tool calls are accumulated
        and emitted in the final "finish" event along with token usage.
        """
        request_params = self._prepare_request(messages, tools)

        params: dict[str, Any] = {
            "messages": request_params["api_messages"],
            "max_tokens": self.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if self.model:
            params["model"] = self.model
        if request_params["tools"]:
            params["tools"] = self._convert_tools(request_params["tools"])

        auth_headers = self._auth_headers(self._session_header(session_id))
        if auth_headers:
            params["extra_headers"] = auth_headers

        log_llm_request(provider="openai", mode="stream", api_base=self.api_base, params=params)

        # Accumulators
        text_content = ""
        thinking_content = ""
        usage: TokenUsage | None = None
        # ``None`` (not "stop") so we can tell "upstream explicitly said stop"
        # apart from "upstream never sent a finish_reason" in the diagnostics
        # log below. core.py tolerates None via ``finish_reason or "stop"``.
        finish_reason: str | None = None

        # Tool call accumulators: {index: {id, name, arguments_str}}
        tool_acc: dict[int, dict[str, str]] = {}
        provider_request_id: str | None = None

        async def _open_stream() -> Any:
            nonlocal provider_request_id
            try:
                raw_response = await _await_if_needed(
                    self.client.chat.completions.with_raw_response.create(**params)
                )
                provider_request_id = getattr(raw_response, "request_id", None) or request_id_from_headers(
                    getattr(raw_response, "headers", None)
                )
                log_llm_response_meta(
                    provider="openai",
                    mode="stream",
                    request_id=provider_request_id,
                    headers=getattr(raw_response, "headers", None),
                )
                return await _await_if_needed(raw_response.parse())
            except AttributeError:
                return await _await_if_needed(self.client.chat.completions.create(**params))

        import asyncio as _asyncio

        max_attempts = max(1, self.retry_config.max_retries + 1) if self.retry_config.enabled else 1
        any_user_yield = False

        for attempt in range(max_attempts):
            # Reset per-attempt accumulators so a retry doesn't leak state from
            # a half-consumed prior attempt.
            text_content = ""
            thinking_content = ""
            usage = None
            finish_reason = None
            tool_acc = {}

            try:
                response_stream = await _open_stream()
            except Exception as exc:
                log_llm_error_meta(provider="openai", mode="stream", exc=exc)
                if attempt < max_attempts - 1 and is_retryable_stream_error(exc):
                    delay = self.retry_config.calculate_delay(attempt)
                    logger.warning(
                        "openai generate_stream open attempt %d/%d failed: %s; retrying in %.2fs",
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

            try:
                async for chunk in response_stream:
                    # Usage info (sent in the final chunk with choices=[])
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage = TokenUsage(
                            prompt_tokens=chunk.usage.prompt_tokens or 0,
                            completion_tokens=chunk.usage.completion_tokens or 0,
                            total_tokens=chunk.usage.total_tokens or 0,
                        )

                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]

                    # Finish reason
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    delta = choice.delta
                    if delta is None:
                        continue

                    # Reasoning / thinking content (DeepSeek, o1, etc.)
                    if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                        thinking_content += delta.reasoning_content
                        any_user_yield = True
                        yield StreamEvent(type="thinking", delta=delta.reasoning_content)

                    # Text content
                    if delta.content:
                        text_content += delta.content
                        any_user_yield = True
                        yield StreamEvent(type="text", delta=delta.content)

                    # Tool call deltas
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_acc:
                                tool_acc[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                    "arguments": "",
                                }
                            else:
                                if tc_delta.id:
                                    tool_acc[idx]["id"] = tc_delta.id
                                if tc_delta.function and tc_delta.function.name:
                                    tool_acc[idx]["name"] = tc_delta.function.name
                            if tc_delta.function and tc_delta.function.arguments:
                                tool_acc[idx]["arguments"] += tc_delta.function.arguments
            except Exception as exc:
                log_llm_error_meta(provider="openai", mode="stream", exc=exc)
                if is_retryable_stream_error(exc):
                    if any_user_yield:
                        # Once we've yielded deltas to the consumer we cannot
                        # rewind — surface partial content instead of retrying.
                        logger.warning(
                            "openai stream interrupted after partial yield "
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
                            "openai generate_stream consume attempt %d/%d dropped before any yield: %s; "
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
                # Successful consume — break out of the retry loop.
                break

        # Build tool calls. If a relay truncates output mid-arguments
        # (`finish_reason="length"`), the accumulated string is invalid JSON
        # and used to silently fall back to ``{}``, which fed an empty-args
        # retry loop back to the model. Now we hard-fail: drop broken
        # tool_calls and force ``finish_reason="length"`` so core.py
        # terminates the turn with a clear MAX_TOKENS stop reason.
        tool_calls: list[ToolCall] = []
        truncated_tool = False
        truncated_info: list[dict[str, Any]] = []
        for idx in sorted(tool_acc):
            entry = tool_acc[idx]
            raw = entry["arguments"]
            try:
                arguments = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                truncated_tool = True
                truncated_info.append({"name": entry["name"], "arguments_len": len(raw)})
                logger.warning(
                    "Truncated tool_call arguments for %r (idx=%d, len=%d): %s",
                    entry["name"], idx, len(raw), exc,
                )
                continue
            if not entry["name"]:
                truncated_tool = True
                truncated_info.append({"name": "", "arguments_len": len(raw)})
                logger.warning("Tool_call idx=%d has no function name; dropping", idx)
                continue
            tool_calls.append(
                ToolCall(
                    id=entry["id"],
                    type="function",
                    function=FunctionCall(
                        name=entry["name"],
                        arguments=arguments,
                    ),
                )
            )

        # Capture the upstream value before the tool-truncation override so the
        # diagnostics distinguish "gateway clipped tool args" (length, set here)
        # from "gateway ended a text turn" (stop / end_turn / None from upstream).
        raw_finish_reason = finish_reason
        if truncated_tool:
            finish_reason = "length"

        # Always-on (INFO) diagnostics: surfaces the upstream finish_reason —
        # including ``None`` when the gateway omitted it entirely — so a
        # mid-sentence "stop" cutoff is identifiable in box-agent-stderr.log
        # without enabling full LLM debug logging.
        logger.info(
            "openai stream finished: raw_finish_reason=%r final_finish_reason=%r "
            "completion_tokens=%s text_len=%d request_id=%s",
            raw_finish_reason,
            finish_reason,
            usage.completion_tokens if usage else None,
            len(text_content),
            provider_request_id,
        )

        yield StreamEvent(
            type="finish",
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
            provider_request_id=provider_request_id,
            truncated_tool_calls=truncated_info or None,
        )
