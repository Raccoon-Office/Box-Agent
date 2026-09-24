"""Generic run-scoped hooks for adapting MCP binary result content.

The MCP loader does not know which optional feature consumes inline binary
content. A run-scoped adapter may translate raw content into transient blocks
and durable tool-result references.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol


class MCPResultAdapter(Protocol):
    """Adapt inline MCP content for a single model request."""

    def allows_transient_followup(
        self,
        *,
        server_name: str,
        remote_name: str,
    ) -> bool: ...

    def transient_followup_content(
        self,
        *,
        server_name: str,
        remote_name: str,
        inline_images: list[dict[str, str]],
    ) -> list[dict[str, Any]] | None: ...

    # Adapters may optionally expose persist_image_references().


_log = logging.getLogger(__name__)

_CURRENT: ContextVar[tuple[MCPResultAdapter, ...]] = ContextVar(
    "box_agent_mcp_result_adapters",
    default=(),
)


def current_mcp_result_adapters() -> tuple[MCPResultAdapter, ...]:
    return _CURRENT.get()


@contextmanager
def bind_mcp_result_adapter(adapter: MCPResultAdapter) -> Iterator[None]:
    """Add one adapter for this execution scope, preserving enclosing adapters."""
    token = _CURRENT.set((*_CURRENT.get(), adapter))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def allowed_mcp_result_adapters(
    *, server_name: str, remote_name: str,
) -> tuple[MCPResultAdapter, ...]:
    allowed = []
    for adapter in _CURRENT.get():
        try:
            if adapter.allows_transient_followup(
                server_name=server_name, remote_name=remote_name,
            ):
                allowed.append(adapter)
        except Exception:
            _log.debug("MCP result adapter eligibility failed", exc_info=True)
    return tuple(allowed)


def adapt_mcp_inline_images(
    *, server_name: str, remote_name: str, inline_images: list[dict[str, str]],
    adapters: tuple[MCPResultAdapter, ...] | None = None,
) -> list[dict[str, Any]] | None:
    """Collect authorized follow-ups without changing the original tool result."""
    if not inline_images:
        return None
    blocks = []
    for adapter in adapters if adapters is not None else allowed_mcp_result_adapters(
        server_name=server_name, remote_name=remote_name,
    ):
        try:
            adapted = adapter.transient_followup_content(
                server_name=server_name,
                remote_name=remote_name,
                inline_images=[dict(item) for item in inline_images],
            )
            if adapted:
                blocks.extend(adapted)
        except Exception:
            _log.debug("MCP result adapter failed", exc_info=True)
    return blocks or None


def persist_mcp_image_references(
    *, server_name: str, remote_name: str, inline_images: list[dict[str, str]],
    adapters: tuple[MCPResultAdapter, ...] | None = None,
) -> list[dict[str, Any]] | None:
    """Collect optional persisted image references from authorized adapters.

    The loader keeps this seam generic. Adapters that do not persist binary
    content simply omit the method and remain transient-only.
    """
    if not inline_images:
        return None
    eligible = adapters if adapters is not None else allowed_mcp_result_adapters(
        server_name=server_name, remote_name=remote_name,
    )
    candidates = tuple(
        adapter for adapter in eligible
        if callable(getattr(adapter, "persist_image_references", None))
    )
    # Avoid re-running transient-only adapters' eligibility checks. Besides
    # being cheaper, this keeps legacy adapters' observable call contract
    # unchanged when no durable method is implemented.
    if not candidates:
        return None
    blocks: list[dict[str, Any]] = []
    for adapter in candidates:
        try:
            method = adapter.persist_image_references
            adapted = method(
                server_name=server_name,
                remote_name=remote_name,
                inline_images=[dict(item) for item in inline_images],
            )
            if adapted:
                blocks.extend(adapted)
        except Exception:
            _log.debug("MCP image reference adapter failed", exc_info=True)
    return blocks or None


__all__ = [
    "MCPResultAdapter",
    "current_mcp_result_adapters",
    "bind_mcp_result_adapter",
    "allowed_mcp_result_adapters",
    "adapt_mcp_inline_images",
    "persist_mcp_image_references",
]
