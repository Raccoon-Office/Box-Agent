from __future__ import annotations

from types import SimpleNamespace

import pytest

from box_agent.agent_runtime import build_llm_client
from box_agent.config import Config, LLMConfig
from box_agent.llm.capabilities import image_input_support
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.schema import LLMProvider


def test_llm_config_keeps_image_capability_unknown_by_default() -> None:
    assert LLMConfig().image_input is None


def test_yaml_can_explicitly_register_image_input_capability(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
        api_key: test
        api_base: https://tokenhub.example/v1
        provider: openai
        model: vision-model
        image_input: true
        """,
        encoding="utf-8",
    )

    config = Config.from_yaml(path)

    assert config.llm.image_input is True


def test_build_llm_client_forwards_explicit_image_capability_only_when_set() -> None:
    received: list[dict[str, object]] = []

    class CaptureClient:
        def __init__(self, **kwargs: object) -> None:
            received.append(kwargs)

    common = dict(
        api_key="test",
        provider=LLMProvider.OPENAI,
        api_base="https://tokenhub.example/v1",
        model="vision-model",
        retry_config=None,
        max_output_tokens=128,
        auth_file="",
        timeout=10,
        client_factory=CaptureClient,
    )
    build_llm_client(**common)
    build_llm_client(**common, image_input=True)

    assert "image_input" not in received[0]
    assert received[1]["image_input"] is True


def test_llm_client_exposes_explicit_image_capability() -> None:
    client = LLMClient(
        api_key="test",
        provider=LLMProvider.OPENAI,
        api_base="https://tokenhub.example/v1",
        model="custom-model",
        image_input=True,
    )

    assert image_input_support(client) is True
    assert image_input_support(client.for_model("custom-model-child")) is True


def test_explicit_false_overrides_allowlisted_model() -> None:
    client = LLMClient(
        api_key="test",
        provider=LLMProvider.OPENAI,
        api_base="https://tokenhub.example/v1",
        model="gpt-5.5",
        image_input=False,
    )

    assert image_input_support(client) is False


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.5",
        "gpt-5.5-2026-04-23",
        "claude-opus-5-5",
        "gemini-4-argon",
        "gemini-3.8-flash",
        "qwen3.8-max",
        "kimi-k3.1-pro",
        "glm-4.6v",
        "glm-5.3-flashx",
        "deepseek-flash",
        "sn-deepseek-v4.1-flash",
        "deepseek-v4-1-flash-261017",
        "MiniMax-M3.1-Flash",
        "grok-4.7",
        "doubao-seed-2-1-pro-260915",
        "  GLM-4.6V  ",
    ],
)
def test_known_vision_models_work_without_catalog_metadata(model) -> None:
    assert image_input_support(SimpleNamespace(model=model)) is True


@pytest.mark.parametrize(
    "model",
    [
        "sn-kimi-k3",
        "sn-kimi-k3.1-pro-highspeed",
        "openai/gpt-6.1-sol",
        "vendor/gpt-6-astra-2026-10-01-custom",
        "anthropic.claude-opus-5-5-v1:0",
        "  VENDOR/QWEN3.8-MAX-0902  ",
    ],
)
def test_allowlist_matches_model_name_inside_gateway_alias(model) -> None:
    assert image_input_support(SimpleNamespace(model=model)) is True


@pytest.mark.parametrize(
    "tags, expected",
    [(["vision", "code"], True), (["code"], False), ([], False)],
)
def test_current_catalog_tags_override_allowlisted_model(tags, expected) -> None:
    llm = SimpleNamespace(
        model="vendor/gpt-6.1-sol-custom",
        auto_model_candidates=(
            {"model": "vendor/gpt-6.1-sol-custom", "tags": tags},
        ),
    )

    assert image_input_support(llm) is expected


def test_catalog_vision_tag_enables_opaque_model_alias() -> None:
    llm = SimpleNamespace(
        model="hosted-123",
        auto_model_candidates=({"model": "hosted-123", "tags": ["vision"]},),
    )

    assert image_input_support(llm) is True


@pytest.mark.parametrize(
    "candidate",
    [
        {"model": "gpt-5.5"},
        {"model": "gpt-5.5", "tags": None},
        {"model": "gpt-5.5", "tags": "vision"},
        {"model": "other-model", "tags": []},
    ],
)
def test_missing_or_invalid_current_tags_fall_back_to_allowlist(candidate) -> None:
    llm = SimpleNamespace(model="gpt-5.5", auto_model_candidates=(candidate,))

    assert image_input_support(llm) is True


@pytest.mark.parametrize(
    "model",
    [
        "unknown",
        "vision-model",
        "deepseek-vl-custom",
        "deepseek-v4-pro",
        "sn-deepseek-v4-pro",
        "gpt-7-unregistered",
        "qwen3-max",
        "kimi-k2",
        "glm-5.3",
        "MiniMax-M2.7",
        "",
    ],
)
def test_unlisted_models_stay_unknown_without_a_declaration(model) -> None:
    llm = SimpleNamespace(model=model, api_base="https://api.deepseek.com")

    assert image_input_support(llm) is None


def test_other_candidates_do_not_declare_current_model_capability() -> None:
    llm = SimpleNamespace(
        model="unknown",
        auto_model_candidates=({"model": "vision-model", "tags": ["vision"]},),
    )

    assert image_input_support(llm) is None
