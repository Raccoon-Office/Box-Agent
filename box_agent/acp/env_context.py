"""Environment-context injection for ACP sessions.

The Electron host knows things the runtime cannot easily probe — bundled
CLI paths (lark-cli, wecom-cli, dingtalk-cli), whether the browser-tools
shortcut has been completed, the user's platform. Hosts pass this through
``session/new._meta.env_context`` so the model stops claiming "you don't
have lark-cli installed" when in fact a bundled binary is sitting at a
known path.

The input is sanitized before it touches the system prompt:

* CLI paths must be absolute, free of control characters/backticks, and
  capped at ``_MAX_PATH_LEN`` chars. Violating entries are dropped.
* ``platform`` must match a conservative ``[A-Za-z0-9_-]`` charset and be
  ``<= _MAX_PLATFORM_LEN`` chars; violating values are discarded.
* Unknown top-level keys are still parsed into ``extras`` (so hosts can
  experiment without coordinating a backend bump) but are **not rendered
  into the system prompt**. They surface in logs only.

The trust model is "local stdio host" — we accept the host's facts but
defend against developer mistakes (accidental secrets in ``extras``,
malformed CLI paths) and accidental prompt-channel pollution (newlines
forging headings, backticks escaping fences).

This module deliberately does **not** consult ``action_hints``.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"cli", "platform", "browser_tools", "memory_configured"}
)

_MAX_PATH_LEN = 512
_MAX_NAME_LEN = 64
_MAX_PLATFORM_LEN = 32
_PLATFORM_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _has_unsafe_chars(value: str) -> bool:
    """True if string contains control chars, backticks, or markdown fence chars."""
    if "`" in value:
        return True
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _sanitize_cli(raw: Any) -> dict[str, str | None]:
    """Filter the cli mapping. Returns only entries that pass validation.

    Drops (with a warning log) any entry whose name has unsafe chars / is
    too long, or whose value is non-null but is not an absolute path /
    contains unsafe chars / exceeds the length cap.
    """
    if not isinstance(raw, dict):
        return {}

    cleaned: dict[str, str | None] = {}
    for name, value in raw.items():
        if not isinstance(name, str) or not name or len(name) > _MAX_NAME_LEN:
            logger.warning("env_context.cli: drop entry with invalid name %r", name)
            continue
        if _has_unsafe_chars(name):
            logger.warning("env_context.cli: drop entry %r — unsafe chars in name", name)
            continue

        if value is None:
            cleaned[name] = None
            continue
        if not isinstance(value, str):
            logger.warning("env_context.cli[%s]: drop — value is %s, not str/null", name, type(value).__name__)
            continue
        if len(value) > _MAX_PATH_LEN:
            logger.warning("env_context.cli[%s]: drop — path exceeds %d chars", name, _MAX_PATH_LEN)
            continue
        if _has_unsafe_chars(value):
            logger.warning("env_context.cli[%s]: drop — unsafe chars in path", name)
            continue
        if not (value.startswith("/") or (len(value) >= 3 and value[1:3] == ":\\")):
            # Accept POSIX absolute paths; tolerate Windows ``C:\...`` style.
            logger.warning("env_context.cli[%s]: drop — path %r is not absolute", name, value)
            continue
        cleaned[name] = value
    return cleaned


def _sanitize_platform(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    if not raw or len(raw) > _MAX_PLATFORM_LEN:
        logger.warning("env_context.platform: drop — length %d", len(raw))
        return None
    if not all(ch in _PLATFORM_ALLOWED_CHARS for ch in raw):
        logger.warning("env_context.platform: drop — disallowed chars in %r", raw)
        return None
    return raw


class BrowserToolsState(BaseModel):
    """Whether host-side browser tooling is installed/enabled.

    ``installed`` and ``enabled`` are independent: the user may have
    installed Chromium but kept the MCP entry disabled.
    """

    model_config = ConfigDict(extra="allow")

    installed: bool | None = None
    enabled: bool | None = None


class EnvContext(BaseModel):
    """Parsed view of ``session/new._meta.env_context``.

    Unknown top-level keys land in ``extras`` (logged only — not rendered
    into the system prompt, see :func:`build_env_context_prompt`). Known
    fields are typed and sanitized; anything that fails validation is
    discarded with a warning rather than rejecting the whole session.
    """

    model_config = ConfigDict(extra="ignore")

    cli: dict[str, str | None] = Field(default_factory=dict)
    platform: str | None = None
    browser_tools: BrowserToolsState | None = None
    memory_configured: bool | None = None
    extras: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_meta(cls, raw: Any) -> "EnvContext | None":
        """Parse ``_meta.env_context``. Returns ``None`` for missing/invalid input."""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            logger.warning("env_context must be an object, got %s", type(raw).__name__)
            return None

        known: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        for key, value in raw.items():
            if key in _KNOWN_TOP_LEVEL_KEYS:
                known[key] = value
            else:
                extras[key] = value
                logger.info("env_context: passthrough unknown key %s (not rendered into prompt)", key)

        # Sanitize known fields up-front so prompt renderer can trust them.
        if "cli" in known:
            known["cli"] = _sanitize_cli(known["cli"])
        if "platform" in known:
            known["platform"] = _sanitize_platform(known["platform"])

        try:
            ctx = cls.model_validate(known)
        except Exception as exc:
            logger.warning("env_context validation failed: %s", exc)
            return None
        ctx.extras = extras
        return ctx

    def is_empty(self) -> bool:
        """Empty iff nothing the prompt cares about. ``extras`` doesn't count."""
        return not (
            self.cli
            or self.platform
            or self.browser_tools is not None
            or self.memory_configured is not None
        )


