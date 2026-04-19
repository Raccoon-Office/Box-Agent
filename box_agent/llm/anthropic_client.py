"""Anthropic LLM client implementation."""

import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..retry import RetryConfig, async_retry
from ..schema import FunctionCall, LLMResponse, Message, StreamEvent, TokenUsage, ToolCall
from .base import LLMClientBase

logger = logging.getLogger(__name__)

# Hard-coded budget for extended thinking. Kept intentionally low — budgets
# larger than this rarely improve answer quality for agentic workflows and
# waste tokens. Tune here if we ever expose it as config.
_THINKING_BUDGET = 8000


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
    ):
        """Initialize Anthropic client.

        Args:
            api_key: API key for authentication
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
        """
        super().__init__(api_key, api_base, model, retry_config)

        # Initialize Anthropic async client
        self.client = anthropic.AsyncAnthropic(
            base_url=api_base,
            api_key=api_key,
            default_headers={"Authorization": f"Bearer {api_key}"},
        )

    async def _make_api_request(
        self,
        system_message: str | None,
        api_messages: list[dict[str, Any]],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
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
            "model": self.model,
            "max_tokens": 16384,
            "messages": api_messages,
        }

        if system_message:
            params["system"] = system_message

        if tools:
            params["tools"] = self._convert_tools(tools)

        if thinking_enabled:
            params["thinking"] = {"type": "enabled", "budget_tokens": _THINKING_BUDGET}

        # Use Anthropic SDK's async messages.create
        response = await self.client.messages.create(**params)
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
                    api_messages.append({"role": msg.role, "content": msg.content})

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

        return system_message, api_messages

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
            )

        return LLMResponse(
            content=text_content,
            thinking=thinking_content if thinking_content else None,
            tool_calls=tool_calls if tool_calls else None,
            finish_reason=response.stop_reason or "stop",
            usage=usage,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
    ) -> LLMResponse:
        """Generate response from Anthropic LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of available tools
            thinking_enabled: Enable Anthropic extended thinking.

        Returns:
            LLMResponse containing the generated content
        """
        # Prepare request
        request_params = self._prepare_request(messages, tools)

        # Make API request with retry logic
        if self.retry_config.enabled:
            # Apply retry logic
            retry_decorator = async_retry(config=self.retry_config, on_retry=self.retry_callback)
            api_call = retry_decorator(self._make_api_request)
            response = await api_call(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
            )
        else:
            # Don't use retry
            response = await self._make_api_request(
                request_params["system_message"],
                request_params["api_messages"],
                request_params["tools"],
                thinking_enabled=thinking_enabled,
            )

        # Parse and return response
        return self._parse_response(response)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        thinking_enabled: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from Anthropic LLM.

        Yields thinking/text deltas as they arrive. Tool calls are accumulated
        and emitted in the final "finish" event along with token usage.
        """
        request_params = self._prepare_request(messages, tools)

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16384,
            "messages": request_params["api_messages"],
        }
        if request_params["system_message"]:
            params["system"] = request_params["system_message"]
        if request_params["tools"]:
            params["tools"] = self._convert_tools(request_params["tools"])
        if thinking_enabled:
            params["thinking"] = {"type": "enabled", "budget_tokens": _THINKING_BUDGET}

        # Accumulators for the finish event
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

        async with self.client.messages.stream(**params) as stream:
            async for event in stream:
                # ── Message start (input token usage) ──
                if event.type == "message_start":
                    msg = event.message
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

                # ── Content block delta ──
                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "thinking_delta":
                        thinking_content += delta.thinking
                        yield StreamEvent(type="thinking", delta=delta.thinking)
                    elif delta.type == "text_delta":
                        text_content += delta.text
                        yield StreamEvent(type="text", delta=delta.text)
                    elif delta.type == "input_json_delta":
                        current_tool_json += delta.partial_json

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

        # Build final usage
        total_input = input_tokens + cache_read_tokens + cache_create_tokens
        usage = TokenUsage(
            prompt_tokens=total_input,
            completion_tokens=output_tokens,
            total_tokens=total_input + output_tokens,
        )

        yield StreamEvent(
            type="finish",
            finish_reason=finish_reason,
            usage=usage,
            tool_calls=tool_calls if tool_calls else None,
        )
