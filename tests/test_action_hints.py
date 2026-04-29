"""Unit tests for box_agent.acp.action_hints."""

from __future__ import annotations

import json
from pathlib import Path

from box_agent.acp.action_hints import (
    build_action_hints_prompt,
    is_memory_scarce,
    is_playwright_unavailable,
)


# ── is_memory_scarce ────────────────────────────────────────────


def test_memory_scarce_when_none() -> None:
    assert is_memory_scarce(None) is True


def test_memory_scarce_when_empty_string() -> None:
    assert is_memory_scarce("") is True
    assert is_memory_scarce("   \n  \n") is True


def test_memory_scarce_when_under_threshold() -> None:
    assert is_memory_scarce("hello") is True


def test_memory_scarce_when_long_but_no_name() -> None:
    long_no_name = "user likes dark mode and prefers concise answers when possible."
    assert is_memory_scarce(long_no_name) is True


def test_memory_not_scarce_when_has_english_name_keyword() -> None:
    text = "User profile:\n- name: Alice\n- role: data scientist with 5 years experience"
    assert is_memory_scarce(text) is False


def test_memory_not_scarce_when_has_chinese_self_intro() -> None:
    text = "用户偏好简洁回答，技术栈是 Python 和 Rust，我叫张伟。"
    assert is_memory_scarce(text) is False


# ── is_playwright_unavailable ───────────────────────────────────


def test_playwright_unavailable_when_path_none() -> None:
    assert is_playwright_unavailable(None) is True


def test_playwright_unavailable_when_file_missing(tmp_path: Path) -> None:
    assert is_playwright_unavailable(tmp_path / "missing.json") is True


def test_playwright_unavailable_when_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{not json", encoding="utf-8")
    assert is_playwright_unavailable(p) is True


def test_playwright_unavailable_when_no_servers(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    assert is_playwright_unavailable(p) is True


def test_playwright_unavailable_when_disabled(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp"],
                        "disabled": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p) is True


def test_playwright_available_when_enabled(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp"],
                        "disabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p) is False


def test_playwright_unavailable_when_mcp_globally_disabled(tmp_path: Path) -> None:
    """Even with an enabled playwright entry, ``enable_mcp=False`` means no MCP loads."""
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "playwright": {
                        "command": "npx",
                        "args": ["@playwright/mcp"],
                        "disabled": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p, mcp_globally_enabled=False) is True


def test_playwright_detected_via_args_when_named_differently(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "browser": {
                        "command": "npx",
                        "args": ["-y", "@playwright/mcp"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert is_playwright_unavailable(p) is False


# ── build_action_hints_prompt ───────────────────────────────────


def test_prompt_empty_when_no_scenarios() -> None:
    assert build_action_hints_prompt(memory_scarce=False, playwright_unavailable=False) == ""


def test_prompt_includes_onboarding_only() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=False)
    assert '"tab": "onboarding"' in out
    assert "browser-tools" not in out


def test_prompt_includes_browser_tools_only() -> None:
    out = build_action_hints_prompt(memory_scarce=False, playwright_unavailable=True)
    assert "browser-tools" in out
    assert "onboarding" not in out


def test_prompt_includes_both_scenarios() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=True)
    assert "onboarding" in out
    assert "browser-tools" in out
    assert "action_hint" in out
    assert "open_settings" in out
