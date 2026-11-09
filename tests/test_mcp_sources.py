from __future__ import annotations

import json
from pathlib import Path

import pytest

from box_agent.tools import mcp_loader, mcp_tool_catalog
from box_agent.tools.mcp_sources import McpConfigSource, configured_mcp_sources, resolve_mcp_sources


@pytest.fixture(autouse=True)
def isolated_tool_catalog(monkeypatch):
    # Discovery readiness belongs to this test's event loop, not a previous file.
    monkeypatch.setattr(mcp_tool_catalog, "_CATALOG", mcp_tool_catalog.MCPToolCatalog())
    # A previous session's managed CUA registration must not become a source here.
    monkeypatch.setattr(mcp_loader, "_cua_service", None)


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


@pytest.mark.parametrize("private_mode", [None, "false", "true"])
@pytest.mark.parametrize("owner,url,keep_private", [
    ("system", "https://xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp", False),
    ("system", "https://code-test.xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp", False),
    ("system", "http://10.158.136.99/api/web/mcp/web_search/v1/mcp", False),
    ("system", "https://xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp/?lang=zh", False),
    ("system", "https://tenant.example/office/mcp/search", True),
    ("system", "https://xiaohuanxiong.com/customer/search", True),
    ("user", "https://xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp", True),
    ("user", "https://tenant.example/office/mcp/search", True),
    ("user", "https://customer.example/custom-mcp", True),
    ("connector", "https://xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp", True),
    ("connector", "https://tenant.example/office/mcp/search", True),
    ("connector", "https://customer.example/custom-mcp", True),
])
def test_private_search_filter_preserves_explicit_service_sources(
    tmp_path: Path, monkeypatch, private_mode, owner, url, keep_private,
) -> None:
    if private_mode is None:
        monkeypatch.delenv("RACCOON_PRIVATE_DEPLOYMENT", raising=False)
    else:
        monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", private_mode)
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(tmp_path / "user.json"))
    monkeypatch.delenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_RESERVED_MCP_SERVER_NAMES", raising=False)
    monkeypatch.setattr(mcp_loader, "_mcp_source_overrides", {})
    monkeypatch.setattr(mcp_loader, "_mcp_sources", ())
    source = tmp_path / f"{owner}.json"
    if owner != "user":
        monkeypatch.setenv(f"BOX_AGENT_{owner.upper()}_MCP_CONFIG_PATH", str(source))
    search = {"url": url, "type": "streamable_http", "headers": {"X-Test": "kept"}}
    other = {"command": "fixture-mcp"}
    if owner == "connector":
        search["_connectorId"] = "customer-search"
        other["_connectorId"] = "customer-tool"
    _write(source, {mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME: search, "other": other})

    resolved = mcp_loader._resolve_registered_sources(str(tmp_path / "user.json"))

    assert (mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME in resolved) is (
        private_mode != "true" or keep_private
    )
    assert resolved["other"].config == other
    if mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME in resolved:
        entry = resolved[mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME]
        assert entry.owner == owner
        assert entry.config == search


