"""Request-body byte budgets survive configuration and application wiring."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

import box_agent.cli as cli
import box_agent.llm.model_profiles as model_profiles
import box_agent.mcp_servers.web_extract_server as web_extract_server
from box_agent.config import Config, LLMConfig, LiteLLMConfig
from box_agent.llm import LLMClient
from box_agent.plugins.builtins import SessionResources
from box_agent.schema import LLMProvider
from box_agent.session_assembly import prepare_model
from box_agent.session_context import HostBindings, SessionContext, SessionOptions


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)


def write_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "api_key": "test-key",
        "api_base": "https://custom.example/v1",
        "model": "main-model",
        "provider": "openai",
        **overrides,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("config_type", [LLMConfig, LiteLLMConfig])
def test_request_body_config_defers_unspecified_limit_to_provider(config_type):
    assert config_type().max_request_body_bytes is None
    assert config_type(max_request_body_bytes=None).max_request_body_bytes is None


@pytest.mark.parametrize("config_type", [LLMConfig, LiteLLMConfig])
def test_request_body_config_accepts_positive_integer(config_type):
    config = config_type(max_request_body_bytes=12_000_000)
    assert config.max_request_body_bytes == 12_000_000
    assert config.model_dump()["max_request_body_bytes"] == 12_000_000


@pytest.mark.parametrize("config_type", [LLMConfig, LiteLLMConfig])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10000000"])
def test_request_body_config_rejects_non_positive_or_non_integer_limit(config_type, value):
    with pytest.raises(ValidationError, match="max_request_body_bytes"):
        config_type(max_request_body_bytes=value)


def test_yaml_keeps_main_and_lite_request_body_limits_independent(tmp_path):
    path = write_config(
        tmp_path,
        max_request_body_bytes=10_000_000,
        lite_llm={
            "api_base": "https://lite.example/v1",
            "api_key": "lite-key",
            "model": "lite-model",
            "max_request_body_bytes": 2_000_000,
        },
    )
    config = Config.from_yaml(path)
    assert config.llm.max_request_body_bytes == 10_000_000
    assert config.lite_llm.max_request_body_bytes == 2_000_000


def test_yaml_explicit_lite_endpoint_does_not_inherit_main_request_body_limit(tmp_path):
    path = write_config(
        tmp_path,
        api_base="https://api.xiaohuanxiong.com/v1",
        max_request_body_bytes=10_000_000,
        lite_llm={
            "api_base": "https://lite.example/v1",
            "api_key": "lite-key",
            "model": "lite-model",
        },
    )
    config = Config.from_yaml(path)
    assert config.llm.max_request_body_bytes == 10_000_000
    assert config.lite_llm.max_request_body_bytes is None


@pytest.mark.parametrize("api_base", ["https://api.xiaohuanxiong.com/v1", "https://custom.example/v1"])
def test_yaml_without_request_body_limit_keeps_provider_default_unresolved(tmp_path, api_base):
    config = Config.from_yaml(write_config(tmp_path, api_base=api_base))
    assert config.llm.max_request_body_bytes is None
    assert config.lite_llm.max_request_body_bytes is None


@pytest.mark.parametrize("key", ["max_request_body_bytes", "llm.max_request_body_bytes"])
def test_cli_request_body_limit_round_trips_in_legacy_flat_yaml(tmp_path, monkeypatch, capsys, key):
    path = write_config(tmp_path)
    monkeypatch.setattr(Config, "find_config_file", lambda _name: path)
    assert cli.cmd_config(set_pair=(key, "9000000")) == 0
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["max_request_body_bytes"] == 9_000_000
    assert "llm" not in data
    assert Config.from_yaml(path).llm.max_request_body_bytes == 9_000_000

    capsys.readouterr()
    assert cli.cmd_config(get_key=key) == 0
    assert capsys.readouterr().out.strip() == "9000000"


def test_cli_lite_request_body_limit_round_trips_without_changing_main(tmp_path, monkeypatch, capsys):
    path = write_config(
        tmp_path,
        max_request_body_bytes=10_000_000,
        lite_llm={
            "api_base": "https://lite.example/v1",
            "api_key": "lite-key",
            "model": "lite-model",
        },
    )
    monkeypatch.setattr(Config, "find_config_file", lambda _name: path)
    assert cli.cmd_config(set_pair=("lite_llm.max_request_body_bytes", "2000000")) == 0
    config = Config.from_yaml(path)
    assert config.llm.max_request_body_bytes == 10_000_000
    assert config.lite_llm.max_request_body_bytes == 2_000_000

    capsys.readouterr()
    assert cli.cmd_config(get_key="lite_llm.max_request_body_bytes") == 0
    assert capsys.readouterr().out.strip() == "2000000"


@pytest.mark.parametrize("value", ["0", "-1", "true", "1.5"])
def test_cli_invalid_request_body_limit_preserves_previous_yaml(tmp_path, monkeypatch, value):
    path = write_config(tmp_path, max_request_body_bytes=10_000_000)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(Config, "find_config_file", lambda _name: path)
    assert cli.cmd_config(set_pair=("llm.max_request_body_bytes", value)) == 1
    assert path.read_text(encoding="utf-8") == before


@pytest.mark.asyncio
async def test_doctor_passes_request_body_limit_to_client_without_model_call(tmp_path, monkeypatch):
    config = Config.from_yaml(write_config(tmp_path, max_request_body_bytes=9_000_000))
    constructed = []

    def client_factory(**kwargs):
        constructed.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def probe(client):
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(cli, "LLMClient", client_factory)
    monkeypatch.setattr(cli, "_probe_llm_api", probe)
    result = await cli._doctor_api_status(config)
    assert result["status"] == "ok"
    assert constructed[0]["max_request_body_bytes"] == 9_000_000


def test_web_extract_passes_main_request_body_limit_to_client(tmp_path, monkeypatch):
    config = Config.from_yaml(write_config(tmp_path, max_request_body_bytes=8_000_000))
    monkeypatch.setattr(Config, "load", lambda: config)
    monkeypatch.setattr(web_extract_server, "LLMClient", lambda **kwargs: SimpleNamespace(**kwargs))
    assert web_extract_server._create_configured_llm().max_request_body_bytes == 8_000_000


def write_profile_registry(tmp_path, **overrides):
    profile = {
        "profileId": "custom",
        "profileRevision": "rev-custom",
        "provider": "openai",
        "apiBase": "https://profile.example/v1",
        "apiKey": "profile-key",
        "defaultModel": "profile-model",
        **overrides,
    }
    path = tmp_path / "model-profiles.json"
    path.write_text(json.dumps({"version": 1, "profiles": {"rev-custom": profile}}), encoding="utf-8")
    return path


@pytest.mark.parametrize("limit", [None, 3_000_000])
def test_profile_uses_own_request_body_limit_independent_of_main(tmp_path, monkeypatch, limit):
    path = write_profile_registry(tmp_path, maxRequestBodyBytes=limit)
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(path))
    monkeypatch.setattr(model_profiles, "LLMClient", lambda **kwargs: SimpleNamespace(**kwargs))
    client = model_profiles.client_for_model_profile(
        {"profileId": "custom", "profileRevision": "rev-custom", "model": "chosen-model"},
        fallback_client=SimpleNamespace(max_request_body_bytes=10_000_000),
    )
    assert client.max_request_body_bytes == limit


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3000000"])
def test_profile_rejects_invalid_request_body_limit(tmp_path, value):
    path = write_profile_registry(tmp_path, maxRequestBodyBytes=value)
    with pytest.raises(model_profiles.ModelProfileUnavailable, match="maxRequestBodyBytes"):
        model_profiles.load_model_profile_revision("rev-custom", registry_path=path)


@pytest.mark.asyncio
async def test_session_model_receives_configured_request_body_limit(tmp_path):
    config = Config.from_yaml(write_config(tmp_path, max_request_body_bytes=8_000_000))
    resources = SessionResources(SessionContext(config, SessionOptions(), HostBindings()))
    try:
        await prepare_model(resources)
        assert resources.llm_client.max_request_body_bytes == 8_000_000
        assert resources.llm_client._client.max_request_body_bytes == 8_000_000
    finally:
        await resources.aclose()


@pytest.mark.asyncio
async def test_session_keeps_borrowed_custom_endpoint_limit_independent(tmp_path):
    config = Config.from_yaml(write_config(tmp_path, max_request_body_bytes=10_000_000))
    borrowed = LLMClient(
        api_key="test-key", provider=LLMProvider.OPENAI,
        api_base="https://custom-208.example/v1", model="custom-208",
    )
    resources = SessionResources(SessionContext(
        config, SessionOptions(), HostBindings(llm_client=borrowed),
    ))
    try:
        await prepare_model(resources)
        assert resources.llm_client is borrowed
        assert resources.llm_client._client.max_request_body_bytes is None
    finally:
        await resources.aclose()
        await borrowed.aclose()


@pytest.mark.asyncio
async def test_custom_208_profile_does_not_inherit_resolved_sn_gateway_limit(tmp_path, monkeypatch):
    registry = write_profile_registry(tmp_path, apiBase="https://custom-208.example/v1")
    monkeypatch.setenv("BOX_AGENT_MODEL_PROFILES_FILE", str(registry))
    main = LLMClient(
        api_key="test-key", provider=LLMProvider.OPENAI,
        api_base="https://api.xiaohuanxiong.com/v1", model="main-model",
    )
    profile = None
    try:
        assert main._client.max_request_body_bytes == 10_000_000
        profile = model_profiles.client_for_model_profile(
            {"profileId": "custom", "profileRevision": "rev-custom", "model": "custom-208"},
            fallback_client=main,
        )
        assert profile.max_request_body_bytes is None
        assert profile._client.max_request_body_bytes is None
    finally:
        if profile is not None:
            await profile.aclose()
        await main.aclose()
