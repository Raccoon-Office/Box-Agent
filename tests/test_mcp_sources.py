from __future__ import annotations

import json
from pathlib import Path

import pytest

from box_agent.tools import mcp_loader
from box_agent.tools.mcp_sources import McpConfigSource, configured_mcp_sources, resolve_mcp_sources


def _write(path: Path, servers: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_configured_sources_keep_standalone_single_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BOX_AGENT_USER_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)

    sources = configured_mcp_sources(str(tmp_path / "mcp.json"))

    assert [(source.owner, source.path) for source in sources] == [
        ("user", tmp_path / "mcp.json")
    ]


def test_resolve_sources_preserves_owner_and_blocks_user_shadowing(
    monkeypatch, tmp_path: Path
) -> None:
    system = tmp_path / "mcp.system.json"
    connector = tmp_path / "connector" / "mcp.json"
    user = tmp_path / "mcp.json"
    _write(system, {"playwright": {"command": "pw"}})
    _write(
        connector,
        {
            "law": {
                "url": "https://example.test/mcp",
                "_connectorId": "pkulaw",
                "_connectorName": "北大法宝",
                "credentialRef": "connector:pkulaw:default",
            }
        },
    )
    _write(user, {"playwright": {"command": "shadow"}, "mine": {"command": "mine"}})
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
    monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(system))
    monkeypatch.setenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", str(connector))

    resolved = resolve_mcp_sources(configured_mcp_sources(str(user)))

    assert resolved.servers["playwright"].owner == "system"
    assert resolved.servers["law"].config_id == "connector:pkulaw"
    assert resolved.servers["law"].connector_id == "pkulaw"
    assert resolved.servers["law"].connector_name == "北大法宝"
    assert resolved.servers["mine"].config_id == "custom-mcp:mine"
    assert resolved.conflicts == ("user:playwright conflicts with system:playwright",)


def test_credential_version_participates_in_server_fingerprint(tmp_path: Path) -> None:
    connector = tmp_path / "mcp.json"
    _write(
        connector,
        {"law": {"url": "https://example.test", "credentialRef": "law-token", "_connectorId": "law"}},
    )
    sources = (McpConfigSource("connector", connector),)

    before = resolve_mcp_sources(sources, {"law-token": 1}).servers["law"]
    after = resolve_mcp_sources(sources, {"law-token": 2}).servers["law"]

    assert before.fingerprint != after.fingerprint


def test_reserved_official_name_is_rejected_even_when_connector_is_disconnected(
    tmp_path: Path,
) -> None:
    user = tmp_path / "mcp.json"
    _write(user, {"pkulaw": {"url": "https://evil.test/mcp"}})

    resolved = resolve_mcp_sources(
        (configured_mcp_sources(str(user))[0],),
        reserved_names={"pkulaw"},
    )

    assert "pkulaw" not in resolved.servers
    assert resolved.conflicts == ("user:pkulaw uses a protected server name",)


def test_connector_source_requires_a_normalized_connector_id(tmp_path: Path) -> None:
    connector = tmp_path / "connector" / "mcp.json"
    _write(connector, {"law": {"url": "https://example.test/mcp", "_connectorId": " PKULAW "}})

    source = McpConfigSource("connector", connector)
    resolved = resolve_mcp_sources((source,))
    assert resolved.servers["law"].connector_id == "pkulaw"

    _write(connector, {"law": {"url": "https://example.test/mcp", "_connectorId": "bad.id"}})
    with pytest.raises(ValueError, match="invalid _connectorId"):
        resolve_mcp_sources((source,))


def test_runtime_connector_source_override_does_not_require_a_physical_file(
    tmp_path: Path,
) -> None:
    user = tmp_path / "mcp.json"
    _write(user, {"mine": {"command": "mine"}})
    connector = McpConfigSource("connector", Path("<runtime:connector>"))

    resolved = resolve_mcp_sources(
        (connector, McpConfigSource("user", user)),
        source_server_overrides={
            "connector": {
                "law": {
                    "url": "https://example.test/mcp",
                    "_connectorId": "pkulaw",
                    "_connectorName": "北大法宝",
                }
            }
        },
    )

    assert resolved.servers["law"].owner == "connector"
    assert resolved.servers["law"].source_path == "<runtime:connector>"
    assert resolved.servers["mine"].owner == "user"


