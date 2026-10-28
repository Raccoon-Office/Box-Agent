"""A native desktop launch cannot construct or reactivate local memory."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.desktop_memory.launch import desktop_launch_agent
from box_agent.agent_runtime import build_memory_manager
from box_agent.desktop_memory.backend import DesktopMemoryBackend


def grant(tmp_path):
    return {
        "BOX_AGENT_MEMORY_MODE": "desktop",
        "BOX_AGENT_MEMORY_BOOTSTRAP_FILE": str(tmp_path / "not-ready" / "bootstrap.json"),
        "BOX_AGENT_MEMORY_IDENTITY_EPOCH": str(uuid4()),
    }


def test_desktop_overlay_precedes_disabled_yaml_and_never_waits_for_bootstrap(tmp_path):
    original = AgentConfig(enable_memory=False, memory_dir=str(tmp_path / "legacy"))
    settings = desktop_launch_agent(original, grant(tmp_path))
    assert settings.enable_memory
    assert not settings.enable_memory_extraction
    assert settings.memory_backend_type == "desktop"
    backend = build_memory_manager(memory_dir=settings.memory_dir, settings=settings,
                                   dedup_jaccard_threshold=settings.memory_dedup_jaccard)
    assert isinstance(backend, DesktopMemoryBackend)
    assert not (tmp_path / "legacy").exists()
    assert not Path(settings.memory_desktop.bootstrap_file).exists()
    assert original.enable_memory is False
    assert original.memory_backend_type == "local"


@pytest.mark.parametrize("field,value", [
    ("BOX_AGENT_MEMORY_MODE", "local"),
    ("BOX_AGENT_MEMORY_MODE", "unknown"),
    ("BOX_AGENT_MEMORY_BOOTSTRAP_FILE", ""),
    ("BOX_AGENT_MEMORY_BOOTSTRAP_FILE", "relative/file"),
    ("BOX_AGENT_MEMORY_BOOTSTRAP_FILE", "/bad\0path"),
    ("BOX_AGENT_MEMORY_IDENTITY_EPOCH", ""),
    ("BOX_AGENT_MEMORY_IDENTITY_EPOCH", "bad"),
])
def test_invalid_native_grant_disables_memory_without_local_fallback(tmp_path, field, value):
    environment = {**grant(tmp_path), field: value}
    settings = desktop_launch_agent(AgentConfig(), environment)
    assert not settings.enable_memory
    assert not settings.enable_memory_extraction
    assert settings.memory_backend_type == "desktop"
    assert settings.memory_desktop.bootstrap_file == ""
    assert str(tmp_path) not in repr(settings)


def test_absent_launch_selector_preserves_standalone_and_offline_config(tmp_path):
    settings = AgentConfig(enable_memory=True, memory_dir=str(tmp_path / "legacy"))
    assert desktop_launch_agent(settings, {}) is settings
    assert desktop_launch_agent(settings, {"BOX_AGENT_MEMORY_BOOTSTRAP_FILE": "/ignored"}) is settings


def test_shared_config_loader_applies_the_same_overlay_for_cli_and_acp(tmp_path, monkeypatch):
    import box_agent.desktop_memory.launch as launch
    monkeypatch.setattr(launch, "_native_environment", None)
    environment = grant(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "config.yaml"
    path.write_text("api_key: synthetic\nmodel: synthetic\nenable_memory: false\n")
    loaded = Config.from_yaml(path)
    assert loaded.agent.enable_memory
    assert loaded.agent.memory_backend_type == "desktop"
    assert loaded.agent.memory_desktop.identity_epoch == environment["BOX_AGENT_MEMORY_IDENTITY_EPOCH"]
    import os
    assert all(key not in os.environ for key in environment)
    assert Config.from_yaml(path).agent.memory_desktop == loaded.agent.memory_desktop
    assert path.read_text().endswith("enable_memory: false\n")


@pytest.mark.asyncio
async def test_acp_initialize_advertises_adapter_without_private_launch_data(tmp_path):
    from box_agent.acp import BoxACPAgent
    settings = desktop_launch_agent(AgentConfig(enable_memory=False), grant(tmp_path))
    config = Config(llm=LLMConfig(api_key="synthetic"), agent=settings, tools=ToolsConfig())
    agent = BoxACPAgent(SimpleNamespace(), config, SimpleNamespace(), [], "base")
    agent._start_memory_bootstrap = lambda: None
    response = await agent.initialize(SimpleNamespace(field_meta={}))
    assert response.field_meta["desktop_memory"] == {
        "protocol_version": 2, "backend": "desktop", "enabled": True, "attempt_log_v1": True,
    }
    assert str(tmp_path) not in response.model_dump_json()
    assert settings.memory_desktop.identity_epoch not in response.model_dump_json()
