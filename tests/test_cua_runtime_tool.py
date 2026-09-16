from __future__ import annotations

import asyncio
import json
import os

import pytest

from box_agent.tools import cua_runtime_tool
from box_agent.tools import cua_runtime_daemon
from box_agent.tools import mcp_loader, mcp_tool_catalog
from box_agent.tools.cua_runtime_config import with_builtin_standalone_cua
from box_agent.tools.cua_runtime_daemon import STANDALONE_CUA_SOCKET
from box_agent.tools.cua_runtime_tool import EnsureCuaReadyTool
from box_agent.tools.mcp_sources import ResolvedMcpServer


def _configure(tmp_path, monkeypatch, args, *, owns_daemon=True):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cua-computer-use": {
                        "command": "cua-driver",
                        "args": args,
                        "startOnDemand": True,
                        "boxAgentOwnedDaemon": owns_daemon,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_server_config",
        lambda _name: {
            "command": "cua-driver",
            "args": args,
            "startOnDemand": True,
            "boxAgentOwnedDaemon": owns_daemon,
        },
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "is_mcp_server_connection_current",
        lambda _name: True,
    )


def _definition(config: dict) -> ResolvedMcpServer:
    return ResolvedMcpServer(
        name="cua-computer-use",
        config=config,
        owner="user",
        config_id="custom-mcp:cua-computer-use",
        connector_id=None,
        connector_name=None,
        source_path="mcp.json",
        fingerprint="fixture",
    )


def test_standalone_runtime_is_injected_without_manual_mcp_config(monkeypatch, tmp_path):
    executable = tmp_path / "cua-driver"
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_CUA_DRIVER_PATH", str(executable))
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)
    monkeypatch.setattr("box_agent.tools.cua_runtime_config.sys.platform", "linux")

    resolved = with_builtin_standalone_cua({})

    config = resolved["cua-computer-use"].config
    assert config["command"] == str(executable)
    assert config["args"] == [
        "mcp", "--socket", STANDALONE_CUA_SOCKET,
    ]
    assert config["env"] == {"CUA_DRIVER_RS_UPDATE_CHECK": "false"}
    assert config["startOnDemand"] is True
    assert config["boxAgentOwnedDaemon"] is True
    assert resolved["cua-computer-use"].owner == "system"


def test_standalone_runtime_discovers_cua_next_to_packaged_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "box-agent-runtime"
    runtime.mkdir()
    executable_name = "cua-driver.exe" if os.name == "nt" else "cua-driver"
    executable = tmp_path / "cua-driver" / executable_name
    executable.parent.mkdir()
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.delenv("BOX_AGENT_CUA_DRIVER_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)
    monkeypatch.setenv("BOX_AGENT_RUNTIME_ROOT", str(runtime))

    resolved = with_builtin_standalone_cua({})

    assert resolved["cua-computer-use"].config["command"] == str(executable)


def test_standalone_runtime_preserves_explicit_operator_config(
    monkeypatch, tmp_path,
):
    executable = tmp_path / "cua-driver"
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)
    explicit = _definition({
        "command": str(executable),
        "args": ["mcp"],
        "env": {"CUA_DRIVER_POLICY_FILE": "/managed/policy.yaml"},
        "startOnDemand": True,
    })

    resolved = with_builtin_standalone_cua({"cua-computer-use": explicit})

    assert resolved["cua-computer-use"] is explicit


def test_standalone_runtime_replaces_electron_embedded_config(
    monkeypatch,
    tmp_path,
):
    executable = tmp_path / "cua-driver"
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_CUA_DRIVER_PATH", str(executable))
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)
    monkeypatch.setattr("box_agent.tools.cua_runtime_config.sys.platform", "darwin")
    embedded = _definition({
        "command": "/electron/cua-driver",
        "args": [
            "mcp",
            "--embedded",
            "--socket",
            "/tmp/electron.sock",
            "--host-bundle-id",
            "com.example.desktop",
        ],
        "env": {"ELECTRON_ONLY": "1"},
        "startOnDemand": True,
    })

    resolved = with_builtin_standalone_cua({"cua-computer-use": embedded})

    definition = resolved["cua-computer-use"]
    assert definition.owner == "system"
    assert definition.config["command"] == str(executable)
    assert definition.config["args"] == ["mcp"]
    assert definition.config["boxAgentOwnedDaemon"] is False