def test_private_search_with_explicit_official_host_boundary_is_preserved(
    tmp_path: Path, monkeypatch,
) -> None:
    system = tmp_path / "system.json"
    url = "https://tenant.xiaohuanxiong.com/api/web/mcp/web_search/v1/mcp"
    _write(system, {mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME: {"url": url}})
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.xiaohuanxiong.com")
    monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(system))
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(tmp_path / "user.json"))
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    monkeypatch.setattr(mcp_loader, "_mcp_source_overrides", {})
    monkeypatch.setattr(mcp_loader, "_mcp_sources", ())

    resolved = mcp_loader._resolve_registered_sources(str(tmp_path / "user.json"))

    assert resolved[mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME].config["url"] == url


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
    for name in ("_mcp_server_definitions", "_mcp_source_overrides", "_mcp_runtime_credentials", "_mcp_credential_targets",
                 "_mcp_runtime_credential_versions", "_mcp_status", "_mcp_reconnect_locks", "_mcp_connection_epochs"):
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
@pytest.mark.parametrize("private_mode", [None, "false", "true"])
@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("reconnect_first", [False, True])
@pytest.mark.parametrize("state", ["fresh", "legacy", "migrated", "private", "custom", "disabled"])
async def test_bootstrapped_search_policy_reaches_registration_and_reconnect(
    tmp_path, monkeypatch, isolated_connector_runtime, private_mode, isolated, reconnect_first, state,
):
    from types import SimpleNamespace
    from box_agent.tools.mcp_bootstrap import (
        HOSTED_SEARCH_SERVER, MANAGED_MCP_SCHEMA_KEY, bootstrap_managed_mcp_config,
    )

    profile = tmp_path / "profile"
    profile.mkdir()
    user = profile / "config/mcp.json"
    config = profile / "config/mcp.system.json" if isolated else user
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    for key in (
        "BOX_AGENT_USER_MCP_CONFIG_PATH", "BOX_AGENT_SYSTEM_MCP_CONFIG_PATH",
        "BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", "BOX_AGENT_RESERVED_MCP_SERVER_NAMES",
        "BOX_AGENT_HOSTED_SEARCH_URL", "BOX_AGENT_RUNTIME_ROOT", "RACCOON_PRIVATE_DEPLOYMENT",
    ):
        monkeypatch.delenv(key, raising=False)
    if private_mode is not None:
        monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", private_mode)
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    if isolated:
        monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
        monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(config))
    name = mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME
    if state in {"legacy", "migrated"}:
        with monkeypatch.context() as before:
            before.setenv("RACCOON_PRIVATE_DEPLOYMENT", "false")
            bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
    elif state in {"private", "custom", "disabled"}:
        url = {
            "private": "https://tenant.example/office/mcp",
            "custom": "https://customer.example/mcp",
            "disabled": HOSTED_SEARCH_SERVER["url"],
        }[state]
        entry = (
            {**HOSTED_SEARCH_SERVER, "disabled": True}
            if state == "disabled" else {"url": url, "disabled": False}
        )
        _write(config, {name: entry})
        payload = json.loads(config.read_text())
        payload[MANAGED_MCP_SCHEMA_KEY] = 1
        config.write_text(json.dumps(payload))
    if state != "legacy":
        bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")

    attempts = []

    async def connect(connection):
        attempts.append(connection.name)
        connection.tools = [mcp_loader.MCPTool(
            name=f"fixture_{connection.name}", remote_name="web_search",
            description="fixture", parameters={}, session=SimpleNamespace(),
            server_name=connection.name, always_load=connection.always_load,
        )]
        return True

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    enabled = state != "disabled" and not (
        private_mode == "true" and state in {"fresh", "legacy", "migrated"}
    )
    tools = await mcp_loader.load_mcp_tools_async(str(user))
    assert (name in attempts) is enabled
    assert any(tool.server_name == name for tool in tools) is enabled
    reconnect = await mcp_loader.reconnect_mcp_server(name)
    assert reconnect["success"] is enabled
    await mcp_loader.reconcile_mcp_sources("system" if isolated else "user")
    assert any(tool.server_name == name for tool in mcp_loader.get_all_mcp_tools()) is enabled
    if not enabled:
        assert name not in attempts
    if state == "disabled":
        assert json.loads(config.read_text())["mcpServers"][name]["disabled"] is True
    if private_mode != "true" and state in {"fresh", "legacy", "migrated"}:
        count = attempts.count(name)
        monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
        bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
        if reconnect_first:
            assert not (await mcp_loader.reconnect_mcp_server(name))["success"]
            assert not any(tool.server_name == name for tool in mcp_loader.get_all_mcp_tools())
            assert not any(conn.name == name for conn in mcp_loader._mcp_connections)
            assert not any(entry.server_name == name for entry in mcp_loader.get_mcp_tool_catalog().snapshot())
        await mcp_loader.reconcile_mcp_sources("system" if isolated else "user")
        assert not any(tool.server_name == name for tool in mcp_loader.get_all_mcp_tools())
        assert not (await mcp_loader.reconnect_mcp_server(name))["success"]
        assert attempts.count(name) == count
    if private_mode == "true" and state in {"fresh", "legacy", "migrated"}:
        # 私有过滤不把默认项永久禁用；返回 C 端仍恢复原来的自动配置行为。
        monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "false")
        bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
        await mcp_loader.reconcile_mcp_sources("system" if isolated else "user")
        assert any(tool.server_name == name for tool in mcp_loader.get_all_mcp_tools())


