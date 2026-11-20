import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from box_agent.plugins.cua.ready_tool import CuaService, create_cua_service
from box_agent.plugins.cua.config import CuaConfig, CuaRuntimeConfig
from box_agent.tools import mcp_loader as loader


@pytest.mark.asyncio
async def test_external_readiness_never_launches_or_stops_host(monkeypatch):
    service = CuaService(CuaConfig(runtime=CuaRuntimeConfig(
        enabled=True, mode="external", driver_path="/trusted/driver", endpoint="/host/socket")))
    monkeypatch.setattr("box_agent.plugins.cua.pinned_release.verify_source", Mock())
    service.manager._probe = AsyncMock(return_value=False)
    service.manager._start = AsyncMock()
    with pytest.raises(RuntimeError, match="Host CUA daemon"):
        await service.prepare()
    await service.close()
    service.manager._start.assert_not_awaited()


def test_host_environment_selects_external_and_enables_plugin(monkeypatch):
    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_CONFIG", json.dumps({
        "enabled": True, "mode": "external", "endpoint": "/host/socket", "driver_path": "/trusted/driver"}))
    config = SimpleNamespace(plugins={})
    service = create_cua_service(config)
    assert service.config.mode == "external"
    assert service.driver_command()[1:] == ["mcp", "--socket", "/host/socket", "--embedded"]
    assert config.plugins["cua"]["runtime"]["enabled"]


@pytest.mark.parametrize("plugin", [
    {"feed_screenshots": False},
    {"feed_screenshots": False, "runtime": None},
    {"feed_screenshots": False, "runtime": {}},
])
def test_host_environment_accepts_empty_plugin_runtime(monkeypatch, plugin):
    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_CONFIG", json.dumps({
        "enabled": True, "mode": "external", "endpoint": "/host/socket",
        "driver_path": "/trusted/driver"}))
    config = SimpleNamespace(plugins={"cua": plugin})
    service = create_cua_service(config)
    assert service.config.enabled
    assert service.config.mode == "external"
    assert service.config.endpoint == "/host/socket"
    assert service.config.driver_path == "/trusted/driver"
    assert not service.settings.feed_screenshots
    assert config.plugins["cua"]["runtime"]["enabled"]


@pytest.mark.asyncio
async def test_service_retains_pending_launch_and_directory_until_cleanup_retry_succeeds():
    from pathlib import Path
    service = CuaService(CuaConfig(runtime=CuaRuntimeConfig(enabled=True, driver_path="/trusted/driver")))
    directory = Path(service.adapter.directory.name)
    pending = SimpleNamespace(close=AsyncMock(side_effect=[RuntimeError("launch unsettled"), None]))
    service.adapter._pending_handle = pending
    with pytest.raises(RuntimeError, match="launch unsettled"):
        await service.close()
    assert service.adapter._pending_handle is pending
    assert directory.exists()
    await service.close()
    assert service.adapter._pending_handle is None
    assert not directory.exists()


@pytest.mark.parametrize("enabled", [False, "false", 0, "off"])
def test_explicit_plugin_disable_wins_over_host_environment(monkeypatch, enabled):
    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_CONFIG", json.dumps({
        "enabled": True, "mode": "external", "endpoint": "/host/socket"}))
    config = SimpleNamespace(plugins={"cua": {"runtime": {"enabled": enabled}}})
    service = create_cua_service(config)
    assert not service.config.enabled
    assert service.config.mode == "external"


@pytest.mark.parametrize("setting", ["feed_screenshots", "persist_images"])
def test_sessions_reuse_cua_runtime_with_independent_vision_settings(monkeypatch, setting):
    runtime = CuaRuntimeConfig(
        enabled=True, mode="external", driver_path="/trusted/driver", endpoint="/host/socket",
    )
    first = CuaService(CuaConfig(runtime=runtime))
    second = CuaService(CuaConfig(runtime=runtime, **{setting: False}))
    cleanup = Mock(wraps=second.adapter.cleanup)
    monkeypatch.setattr(second.adapter, "cleanup", cleanup)
    monkeypatch.setattr(loader, "_cua_service", None)
    try:
        assert loader.register_cua_service(first) is first
        assert loader.register_cua_service(second) is first
        assert getattr(first.settings, setting) is True
        assert getattr(second.settings, setting) is False
        cleanup.assert_called_once()
    finally:
        first.adapter.cleanup()
        second.adapter.cleanup()


@pytest.mark.parametrize("server_name,endpoint", [
    ("another-desktop", "/host/socket"),
    ("computer-use", "/another/socket"),
])
def test_sessions_reject_conflicting_cua_runtime_ownership(monkeypatch, server_name, endpoint):
    first = CuaService(CuaConfig(runtime=CuaRuntimeConfig(
        enabled=True, mode="external", driver_path="/trusted/driver", endpoint="/host/socket",
    )))
    second = CuaService(CuaConfig(server_name=server_name, runtime=CuaRuntimeConfig(
        enabled=True, mode="external", driver_path="/trusted/driver", endpoint=endpoint,
    )))
    monkeypatch.setattr(loader, "_cua_service", None)
    try:
        loader.register_cua_service(first)
        with pytest.raises(RuntimeError, match="CUA runtime is already registered"):
            loader.register_cua_service(second)
    finally:
        first.adapter.cleanup()
        second.adapter.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_loader_excludes_retired_cua_sources_on_load_and_refresh(tmp_path, monkeypatch, disabled):
    file = tmp_path / "mcp.json"
    config = {"command": "/host/cua-driver", "args": ["mcp", "--embedded"], "disabled": disabled}
    file.write_text(json.dumps({"mcpServers": {"computer-use": config, "custom-desktop": config,
                                              "other": {"command": "other-mcp", "disabled": True}}}))
    service = CuaService(CuaConfig(server_name="custom-desktop", runtime=CuaRuntimeConfig(enabled=True)))
    monkeypatch.setattr(loader, "_cua_service", service)
    service.prepare = AsyncMock()
    try:
        assert await loader.load_mcp_tools_async(str(file)) == []
        assert set(loader._mcp_server_definitions) == {"other"}
        assert (await loader.reconcile_mcp_sources())["success"]
        for name in ("computer-use", service.name):
            assert name not in loader._mcp_server_definitions
            assert not (await loader.reconnect_mcp_server(name))["success"]
        service.prepare.assert_not_awaited()
    finally:
        await loader.cleanup_mcp_connections()


def test_legacy_interface_choice_does_not_change_repl_service(monkeypatch):
    runtime = CuaRuntimeConfig(enabled=True, mode='external',
                               driver_path='/host/driver', endpoint='/host/socket')
    first = CuaService(CuaConfig(tool_interface="driver", runtime=runtime))
    second = CuaService(CuaConfig(tool_interface='app', runtime=runtime))
    monkeypatch.setattr(loader, '_cua_service', first)
    try:
        assert loader.register_cua_service(second) is first
    finally:
        first.adapter.cleanup()
        second.adapter.cleanup()