def test_macos_standalone_uses_app_owned_daemon(monkeypatch, tmp_path):
    executable = tmp_path / "cua-driver"
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_CUA_DRIVER_PATH", str(executable))
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)
    monkeypatch.setattr("box_agent.tools.cua_runtime_config.sys.platform", "darwin")

    config = with_builtin_standalone_cua({})["cua-computer-use"].config

    assert config["args"] == ["mcp"]
    assert config["boxAgentOwnedDaemon"] is False


def test_standalone_runtime_forwards_cua_security_environment(monkeypatch, tmp_path):
    executable = tmp_path / "cua-driver"
    executable.write_text("binary", encoding="utf-8")
    monkeypatch.setenv("BOX_AGENT_CUA_DRIVER_PATH", str(executable))
    monkeypatch.setenv("CUA_DRIVER_POLICY_FILE", "/managed/policy.yaml")
    monkeypatch.setenv("CUA_DRIVER_PERMISSION_MODE", "bounded")
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)

    config = with_builtin_standalone_cua({})["cua-computer-use"].config

    assert config["env"]["CUA_DRIVER_POLICY_FILE"] == "/managed/policy.yaml"
    assert config["env"]["CUA_DRIVER_PERMISSION_MODE"] == "bounded"


def test_embedded_host_keeps_host_managed_cua_config(monkeypatch):
    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_MODE", "embedded")
    embedded = _definition({
        "command": "/runtime/cua-driver",
        "args": ["mcp", "--embedded", "--socket", "/tmp/live.sock"],
        "startOnDemand": True,
    })

    resolved = with_builtin_standalone_cua({"cua-computer-use": embedded})

    assert resolved["cua-computer-use"] is embedded


def test_standalone_runtime_respects_explicit_disable(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_CUA_RUNTIME_MODE", raising=False)
    disabled = _definition({
        "command": "cua-driver",
        "args": ["mcp", "--embedded", "--socket", "/tmp/electron.sock"],
        "disabled": True,
    })

    resolved = with_builtin_standalone_cua({"cua-computer-use": disabled})

    assert resolved["cua-computer-use"] is disabled


@pytest.mark.asyncio
async def test_ready_connection_is_reused_without_reconnect(tmp_path, monkeypatch):
    _configure(
        tmp_path,
        monkeypatch,
        ["mcp", "--socket", STANDALONE_CUA_SOCKET],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_status",
        lambda: [{"name": "cua-computer-use", "state": "connected"}],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_tools_for_server",
        lambda _name: [object(), object()],
    )

    async def unexpected_reconnect(_name):
        raise AssertionError("ready Cua connection must be reused")

    async def unexpected_daemon_start(_command, *, socket_path):
        raise AssertionError("ready Cua connection must not restart its daemon")

    monkeypatch.setattr(cua_runtime_tool, "reconnect_mcp_server", unexpected_reconnect)
    monkeypatch.setattr(
        cua_runtime_tool,
        "ensure_standalone_cua_daemon",
        unexpected_daemon_start,
    )

    result = await EnsureCuaReadyTool().execute()

    assert result.success is True
    assert result.raw_output == {
        "status": "ready",
        "mode": "standalone",
        "daemon_running": True,
        "mcp_connected": True,
        "tool_count": 2,
        "next_action": (
            "Use tool_search with server_name='cua-computer-use' to activate the "
            "desktop tools needed for the task."
        ),
    }


@pytest.mark.asyncio
async def test_standalone_mode_starts_daemon_before_connecting_proxy(tmp_path, monkeypatch):
    _configure(
        tmp_path,
        monkeypatch,
        ["mcp", "--socket", STANDALONE_CUA_SOCKET],
    )
    state = {"connected": False}
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_status",
        lambda: [{
            "name": "cua-computer-use",
            "state": "connected" if state["connected"] else "idle",
        }],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_tools_for_server",
        lambda _name: [object()] if state["connected"] else [],
    )

    async def reconnect(name):
        assert name == "cua-computer-use"
        state["connected"] = True
        return {"success": True}

    async def ensure_daemon(command, *, socket_path):
        assert command == "cua-driver"
        assert socket_path == STANDALONE_CUA_SOCKET
        return True, None

    monkeypatch.setattr(cua_runtime_tool, "reconnect_mcp_server", reconnect)
    monkeypatch.setattr(
        cua_runtime_tool,
        "ensure_standalone_cua_daemon",
        ensure_daemon,
    )

    result = await EnsureCuaReadyTool().execute()

    assert result.success is True
    assert result.raw_output["mode"] == "standalone"
    assert result.raw_output["tool_count"] == 1