@pytest.mark.asyncio
@pytest.mark.parametrize("choice_type", ["url-only", "full-preset", "legacy-custom"])
async def test_private_bootstrap_keeps_explicit_public_choice_in_user_file(
    tmp_path, monkeypatch, isolated_connector_runtime, choice_type,
):
    from box_agent.tools.mcp_bootstrap import (
        HOSTED_SEARCH_SERVER, MANAGED_MCP_SCHEMA_KEY, bootstrap_managed_mcp_config,
    )

    config = tmp_path / "mcp.json"
    name = mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME
    choice = {"url": HOSTED_SEARCH_SERVER["url"]} if choice_type == "url-only" else dict(HOSTED_SEARCH_SERVER)
    if choice_type == "legacy-custom":
        choice["headers"] = {"Authorization": "Bearer customer-fixture"}
    _write(config, {name: choice})
    if choice_type == "legacy-custom":
        payload = json.loads(config.read_text())
        payload[MANAGED_MCP_SCHEMA_KEY] = 1
        config.write_text(json.dumps(payload))
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    monkeypatch.delenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", raising=False)
    monkeypatch.delenv("BOX_AGENT_HOSTED_SEARCH_URL", raising=False)
    bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
    assert mcp_loader._resolve_registered_sources(str(config))[name].config == choice
    assert not bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract").changed
    assert mcp_loader._resolve_registered_sources(str(config))[name].config == choice


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_settings", [
    {}, {"headers": {"Authorization": "Bearer customer-fixture"}}, {"alwaysLoad": False},
])
async def test_regenerated_search_does_not_inherit_deleted_explicit_choice(
    tmp_path, monkeypatch, isolated_connector_runtime, custom_settings,
):
    from box_agent.tools.mcp_bootstrap import (
        HOSTED_SEARCH_SERVER, MANAGED_SEARCH_DEFAULT_KEY, bootstrap_managed_mcp_config,
    )

    config = tmp_path / "mcp.json"
    name = mcp_loader.HOSTED_WEB_SEARCH_SERVER_NAME
    _write(config, {name: {"url": HOSTED_SEARCH_SERVER["url"]}})
    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    monkeypatch.setenv("RACCOON_PRIVATE_OFFICE_URL", "https://tenant.example/office")
    monkeypatch.delenv("BOX_AGENT_HOSTED_SEARCH_URL", raising=False)
    bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
    payload = json.loads(config.read_text())
    assert payload[MANAGED_SEARCH_DEFAULT_KEY] is False
    payload["mcpServers"].pop(name)
    config.write_text(json.dumps(payload))

    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "false")
    bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
    assert json.loads(config.read_text())[MANAGED_SEARCH_DEFAULT_KEY] is True
    assert name in mcp_loader._resolve_registered_sources(str(config))
    if custom_settings:
        payload = json.loads(config.read_text())
        payload["mcpServers"][name].update(custom_settings)
        config.write_text(json.dumps(payload))

    monkeypatch.setenv("RACCOON_PRIVATE_DEPLOYMENT", "true")
    assert (name in mcp_loader._resolve_registered_sources(str(config))) is bool(custom_settings)
    bootstrap_managed_mcp_config(config, web_extract_command="fixture-extract")
    resolved = mcp_loader._resolve_registered_sources(str(config))
    assert (name in resolved) is bool(custom_settings)
    if custom_settings:
        assert all(resolved[name].config[key] == value for key, value in custom_settings.items())


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
async def test_host_managed_user_server_makes_no_connection_until_credentials_are_restored(
    monkeypatch, isolated_connector_runtime,
):
    from unittest.mock import Mock

    config = {"url": "https://remote.test/mcp", "type": "streamable_http", "alwaysLoad": True}
    _write(Path(mcp_loader._mcp_config_path), {"remote": config})
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CREDENTIAL_TARGETS", json.dumps({
        "remote": {key: value for key, value in config.items() if key != "alwaysLoad"},
    }))
    build = Mock(wraps=mcp_loader._build_connection)
    monkeypatch.setattr(mcp_loader, "_build_connection", build)
    attempts = []

    async def connect(connection):
        attempts.append(dict(connection.headers))
        assert connection.headers == {"Authorization": "Bearer restored-token"}
        return True

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    assert await mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path) == []
    build.assert_not_called()
    assert attempts == []
    assert mcp_loader.get_mcp_status()[0]["state"] == "connecting"
    assert mcp_loader.get_mcp_status()[0]["error"] is None
    assert not mcp_loader.is_mcp_loading()

    mcp_loader.set_mcp_runtime_credential(
        "user-mcp:remote", {"Authorization": "Bearer restored-token"},
        target={"owner": "user", "name": "remote", "config": config},
    )
    result = await mcp_loader.reconnect_mcp_server("remote")
    assert result["success"] is True
    assert len(attempts) == 1
    build.assert_called_once()
    assert mcp_loader.get_mcp_status()[0]["state"] == "connected"


