"""SenseNova DSML tool-call fallback: parser plus recovery hook.

SenseNova models (``sensenova-*``) occasionally misroute their output
channels: the response arrives with ``finish_reason="stop"``, empty
``content`` and no structured ``tool_calls``, while the *actual* tool call is
emitted as native DSML markup inside the thinking (reasoning) field — or,
more rarely, inside content::

    <｜DSML｜tool_calls>
    <｜DSML｜invoke name="bash">
    <｜DSML｜parameter name="command" string="true">ls -la</｜DSML｜parameter>
    </｜DSML｜invoke>
    </｜DSML｜tool_calls>

The delimiter is the fullwidth vertical line U+FF5C (``｜``); some relays
normalize it to ASCII ``|``, so both are accepted. The parser is pure and
side-effect free: parsing never raises, and malformed/unclosed/nameless
blocks are skipped rather than aborting the salvage.

``DsmlToolCallRecoveryHook`` wires the parser into the agent loop as a
lifecycle hook (registered ahead of user hooks by ``box_agent.composition``):
it mutates the leaked response in place so the kernel treats it as a normal
tool-call turn.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .hooks import BaseHook
from .schema import FunctionCall, LLMResponse, ToolCall

_log = logging.getLogger(__name__)

# Optional whitespace is tolerated around every token; the delimiter may be
# the fullwidth U+FF5C or an ASCII pipe after relay normalization.
_D = r"\s*[|｜]\s*"

_INVOKE_RE = re.compile(
    rf"<{_D}DSML{_D}invoke\s+name\s*=\s*\"(?P<name>[^\"\n]*)\"\s*>"
    rf"(?P<body>.*?)"
    rf"<\s*/{_D}DSML{_D}invoke\s*>",
    re.DOTALL,
)

_PARAMETER_RE = re.compile(
    rf"<{_D}DSML{_D}parameter\s+name\s*=\s*\"(?P<name>[^\"\n]*)\""
    rf"(?P<attrs>[^>]*)>"
    rf"(?P<value>.*?)"
    rf"<\s*/{_D}DSML{_D}parameter\s*>",
    re.DOTALL,
)

# Any dangling DSML wrapper tag (the tool_calls envelope open/close) that
# remains after parsed invokes are removed.
_WRAPPER_TAG_RE = re.compile(
    rf"<\s*/?{_D}DSML{_D}tool_calls\s*>",
)

# An opening DSML tag left over inside an invoke body means a parameter (or
# nested markup) was cut off mid-stream — the block is malformed.
_LEFTOVER_DSML_TAG_RE = re.compile(rf"<\s*/?{_D}DSML{_D}")

_STRING_ATTR_RE = re.compile(r"string\s*=\s*\"true\"", re.IGNORECASE)

# Sanity bound so a pathological response cannot spawn unbounded calls.
_MAX_RECOVERED_CALLS = 8


@dataclass(frozen=True)
class DsmlToolCall:
    """One successfully parsed DSML ``invoke`` block.

    ``span`` is the half-open character range of the full invoke block in the
    source text, so callers can remove exactly what was consumed.
    """

    name: str
    arguments: dict[str, Any]
    span: tuple[int, int]


def _parse_invoke_body(body: str) -> dict[str, Any] | None:
    """Parse parameters from one invoke body.

    Returns ``None`` when the body contains unclosed DSML markup (a
    truncated/malformed block); plain stray text between parameters is
    tolerated and ignored.
    """

    arguments: dict[str, Any] = {}
    for match in _PARAMETER_RE.finditer(body):
        param_name = match.group("name").strip()
        if not param_name:
            return None
        raw_value = match.group("value").strip()
        if _STRING_ATTR_RE.search(match.group("attrs")):
            arguments[param_name] = raw_value
        else:
            # Non-string parameters carry JSON-ish literals; fall back to the
            # raw text when the value is not valid JSON.
            try:
                arguments[param_name] = json.loads(raw_value)
            except (TypeError, ValueError):
                arguments[param_name] = raw_value
    leftover = _PARAMETER_RE.sub("", body)
    if _LEFTOVER_DSML_TAG_RE.search(leftover):
        return None
    return arguments


def parse_dsml_tool_calls(text: str | None) -> list[DsmlToolCall]:
    """Extract every complete, well-formed DSML invoke block from ``text``.

    Tolerates surrounding thinking prose, blank lines, and multiple invokes
    (inside or outside a ``tool_calls`` envelope). Blocks that are unclosed,
    nameless, or contain unclosed parameter markup are skipped individually;
    remaining valid invokes are still returned. Never raises.
    """

    if not text:
        return []
    calls: list[DsmlToolCall] = []
    try:
        for match in _INVOKE_RE.finditer(text):
            if len(calls) >= _MAX_RECOVERED_CALLS:
                break
            name = match.group("name").strip()
            if not name:
                continue
            arguments = _parse_invoke_body(match.group("body"))
            if arguments is None:
                continue
            calls.append(
                DsmlToolCall(
                    name=name,
                    arguments=arguments,
                    span=(match.start(), match.end()),
                )
            )
    except Exception:
        # Defensive: a fallback parser must never break the agent loop.
        return []
    return calls


def strip_dsml_blocks(text: str | None, calls: list[DsmlToolCall]) -> str | None:
    """Remove parsed invoke blocks and dangling DSML wrapper tags.

    Surrounding thinking text is preserved. Returns ``None`` when nothing
    meaningful remains, so callers can store ``thinking=None`` instead of an
    empty string.
    """

    if not text:
        return None
    cleaned = text
    for call in sorted(calls, key=lambda c: c.span[0], reverse=True):
        start, end = call.span
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = _WRAPPER_TAG_RE.sub("", cleaned)
    # Collapse the blank-line runs left behind by removed blocks.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or None


def recover_dsml_tool_calls(response: LLMResponse) -> list[str]:
    """Fill ``response.tool_calls`` from DSML markup, in place.

    Callers must apply their own gating (SenseNova model, no structured
    tool_calls, empty content) before invoking this. Thinking is consulted
    first; content is the fallback source. Successfully parsed blocks are
    removed from the field they were recovered from; other text is kept.

    Returns the recovered tool names (empty when nothing was salvageable, in
    which case the response is left untouched).
    """

    thinking_calls = parse_dsml_tool_calls(response.thinking)
    content_calls: list[DsmlToolCall] = []
    if not thinking_calls and isinstance(response.content, str):
        content_calls = parse_dsml_tool_calls(response.content)
    parsed = thinking_calls or content_calls
    if not parsed:
        return []

    response.tool_calls = [
        ToolCall(
            id=f"call_{uuid.uuid4().hex[:24]}",
            type="function",
            function=FunctionCall(name=call.name, arguments=call.arguments),
        )
        for call in parsed
    ]
    if thinking_calls:
        response.thinking = strip_dsml_blocks(response.thinking, thinking_calls)
    if content_calls:
        cleaned = strip_dsml_blocks(response.content, content_calls)
        response.content = cleaned or ""
    # Mirror the client's own SenseNova recovery: a salvaged call set is a
    # tool-call turn, not a natural stop.
    response.finish_reason = "tool_calls"
    return [call.name for call in parsed]


# ── Lifecycle hook ────────────────────────────────────────────


class DsmlToolCallRecoveryHook(BaseHook):
    """Recover DSML-leaked SenseNova tool calls by mutating the response.

    Observes ``on_llm_response`` and mutates the response **in place** (the
    kernel passes the live ``LLMResponse`` by reference; see the
    ``BaseHook.on_llm_response`` contract): recovered calls are written to
    ``response.tool_calls`` with ``finish_reason="tool_calls"``, and the
    parsed DSML blocks are stripped from the field they leaked into.
    Downstream logic (permissions, visibility, budgets, persistence) then
    treats the response exactly like a native tool-call turn.

    Gating is deliberately strict so normal responses are never touched:
    the model must belong to the SenseNova family, the response must have
    no structured tool calls, and ``content`` must be empty after
    stripping.  When parsing finds no complete DSML invoke block, the
    response is left untouched and the existing empty-final-answer retry
    applies unchanged.

    The model name is resolved per call: ``response.model`` when present
    (future-proofing), otherwise the injected ``model_getter``/``llm``
    source.  A hook constructed without any model source (e.g. via
    ``config.yaml`` ``load_hooks``, which instantiates classes with no
    arguments) is inert — it never matches the SenseNova gate.
    """

    def __init__(
        self,
        model_getter: Callable[[], str | None] | None = None,
        *,
        llm: Any = None,
    ) -> None:
        if model_getter is None and llm is not None:
            model_getter = lambda: getattr(llm, "model", "")  # noqa: E731
        self._model_getter = model_getter or (lambda: "")

    def _resolve_model(self, response: Any) -> str:
        model = getattr(response, "model", None)
        if isinstance(model, str) and model:
            return model
        try:
            candidate = self._model_getter()
        except Exception:
            return ""
        return candidate if isinstance(candidate, str) else ""

    async def on_llm_response(self, *, response: Any) -> None:
        try:
            if getattr(response, "tool_calls", None):
                return
            content = getattr(response, "content", None)
            if not isinstance(content, str) or content.strip():
                return
            # Deferred import: keeps this module free of client import-time
            # coupling and reuses the canonical SenseNova dialect detection
            # (prefixes "sensenova-"/"sn-sensenova-" plus the operator env
            # extension), identical to the client's own recovery gating.
            from .llm.openai_client import _is_sensenova_model

            if not _is_sensenova_model(self._resolve_model(response)):
                return
            recovered_names = recover_dsml_tool_calls(response)
            if recovered_names:
                _log.warning(
                    "sensenova_dsml_fallback: recovered tool calls %s from "
                    "thinking/content DSML markup provider_response_id=%s",
                    recovered_names,
                    getattr(response, "provider_response_id", None),
                )
        except Exception:
            # A recovery hook must never break the main loop (HookManager /
            # HookBus also swallow and log hook errors).
            _log.warning("sensenova_dsml_fallback: recovery failed", exc_info=True)
