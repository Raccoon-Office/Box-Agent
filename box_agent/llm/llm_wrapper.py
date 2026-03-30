"""LLM client wrapper that supports multiple providers.

This module provides a unified interface for different LLM providers
(Anthropic and OpenAI) through a single LLMClient class.
"""

import logging
from collections.abc import AsyncIterator

from ..retry import RetryConfig
from ..schema import LLMProvider, LLMResponse, Message, StreamEvent
from .anthropic_client import AnthropicClient
from .base import LLMClientBase
from .openai_client import OpenAIClient

logger = logging.getLogger(__name__)


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
    ):
        """Initialize LLM client with specified provider.

        Args:
            api_key: API key for authentication
            provider: LLM provider (anthropic or openai)
            api_base: Base URL for the API
            model: Model name to use
            retry_config: Optional retry configuration
        """
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.retry_config = retry_config or RetryConfig()

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
            )
        elif provider == LLMProvider.OPENAI:
            self._client = OpenAIClient(
                api_key=api_key,
                api_base=api_base,
                model=model,
                retry_config=retry_config,
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

        logger.info("Initialized LLM client with provider: %s, api_base: %s", provider, api_base)

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
    ) -> LLMResponse:
        """Generate response from LLM.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Returns:
            LLMResponse containing the generated content
        """
        return await self._client.generate(messages, tools)

    async def generate_stream(
        self,
        messages: list[Message],
        tools: list | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Generate streaming response from LLM.

        Yields StreamEvent chunks for thinking/text deltas as they arrive.
        The final event has type="finish" and carries tool_calls + usage.

        Args:
            messages: List of conversation messages
            tools: Optional list of Tool objects or dicts

        Yields:
            StreamEvent chunks
        """
        async for event in self._client.generate_stream(messages, tools):
            yield event