@pytest.mark.asyncio
async def test_stale_ready_connection_is_reconnected(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch, ["mcp"], owns_daemon=False)
    state = {"reconnect_calls": 0}
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_status",
        lambda: [{"name": "cua-computer-use", "state": "connected"}],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_tools_for_server",
        lambda _name: [object()],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "is_mcp_server_connection_current",
        lambda _name: False,
    )

    async def reconnect(name):
        assert name == "cua-computer-use"
        state["reconnect_calls"] += 1
        return {"success": True}

    monkeypatch.setattr(cua_runtime_tool, "reconnect_mcp_server", reconnect)

    result = await EnsureCuaReadyTool().execute()

    assert result.success is True
    assert state["reconnect_calls"] == 1


@pytest.mark.asyncio
async def test_runtime_transition_stops_daemon_started_from_stale_config(
    tmp_path,
    monkeypatch,
):
    old_args = ["mcp", "--socket", STANDALONE_CUA_SOCKET]
    _configure(tmp_path, monkeypatch, old_args)
    old = _definition({
        "command": "cua-driver",
        "args": old_args,
        "boxAgentOwnedDaemon": True,
    })
    explicit = ResolvedMcpServer(
        name=old.name,
        config={"command": "cua-driver", "args": ["mcp"]},
        owner=old.owner,
        config_id=old.config_id,
        connector_id=old.connector_id,
        connector_name=old.connector_name,
        source_path=old.source_path,
        fingerprint="explicit",
    )
    daemon_probe_started = asyncio.Event()
    transition_finished = asyncio.Event()
    calls: list[str] = []

    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_status",
        lambda: [{"name": "cua-computer-use", "state": "idle"}],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_tools_for_server",
        lambda _name: [object()],
    )
    monkeypatch.setattr(
        mcp_loader,
        "_mcp_server_definitions",
        {"cua-computer-use": old},
    )
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", "/tmp/mcp.json")
    monkeypatch.setattr(
        mcp_loader,
        "_resolve_registered_sources",
        lambda _path: {"cua-computer-use": explicit},
    )

    async def reconnect_locked(_name):
        calls.append("connect")
        return {"success": True}

    async def ensure_daemon(command, *, socket_path):
        daemon_probe_started.set()
        await transition_finished.wait()
        monkeypatch.setattr(cua_runtime_daemon, "_owned_process", object())
        monkeypatch.setattr(cua_runtime_daemon, "_owned_command", command)
        monkeypatch.setattr(cua_runtime_daemon, "_owned_socket", socket_path)
        calls.append("start-stale")
        return True, None

    async def stop_daemon():
        calls.append("stop-stale")
        monkeypatch.setattr(cua_runtime_daemon, "_owned_process", None)
        monkeypatch.setattr(cua_runtime_daemon, "_owned_command", None)
        monkeypatch.setattr(cua_runtime_daemon, "_owned_socket", None)

    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", reconnect_locked)
    monkeypatch.setattr(cua_runtime_tool, "ensure_standalone_cua_daemon", ensure_daemon)
    monkeypatch.setattr(cua_runtime_daemon, "stop_standalone_cua_daemon", stop_daemon)

    ensure_task = asyncio.create_task(EnsureCuaReadyTool().execute())
    await daemon_probe_started.wait()
    await mcp_loader.reconnect_mcp_server("cua-computer-use")
    transition_finished.set()

    result = await ensure_task

    assert result.success is True
    assert calls == ["connect", "start-stale", "stop-stale", "connect"]
    assert cua_runtime_daemon.get_standalone_cua_daemon_runtime() is None


