"""Action Hint guidance: instruct the model to emit ``action_hint`` markdown
fences for the frontend to render as clickable settings shortcuts.

Two trigger scenarios:

* ``onboarding``: the user's MEMORY.md is scarce (very short, or contains no
  identity hint such as a name).
* ``browser-tools``: the user needs a capability that the available browser
  backend cannot provide and the Playwright MCP server is missing or disabled.

The model decides at generation time *whether* a hint fits the conversation;
this module only injects the contract and lists which tabs are eligible for
this session.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from box_agent.session_prompts import (
    is_memory_scarce, is_playwright_unavailable,
    is_playwright_unavailable_from_env_context, build_action_hints_prompt,
)

_ACTION_HINT_START = "```action_hint"
_ACTION_HINT_FENCE = "```"
_ALLOWED_ACTION_HINT_TABS = frozenset({"onboarding", "browser-tools"})
_ACTION_HINT_BLOCK_RE = re.compile(
    r"```action_hint[ \t]*(?:\r?\n)?(?P<payload>\{[\s\S]*?\})[ \t\r\n]*```"
)
_JSON_STRING_RE = r'"(?P<value>(?:\\.|[^"\\])*)"'


def normalize_action_hint_blocks(text: str) -> str:
    """Normalize known model drift back to the fenced action_hint contract.

    The model sometimes writes JSON on the opening fence line, for example
    `````action_hint {...}`````. The frontend only treats the fence as a
    machine-readable hint when JSON starts on the next line, so ACP repairs the
    narrow, validated shape before streaming it to the host.
    """

    def replace(match: re.Match[str]) -> str:
        normalized_payload = _normalize_action_hint_payload(match.group("payload"))
        if normalized_payload is None:
            return match.group(0)
        return f"{_ACTION_HINT_START}\n{normalized_payload}\n{_ACTION_HINT_FENCE}"

    return _ACTION_HINT_BLOCK_RE.sub(replace, text)


class ActionHintStreamNormalizer:
    """Incrementally normalize action_hint text without delaying normal output."""

    def __init__(self) -> None:
        self._buffer = ""
        self._holding_action_hint = False

    def push(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buffer += chunk
        return self._drain()

    def finish(self) -> list[str]:
        output = self._drain()
        if self._buffer:
            output.append(normalize_action_hint_blocks(self._buffer))
            self._buffer = ""
        self._holding_action_hint = False
        return output

    def _drain(self) -> list[str]:
        output: list[str] = []
        while self._buffer:
            if not self._holding_action_hint:
                start = self._buffer.find(_ACTION_HINT_START)
                if start == -1:
                    keep = _partial_action_hint_start_len(self._buffer)
                    emit_len = len(self._buffer) - keep
                    if emit_len > 0:
                        output.append(self._buffer[:emit_len])
                        self._buffer = self._buffer[emit_len:]
                    break
                if start > 0:
                    output.append(self._buffer[:start])
                    self._buffer = self._buffer[start:]
                self._holding_action_hint = True

            close = self._buffer.find(_ACTION_HINT_FENCE, len(_ACTION_HINT_START))
            if close == -1:
                break

            block_end = close + len(_ACTION_HINT_FENCE)
            block = self._buffer[:block_end]
            output.append(normalize_action_hint_blocks(block))
            self._buffer = self._buffer[block_end:]
            self._holding_action_hint = False

        return output


def _normalize_action_hint_payload(payload: str) -> str | None:
    data = _load_jsonish_action_hint_payload(payload.strip())
    if not isinstance(data, dict):
        return None

    action = data.get("action")
    params = data.get("params")
    tab = params.get("tab") if isinstance(params, dict) else None
    display_text = data.get("display_text")
    if (
        action != "open_settings"
        or tab not in _ALLOWED_ACTION_HINT_TABS
        or not isinstance(display_text, str)
    ):
        return None

    display_text = display_text.replace("\r", "").replace("\n", "").strip()
    if not display_text:
        return None

    normalized = {
        "action": "open_settings",
        "params": {"tab": tab},
        "display_text": display_text,
    }
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _load_jsonish_action_hint_payload(payload: str) -> dict[str, object] | None:
    try:
        data = json.loads(payload)
    except ValueError:
        repaired = _escape_control_chars_inside_json_strings(payload)
        try:
            data = json.loads(repaired)
        except ValueError:
            data = _extract_action_hint_payload(payload)
    return data if isinstance(data, dict) else None


def _escape_control_chars_inside_json_strings(text: str) -> str:
    chars: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            chars.append(char)
            if char == '"':
                in_string = True
            continue

        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            chars.append(char)
            escaped = True
        elif char == '"':
            chars.append(char)
            in_string = False
        elif char == "\n":
            chars.append("\\n")
        elif char == "\r":
            chars.append("\\r")
        elif char == "\t":
            chars.append("\\t")
        else:
            chars.append(char)
    return "".join(chars)


def _extract_action_hint_payload(payload: str) -> dict[str, object] | None:
    action = _extract_jsonish_string(payload, r'"action"\s*:\s*' + _JSON_STRING_RE)
    tab = _extract_jsonish_string(payload, r'"tab"\s*:\s*' + _JSON_STRING_RE)
    display_text = _extract_jsonish_string(
        payload,
        r'"display_text"\s*:\s*' + _JSON_STRING_RE,
    )
    if action is None or tab is None or display_text is None:
        return None
    return {
        "action": action,
        "params": {"tab": tab},
        "display_text": display_text,
    }


def _extract_jsonish_string(payload: str, pattern: str) -> str | None:
    match = re.search(pattern, payload, flags=re.DOTALL)
    if match is None:
        return None
    value = _escape_control_chars_inside_json_strings(f'"{match.group("value")}"')
    try:
        loaded = json.loads(value)
    except ValueError:
        return match.group("value")
    return loaded if isinstance(loaded, str) else None


def _partial_action_hint_start_len(text: str) -> int:
    max_len = min(len(text), len(_ACTION_HINT_START) - 1)
    for size in range(max_len, 0, -1):
        if _ACTION_HINT_START.startswith(text[-size:]):
            return size
    return 0