@pytest.mark.asyncio
@pytest.mark.parametrize("targets,owner,config", [
    (None, "user", {"url": "https://remote.test/mcp"}),
    ("{}", "user", {"url": "https://remote.test/mcp"}),
    ("invalid-json", "user", {"url": "https://remote.test/mcp"}),
    ("[]", "user", {"url": "https://remote.test/mcp"}),
    ('{"remote":{"url":"https://before.test/mcp"}}', "user", {"url": "https://changed.test/mcp"}),
    ('{"other":{"url":"https://remote.test/mcp"}}', "user", {"url": "https://remote.test/mcp"}),
    ("match", "user", {"url": "https://remote.test/mcp", "headers": {"AUTHORIZATION": "Bearer manual"}}),
    ("match", "user", {"command": "fixture-server"}),
    ("match", "system", {"url": "https://system.test/mcp"}),
    ("match", "connector", {"url": "https://connector.test/mcp", "_connectorId": "fixture"}),
])
async def test_host_user_deferral_preserves_other_startup_paths(
    monkeypatch, tmp_path, isolated_connector_runtime, targets, owner, config,
):
    if targets is None:
        monkeypatch.delenv("BOX_AGENT_USER_MCP_CREDENTIAL_TARGETS", raising=False)
    else:
        monkeypatch.setenv("BOX_AGENT_USER_MCP_CREDENTIAL_TARGETS",
                           json.dumps({"remote": config}) if targets == "match" else targets)
    if owner == "user":
        _write(Path(mcp_loader._mcp_config_path), {"remote": config})
    elif owner == "system":
        system = tmp_path / "system.json"
        _write(system, {"remote": config})
        monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(system))
    else:
        monkeypatch.setattr(mcp_loader, "_mcp_source_overrides", {owner: {"remote": config}})
    attempts = []

    async def connect(connection):
        attempts.append(connection.name)
        return True

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    await mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path)
    assert attempts == ["remote"]
    assert mcp_loader.get_mcp_status()[0]["state"] == "connected"


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

    connection = SimpleNamespace(name="law", last_error=None, connect=connect, disconnect=AsyncMock(), tools=[
        FakeMCPTool("legal_lookup", "law", connector_id="law"),
    ])
    other = SimpleNamespace(name="other", last_error=None, connect=connect_other, disconnect=AsyncMock(), tools=[])
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
                          transport_label=server["url"], last_error=None,
                          disconnect=AsyncMock(), tools=[FakeMCPTool("old_lookup", "law", connector_id="law")])
    new = SimpleNamespace(name="law", url="https://new.example.test/mcp", command=None,
                          transport_label="https://new.example.test/mcp", last_error=None,
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


def test_host_user_credentials_bind_to_exact_config_without_file_refs(tmp_path, monkeypatch):
    from dataclasses import replace

    path = tmp_path / "mcp.json"
    original = {"url": "https://user.test/mcp", "headers": {"X-Key": "keep"}}
    _write(path, {"custom": original, "other": {"url": "https://other.test/mcp"}})
    monkeypatch.setattr(mcp_loader, "_mcp_credential_targets", {})
    monkeypatch.setattr(mcp_loader, "_mcp_runtime_credential_versions", {})
    definitions = resolve_mcp_sources((McpConfigSource("user", path),)).servers
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", definitions)
    monkeypatch.setattr(mcp_loader, "_mcp_runtime_credentials", {})
    def bind(name, config, headers):
        return mcp_loader.set_mcp_runtime_credential(
            f"user-mcp:{name}", headers,
            target={"owner": "user", "name": name, "config": config},
        )
    before = definitions["custom"].fingerprint
    assert bind("custom", original, {"Authorization": "Bearer test-only"}) == ["custom"]
    updated = resolve_mcp_sources((McpConfigSource("user", path),), mcp_loader._mcp_runtime_credential_versions).servers
    assert updated["custom"].fingerprint != before
    assert updated["other"].fingerprint == definitions["other"].fingerprint
    assert mcp_loader._materialize_server_config(updated["custom"])["headers"] == {
        "X-Key": "keep", "Authorization": "Bearer test-only",
    }
    assert "Authorization" not in path.read_text()
    changed = replace(updated["custom"], config={**original, "url": "https://attacker.test/mcp"})
    assert mcp_loader._materialize_server_config(changed)["headers"] == {"X-Key": "keep"}
    changed_header = replace(updated["custom"], config={**original, "headers": {"X-Key": "changed"}})
    assert "Authorization" not in mcp_loader._materialize_server_config(changed_header)["headers"]
    protected = replace(updated["custom"], owner="system")
    assert "Authorization" not in mcp_loader._materialize_server_config(protected)["headers"]
    mcp_loader.clear_mcp_runtime_credential("user-mcp:custom", target={"owner": "user", "name": "custom"})
    assert "Authorization" not in mcp_loader._materialize_server_config(updated["custom"])["headers"]
    with pytest.raises(ValueError, match="without credentialRef"):
        bind("custom", {**original, "credentialRef": "connector:secret"}, {"Authorization": "test"})
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", {"protected": protected})
    with pytest.raises(ValueError, match="protected server"):
        bind("protected", original, {"Authorization": "test"})


def test_unified_credentials_preserve_manual_headers_and_isolate_owners(tmp_path, monkeypatch):
    from dataclasses import replace

    path = tmp_path / "mcp.json"
    manual = {"url": "https://user.test/mcp", "headers": {"Authorization": "Bearer manual"}}
    _write(path, {"custom": manual})
    definitions = resolve_mcp_sources((McpConfigSource("user", path),)).servers
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", definitions)
    monkeypatch.setattr(mcp_loader, "_mcp_runtime_credentials", {})
    monkeypatch.setattr(mcp_loader, "_mcp_credential_targets", {})
    monkeypatch.setattr(mcp_loader, "_mcp_runtime_credential_versions", {})
    definition = definitions["custom"]
    assert mcp_loader._materialize_server_config(definition)["headers"] == manual["headers"]
    target = {"owner": "user", "name": "custom", "config": manual}
    mcp_loader.set_mcp_runtime_credential("user-mcp:custom", {"Authorization": "Bearer oauth"}, target=target)
    assert mcp_loader._materialize_server_config(definition)["headers"]["Authorization"] == "Bearer oauth"
    stolen = replace(definition, owner="connector", config={**manual, "credentialRef": "user-mcp:custom"})
    assert mcp_loader._materialize_server_config(stolen)["headers"] == manual["headers"]
    assert mcp_loader.clear_mcp_runtime_credential("user-mcp:custom", target={"owner": "user", "name": "custom"}) == ["custom"]
    assert mcp_loader._materialize_server_config(definition)["headers"] == manual["headers"]
    assert path.read_text().count("oauth") == 0
    mcp_loader.set_mcp_runtime_credential("user-mcp:custom", {"Authorization": "test"})
    assert mcp_loader._materialize_server_config(definition)["headers"] == manual["headers"]
    mcp_loader.clear_mcp_runtime_credential("user-mcp:custom")
    with pytest.raises(ValueError, match="must match"):
        mcp_loader.set_mcp_runtime_credential("connector:other", {"Authorization": "test"}, target=target)
    assert mcp_loader._mcp_runtime_credentials == {}


@pytest.mark.asyncio
async def test_legacy_connector_ref_does_not_block_public_cold_start(monkeypatch, tmp_path, isolated_connector_runtime):
    from unittest.mock import AsyncMock
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog

    connector = tmp_path / "connectors.json"
    _write(connector, {"legacy": {"url": "https://legacy.test/mcp", "_connectorId": "example", "credentialRef": "user-mcp:legacy"}})
    _write(Path(mcp_loader._mcp_config_path), {"public": {"url": "https://public.test/mcp"}})
    monkeypatch.setenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", str(connector))
    monkeypatch.delenv("BOX_AGENT_USER_MCP_CREDENTIAL_TARGETS", raising=False)
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    mcp_loader.set_mcp_runtime_credential("user-mcp:legacy", {"Authorization": "Bearer legacy-test"})
    attempted = []
    async def connect(conn):
        attempted.append(conn.name)
        if conn.name == "legacy":
            assert conn.headers["Authorization"] == "Bearer legacy-test"
        return True
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "disconnect", AsyncMock())
    await mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path)
    assert attempted == ["legacy", "public"]
    assert {name: status.state for name, status in mcp_loader._mcp_status.items()} == {"legacy": "connected", "public": "connected"}
    await mcp_loader.cleanup_mcp_connections()