@pytest.mark.asyncio
async def test_standalone_mode_does_not_connect_proxy_when_daemon_fails(
    tmp_path, monkeypatch,
):
    _configure(
        tmp_path,
        monkeypatch,
        ["mcp", "--socket", STANDALONE_CUA_SOCKET],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_status",
        lambda: [{"name": "cua-computer-use", "state": "idle"}],
    )
    monkeypatch.setattr(cua_runtime_tool, "get_mcp_tools_for_server", lambda _name: [])

    async def fail_daemon(_command, *, socket_path):
        assert socket_path == STANDALONE_CUA_SOCKET
        return False, "daemon failed"

    async def unexpected_reconnect(_name):
        raise AssertionError("MCP proxy must not connect before the daemon is ready")

    monkeypatch.setattr(
        cua_runtime_tool,
        "ensure_standalone_cua_daemon",
        fail_daemon,
    )
    monkeypatch.setattr(cua_runtime_tool, "reconnect_mcp_server", unexpected_reconnect)

    result = await EnsureCuaReadyTool().execute()

    assert result.success is False
    assert result.error == "daemon failed"
    assert result.raw_output["daemon_running"] is False
    assert result.raw_output["mcp_connected"] is False


@pytest.mark.asyncio
async def test_embedded_mode_does_not_fallback_when_host_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_MODE", "embedded")
    _configure(
        tmp_path,
        monkeypatch,
        ["mcp", "--embedded", "--socket", "/private/runtime.sock"],
    )
    monkeypatch.setattr(cua_runtime_tool, "get_mcp_status", lambda: [])
    monkeypatch.setattr(cua_runtime_tool, "get_mcp_tools_for_server", lambda _name: [])

    async def reconnect(_name):
        return {"success": False, "error": "no daemon listening on embedded socket"}

    monkeypatch.setattr(cua_runtime_tool, "reconnect_mcp_server", reconnect)

    result = await EnsureCuaReadyTool().execute()

    assert result.success is False
    assert result.raw_output["status"] == "host_unavailable"
    assert result.raw_output["mode"] == "app-hosted"


@pytest.mark.asyncio
async def test_concurrent_calls_start_runtime_once(tmp_path, monkeypatch):
    _configure(
        tmp_path,
        monkeypatch,
        ["mcp", "--socket", STANDALONE_CUA_SOCKET],
    )
    state = {"connected": False, "daemon_calls": 0, "reconnect_calls": 0}
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_status",
        lambda: [{
            "name": "cua-computer-use",
            "state": "connected" if state["connected"] else "idle",
        }],
    )
    monkeypatch.setattr(
        cua_runtime_tool,
        "get_mcp_tools_for_server",
        lambda _name: [object()] if state["connected"] else [],
    )

    async def reconnect(_name):
        state["reconnect_calls"] += 1
        await asyncio.sleep(0)
        state["connected"] = True
        return {"success": True}

    async def ensure_daemon(_command, *, socket_path):
        assert socket_path == STANDALONE_CUA_SOCKET
        state["daemon_calls"] += 1
        await asyncio.sleep(0)
        return True, None

    monkeypatch.setattr(cua_runtime_tool, "reconnect_mcp_server", reconnect)
    monkeypatch.setattr(
        cua_runtime_tool,
        "ensure_standalone_cua_daemon",
        ensure_daemon,
    )

    first, second = await asyncio.gather(
        EnsureCuaReadyTool().execute(),
        EnsureCuaReadyTool().execute(),
    )

    assert first.success is True
    assert second.success is True
    assert state["daemon_calls"] == 1
    assert state["reconnect_calls"] == 1


@pytest.mark.asyncio
async def test_mcp_cleanup_stops_owned_standalone_daemon(monkeypatch):
    calls = 0

    async def stop_daemon():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(cua_runtime_daemon, "stop_standalone_cua_daemon", stop_daemon)
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [])

    await mcp_loader.cleanup_mcp_connections()

    assert calls == 1


@pytest.mark.asyncio
async def test_disabling_cua_stops_owned_standalone_daemon(monkeypatch):
    calls = 0

    async def stop_daemon():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(cua_runtime_daemon, "stop_standalone_cua_daemon", stop_daemon)
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [])

    await mcp_loader.disconnect_mcp_server("cua-computer-use")

    assert calls == 1