def test_loader_registers_runtime_connector_source_without_connector_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "mcp.json"
    _write(user, {"mine": {"command": "mine"}})
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
    monkeypatch.delenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_RESERVED_MCP_SERVER_NAMES", raising=False)
    monkeypatch.setattr(
        mcp_loader,
        "_mcp_source_overrides",
        {
            "connector": {
                "law": {
                    "url": "https://example.test/mcp",
                    "_connectorId": "pkulaw",
                }
            }
        },
    )

    resolved = mcp_loader._resolve_registered_sources(str(user))

    assert resolved["law"].owner == "connector"
    assert resolved["law"].source_path == "<runtime:connector>"
    assert resolved["mine"].owner == "user"


@pytest.mark.asyncio
async def test_replace_runtime_connector_source_reconciles_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_loader, "_mcp_source_overrides", {})
    captured: list[str | None] = []

    async def reconcile(source: str | None) -> dict:
        captured.append(source)
        return {"success": True, "source": source, "results": []}

    monkeypatch.setattr(mcp_loader, "_reconcile_mcp_sources_locked", reconcile)

    result = await mcp_loader.replace_mcp_source(
        "connector",
        {
            "mcpServers": {
                "law": {
                    "url": "https://example.test/mcp",
                    "_connectorId": "pkulaw",
                }
            }
        },
    )

    assert result["success"] is True
    assert captured == ["connector"]
    assert set(mcp_loader._mcp_source_overrides["connector"]) == {"law"}


@pytest.fixture
def isolated_connector_runtime(monkeypatch, tmp_path):
    import asyncio

    user = tmp_path / "mcp.json"
    _write(user, {})
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
    monkeypatch.delenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    monkeypatch.setattr(mcp_loader, "_mcp_config_path", str(user))
    for name in ("_mcp_server_definitions", "_mcp_source_overrides", "_mcp_runtime_credentials",
                 "_mcp_runtime_credential_versions", "_mcp_status", "_mcp_reconnect_locks"):
        monkeypatch.setattr(mcp_loader, name, {})
    monkeypatch.setattr(mcp_loader, "_mcp_connections", [])
    monkeypatch.setattr(mcp_loader, "_mcp_loading", False)
    monkeypatch.setattr(mcp_loader, "_mcp_source_reconcile_lock", asyncio.Lock())
    return {
        "mcpServers": {
            "law": {"url": "https://law.test/mcp", "_connectorId": "pkulaw", "credentialRef": "law-token"},
            "oauth": {"url": "https://oauth.test/mcp", "_connectorId": "qixin"},
        }
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["connector", "system", "user"])
@pytest.mark.parametrize("retry_succeeds", [True, False])
async def test_unchanged_source_retries_failed_servers_without_restarting_healthy_servers(
    monkeypatch, tmp_path, isolated_connector_runtime, source, retry_succeeds,
):
    attempts = {"retry-server": 0, "healthy-server": 0}
    config = {
        "mcpServers": {
            "retry-server": {"url": "https://retry.test/mcp", "_connectorId": "qixin"},
            "healthy-server": {"url": "https://healthy.test/mcp", "_connectorId": "pkulaw"},
        },
    }

    async def connect(connection):
        attempts[connection.name] += 1
        return connection.name == "healthy-server" or (
            retry_succeeds and attempts[connection.name] > 1
        )

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    if source == "system":
        system_path = tmp_path / "mcp.system.json"
        _write(system_path, config["mcpServers"])
        monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(system_path))
    elif source == "user":
        _write(Path(mcp_loader._mcp_config_path), config["mcpServers"])

    async def reconcile():
        if source == "connector":
            return await mcp_loader.replace_mcp_source(source, config)
        return await mcp_loader.reconcile_mcp_sources(source)

    first = await reconcile()
    assert first["success"] is False
    assert attempts == {"retry-server": 1, "healthy-server": 1}

    second = await reconcile()
    assert attempts == {"retry-server": 2, "healthy-server": 1}
    assert second["success"] is retry_succeeds
    assert mcp_loader._mcp_status["retry-server"].state == (
        "connected" if retry_succeeds else "failed"
    )
    if retry_succeeds:
        assert (await reconcile())["success"] is True
        assert attempts == {"retry-server": 2, "healthy-server": 1}


@pytest.mark.asyncio
async def test_late_credential_is_not_acknowledged_by_another_server_reconnect(
    monkeypatch, isolated_connector_runtime,
):
    attempts = []

    async def connect(name):
        attempts.append(name)
        if name == "law" and attempts.count("law") == 1:
            # Credential arrives while the original connection is failing.
            mcp_loader.set_mcp_runtime_credential("law-token", {"Authorization": "Bearer test"})
            return {"success": False}
        return {"success": True}

    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", connect)
    await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime)
    await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime)
    assert attempts == ["law", "oauth", "law"]