def test_equal_credential_refs_keep_owner_headers_and_versions_isolated(monkeypatch, tmp_path, isolated_connector_runtime):
    user = Path(mcp_loader._mcp_config_path)
    connector = tmp_path / "connectors.json"
    config = {"url": "https://user.test/mcp"}
    reference = "user-mcp:remote"
    _write(user, {"remote": config})
    _write(connector, {"legacy": {"url": "https://legacy.test/mcp", "_connectorId": "example", "credentialRef": reference}})
    sources = (McpConfigSource("connector", connector), McpConfigSource("user", user))
    def resolve():
        return resolve_mcp_sources(sources, mcp_loader._mcp_runtime_credential_versions).servers
    original = resolve()
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", original)
    mcp_loader.set_mcp_runtime_credential(reference, {"Authorization": "Bearer connector-test"})
    connector_updated = resolve()
    assert connector_updated["remote"].fingerprint == original["remote"].fingerprint
    assert connector_updated["legacy"].fingerprint != original["legacy"].fingerprint
    target = {"owner": "user", "name": "remote", "config": config}
    mcp_loader.set_mcp_runtime_credential(reference, {"Authorization": "Bearer user-test"}, target=target)
    user_updated = resolve()
    assert user_updated["legacy"].fingerprint == connector_updated["legacy"].fingerprint
    assert user_updated["remote"].fingerprint != connector_updated["remote"].fingerprint
    def headers(name):
        return mcp_loader._materialize_server_config(user_updated[name]).get("headers", {})
    assert headers("legacy")["Authorization"] == "Bearer connector-test"
    assert headers("remote")["Authorization"] == "Bearer user-test"
    mcp_loader.clear_mcp_runtime_credential(reference)
    assert headers("remote")["Authorization"] == "Bearer user-test"
    assert headers("legacy") == {}
    assert resolve()["remote"].fingerprint == user_updated["remote"].fingerprint
    mcp_loader.set_mcp_runtime_credential(reference, {"Authorization": "Bearer connector-test"})
    before_user_clear = resolve()
    mcp_loader.clear_mcp_runtime_credential(reference, target={"owner": "user", "name": "remote"})
    assert headers("legacy")["Authorization"] == "Bearer connector-test"
    assert headers("remote") == {}
    assert resolve()["legacy"].fingerprint == before_user_clear["legacy"].fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["connector", "system", "user"])