@pytest.mark.asyncio
async def test_replacing_owned_runtime_stops_daemon_before_explicit_config(
    monkeypatch,
):
    owned = _definition({
        "command": "/runtime/cua-driver",
        "args": ["mcp", "--socket", "/tmp/owned.sock"],
        "boxAgentOwnedDaemon": True,
    })
    explicit = _definition({
        "command": "/operator/cua-driver",
        "args": ["mcp"],
        "env": {"CUA_DRIVER_POLICY_FILE": "/operator/policy.yaml"},
    })
    explicit = ResolvedMcpServer(
        name=explicit.name,
        config=explicit.config,
        owner=explicit.owner,
        config_id=explicit.config_id,
        connector_id=explicit.connector_id,
        connector_name=explicit.connector_name,
        source_path=explicit.source_path,
        fingerprint="explicit",
    )
    calls: list[str] = []

    async def stop_daemon():
        calls.append("stop")

    async def reconnect_locked(name):
        calls.append(f"reconnect:{name}")
        return {"success": True}

    monkeypatch.setattr(cua_runtime_daemon, "stop_standalone_cua_daemon", stop_daemon)
    monkeypatch.setattr(
        cua_runtime_daemon,
        "get_standalone_cua_daemon_runtime",
        lambda: ("/runtime/cua-driver", "/tmp/owned.sock"),
    )
    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", reconnect_locked)
    monkeypatch.setattr(
        mcp_loader,
        "_resolve_registered_sources",
        lambda _path: {"cua-computer-use": explicit},
    )
    monkeypatch.setattr(
        mcp_loader,
        "_mcp_server_definitions",
        {"cua-computer-use": owned},
    )
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", "/tmp/mcp.json")

    result = await mcp_loader.reconnect_mcp_server("cua-computer-use")

    assert result["success"] is True
    assert calls == ["stop", "reconnect:cua-computer-use"]
    assert mcp_loader._mcp_server_definitions["cua-computer-use"] is explicit


@pytest.mark.asyncio
async def test_initial_mcp_load_leaves_start_on_demand_server_idle(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cua-computer-use": {
                        "command": "cua-driver",
                        "args": ["mcp"],
                        "startOnDemand": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    catalog = mcp_tool_catalog.MCPToolCatalog()
    monkeypatch.setattr(mcp_tool_catalog, "_CATALOG", catalog)
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [])
    monkeypatch.setattr(mcp_loader, "_mcp_status", {})
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", None)

    async def unexpected_connect(_self):
        raise AssertionError("startOnDemand server must not connect during startup")

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", unexpected_connect)

    tools = await mcp_loader.load_mcp_tools_async(str(config_path))

    assert tools == []
    status = mcp_loader.get_mcp_status()
    assert len(status) == 1
    assert status[0] | {"sourcePath": "<runtime>"} == {
        "name": "cua-computer-use",
        "owner": "user",
        "configId": "custom-mcp:cua-computer-use",
        "connectorId": None,
        "connectorName": None,
        "sourcePath": "<runtime>",
        "state": "idle",
        "transport": "cua-driver",
        "toolCount": 0,
        "tools": [],
        "error": None,
        "authStatus": None,
    }


def test_get_mcp_server_config_refreshes_config_written_after_startup(
    tmp_path,
    monkeypatch,
):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", str(config_path))
    previous = _definition({
        "command": "cua-driver",
        "args": ["mcp", "--socket", "/tmp/stale.sock"],
        "startOnDemand": True,
    })
    monkeypatch.setattr(
        mcp_loader,
        "_mcp_server_definitions",
        {"cua-computer-use": previous},
    )

    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "cua-computer-use": {
                        "command": "cua-driver",
                        "args": ["mcp"],
                        "startOnDemand": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert mcp_loader.get_mcp_server_config("cua-computer-use") == {
        "command": "cua-driver",
        "args": ["mcp"],
        "startOnDemand": True,
    }
    assert mcp_loader._mcp_server_definitions["cua-computer-use"] is previous
