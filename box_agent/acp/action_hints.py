"""Action Hint guidance: instruct the model to emit ``action_hint`` markdown
fences for the frontend to render as clickable settings shortcuts.

Two trigger scenarios:

* ``onboarding``: the user's MEMORY.md is scarce (very short, or contains no
  identity hint such as a name).
* ``browser-tools``: the user is asking about browser automation / web
  scraping but the Playwright MCP server is missing or disabled.

The model decides at generation time *whether* a hint fits the conversation;
this module only injects the contract and lists which tabs are eligible for
this session.
"""

from __future__ import annotations

import json
from pathlib import Path

_MEMORY_MIN_CHARS = 30
# Lower-cased identity keywords. If any is present we consider the user has
# shared at least their name and skip the onboarding hint.
_IDENTITY_KEYWORDS = ("name", "姓名", "我叫", "我是", "叫我")

_HINT_FORMAT_BLOCK = (
    "```action_hint\n"
    "{\n"
    '  "action": "open_settings",\n'
    '  "params": {"tab": "<tab-name>"},\n'
    '  "display_text": "<面向用户的一句话引导文案>"\n'
    "}\n"
    "```"
)


def is_memory_scarce(memory_text: str | None) -> bool:
    """Return True when MEMORY.md is empty, very short, or has no name hint."""
    if not memory_text:
        return True
    stripped = memory_text.strip()
    if len(stripped) < _MEMORY_MIN_CHARS:
        return True
    lowered = stripped.lower()
    return not any(keyword in lowered for keyword in _IDENTITY_KEYWORDS)


def is_playwright_unavailable(
    mcp_config_path: Path | None,
    *,
    mcp_globally_enabled: bool = True,
) -> bool:
    """Return True when the Playwright MCP server is absent or disabled.

    Missing file or unreadable JSON is treated as "unavailable" — the model
    is told it cannot rely on a browser tool either way. ``mcp_globally_enabled``
    short-circuits to True when the runtime has MCP turned off entirely
    (config ``tools.enable_mcp = false``); in that case no entry in mcp.json
    is going to load.
    """
    if not mcp_globally_enabled:
        return True
    if mcp_config_path is None or not mcp_config_path.exists():
        return True
    try:
        data = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        return True

    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        haystack = f"{name} {entry.get('command', '')} {' '.join(map(str, entry.get('args', []) or []))}".lower()
        if "playwright" not in haystack:
            continue
        if entry.get("disabled", False):
            continue
        return False  # Found an enabled playwright entry
    return True


def build_action_hints_prompt(
    *,
    memory_scarce: bool,
    playwright_unavailable: bool,
) -> str:
    """Build the system-prompt section that defines the action_hint contract.

    Returns an empty string when no scenario is active so the prompt stays
    lean. Each enabled scenario contributes one bullet describing the tab
    and when to use it.
    """
    rules: list[str] = []
    if memory_scarce:
        rules.append(
            '- 用户问候、自我介绍、问"你是谁/你能做什么"等关系建立类话题，且当前对用户了解很少时 → '
            '使用 `"tab": "onboarding"`，引导用户去"个人记忆"页完善信息。'
        )
    if playwright_unavailable:
        rules.append(
            "- 用户提出需要浏览器操作的需求（打开网页、抓取页面、截图、自动化点击、Playwright 等），"
            '但当前会话没有可用的浏览器工具时 → 使用 `"tab": "browser-tools"`，引导用户去启用浏览器工具。'
        )

    if not rules:
        return ""

    return (
        "## 用户引导提示 (Action Hint)\n"
        "当用户的当前问题真正契合下述场景时，在你的回复末尾追加一个 `action_hint` 围栏块。"
        "前端会解析它并渲染为可点击链接，引导用户打开对应的设置页。\n\n"
        "### 格式契约\n"
        f"{_HINT_FORMAT_BLOCK}\n\n"
        "### 触发场景（仅以下场景启用）\n"
        + "\n".join(rules)
        + "\n\n"
        "### 约束\n"
        "- 一次回复最多输出一个 `action_hint` 块。\n"
        "- 块内必须是合法 JSON，且 `tab` 字段必须取自上述列表。\n"
        "- 用户语境不契合时不要输出，避免打扰。\n"
        "- 正文先正常回答用户的问题，再追加这个块；不要把它放在正文中间。"
    )