@pytest.mark.parametrize("load_mode", ["cold", "reconnect"])
async def test_user_credential_clear_only_retires_user_owned_handshake(
    monkeypatch, tmp_path, isolated_connector_runtime, owner, load_mode,
):
    import asyncio
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from tests.test_mcp_tool_search import FakeMCPTool

    user = Path(mcp_loader._mcp_config_path)
    config = {"url": "https://user.test/mcp"}
    reference = "user-mcp:shared"
    _write(user, {"shared": config})
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", resolve_mcp_sources((McpConfigSource("user", user),)).servers)
    mcp_loader.set_mcp_runtime_credential(reference, {"Authorization": "Bearer user-test"}, target={"owner": "user", "name": "shared", "config": config})
    if owner != "user":
        protected = tmp_path / f"{owner}.json"
        protected_config = {"url": "https://protected.test/mcp"}
        if owner == "connector":
            protected_config.update({"_connectorId": "example", "credentialRef": reference})
            mcp_loader.set_mcp_runtime_credential(reference, {"Authorization": "Bearer connector-test"})
        _write(protected, {"shared": protected_config})
        monkeypatch.setenv(f"BOX_AGENT_{owner.upper()}_MCP_CONFIG_PATH", str(protected))

    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    started, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def connect(conn):
        started.set()
        await release.wait()
        conn.tools = [FakeMCPTool("shared_tool", "shared")]
        return True

    async def disconnect(conn):
        closed.append(conn.name)
        conn.tools = []

    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "disconnect", disconnect)
    loading = asyncio.create_task(
        mcp_loader.load_mcp_tools_async(str(user)) if load_mode == "cold"
        else mcp_loader.reconnect_mcp_server("shared")
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert mcp_loader._mcp_server_definitions["shared"].owner == owner
        affected = mcp_loader.clear_mcp_runtime_credential(reference, target={"owner": "user", "name": "shared"})
        assert ("user", reference) not in mcp_loader._mcp_runtime_credentials
        assert reference not in mcp_loader._mcp_credential_targets
        if owner == "connector":
            assert mcp_loader._mcp_runtime_credentials[reference] == {"Authorization": "Bearer connector-test"}
        release.set()
        result = await asyncio.wait_for(loading, 2)
        if owner == "user":
            assert affected == ["shared"]
            assert closed == ["shared"]
            assert catalog.snapshot() == ()
            assert mcp_loader._mcp_connections == []
            if load_mode == "cold":
                assert result == []
            else:
                assert result["superseded"]
        else:
            assert affected == []
            assert closed == []
            assert [entry.model_name for entry in catalog.snapshot()] == ["shared_tool"]
            assert len(mcp_loader._mcp_connections) == 1
            assert mcp_loader._mcp_status["shared"].state == "connected"
            if load_mode == "cold":
                assert [tool.name for tool in result] == ["shared_tool"]
            else:
                assert result["success"]
        assert not mcp_loader._mcp_loading
    finally:
        release.set()
        await asyncio.gather(loading, return_exceptions=True)
        await mcp_loader.cleanup_mcp_connections()


@pytest.mark.asyncio
async def test_cold_start_build_failure_is_limited_to_that_server(monkeypatch, isolated_connector_runtime):
    from unittest.mock import AsyncMock
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog

    _write(Path(mcp_loader._mcp_config_path), {"broken": {"url": "https://broken.test/mcp"}, "public": {"url": "https://public.test/mcp"}})
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    build = mcp_loader._build_connection
    def build_one(definition):
        if definition.name == "broken":
            raise ValueError("invalid server configuration")
        return build(definition)
    monkeypatch.setattr(mcp_loader, "_build_connection", build_one)
    connect = AsyncMock(return_value=True)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "disconnect", AsyncMock())
    await mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path)
    connect.assert_awaited_once()
    assert mcp_loader._mcp_status["broken"].state == "failed"
    assert mcp_loader._mcp_status["public"].state == "connected"
    await mcp_loader.cleanup_mcp_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
