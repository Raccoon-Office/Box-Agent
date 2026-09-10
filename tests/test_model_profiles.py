import json
import logging
from pathlib import Path
from types import SimpleNamespace

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


def test_missing_revision_uses_latest_valid_same_profile(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="box_agent.llm.model_profiles")
    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    data = json.loads(registry.read_text())
    original = data["profiles"]["rev-hosted"]
    original["createdAt"] = "2026-09-01T00:00:00.000Z"
    data["profiles"]["rev-new"] = {
        **original, "profileRevision": "rev-new",
        "createdAt": "2026-09-10T00:00:00.000Z", "apiKey": "new-key",
    }
    data["profiles"]["rev-invalid"] = {
        **original, "profileRevision": "rev-invalid",
        "createdAt": "2026-09-11T00:00:00.000Z", "maxTokens": -1,
    }
    registry.write_text(json.dumps(data))
    before = registry.read_bytes()
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    fallback = _fallback_client()
    binding = {"profileId": "hosted", "profileRevision": "missing", "model": "chosen-model"}

    client = client_for_model_profile(binding, fallback_client=fallback)

    assert (client.provider, client.api_base, client.model) == (
        LLMProvider.OPENAI, "https://hosted.example/v1", "chosen-model"
    )
    assert client.api_key == "new-key"
    assert binding["profileRevision"] == "missing"
    assert registry.read_bytes() == before
    assert fallback.model == "fallback-model"
    assert "resolved_revision=rev-new" in caplog.text
    assert "new-key" not in caplog.text
    exact = client_for_model_profile(
        {**binding, "profileRevision": "rev-hosted"}, fallback_client=fallback
    )
    assert exact.api_key == "hosted-key"


@pytest.mark.parametrize("profile_id", ["deleted-profile", "custom"])
def test_revision_recovery_never_uses_another_profile(tmp_path, monkeypatch, profile_id):
    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    data = json.loads(registry.read_text())
    del data["profiles"]["rev-custom"]
    registry.write_text(json.dumps(data))
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    with pytest.raises(ModelProfileUnavailable, match="revision is unavailable"):
        client_for_model_profile(
            {"profileId": profile_id, "profileRevision": "missing", "model": "chosen"},
            fallback_client=_fallback_client(),
        )


@pytest.mark.parametrize(
    "change", [{"apiBase": "https://other.example/v1"}, {"provider": "anthropic"}]
)
def test_revision_recovery_rejects_ambiguous_provider_routes(tmp_path, monkeypatch, change):
    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    data = json.loads(registry.read_text())
    data["profiles"]["other-route"] = {
        **data["profiles"]["rev-hosted"], **change,
        "profileRevision": "other-route", "createdAt": "2026-09-10T00:00:00.000Z",
    }
    registry.write_text(json.dumps(data))
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    with pytest.raises(ModelProfileUnavailable, match="ambiguous"):
        client_for_model_profile(
            {"profileId": "hosted", "profileRevision": "missing", "model": "chosen"},
            fallback_client=_fallback_client(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_routing", [False, True])
async def test_old_acp_conversation_continues_when_model_revision_is_missing(
    tmp_path, monkeypatch, auto_routing
):
    import box_agent.acp as acp_module
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.schema import LLMResponse, StreamEvent
    from box_agent.session_log import SessionLog
    from tests.test_acp import DummyConn

    registry = tmp_path / "model-profiles.json"
    _write_registry(registry)
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    monkeypatch.setattr(acp_module, "state_path", lambda relative: tmp_path / "profile" / relative)
    calls = []

    async def stream(client, messages, tools=None, **kwargs):
        calls.append((client.api_base, client.model))
        assert any(message.content == "old conversation" for message in messages)
        yield StreamEvent(type="text", delta="continued successfully")
        yield StreamEvent(type="finish", finish_reason="stop")

    async def generate(client, messages=None, tools=None, **kwargs):
        return LLMResponse(content='{"continue": false}', finish_reason="stop")

    monkeypatch.setattr(LLMClient, "generate_stream", stream)
    monkeypatch.setattr(LLMClient, "generate", generate)
    log = SessionLog.create(tmp_path / "profile" / "sessions", session_id="old-chat", cwd=tmp_path)
    log.append("user/message", {"role": "user", "content": "old conversation"}, surface_op="append")
    log.flush()
    log.close()
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(workspace_dir=str(tmp_path), max_steps=1),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False),
    )
    adapter = acp_module.BoxACPAgent(DummyConn(), config, _fallback_client(), [], "system")
    binding = {
        "source": "profile", "version": 2, "bindingId": "old-binding",
        "profileId": "hosted", "profileRevision": "missing",
        "routingMode": "auto" if auto_routing else "manual",
        "model": "chosen-model", "contextWindow": 180000, "maxTokens": 16000,
    }
    if auto_routing:
        binding["autoRouting"] = {"models": [{
            "model": "chosen-model", "tags": ["general", "auto"], "abilityLevel": 3,
            "contextWindow": 180000, "maxTokens": 16000,
        }]}
    session = await adapter.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={
        "session_id": "old-chat",
        "llm_binding": binding,
    }))
    state = adapter._sessions[session.sessionId]
    try:
        result = await adapter.prompt(SimpleNamespace(
            sessionId=session.sessionId, prompt=[{"text": "continue"}], field_meta={}
        ))
        assert result.stopReason == "end_turn"
        assert calls == [("https://hosted.example/v1", "chosen-model")]
        assert state.agent.messages[-1].content == "continued successfully"
    finally:
        state.agent.session_log.close()
