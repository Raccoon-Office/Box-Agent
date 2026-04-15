"""Agent lifecycle hooks.

Hooks let external code observe and optionally intercept key points in
the agent execution loop.  They are registered via ``config.yaml`` (as
dotted class paths) or programmatically, and are loaded identically by
both CLI and ACP — ensuring consistent behaviour across transports.

Usage in config.yaml::

    hooks:
      - "box_agent.hooks.builtin.SafetyHook"
      - "my_project.hooks.AuditHook"

Programmatic::

    from box_agent.hooks import BaseHook

    class MyHook(BaseHook):
        async def on_tool_result(self, *, tool_call_id, tool_name,
                                  success, content, error):
            if "secret" in content:
                return ("[REDACTED]", None)
            return None

    agent = Agent(..., hooks=[MyHook()])
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# ── Base hook (no-op defaults) ─────────────────────────────────


class BaseHook:
    """Convenience base class with no-op defaults for all lifecycle hooks.

    Subclass and override only the methods you need.  Duck typing is
    also supported: any object with the right async methods works
    without inheriting from ``BaseHook``.
    """

    async def on_agent_start(
        self,
        *,
        messages: list,
        tools: dict[str, Any],
        max_steps: int,
    ) -> None:
        """Called once before the agent loop begins."""

    async def on_step_start(self, *, step: int, max_steps: int) -> None:
        """Called at the beginning of each step (before the LLM call)."""

    async def on_llm_response(self, *, response: Any) -> None:
        """Called after the LLM response is constructed."""

    async def on_tool_start(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Called before tool execution.

        Return a modified ``arguments`` dict to override, or ``None``
        to keep the original.
        """
        return None

    async def on_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        success: bool,
        content: str,
        error: str | None,
    ) -> tuple[str, str | None] | None:
        """Called after tool execution, before the result event is yielded.

        Return a ``(content, error)`` tuple to override, or ``None``
        to keep the original.  Useful for safety filtering.
        """
        return None

    async def on_step_end(
        self,
        *,
        step: int,
        elapsed_seconds: float,
        total_elapsed_seconds: float,
    ) -> None:
        """Called when a step completes."""

    async def on_done(self, *, stop_reason: Any, final_content: str) -> None:
        """Called before the agent loop exits."""

    async def on_error(
        self,
        *,
        message: str,
        is_fatal: bool,
        exception: Exception | None,
    ) -> None:
        """Called when an error occurs."""


# ── Hook manager ───────────────────────────────────────────────


