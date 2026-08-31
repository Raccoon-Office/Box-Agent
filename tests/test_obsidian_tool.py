from __future__ import annotations

import json
from pathlib import Path

import pytest

from box_agent.tools.obsidian_tool import (
    OBSIDIAN_CONFIG_ENV,
    OBSIDIAN_LAUNCH_SCOPE,
    OBSIDIAN_PERMISSION_SCOPE,
    ObsidianCreateNoteTool,
    ObsidianDailyNoteTool,
    ObsidianUpdateNoteTool,
)
from box_agent.tools.skill_loader import SkillLoader


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "Vault"
    (path / ".obsidian").mkdir(parents=True)
    return path


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    path = tmp_path / "obsidian"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict) -> Path:
    config_path = tmp_path / "obsidian.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(OBSIDIAN_CONFIG_ENV, str(config_path))
    return config_path


async def test_create_note_invokes_cli_with_safe_argv(
    tmp_path: Path,
    vault: Path,
    cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch, {"enabled": True, "vault_path": str(vault), "cli_path": str(cli), "app_running": True})
    calls: list[list[str]] = []

    async def fake_run_process(args: list[str], timeout: int = 60):
        calls.append(args)
        return 0, "ok", ""

    monkeypatch.setattr("box_agent.tools.obsidian_tool._run_process", fake_run_process)

    result = await ObsidianCreateNoteTool().execute(
        title="今日 TODO",
        content="- [ ] 写测试\n- [ ] 发报告",
        folder="Daily",
        open_after=True,
    )

    assert result.success
    assert calls == [[
        str(cli),
        "create",
        "path=Daily/今日 TODO.md",
        "content=- [ ] 写测试\\n- [ ] 发报告",
        "open",
    ]]
    assert result.raw_output == {"path": "Daily/今日 TODO.md", "operation": "create"}


async def test_update_note_append_then_open(
    tmp_path: Path,
    vault: Path,
    cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch, {"enabled": True, "vault_path": str(vault), "cli_path": str(cli), "app_running": True})
    calls: list[list[str]] = []

    async def fake_run_process(args: list[str], timeout: int = 60):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr("box_agent.tools.obsidian_tool._run_process", fake_run_process)

    result = await ObsidianUpdateNoteTool().execute(
        path="Notes/weekly.md",
        content="补充内容",
        mode="append",
        inline=True,
        open_after=True,
    )

    assert result.success
    assert calls == [
        [str(cli), "append", "path=Notes/weekly.md", "content=补充内容", "inline"],
        [str(cli), "open", "path=Notes/weekly.md"],
    ]


async def test_daily_note_append_then_open(
    tmp_path: Path,
    vault: Path,
    cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch, {"enabled": True, "vault_path": str(vault), "cli_path": str(cli), "app_running": True})
    calls: list[list[str]] = []

    async def fake_run_process(args: list[str], timeout: int = 60):
        calls.append(args)
        return 0, "", ""

    monkeypatch.setattr("box_agent.tools.obsidian_tool._run_process", fake_run_process)

    result = await ObsidianDailyNoteTool().execute(action="append", content="- [ ] 今日待办")

    assert result.success
    assert calls == [
        [str(cli), "daily:append", "content=- [ ] 今日待办"],
        [str(cli), "daily"],
    ]


async def test_rejects_unsafe_paths() -> None:
    tool = ObsidianUpdateNoteTool()

    for path in ("/tmp/x.md", "../x.md", "x.txt", "folder/../x.md"):
        result = await tool.execute(path=path, content="content")
        assert not result.success


async def test_missing_vault_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path) -> None:
    write_config(tmp_path, monkeypatch, {"enabled": True, "vault_path": str(tmp_path / "missing"), "cli_path": str(cli)})

    result = await ObsidianCreateNoteTool().execute(title="t", content="c", open_after=False)

    assert not result.success
    assert "Vault" in (result.error or "")


async def test_non_vault_directory_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path) -> None:
    not_vault = tmp_path / "not-vault"
    not_vault.mkdir()
    write_config(tmp_path, monkeypatch, {"enabled": True, "vault_path": str(not_vault), "cli_path": str(cli)})

    result = await ObsidianCreateNoteTool().execute(title="t", content="c", open_after=False)

    assert not result.success
    assert ".obsidian" in (result.error or "")


async def test_missing_bare_cli_returns_friendly_error(
    tmp_path: Path,
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "vault_path": str(vault),
            "cli_path": "definitely-missing-obsidian-cli",
            "app_running": True,
        },
    )

    result = await ObsidianCreateNoteTool().execute(title="t", content="c", open_after=False)

    assert not result.success
    assert "Obsidian CLI 未找到" in (result.error or "")


async def test_permission_request_then_retry_launches_app(
    tmp_path: Path,
    vault: Path,
    cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_config(tmp_path, monkeypatch, {"enabled": True, "vault_path": str(vault), "cli_path": str(cli), "app_running": False})
    calls: list[list[str]] = []
    launched: list[bool] = []

    async def fake_run_process(args: list[str], timeout: int = 60):
        calls.append(args)
        return 0, "ok", ""

    async def fake_launch(_config: dict):
        launched.append(True)
        return True, ""

    async def fake_wait(_config: dict, timeout: float = 30.0):
        return True

    monkeypatch.setattr("box_agent.tools.obsidian_tool._run_process", fake_run_process)
    monkeypatch.setattr("box_agent.tools.obsidian_tool._launch_obsidian", fake_launch)
    monkeypatch.setattr("box_agent.tools.obsidian_tool._wait_cli_ready", fake_wait)

    tool = ObsidianCreateNoteTool()
    first = await tool.execute(title="t", content="c")

    assert not first.success
    assert first.permission_request is not None
    assert first.permission_request["scope"] == OBSIDIAN_PERMISSION_SCOPE
    assert first.permission_request["requested_scope"] == OBSIDIAN_LAUNCH_SCOPE

    tool.approve_permission_request(first.permission_request)
    second = await tool.execute(title="t", content="c")

    assert second.success
    assert launched == [True]
    assert calls == [[str(cli), "create", "path=t.md", "content=c", "open"]]


def test_obsidian_skill_remains_discoverable_after_market_install() -> None:
    skills_dir = Path(__file__).resolve().parent.parent / "box_agent" / "skills"
    builtin_loader = SkillLoader(sources=[(skills_dir, "builtin")])
    builtin_loader.discover_skills()
    assert builtin_loader.get_skill("obsidian") is None

    loader = SkillLoader(sources=[(skills_dir / "obsidian", "user")])
    loader.discover_skills()

    skill = loader.get_skill("obsidian")
    assert skill is not None
    assert skill.source == "user"
    assert "obsidian" in loader.list_skills()


def test_obsidian_skill_mentions_context_writeback_policy() -> None:
    skill_path = Path(__file__).resolve().parent.parent / "box_agent" / "skills" / "obsidian" / "SKILL.md"
    text = skill_path.read_text(encoding="utf-8")

    assert "obsidian_context" in text
    assert "obsidian_update_note" in text
    assert "Vault 相对 `path`" in text
    assert "不要修改 workspace 文件" in text