@pytest.mark.asyncio
async def test_scoped_oauth_update_does_not_publish_or_reconnect_other_connectors(
    monkeypatch, isolated_connector_runtime,
):
    attempts = []

    async def connect(name):
        attempts.append(name)
        return {"success": True}

    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", connect)
    await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime, ["qixin"])
    assert attempts == ["oauth"]
    assert set(mcp_loader._mcp_source_overrides["connector"]) == {"oauth"}
    await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime, ["pkulaw"])
    assert attempts == ["oauth", "law"]
    assert set(mcp_loader._mcp_source_overrides["connector"]) == {"oauth", "law"}
    await mcp_loader.replace_mcp_source("connector", {"mcpServers": {}}, ["pkulaw"])
    assert set(mcp_loader._mcp_source_overrides["connector"]) == {"oauth"}


@pytest.mark.asyncio
async def test_missing_credential_waits_without_sending_an_unauthenticated_request(
    monkeypatch, isolated_connector_runtime,
):
    from unittest.mock import AsyncMock, Mock

    build_connection = mcp_loader._build_connection
    build = Mock(side_effect=AssertionError("must not create a transport without credentials"))
    monkeypatch.setattr(mcp_loader, "_build_connection", build)
    result = await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime, ["pkulaw"])
    assert result["results"][0]["waitingForCredential"] is True
    assert not build.called
    assert mcp_loader.get_mcp_status()[0]["state"] == "connecting"

    # The same source must become connectable when its credential arrives.
    monkeypatch.setattr(mcp_loader, "_build_connection", build_connection)
    connect = AsyncMock(return_value=True)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    mcp_loader.set_mcp_runtime_credential("law-token", {"Authorization": "Bearer saved"})
    result = await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime, ["pkulaw"])
    assert result["success"] is True
    connect.assert_awaited_once()
    assert mcp_loader.get_mcp_status()[0]["state"] == "connected"


@pytest.mark.asyncio
async def test_scoped_source_rejects_invalid_connector_filter(isolated_connector_runtime):
    result = await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime, [])
    assert result["success"] is False
    assert mcp_loader._mcp_source_overrides == {}


@pytest.mark.asyncio
async def test_slow_connector_does_not_delay_starting_other_ready_connectors(
    monkeypatch, isolated_connector_runtime,
):
    import asyncio

    other_started = asyncio.Event()

    async def connect(name):
        if name == "law":
            await asyncio.wait_for(other_started.wait(), timeout=1)
        else:
            other_started.set()
        return {"success": True}

    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", connect)
    result = await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime)
    assert result["success"] is True


@pytest.mark.asyncio
async def test_cold_load_waits_for_required_credentials_before_creating_transport(
    monkeypatch, isolated_connector_runtime,
):
    from unittest.mock import Mock

    monkeypatch.setattr(mcp_loader, "_mcp_source_overrides", {
        "connector": {"law": isolated_connector_runtime["mcpServers"]["law"]},
    })
    build = Mock(side_effect=AssertionError("unauthenticated transport must not be created"))
    monkeypatch.setattr(mcp_loader, "_build_connection", build)
    assert await mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path) == []
    assert not build.called
    assert mcp_loader.get_mcp_status()[0]["error"] == "Waiting for host credential"


@pytest.mark.asyncio
async def test_scoped_replacement_uses_normalized_connector_identity(
    monkeypatch, isolated_connector_runtime,
):
    from unittest.mock import AsyncMock

    connect = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", connect)
    isolated_connector_runtime["mcpServers"]["law"]["_connectorId"] = " PKULAW "
    result = await mcp_loader.replace_mcp_source(
        "connector", isolated_connector_runtime, [" PKULAW "],
    )
    assert result["success"] is True
    connect.assert_awaited_once_with("law")
    await mcp_loader.replace_mcp_source("connector", {"mcpServers": {}}, ["pkulaw"])
    assert mcp_loader._mcp_source_overrides["connector"] == {}


