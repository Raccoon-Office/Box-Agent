import json
from pathlib import Path

import pytest

from box_agent.llm import LLMClient
from box_agent.llm.model_profiles import (
    ModelProfileUnavailable,
    client_for_model_profile,
    load_model_profile_revision,
)
from box_agent.schema import LLMProvider


def _write_registry(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    "rev-hosted": {
                        "profileId": "hosted",
                        "profileRevision": "rev-hosted",
                        "provider": "openai",
                        "apiBase": "https://hosted.example/v1",
                        "apiKey": "hosted-key",
                        "authFile": "",
                        "defaultModel": "hosted-default",
                        "contextWindow": 180000,
                        "maxTokens": 16000,
                    },
                    "rev-custom": {
                        "profileId": "custom",
                        "profileRevision": "rev-custom",
                        "provider": "anthropic",
                        "apiBase": "https://custom.example/v1",
                        "apiKey": "custom-key",
                        "authFile": "",
                        "defaultModel": "custom-default",
                        "contextWindow": 64000,
                        "maxTokens": 8000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def _fallback_client() -> LLMClient:
    return LLMClient(
        api_key="fallback-key",
        provider=LLMProvider.OPENAI,
        api_base="https://fallback.example/v1",
        model="fallback-model",
    )


def test_profile_clients_keep_provider_and_endpoint_isolated(tmp_path, monkeypatch):
    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    fallback = _fallback_client()

    hosted = client_for_model_profile(
        {
            "profileId": "hosted",
            "profileRevision": "rev-hosted",
            "model": "hosted-selected",
        },
        fallback_client=fallback,
    )
    custom = client_for_model_profile(
        {
            "profileId": "custom",
            "profileRevision": "rev-custom",
            "model": "custom-selected",
        },
        fallback_client=fallback,
    )

    assert (hosted.provider, hosted.api_base, hosted.model) == (
        LLMProvider.OPENAI,
        "https://hosted.example/v1",
        "hosted-selected",
    )
    assert (custom.provider, custom.api_base, custom.model) == (
        LLMProvider.ANTHROPIC,
        "https://custom.example/v1",
        "custom-selected",
    )
    assert hosted.timeout == 1200.0
    assert custom.timeout == 1200.0
    assert fallback.api_base == "https://fallback.example/v1"


def test_profile_revision_must_exist(tmp_path, monkeypatch):
    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))

    with pytest.raises(ModelProfileUnavailable, match="revision is unavailable"):
        load_model_profile_revision("missing")


def test_profile_id_must_match_revision(tmp_path, monkeypatch):
    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))

    with pytest.raises(ModelProfileUnavailable, match="does not match"):
        client_for_model_profile(
            {
                "profileId": "wrong",
                "profileRevision": "rev-hosted",
                "model": "hosted-selected",
            },
            fallback_client=_fallback_client(),
        )
