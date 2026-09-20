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
        # With a session log, the durable sidecar/reference path is the single
        # request channel. Keep the legacy transient fallback only for hosts
        # that have no durable SessionLog directory.
        if self.sidecar is not None:
            return None
        if not self.allows_transient_followup(
            server_name=server_name, remote_name=remote_name,
        ):
            return None
        blocks = []
        for entry in inline_images:
            block = encode_canonical_cua_image(
                entry.get("data", ""), entry.get("mime_type", ""),
            )
            if block is not None:
                blocks.append(block)
        return blocks or None

    def durable_followup_content(
        self, *, server_name: str, remote_name: str,
        inline_images: list[dict[str, str]],
    ) -> list[dict[str, Any]] | None:
        """Persist image bytes and return Surface-safe reference blocks."""
        if not self.allows_transient_followup(
            server_name=server_name, remote_name=remote_name,
        ) or self.sidecar is None:
            return None
        references: list[dict[str, Any]] = []
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
        if not self.active:
            return events
        return _ScopedCuaEvents(events, self)

    def install_agent(self, agent: Any) -> None:
        """Bind this adapter through the generic run activation capability."""
        if not self.active or self._agent is not None:
            return
        original = agent.run_events
        self._original_instance_method = vars(agent).get("run_events", _MISSING)
        self._agent = agent

        def bound_run_events(*args: Any, **kwargs: Any):
            return self.bind_run(original(*args, **kwargs))

        agent.run_events = bound_run_events

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

    def adapt_provider_messages(self, messages: list[Any]) -> list[Any]:
        """Hydrate only the newest sidecar image in a provider request copy."""
        if not self.active or self.sidecar is None:
            return messages
        latest: tuple[int, int] | None = None
        for message_index, message in enumerate(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                continue
            for block_index, block in enumerate(content):
                if (
                    isinstance(block, dict)
                    and block.get("type") == "input_image"
                    and "data" not in block
                    and isinstance(block.get("contentRef"), str)
                ):
                    latest = (message_index, block_index)
        if latest is None:
            return messages
        hydrated_messages: list[Any] = []
        for message_index, message in enumerate(messages):
            content = getattr(message, "content", None)
            if not isinstance(content, list):
                hydrated_messages.append(message)
                continue
            new_content: list[dict[str, Any]] = []
            for block_index, block in enumerate(content):
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                if block.get("type") != "input_image" or "data" in block:
                    new_content.append(dict(block))
                    continue
                if (message_index, block_index) != latest:
                    # Historical sidecars remain in the durable Surface but
                    # cannot be sent to providers as unresolved input images.
                    continue
                hydrated = self.sidecar.hydrate(block)
                if hydrated is not None:
                    new_content.append(hydrated)
            if new_content:
                hydrated_messages.append(message.model_copy(update={"content": new_content}))
        return hydrated_messages


class _ScopedCuaEvents:
    """Bind per pull so anext/aclose can safely run in different tasks."""

    def __init__(self, events: AsyncIterator[Any], bindings: CuaBindings) -> None:
        self._events = events.__aiter__()
        self._bindings = bindings

    def __aiter__(self) -> "_ScopedCuaEvents":
        return self

    async def __anext__(self) -> Any:
        with bind_mcp_result_adapter(self._bindings):
            return await self._events.__anext__()

    async def aclose(self) -> None:
        close = getattr(self._events, "aclose", None)
        if close is not None:
            with bind_mcp_result_adapter(self._bindings):
                await close()


def build_cua_bindings(
    *, llm: Any, config: CuaConfig, sidecar_dir: Path | None = None,
) -> CuaBindings:
    sidecar = CuaImageSidecar(sidecar_dir) if sidecar_dir is not None else None
    return CuaBindings(
        llm=llm, config=config, active=cua_vision_enabled(config), sidecar=sidecar,
    )