def _format_cli_section(cli: dict[str, str | None]) -> list[str]:
    if not cli:
        return []
    available: list[str] = []
    missing: list[str] = []
    for name, path in sorted(cli.items()):
        if path:
            available.append(f"  - `{name}`: `{path}`")
        else:
            missing.append(f"`{name}`")
    lines: list[str] = []
    if available:
        lines.append("- 可用 CLI（机器上已安装，可以通过 bash 工具直接调用）：")
        lines.extend(available)
    if missing:
        lines.append("- 未安装 CLI（不要假装能调用）：" + ", ".join(missing))
    return lines


def _format_browser_tools(state: BrowserToolsState) -> list[str]:
    if state.installed is None and state.enabled is None:
        return []
    parts = []
    if state.installed is not None:
        parts.append(f"installed={str(state.installed).lower()}")
    if state.enabled is not None:
        parts.append(f"enabled={str(state.enabled).lower()}")
    return [f"- 浏览器工具状态：{', '.join(parts)}"]


def build_env_context_prompt(ctx: EnvContext | None) -> str:
    """Render ``EnvContext`` into a markdown checklist for the system prompt.

    Only sanitized known fields go in. ``extras`` is intentionally
    excluded: passthrough values must not be promoted into the model's
    high-priority context without explicit backend support.
    """
    if ctx is None or ctx.is_empty():
        return ""

    lines: list[str] = ["## 当前用户环境", ""]
    if ctx.platform:
        lines.append(f"- 操作系统：`{ctx.platform}`")
    lines.extend(_format_cli_section(ctx.cli))
    if ctx.browser_tools is not None:
        lines.extend(_format_browser_tools(ctx.browser_tools))
    if ctx.memory_configured is not None:
        state = "已完成" if ctx.memory_configured else "未完成"
        lines.append(f"- 个人记忆配置：{state}")

    lines.append("")
    lines.append(
        "请把以上信息当作事实依据：不要否认已列出可用的工具，也不要假装能调用未列出的工具。"
        "如果用户的需求需要某个未安装的工具，明确告知并建议安装途径。"
    )
    return "\n".join(lines)
