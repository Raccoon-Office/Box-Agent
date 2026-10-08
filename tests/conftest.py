"""Shared pytest fixtures and helpers.

Integration tests that hit real LLM/MCP endpoints are skipped automatically
when their dependencies (config.yaml, mcp.json, valid API key) are missing,
so the suite can run in CI / clean checkouts.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_user_state(monkeypatch, tmp_path_factory):
    """Give each test its own default state without imposing profile restrictions."""
    test_home = tmp_path_factory.mktemp("user-home")
    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(test_home))
    monkeypatch.setenv("HOME", str(test_home))


@pytest.fixture(autouse=True)
def _disable_session_trace_unless_test_enables_it(monkeypatch):
    """Keep ordinary tests from writing durable diagnostics into the real home."""

    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_ENABLED", "0")


def _load_yaml_or_skip(rel_path: str) -> dict[str, Any]:
    config_path = _REPO_ROOT / rel_path
    if not config_path.exists():
        pytest.skip(f"{rel_path} not present — integration test skipped")
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json_or_skip(rel_path: str) -> dict[str, Any]:
    config_path = _REPO_ROOT / rel_path
    if not config_path.exists():
        pytest.skip(f"{rel_path} not present — integration test skipped")
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def llm_config() -> dict[str, Any]:
    """Load box_agent/config/config.yaml or skip.

    Also skips when api_key is missing or is the example placeholder, since
    such tests would only fail on the network call.
    """
    cfg = _load_yaml_or_skip("box_agent/config/config.yaml")
    api_key = cfg.get("api_key") or ""
    if not api_key or api_key.startswith("your-") or api_key == "sk-...":
        pytest.skip("api_key not configured — integration test skipped")
    return cfg


@pytest.fixture
def mcp_config_optional() -> dict[str, Any]:
    """Load box_agent/config/mcp.json or skip."""
    return _load_json_or_skip("box_agent/config/mcp.json")


@pytest.fixture
def posix_shell(monkeypatch):
    """Use Git Bash for tests explicitly exercising POSIX shell syntax on Windows."""
    if os.name != "nt":
        return
    git = shutil.which("git")
    bash = Path(git).parent.parent / "usr/bin/bash.exe" if git else None
    if bash is None or not bash.is_file():
        pytest.skip("POSIX shell commands require Git Bash on Windows")
    monkeypatch.setattr("box_agent.tools.bash_tool.bundled_win_bash", lambda: bash)


@pytest.fixture
def posix_command_parser(monkeypatch):
    from types import SimpleNamespace
    import box_agent.tools.shell_inspection as inspection
    monkeypatch.setattr(inspection, "platform", SimpleNamespace(system=lambda: "Linux"))
    monkeypatch.setattr("box_agent.tools.bash_tool.platform", SimpleNamespace(system=lambda: "Linux"))