@pytest.mark.parametrize("operation", ["reconnect", "disconnect"])
async def test_late_cold_handshake_cannot_publish_after_oauth_update(monkeypatch, isolated_connector_runtime, outcome, operation):
    import asyncio
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from tests.test_mcp_tool_search import FakeMCPTool

    config = {"url": "https://remote.test/mcp", "headers": {"Authorization": "Bearer old-mock"}}
    _write(Path(mcp_loader._mcp_config_path), {"remote": config})
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CREDENTIAL_TARGETS", json.dumps({"remote": config}))
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    started, release = asyncio.Event(), asyncio.Event()
    old_connections, closed = [], []
    async def connect(conn):
        old = conn.headers["Authorization"] == "Bearer old-mock"
        if old:
            old_connections.append(conn)
            started.set()
            await release.wait()
            if outcome == "exception":
                raise RuntimeError("late old handshake failed")
            if outcome == "failure":
                conn.last_error = "late old handshake failed"
                return False
        conn.tools = [FakeMCPTool("tool_manual" if old else "tool_oauth", "remote")]
        return True
    async def disconnect(conn):
        closed.append(conn)
        conn.tools = []
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "disconnect", disconnect)
    cold = asyncio.create_task(mcp_loader.load_mcp_tools_async(mcp_loader._mcp_config_path))
    try:
        await asyncio.wait_for(started.wait(), 2)
        mcp_loader.set_mcp_runtime_credential("user-mcp:remote", {"Authorization": "Bearer new-mock"}, target={"owner": "user", "name": "remote", "config": config})
        result = await mcp_loader.reconnect_mcp_server("remote")
        assert result["success"]
        assert [entry.model_name for entry in catalog.snapshot()] == ["tool_oauth"]
        if operation == "disconnect":
            mcp_loader.clear_mcp_runtime_credential("user-mcp:remote", target={"owner": "user", "name": "remote"})
            await mcp_loader.disconnect_mcp_server("remote")
        release.set()
        loaded = await asyncio.wait_for(cold, 2)
        expected = ["tool_oauth"] if operation == "reconnect" else []
        assert [entry.model_name for entry in catalog.snapshot()] == expected
        assert [tool.name for tool in loaded] == expected
        assert mcp_loader._mcp_status["remote"].state == ("connected" if operation == "reconnect" else "disabled")
        assert len(mcp_loader._mcp_connections) == len(expected)
        assert closed.count(old_connections[0]) == 1
        assert old_connections[0].tools == []
    finally:
        release.set()
        await asyncio.gather(cold, return_exceptions=True)
        await mcp_loader.cleanup_mcp_connections()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "exception"])