@pytest.mark.asyncio
async def test_connector_update_does_not_acknowledge_or_apply_pending_user_source_changes(
    monkeypatch, isolated_connector_runtime,
):
    from unittest.mock import AsyncMock

    user_path = Path(mcp_loader._mcp_config_path)
    _write(user_path, {"mine": {"command": "before"}})
    monkeypatch.setattr(
        mcp_loader, "_mcp_server_definitions",
        mcp_loader._resolve_registered_sources(str(user_path)),
    )
    _write(user_path, {"mine": {"command": "after"}})
    connect = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", connect)
    await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime, ["qixin"])
    connect.assert_awaited_once_with("oauth")
    assert mcp_loader._mcp_server_definitions["mine"].config["command"] == "before"
    await mcp_loader.reconcile_mcp_sources("user")
    assert connect.await_args_list[-1].args == ("mine",)
    assert mcp_loader._mcp_server_definitions["mine"].config["command"] == "after"


@pytest.mark.parametrize("owner", ["user", "system"])
def test_non_connector_sources_cannot_reference_runtime_credentials(tmp_path, monkeypatch, owner):
    from dataclasses import replace

    path = tmp_path / "mcp.json"
    _write(path, {"law": {
        "url": "https://untrusted.invalid/mcp",
        "_connectorId": "law",
        "credentialRef": "connector:law:default",
    }})
    monkeypatch.setattr(mcp_loader, "_mcp_runtime_credentials", {
        "connector:law:default": {"Authorization": "Bearer test-sentinel"},
    })
    definition = resolve_mcp_sources((McpConfigSource("connector", path),)).servers["law"]
    assert mcp_loader._materialize_server_config(definition)["headers"] == {
        "Authorization": "Bearer test-sentinel",
    }
    with pytest.raises(ValueError, match="connector-owned source"):
        resolve_mcp_sources((McpConfigSource(owner, path),))
    # Older or programmatically constructed definitions cannot bypass parsing.
    with pytest.raises(ValueError, match="connector-owned source"):
        mcp_loader._materialize_server_config(replace(definition, owner=owner))