class HookManager:
    """Dispatches lifecycle calls to a list of hooks.

    Hook errors are swallowed and logged — they never crash the core
    loop.  For interceptor hooks (``on_tool_start``, ``on_tool_result``),
    the first hook to return a non-``None`` value wins; subsequent hooks
    receive the modified value.
    """

    def __init__(self, hooks: list | None = None) -> None:
        self._hooks: list = list(hooks or [])

    @property
    def hooks(self) -> list:
        return self._hooks

    def add(self, hook: Any) -> None:
        self._hooks.append(hook)

    # ── Observational fires ────────────────────────────────

    async def fire_agent_start(
        self, *, messages: list, tools: dict[str, Any], max_steps: int
    ) -> None:
        for hook in self._hooks:
            method = getattr(hook, "on_agent_start", None)
            if method is None:
                continue
            try:
                await method(messages=messages, tools=tools, max_steps=max_steps)
            except Exception as exc:
                log.warning("Hook %s.on_agent_start failed: %s", type(hook).__name__, exc)

    async def fire_step_start(self, *, step: int, max_steps: int) -> None:
        for hook in self._hooks:
            method = getattr(hook, "on_step_start", None)
            if method is None:
                continue
            try:
                await method(step=step, max_steps=max_steps)
            except Exception as exc:
                log.warning("Hook %s.on_step_start failed: %s", type(hook).__name__, exc)

    async def fire_llm_response(self, *, response: Any) -> None:
        for hook in self._hooks:
            method = getattr(hook, "on_llm_response", None)
            if method is None:
                continue
            try:
                await method(response=response)
            except Exception as exc:
                log.warning("Hook %s.on_llm_response failed: %s", type(hook).__name__, exc)

    async def fire_step_end(
        self, *, step: int, elapsed_seconds: float, total_elapsed_seconds: float
    ) -> None:
        for hook in self._hooks:
            method = getattr(hook, "on_step_end", None)
            if method is None:
                continue
            try:
                await method(
                    step=step,
                    elapsed_seconds=elapsed_seconds,
                    total_elapsed_seconds=total_elapsed_seconds,
                )
            except Exception as exc:
                log.warning("Hook %s.on_step_end failed: %s", type(hook).__name__, exc)

    async def fire_done(self, *, stop_reason: Any, final_content: str) -> None:
        for hook in self._hooks:
            method = getattr(hook, "on_done", None)
            if method is None:
                continue
            try:
                await method(stop_reason=stop_reason, final_content=final_content)
            except Exception as exc:
                log.warning("Hook %s.on_done failed: %s", type(hook).__name__, exc)

    async def fire_error(
        self, *, message: str, is_fatal: bool, exception: Exception | None
    ) -> None:
        for hook in self._hooks:
            method = getattr(hook, "on_error", None)
            if method is None:
                continue
            try:
                await method(message=message, is_fatal=is_fatal, exception=exception)
            except Exception as exc:
                log.warning("Hook %s.on_error failed: %s", type(hook).__name__, exc)

    # ── Interceptor fires ──────────────────────────────────

    async def fire_tool_start(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Returns (possibly modified) arguments."""
        current_args = arguments
        for hook in self._hooks:
            method = getattr(hook, "on_tool_start", None)
            if method is None:
                continue
            try:
                result = await method(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=current_args,
                )
                if result is not None:
                    current_args = result
            except Exception as exc:
                log.warning("Hook %s.on_tool_start failed: %s", type(hook).__name__, exc)
        return current_args

    async def fire_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        success: bool,
        content: str,
        error: str | None,
    ) -> tuple[str, str | None]:
        """Returns (possibly modified) (content, error)."""
        current_content = content
        current_error = error
        for hook in self._hooks:
            method = getattr(hook, "on_tool_result", None)
            if method is None:
                continue
            try:
                result = await method(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    success=success,
                    content=current_content,
                    error=current_error,
                )
                if result is not None:
                    current_content, current_error = result
            except Exception as exc:
                log.warning("Hook %s.on_tool_result failed: %s", type(hook).__name__, exc)
        return current_content, current_error


# ── Loader ─────────────────────────────────────────────────────

# User hooks directory — added to sys.path so that config.yaml
# can reference modules by short name (e.g. ``"safety.SafetyHook"``).
USER_HOOKS_DIR = Path.home() / ".box-agent" / "hooks"


def _ensure_hooks_dir_on_path() -> None:
    """Add ``~/.box-agent/hooks/`` to ``sys.path`` if it exists."""
    hooks_dir = str(USER_HOOKS_DIR)
    if USER_HOOKS_DIR.is_dir() and hooks_dir not in sys.path:
        sys.path.insert(0, hooks_dir)


def load_hooks(class_paths: list[str]) -> list:
    """Import and instantiate hook classes from dotted paths.

    Each entry in *class_paths* is a fully-qualified class name.
    Two resolution modes are supported:

    1. **Package-qualified** (e.g. ``"box_agent.hooks.BaseHook"``) —
       standard ``importlib`` resolution against ``sys.path``.
    2. **User hooks** (e.g. ``"safety.SafetyHook"``) — the directory
       ``~/.box-agent/hooks/`` is prepended to ``sys.path`` so that
       user-authored modules placed there are importable directly.

    Example ``~/.box-agent/hooks/safety.py``::

        from box_agent.hooks import BaseHook

        class SafetyHook(BaseHook):
            async def on_tool_result(self, *, tool_call_id, tool_name,
                                      success, content, error):
                if "secret" in content:
                    return ("[REDACTED]", error)
                return None

    Then in ``config.yaml``::

        hooks:
          - "safety.SafetyHook"

    Malformed or missing entries are logged as warnings and skipped —
    they never prevent the agent from starting.
    """
    _ensure_hooks_dir_on_path()

    hooks: list = []
    for path in class_paths:
        try:
            module_path, class_name = path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            hooks.append(cls())
        except Exception as exc:
            log.warning("Failed to load hook %r: %s", path, exc)
    return hooks
