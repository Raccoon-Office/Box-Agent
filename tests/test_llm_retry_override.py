"""Per-call retry overrides for bounded utility requests."""

from __future__ import annotations

import pytest

from box_agent.llm import AnthropicClient, OpenAIClient
from box_agent.retry import RetryConfig
from box_agent.schema import Message


@pytest.mark.asyncio
@pytest.mark.parametrize("client_type", [OpenAIClient, AnthropicClient])
async def test_generate_can_disable_client_retry_for_one_call(client_type):
    client = client_type(
        api_key="test",
        api_base="https://example.test",
        model="test-model",
        retry_config=RetryConfig(
            enabled=True,
            max_retries=3,
            initial_delay=0,
        ),
    )
    calls = 0

    async def fail_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("The model service could not complete this request")

    client._make_api_request = fail_once

    with pytest.raises(RuntimeError):
        await client.generate(
            [Message(role="user", content="inspect")],
            retry_enabled=False,
        )

    assert calls == 1
