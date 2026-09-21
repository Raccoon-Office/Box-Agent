"""Run-scoped Cua vision adapter for the generic MCP result extension."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from box_agent.llm.capabilities import image_input_support
from box_agent.tools.mcp_result_hooks import bind_mcp_result_adapter

from .config import CuaConfig
from .image_encoding import encode_canonical_cua_image
from .sidecar import CuaImageSidecar

__all__ = ["CuaBindings", "build_cua_bindings", "cua_vision_enabled"]

_VISION_DISABLE_VALUES = {"0", "false", "no"}
_MISSING = object()


def cua_vision_enabled(config: CuaConfig) -> bool:
    """Read the plugin's vision setting and environment kill-switch."""
    raw = os.environ.get("BOX_AGENT_CUA_VISION")
    return config.feed_screenshots and (
        raw is None or raw.strip().lower() not in _VISION_DISABLE_VALUES
    )


@dataclass
class CuaBindings:
    """A plugin-owned MCP adapter; the model client is never replaced."""

    llm: Any
    config: CuaConfig
    active: bool
    # Plugin activation and screenshot feeding are separate gates: disabling
    # vision must still expose the configured CUA MCP tools as text tools.
    mcp_active: bool = True
    _agent: Any = field(default=None, init=False, repr=False)
    _original_instance_method: Any = field(default=_MISSING, init=False, repr=False)
    sidecar: CuaImageSidecar | None = field(default=None, repr=False)

    def allows_transient_followup(self, *, server_name: str, remote_name: str) -> bool:
        return (
            self.active
            and server_name == self.config.server_name
            and image_input_support(self.llm) is True
        )

    def transient_followup_content(
        self, *, server_name: str, remote_name: str,
        inline_images: list[dict[str, str]],
    ) -> list[dict[str, Any]] | None:
        if not self.allows_transient_followup(
            server_name=server_name, remote_name=remote_name,
        ):
            return None
        blocks = []
        for entry in inline_images:
            block = encode_canonical_cua_image(
                entry.get("data", ""), entry.get("mime_type", ""),
            )
            if block is None:
                continue
            blocks.append(block)
        return blocks or None

    def persist_image_references(
        self, *, server_name: str, remote_name: str,
        inline_images: list[dict[str, str]],
    ) -> list[dict[str, Any]] | None:
        """Persist CUA screenshots so the tool reply can expose their paths."""
        if not self.allows_transient_followup(
            server_name=server_name, remote_name=remote_name,
        ) or self.sidecar is None:
            return None
        references = []
        for entry in inline_images:
            block = encode_canonical_cua_image(
                entry.get("data", ""), entry.get("mime_type", ""),
            )
            if block is None:
                continue
            reference = self.sidecar.persist_block(block)
            if reference is not None:
                references.append(reference)
        return references or None

    def bind_run(self, events: AsyncIterator[Any]) -> AsyncIterator[Any]:
        if not self.mcp_active:
            return events
        return _ScopedCuaEvents(events, self)

    def install_agent(self, agent: Any) -> None:
        """Bind this adapter through the generic run activation capability."""
        if not self.mcp_active or self._agent is not None:
            return
        original = agent.run_events
        self._original_instance_method = vars(agent).get("run_events", _MISSING)
        self._agent = agent
        self._activate_server_tools()

        def bound_run_events(*args: Any, **kwargs: Any):
            return self.bind_run(original(*args, **kwargs))

        agent.run_events = bound_run_events

    def _activate_server_tools(self) -> None:
        """Expose the configured MCP server through the existing session map."""
        if not self.mcp_active or self._agent is None:
            return
        exposure = getattr(self._agent, "mcp_tool_exposure", None)
        activate = getattr(exposure, "activate_server", None)
        if callable(activate):
            try:
                activate(self.config.server_name)
            except Exception:
                # A late/failed MCP catalog publication must not make the
                # ordinary Agent run fail; the normal tool-search path can
                # still discover the server when it becomes ready.
                return

    def close(self) -> None:
        """Restore the instance exactly as it was before activation."""
        if self._agent is None:
            return
        if self._original_instance_method is _MISSING:
            del self._agent.run_events
        else:
            self._agent.run_events = self._original_instance_method
        self._agent = None
        self._original_instance_method = _MISSING


class _ScopedCuaEvents:
    """Bind per pull so anext/aclose can safely run in different tasks."""

    def __init__(self, events: AsyncIterator[Any], bindings: CuaBindings) -> None:
        self._events = events.__aiter__()
        self._bindings = bindings

    def __aiter__(self) -> "_ScopedCuaEvents":
        return self

    async def __anext__(self) -> Any:
        with bind_mcp_result_adapter(self._bindings):
            self._bindings._activate_server_tools()
            return await self._events.__anext__()

    async def aclose(self) -> None:
        close = getattr(self._events, "aclose", None)
        if close is not None:
            with bind_mcp_result_adapter(self._bindings):
                await close()


def build_cua_bindings(
    *, llm: Any, config: CuaConfig, sidecar_dir: Path | None = None,
    enabled: bool = True,
) -> CuaBindings:
    sidecar = CuaImageSidecar(sidecar_dir) if sidecar_dir is not None else None
    return CuaBindings(
        llm=llm, config=config, active=enabled and cua_vision_enabled(config),
        mcp_active=enabled, sidecar=sidecar,
    )
