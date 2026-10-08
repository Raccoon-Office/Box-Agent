"""Unit tests for box_agent.acp.action_hints."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent import session_assembly
from box_agent.acp.action_hints import (
    ActionHintStreamNormalizer,
    build_action_hints_prompt,
    is_memory_scarce,
    is_playwright_unavailable,
    is_playwright_unavailable_from_env_context,
    normalize_action_hint_blocks,
)


@pytest.fixture(autouse=True)
def isolate_mcp_sources(monkeypatch) -> None:
    for name in (
        "BOX_AGENT_MCP_CONFIG_PATH",
        "BOX_AGENT_USER_MCP_CONFIG_PATH",
        "BOX_AGENT_SYSTEM_MCP_CONFIG_PATH",
        "BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH",
        "BOX_AGENT_HOME",
    ):
        monkeypatch.delenv(name, raising=False)


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


def test_playwright_unavailable_from_env_context_when_available_false() -> None:
    ctx = SimpleNamespace(browser_tools=SimpleNamespace(available=False))
    assert is_playwright_unavailable_from_env_context(ctx) is True


def test_playwright_available_from_env_context_when_available_true() -> None:
    ctx = SimpleNamespace(browser_tools=SimpleNamespace(available=True))
    assert is_playwright_unavailable_from_env_context(ctx) is False


def test_playwright_env_context_absent_does_not_force_unavailable() -> None:
    assert is_playwright_unavailable_from_env_context(None) is False
    assert is_playwright_unavailable_from_env_context(SimpleNamespace()) is False


@pytest.mark.parametrize("user_file_exists", [True, False])
@pytest.mark.parametrize("host_available", [True, None])
def test_enabled_system_browser_does_not_request_enablement(
    monkeypatch, tmp_path: Path, user_file_exists: bool, host_available: bool | None,
) -> None:
    user = tmp_path / "mcp.json"
    system = tmp_path / "mcp.system.json"
    if user_file_exists:
        user.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    system.write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "pw", "disabled": False}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOX_AGENT_MCP_CONFIG_PATH", str(user))
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
    monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(system))
    monkeypatch.setattr(session_assembly.Config, "find_config_file", lambda _: None)
    config = SimpleNamespace(tools=SimpleNamespace(enable_mcp=True, mcp_config_path=str(user)))
    memory = SimpleNamespace(read_core=lambda: "User name: Alice, prefers concise answers.")
    host = (
        SimpleNamespace(browser_tools=SimpleNamespace(available=host_available))
        if host_available is not None else None
    )

    assert session_assembly._build_action_hints_prompt(config, memory, host) == ""


@pytest.mark.parametrize(
    "system_disabled,user_disabled,host_available,mcp_enabled,expect_hint",
    [
        (False, True, True, True, False),
        (True, False, True, True, True),
        (False, False, False, True, True),
        (False, False, True, False, True),
    ],
)
def test_browser_hint_respects_system_precedence_and_availability_gates(
    monkeypatch, tmp_path: Path, system_disabled: bool, user_disabled: bool,
    host_available: bool, mcp_enabled: bool, expect_hint: bool,
) -> None:
    user = tmp_path / "mcp.json"
    system = tmp_path / "mcp.system.json"
    for path, disabled in ((user, user_disabled), (system, system_disabled)):
        path.write_text(
            json.dumps({"mcpServers": {"playwright": {"command": "pw", "disabled": disabled}}}),
            encoding="utf-8",
        )
    monkeypatch.setenv("BOX_AGENT_MCP_CONFIG_PATH", str(user))
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
    monkeypatch.setenv("BOX_AGENT_SYSTEM_MCP_CONFIG_PATH", str(system))
    config = SimpleNamespace(tools=SimpleNamespace(enable_mcp=mcp_enabled, mcp_config_path=str(user)))
    memory = SimpleNamespace(read_core=lambda: "User name: Alice, prefers concise answers.")
    host = SimpleNamespace(browser_tools=SimpleNamespace(available=host_available))

    prompt = session_assembly._build_action_hints_prompt(config, memory, host)

    assert ('"tab": "browser-tools"' in prompt) is expect_hint


def test_browser_available_in_connector_source(monkeypatch, tmp_path: Path) -> None:
    user = tmp_path / "mcp.json"
    connector = tmp_path / "mcp.connector.json"
    connector.write_text(
        json.dumps({"mcpServers": {"browser": {
            "command": "npx", "args": ["@playwright/mcp"], "_connectorId": "browser",
        }}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOX_AGENT_USER_MCP_CONFIG_PATH", str(user))
    monkeypatch.setenv("BOX_AGENT_CONNECTOR_MCP_CONFIG_PATH", str(connector))

    assert is_playwright_unavailable(user) is False


@pytest.mark.parametrize("key", ["mcpServers", "servers"])
def test_standalone_browser_hint_keeps_single_file_formats(tmp_path: Path, key: str) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({key: {"playwright": {"command": "pw"}}}), encoding="utf-8")

    assert is_playwright_unavailable(path) is False


@pytest.mark.parametrize("disabled", [True, False])
def test_standalone_browser_hint_uses_configured_file(
    monkeypatch, tmp_path: Path, disabled: bool,
) -> None:
    user = tmp_path / "mcp.json"
    user.write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "pw", "disabled": disabled}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(session_assembly, "state_path", lambda _: tmp_path / "missing.json")
    monkeypatch.setattr(session_assembly.Config, "find_config_file", lambda _: user)
    config = SimpleNamespace(tools=SimpleNamespace(enable_mcp=True, mcp_config_path=str(user)))
    memory = SimpleNamespace(read_core=lambda: "User name: Alice, prefers concise answers.")

    prompt = session_assembly._build_action_hints_prompt(config, memory)

    assert ('"tab": "browser-tools"' in prompt) is disabled


@pytest.mark.parametrize("payload", [[], None, {"mcpServers": []}, {"mcpServers": {"playwright": []}}])
def test_browser_unavailable_with_invalid_config_shape(tmp_path: Path, payload) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert is_playwright_unavailable(path) is True


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
    assert "user_browser_*" in out
    assert "不要仅因受管浏览器缺失" in out


def test_prompt_includes_both_scenarios() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=True)
    assert "onboarding" in out
    assert "browser-tools" in out
    assert "action_hint" in out
    assert "open_settings" in out


def test_prompt_forbids_xml_action_hint_tags() -> None:
    """Prompt must hard-forbid the <action_hint> XML wrapper the model
    occasionally emits, which the frontend cannot reliably parse."""
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=False)
    assert "<action_hint>" in out  # mentioned only as a prohibited form
    assert "禁止" in out
    # The positive example must use the triple-backtick fence.
    assert "```action_hint" in out


def test_prompt_forbids_json_on_action_hint_fence_line() -> None:
    out = build_action_hints_prompt(memory_scarce=True, playwright_unavailable=False)

    assert "不要在同一行追加" in out
    assert "JSON 必须从下一行开始" in out
    assert "display_text" in out
    assert "不要包含换行符" in out


def test_normalize_action_hint_repairs_json_on_fence_line() -> None:
    malformed = (
        "```action_hint { "
        '"action": "open_settings", '
        '"params": {"tab": "onboarding"}, '
        '"display_text": "去个人记忆页完善偏好，我会更懂你的工作方式。\n" '
        "} ```"
    )

    out = normalize_action_hint_blocks(malformed)

    assert out.startswith("```action_hint\n")
    assert "```action_hint {" not in out
    payload = out.removeprefix("```action_hint\n").removesuffix("\n```")
    data = json.loads(payload)
    assert data == {
        "action": "open_settings",
        "params": {"tab": "onboarding"},
        "display_text": "去个人记忆页完善偏好，我会更懂你的工作方式。",
    }


def test_action_hint_stream_normalizer_handles_split_fence_start() -> None:
    normalizer = ActionHintStreamNormalizer()

    chunks = []
    chunks.extend(normalizer.push("好的。```action"))
    chunks.extend(
        normalizer.push(
            '_hint { "action": "open_settings", '
            '"params": {"tab": "browser-tools"}, '
            '"display_text": "去启用浏览器工具" } ```'
        )
    )
    chunks.extend(normalizer.finish())
    out = "".join(chunks)

    assert out.startswith("好的。```action_hint\n")
    assert "```action_hint {" not in out
    payload = out.split("```action_hint\n", 1)[1].removesuffix("\n```")
    assert json.loads(payload) == {
        "action": "open_settings",
        "params": {"tab": "browser-tools"},
        "display_text": "去启用浏览器工具",
    }
