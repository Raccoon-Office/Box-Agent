from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from box_agent.acp.env_context import build_env_context_prompt
from box_agent.cli import _doctor_obsidian_status_line, build_cli_env_context
from box_agent.tools.obsidian_tool import OBSIDIAN_APP_ENV, OBSIDIAN_CLI_ENV, OBSIDIAN_CONFIG_ENV


def _clear_obsidian_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OBSIDIAN_CLI_ENV, raising=False)
    monkeypatch.delenv(OBSIDIAN_APP_ENV, raising=False)


def _write_obsidian_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict) -> Path:
    config_path = tmp_path / "obsidian.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv(OBSIDIAN_CONFIG_ENV, str(config_path))
    _clear_obsidian_env(monkeypatch)
    return config_path


def _fake_cli(tmp_path: Path) -> Path:
    cli = tmp_path / ("obsidian.exe" if os.name == "nt" else "obsidian")
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    return cli


def _fake_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "Vault"
    (vault / ".obsidian").mkdir(parents=True)
    return vault


def test_cli_env_context_reads_obsidian_json_and_renders_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _fake_vault(tmp_path)
    cli = _fake_cli(tmp_path)
    _write_obsidian_config(
        tmp_path,
        monkeypatch,
        {
            "enabled": True,
            "vault_path": str(vault),
            "vault_name": "工作笔记",
            "cli_path": str(cli),
            "cli_available": True,
            "app_running": False,
        },
    )

    ctx = build_cli_env_context()

    assert ctx is not None
    assert ctx.obsidian is not None
    assert ctx.obsidian.vault_path == str(vault)
    out = build_env_context_prompt(ctx)
    assert "Obsidian 状态" in out
    assert "`obsidian_create_note`" in out
    assert "obsidian_context" in out
    assert "`obsidian_update_note`" in out
    assert "不要修改 workspace 副本" in out
    assert "不要用 bash" in out


def test_cli_env_context_absent_without_obsidian_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OBSIDIAN_CONFIG_ENV, str(tmp_path / "missing.json"))
    _clear_obsidian_env(monkeypatch)

    assert build_cli_env_context() is None


def test_doctor_obsidian_reports_unconfigured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OBSIDIAN_CONFIG_ENV, str(tmp_path / "missing.json"))
    _clear_obsidian_env(monkeypatch)

    assert "not configured" in _doctor_obsidian_status_line()


def test_doctor_obsidian_reports_non_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    not_vault = tmp_path / "not-vault"
    not_vault.mkdir()
    cli = _fake_cli(tmp_path)
    _write_obsidian_config(
        tmp_path,
        monkeypatch,
        {"enabled": True, "vault_path": str(not_vault), "cli_path": str(cli)},
    )

    line = _doctor_obsidian_status_line()

    assert "❌ Obsidian" in line
    assert ".obsidian" in line


def test_doctor_obsidian_reports_missing_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _fake_cli(tmp_path)
    _write_obsidian_config(
        tmp_path,
        monkeypatch,
        {"enabled": True, "vault_path": str(tmp_path / "missing-vault"), "cli_path": str(cli)},
    )

    line = _doctor_obsidian_status_line()

    assert "❌ Obsidian" in line
    assert "Vault missing" in line


def test_doctor_obsidian_reports_missing_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _fake_vault(tmp_path)
    _write_obsidian_config(
        tmp_path,
        monkeypatch,
        {"enabled": True, "vault_path": str(vault), "cli_path": "definitely-missing-obsidian-cli"},
    )

    line = _doctor_obsidian_status_line()

    assert "❌ Obsidian" in line
    assert "CLI not found" in line


def test_doctor_obsidian_reports_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _fake_vault(tmp_path)
    cli = _fake_cli(tmp_path)
    _write_obsidian_config(
        tmp_path,
        monkeypatch,
        {"enabled": True, "vault_path": str(vault), "vault_name": "工作笔记", "cli_path": str(cli)},
    )

    line = _doctor_obsidian_status_line()

    assert "✅ Obsidian" in line
    assert "工作笔记" in line
    assert str(cli) in line
