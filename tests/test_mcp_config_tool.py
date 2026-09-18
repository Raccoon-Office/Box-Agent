from __future__ import annotations

import json

import pytest

from box_agent.tools.mcp_config_tool import McpConfigTool


async def test_browser_inspection_prefers_runtime_over_unrelated_user_file(monkeypatch):
    monkeypatch.setattr("box_agent.tools.mcp_loader.inspect_live_browser", lambda: {
        "source": "runtime", "enabled": True, "mode": "headed", "scope": "session",
    })
    def unexpected_read():
        raise AssertionError("must not read user MCP config")
    monkeypatch.setattr("box_agent.tools.mcp_config_tool._resolve_write_target", unexpected_read)
    result = await McpConfigTool().execute(action="inspect_browser")
    assert result.success
    assert json.loads(result.content)["mode"] == "headed"


async def test_browser_mode_action_is_session_local_and_does_not_write_config(monkeypatch):
    from box_agent.tools.playwright_session_pool import PlaywrightSessionPool
    from unittest.mock import AsyncMock
    pool = AsyncMock(spec=PlaywrightSessionPool)
    pool.set_browser_mode.return_value = {"mode": "headed", "browser_started": True}
    monkeypatch.setattr("box_agent.tools.mcp_loader.get_playwright_session_pool", lambda: pool)
    def unexpected_write():
        raise AssertionError("must not write global MCP config")
    monkeypatch.setattr("box_agent.tools.mcp_config_tool._resolve_write_target", unexpected_write)
    result = await McpConfigTool().execute(action="set_browser_mode", mode="headed")
    assert result.success
    pool.set_browser_mode.assert_awaited_once_with("headed")
    pool.set_browser_mode.side_effect = RuntimeError("launch failed")
    assert not (await McpConfigTool().execute(action="set_browser_mode", mode="headed")).success
    monkeypatch.setattr("box_agent.tools.mcp_loader.get_playwright_session_pool", lambda: None)
    assert not (await McpConfigTool().execute(action="set_browser_mode", mode="headed")).success


@pytest.mark.asyncio
async def test_list_is_config_only_and_does_not_expose_connection_secrets(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "search": {
                        "url": "https://example.test/mcp?apiKey=secret-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(action="list")

    assert result.success is True
    assert "not a tool inventory" in result.content
    assert "tool_search" in result.content
    assert "transport=url" in result.content
    assert "secret-token" not in result.content


@pytest.mark.asyncio
async def test_inspect_browser_reports_current_headed_isolated_config(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "node",
                        "args": [
                            "bootstrap.js",
                            "playwright-mcp.js",
                            "--executable-path",
                            "managed-chromium.exe",
                            "--isolated",
                        ],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(action="inspect_browser")

    assert result.success is True
    assert f"Browser automation config: {config_path}" in result.content
    assert "mode=headed" in result.content
    assert "isolated=true" in result.content
    assert "profile=ephemeral-isolated" in result.content
    assert "executable=configured=true" in result.content
    assert "enabled=true" in result.content
    assert "not proof of the running process" in result.content


@pytest.mark.asyncio
async def test_inspect_browser_reports_headless_persistent_profile(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "args": [
                            "playwright-mcp.js",
                            "--headless",
                            "--user-data-dir",
                            "profile",
                        ],
                        "disabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(action="inspect_browser")

    assert result.success is True
    assert "mode=headless" in result.content
    assert "isolated=false" in result.content
    assert "profile=persistent-or-shared" in result.content
    assert "enabled=false" in result.content


@pytest.mark.asyncio
async def test_inspect_browser_handles_missing_server_without_exposing_config(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "private-server": {
                        "headers": {"Authorization": "secret-token"}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(action="inspect_browser")

    assert result.success is True
    assert "mode=unknown" in result.content
    assert "enabled=false" in result.content
    assert "secret-token" not in result.content


@pytest.mark.asyncio
async def test_update_removes_browser_arg_and_preserves_managed_config(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    original = {
        "mcpServers": {
            "playwright": {
                "command": "node",
                "args": [
                    "bootstrap.js",
                    "playwright-mcp.js",
                    "--headless",
                    "--isolated",
                    "--executable-path",
                    "managed-chromium.exe",
                    "--headless",
                ],
                "env": {"SAFE": "1"},
                "execute_timeout": 90,
                "disabled": False,
            },
            "other": {"url": "https://example.com/mcp"},
        }
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(
        action="update",
        name="playwright",
        config={"args_remove": ["--headless"]},
    )

    assert result.success is True
    assert "updated" in result.content
    written = json.loads(config_path.read_text(encoding="utf-8"))
    playwright = written["mcpServers"]["playwright"]
    assert "--headless" not in playwright["args"]
    assert "--isolated" in playwright["args"]
    assert playwright["command"] == "node"
    assert playwright["env"] == {"SAFE": "1"}
    assert playwright["execute_timeout"] == 90
    assert playwright["disabled"] is False
    assert written["mcpServers"]["other"] == {"url": "https://example.com/mcp"}


@pytest.mark.asyncio
async def test_update_adds_browser_arg_idempotently(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "node",
                        "args": ["playwright-mcp.js", "--isolated"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )
    tool = McpConfigTool()

    first = await tool.execute(
        action="update",
        name="playwright",
        config={"args_add": ["--headless"]},
    )
    second = await tool.execute(
        action="update",
        name="playwright",
        config={"args_add": ["--headless"]},
    )

    assert first.success is True
    assert second.success is True
    written = json.loads(config_path.read_text(encoding="utf-8"))
    args = written["mcpServers"]["playwright"]["args"]
    assert args.count("--headless") == 1
    assert "--isolated" in args


@pytest.mark.asyncio
async def test_update_rejects_missing_server(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(
        action="update",
        name="playwright",
        config={"args_remove": ["--headless"]},
    )

    assert result.success is False
    assert "Server 'playwright' not found" in (result.error or "")


@pytest.mark.asyncio
async def test_update_changes_fields_and_removes_fields(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "url": "https://old.example/mcp",
                        "headers": {"Authorization": "secret"},
                        "execute_timeout": 30,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(
        action="update",
        name="remote",
        config={
            "url": "https://new.example/mcp",
            "execute_timeout": 60,
            "remove_fields": ["headers"],
        },
    )

    assert result.success is True
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["remote"] == {
        "url": "https://new.example/mcp",
        "execute_timeout": 60,
    }


@pytest.mark.asyncio
async def test_add_preserves_explicit_always_load_and_reports_pending_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setattr(
        "box_agent.tools.mcp_config_tool._resolve_write_target",
        lambda: config_path,
    )

    result = await McpConfigTool().execute(
        action="add",
        name="core",
        config={"command": "node", "args": ["server.js"], "alwaysLoad": True},
    )

    assert result.success is True
    assert "only the configuration write" in result.content
    assert "pending" in result.content
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["mcpServers"]["core"]["alwaysLoad"] is True