async def test_disconnect_retires_a_pending_hot_handshake(monkeypatch, isolated_connector_runtime, outcome):
    import asyncio
    from unittest.mock import AsyncMock
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from tests.test_mcp_tool_search import FakeMCPTool

    config = {"url": "https://remote.test/mcp"}
    _write(Path(mcp_loader._mcp_config_path), {"remote": config})
    catalog = MCPToolCatalog()
    monkeypatch.setattr(mcp_loader, "get_mcp_tool_catalog", lambda: catalog)
    started, release = asyncio.Event(), asyncio.Event()
    async def connect(conn):
        started.set()
        await release.wait()
        if outcome == "exception":
            raise RuntimeError("late handshake failed")
        if outcome == "failure":
            conn.last_error = "late handshake failed"
            return False
        conn.tools = [FakeMCPTool("obsolete", "remote")]
        return True
    disconnect = AsyncMock()
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "connect", connect)
    monkeypatch.setattr(mcp_loader.MCPServerConnection, "disconnect", disconnect)
    hot = asyncio.create_task(mcp_loader.reconnect_mcp_server("remote"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await mcp_loader.disconnect_mcp_server("remote")
        release.set()
        result = await asyncio.wait_for(hot, 2)
        assert result["superseded"]
        assert mcp_loader._mcp_connections == []
        assert catalog.snapshot() == ()
        assert mcp_loader._mcp_status["remote"].state == "disabled"
        disconnect.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(hot, return_exceptions=True)


@pytest.mark.parametrize("owner", ["user", "connector"])
@pytest.mark.parametrize("configured_key,runtime_key", [("authorization", "Authorization"), ("Authorization", "authorization"), ("aUtHoRiZaTiOn", "AUTHORIZATION")])
def test_runtime_headers_override_case_insensitively_in_http_requests(monkeypatch, tmp_path, isolated_connector_runtime, owner, configured_key, runtime_key):
    import httpx

    config = {"url": "https://remote.test/mcp", "headers": {configured_key: "Bearer old-mock", "X-Project": "keep", "x-project": "also-keep"}}
    reference = "user-mcp:remote"
    user_config = dict(config)
    if owner == "connector":
        config = {**config, "_connectorId": "example", "credentialRef": reference}
    path = tmp_path / "source.json"
    _write(path, {"remote": config})
    definition = resolve_mcp_sources((McpConfigSource(owner, path),)).servers["remote"]
    monkeypatch.setattr(mcp_loader, "_mcp_server_definitions", {"remote": definition})
    target = {"owner": "user", "name": "remote", "config": user_config} if owner == "user" else None
    mcp_loader.set_mcp_runtime_credential(reference, {runtime_key: "Bearer new-mock"}, target=target)
    headers = mcp_loader._materialize_server_config(definition)["headers"]
    request = httpx.Request("POST", config["url"], headers=headers)
    assert request.headers.get_list("authorization") == ["Bearer new-mock"]
    assert request.headers.get_list("x-project") == ["keep", "also-keep"]
