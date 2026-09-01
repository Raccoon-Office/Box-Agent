"""Regression tests for configurable LLM client timeout.

Before this fix the provider SDK clients were constructed without a ``timeout``,
so the SDK default (600s) always applied and there was no way to tune it from
config. A stalled gateway would hang for the full default before surfacing an
``APITimeoutError``. These tests pin the timeout threading:

    config.yaml -> LLMConfig/LiteLLMConfig -> LLMClient -> {OpenAI,Anthropic}Client
    -> AsyncOpenAI/AsyncAnthropic(timeout=...)
"""

import os
import tempfile
import textwrap

from box_agent.config import Config
from box_agent.llm import AnthropicClient, OpenAIClient
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.schema import LLMProvider


def _write_config(body: str) -> str:
    d = tempfile.mkdtemp()
    path = os.path.join(d, "config.yaml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(body))
    return path


def test_openai_client_forwards_timeout_to_sdk():
    client = OpenAIClient(api_key="k", api_base="https://x/v1", model="m", timeout=42.0)
    assert client.timeout == 42.0
    assert client.client.timeout == 42.0


def test_anthropic_client_forwards_timeout_to_sdk():
    client = AnthropicClient(api_key="k", api_base="https://x", model="m", timeout=43.0)
    assert client.timeout == 43.0
    assert client.client.timeout == 43.0


def test_default_timeout_matches_sdk_default():
    # Omitting the argument preserves the historical SDK default (no behavior
    # change for callers that never set it).
    assert OpenAIClient(api_key="k", api_base="https://x/v1", model="m").client.timeout == 600.0
    assert AnthropicClient(api_key="k", api_base="https://x", model="m").client.timeout == 600.0


def test_wrapper_threads_timeout_to_underlying_client():
    w = LLMClient(
        api_key="k",
        provider=LLMProvider.OPENAI,
        api_base="https://x/v1",
        model="m",
        timeout=44.0,
    )
    assert w.timeout == 44.0
    assert w._client.client.timeout == 44.0


def test_config_parses_timeout_for_main_and_lite():
    path = _write_config(
        """
        provider: openai
        api_base: https://gw.example.com/v1
        api_key: sk-test
        model: m
        timeout: 90
        lite_llm:
          provider: openai
          api_base: https://gw.example.com/v1
          api_key: sk-test
          model: lm
          timeout: 30
        """
    )
    cfg = Config.from_yaml(path)
    assert cfg.llm.timeout == 90.0
    assert cfg.lite_llm.timeout == 30.0


def test_config_timeout_defaults_when_omitted():
    path = _write_config(
        """
        provider: openai
        api_base: https://gw.example.com/v1
        api_key: sk-test
        model: m
        """
    )
    cfg = Config.from_yaml(path)
    assert cfg.llm.timeout == 1200.0
    # Lite block absent -> lightweight calls retain the shorter SDK default.
    assert cfg.lite_llm.timeout == 600.0