@pytest.mark.asyncio
@pytest.mark.parametrize("targets", [None, ["law"]])
async def test_connector_removal_revokes_live_owner_before_user_fallback(
    monkeypatch, isolated_connector_runtime, targets,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from tests.test_mcp_tool_search import FakeMCPTool

    user = Path(mcp_loader._mcp_config_path)
    _write(user, {"shared": {"url": "https://user.example.test/mcp"}})
    mcp_loader._mcp_source_overrides["connector"] = {
        "shared": {"url": "https://connector.example.test/mcp", "_connectorId": "law"},
    }
    mcp_loader._mcp_server_definitions = mcp_loader._resolve_registered_sources(str(user))
    tool = FakeMCPTool("law_search", "shared", connector_id="law")
    catalog = MCPToolCatalog()
    catalog.replace_server("shared", [tool])
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    connection = SimpleNamespace(name="shared", tools=[tool], disconnect=AsyncMock())
    mcp_loader._mcp_connections = [connection]
    mcp_loader._record_status("shared", "connected")
    reconnect = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", reconnect)

    result = await mcp_loader.replace_mcp_source("connector", {"mcpServers": {}}, targets)

    assert result["success"] is True
    assert result["results"][0]["action"] == "removed"
    connection.disconnect.assert_awaited_once()
    reconnect.assert_not_awaited()
    assert mcp_loader._mcp_connections == []
    assert "shared" not in mcp_loader._mcp_server_definitions
    assert catalog.snapshot() == ()
    assert mcp_loader._mcp_status["shared"].state == "disabled"
    # The shadowed source is only activated by its own explicit reconciliation.
    await mcp_loader.reconcile_mcp_sources("user")
    reconnect.assert_awaited_once_with("shared")
    assert mcp_loader._mcp_server_definitions["shared"].owner == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_override", [True, False])
async def test_scoped_connector_update_cannot_take_another_connectors_server_name(
    monkeypatch, tmp_path, isolated_connector_runtime, runtime_override,
):
    from copy import deepcopy
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    existing = {"shared": {"url": "https://other.example.test/mcp", "_connectorId": "other"}}
    if runtime_override:
        mcp_loader._mcp_source_overrides["connector"] = existing
    else:
        connector = tmp_path / "connector.json"
        _write(connector, existing)
        monkeypatch.setenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", str(connector))
    mcp_loader._mcp_server_definitions = mcp_loader._resolve_registered_sources(mcp_loader._mcp_config_path)
    before = dict(mcp_loader._mcp_server_definitions)
    overrides = deepcopy(mcp_loader._mcp_source_overrides)
    connection = SimpleNamespace(name="shared", tools=[], disconnect=AsyncMock())
    mcp_loader._mcp_connections = [connection]
    mcp_loader._record_status("shared", "connected")
    reconnect = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(mcp_loader, "_reconnect_mcp_server_locked", reconnect)

    result = await mcp_loader.replace_mcp_source("connector", {"mcpServers": {
        "shared": {"url": "https://law.example.test/mcp", "_connectorId": "law"},
    }}, ["law"])

    assert result["success"] is False
    assert "outside connectorIds" in result["error"]
    assert mcp_loader._mcp_source_overrides == overrides
    assert mcp_loader._mcp_server_definitions == before
    assert mcp_loader._mcp_status["shared"].state == "connected"
    assert mcp_loader._mcp_connections == [connection]
    connection.disconnect.assert_not_awaited()
    reconnect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("disable", [False, True], ids=["remove", "disable"])
async def test_source_revocation_waits_for_reconnect_without_blocking_other_servers(
    monkeypatch, isolated_connector_runtime, disable,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from box_agent.acp import _connected_connector_ids
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from tests.test_mcp_tool_search import FakeMCPTool

    server = {"url": "https://law.example.test/mcp", "_connectorId": "law"}
    mcp_loader._mcp_source_overrides["connector"] = {"law": server}
    mcp_loader._mcp_server_definitions = mcp_loader._resolve_registered_sources(mcp_loader._mcp_config_path)
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    started, finish, other_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def connect():
        started.set()
        await finish.wait()
        return True

    async def connect_other():
        other_started.set()
        return True

    connection = SimpleNamespace(name="law", connect=connect, disconnect=AsyncMock(), tools=[
        FakeMCPTool("legal_lookup", "law", connector_id="law"),
    ])
    other = SimpleNamespace(name="other", connect=connect_other, disconnect=AsyncMock(), tools=[])
    monkeypatch.setattr(mcp_loader, "_build_connection", lambda definition: connection if definition.name == "law" else other)
    reconnect = asyncio.create_task(mcp_loader.reconnect_mcp_server("law"))
    await asyncio.wait_for(started.wait(), timeout=1)
    replacement = {"other": {"url": "https://other.example.test/mcp", "_connectorId": "other"}}
    if disable:
        replacement["law"] = {**server, "disabled": True}
    revoke = asyncio.create_task(mcp_loader.replace_mcp_source("connector", {"mcpServers": replacement}))
    try:
        await asyncio.wait_for(other_started.wait(), timeout=1)
        assert not revoke.done()
    finally:
        finish.set()
        update, _ = await asyncio.wait_for(asyncio.gather(revoke, reconnect), timeout=5)
    assert update["success"] is True
    assert mcp_loader._mcp_connections == [other]
    assert catalog.snapshot() == ()
    connection.disconnect.assert_awaited_once()
    other.disconnect.assert_not_awaited()
    assert mcp_loader._mcp_status["law"].state == "disabled"
    assert _connected_connector_ids({"law", "other"}) == frozenset({"other"})
    if disable:
        assert mcp_loader._mcp_server_definitions["law"].config["disabled"] is True
    else:
        assert "law" not in mcp_loader._mcp_server_definitions


@pytest.mark.asyncio
async def test_partial_connector_removal_keeps_remaining_healthy_server_available(
    monkeypatch, isolated_connector_runtime,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from box_agent.acp import _connected_connector_ids, _connector_status_context

    servers = {name: {"url": f"https://{name}.example.test/mcp", "_connectorId": "law"}
               for name in ("removed", "kept")}
    mcp_loader._mcp_source_overrides["connector"] = servers
    mcp_loader._mcp_server_definitions = mcp_loader._resolve_registered_sources(mcp_loader._mcp_config_path)
    connections = [SimpleNamespace(name=name, tools=[], disconnect=AsyncMock()) for name in servers]
    mcp_loader._mcp_connections = list(connections)
    for name in servers:
        mcp_loader._record_status(name, "connected")
    result = await mcp_loader.replace_mcp_source("connector", {"mcpServers": {"kept": servers["kept"]}}, ["law"])
    assert result["success"] is True
    assert mcp_loader._mcp_status["removed"].state == "disabled"
    assert mcp_loader._mcp_connections == [connections[1]]
    assert _connected_connector_ids({"law"}) == frozenset({"law"})
    assert _connector_status_context({"law"}) == "<connector-status>\nlaw law: connected\n</connector-status>"
    del mcp_loader._mcp_status["kept"]
    assert _connected_connector_ids({"law"}) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "reconcile"])
@pytest.mark.parametrize("loader_entered", [False, True], ids=["pre-gate", "cold-connect"])
async def test_source_update_does_not_mutate_state_when_startup_readiness_times_out(
    monkeypatch, isolated_connector_runtime, operation, loader_entered,
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    monkeypatch.setattr(mcp_loader, "_mcp_loading", loader_entered)
    wait = AsyncMock(return_value=False)
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: SimpleNamespace(initial_loading=True, wait_until_ready=wait))
    if operation == "replace":
        result = await mcp_loader.replace_mcp_source("connector", isolated_connector_runtime)
    else:
        result = await mcp_loader.reconcile_mcp_sources("connector")
    assert result["success"] is False
    assert "startup is still loading" in result["error"]
    wait.assert_awaited_once()
    assert mcp_loader._mcp_source_overrides == {}
    assert mcp_loader._mcp_server_definitions == {}
    assert mcp_loader._mcp_connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove", "disable", "replace"])
async def test_source_update_waits_for_cold_discovery_before_replacing_its_connections(
    monkeypatch, isolated_connector_runtime, operation,
):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from tests.test_mcp_tool_search import FakeMCPTool

    server = {"url": "https://old.example.test/mcp", "_connectorId": "law"}
    mcp_loader._mcp_source_overrides["connector"] = {"law": server}
    monkeypatch.setattr(mcp_loader, "_mcp_sources", ())
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    started, finish, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    wait_until_ready = catalog.wait_until_ready

    async def wait(**kwargs):
        waiting.set()
        return await wait_until_ready(**kwargs)

    monkeypatch.setattr(catalog, "wait_until_ready", wait)

    async def connect_old():
        started.set()
        await finish.wait()
        return True

    old = SimpleNamespace(name="law", url=server["url"], command=None, connect=connect_old,
                          disconnect=AsyncMock(), tools=[FakeMCPTool("old_lookup", "law", connector_id="law")])
    new = SimpleNamespace(name="law", url="https://new.example.test/mcp", command=None,
                          connect=AsyncMock(return_value=True), disconnect=AsyncMock(),
                          tools=[FakeMCPTool("new_lookup", "law", connector_id="law")])
    monkeypatch.setattr(mcp_loader, "_build_connection", lambda definition: old if definition.config["url"] == old.url else new)
    cold = asyncio.create_task(mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path))
    await asyncio.wait_for(started.wait(), timeout=1)
    replacement = {} if operation == "remove" else {"law": {
        **server, **({"disabled": True} if operation == "disable" else {"url": new.url}),
    }}
    update = asyncio.create_task(mcp_loader.replace_mcp_source("connector", {"mcpServers": replacement}, ["law"]))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert not update.done()
        assert mcp_loader._mcp_source_overrides["connector"] == {"law": server}
    finally:
        finish.set()
        _, result = await asyncio.wait_for(asyncio.gather(cold, update), timeout=5)
    assert result["success"] is True
    old.disconnect.assert_awaited_once()
    if operation == "replace":
        assert mcp_loader._mcp_connections == [new]
        assert [entry.model_name for entry in catalog.snapshot()] == ["new_lookup"]
    else:
        assert mcp_loader._mcp_connections == []
        assert catalog.snapshot() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replace", "reconcile"])
async def test_source_update_checks_startup_after_acquiring_the_source_lock(
    monkeypatch, isolated_connector_runtime, operation,
):
    import asyncio
    from unittest.mock import AsyncMock
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog

    catalog = MCPToolCatalog()
    wait = AsyncMock(return_value=False)
    monkeypatch.setattr(catalog, "wait_until_ready", wait)
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    lock = mcp_loader._mcp_source_reconcile_lock
    await lock.acquire()
    call = (mcp_loader.replace_mcp_source("connector", isolated_connector_runtime)
            if operation == "replace" else mcp_loader.reconcile_mcp_sources("connector"))
    update = asyncio.create_task(call)
    try:
        await asyncio.sleep(0)
        catalog.mark_loading()
    finally:
        lock.release()
    result = await asyncio.wait_for(update, timeout=1)
    assert not result["success"]
    wait.assert_awaited_once()
    assert mcp_loader._mcp_source_overrides == {}
    assert mcp_loader._mcp_server_definitions == {}
