from __future__ import annotations

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


def test_explicit_false_overrides_model_name_heuristic() -> None:
    client = LLMClient(
        api_key="test",
        provider=LLMProvider.OPENAI,
        api_base="https://tokenhub.example/v1",
        model="vision-model",
        image_input=False,
    )

    assert image_input_support(client) is False
