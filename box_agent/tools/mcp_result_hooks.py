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
    # Independent allows_image_persistence() and result_context() are optional.


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
            # Use shared retention only when enabled for this result. An
            # adapter with persistence disabled may still allow a one-shot image.
            image_context = getattr(adapter, "image_context_for_result", None)
            allows_context = getattr(adapter, "allows_image_context", None)
            if (callable(image_context) and callable(allows_context)
                    and allows_context(server_name=server_name, remote_name=remote_name)):
                continue
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
    arguments: dict[str, Any] | None = None,
    structured_content: dict[str, Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Collect authorized follow-ups without changing the original tool result."""
    if not inline_images:
        return None
    blocks = []
    for adapter in adapters if adapters is not None else allowed_mcp_result_adapters(
        server_name=server_name, remote_name=remote_name,
    ):
        try:
            method = getattr(adapter, "transient_followup_for_result", None)
            kwargs = dict(
                server_name=server_name, remote_name=remote_name,
                inline_images=[dict(item) for item in inline_images],
            )
            if callable(method):
                adapted = method(
                    **kwargs, arguments=arguments or {},
                    structured_content=structured_content,
                )
            else:
                adapted = adapter.transient_followup_content(**kwargs)
            if adapted:
                blocks.extend(adapted)
        except Exception:
            _log.debug("MCP result adapter failed", exc_info=True)
    return blocks or None


def adapt_mcp_image_context(
    *, server_name: str, remote_name: str, inline_images: list[dict[str, str]],
    arguments: dict[str, Any], structured_content: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Collect trusted images without exposing their bytes in public results."""
    if not inline_images:
        return None, None
    blocks: list[dict[str, Any]] = []
    source = None
    for adapter in _CURRENT.get():
        method = getattr(adapter, "image_context_for_result", None)
        predicate = getattr(adapter, "allows_image_context", None)
        if not callable(method) or not callable(predicate):
            continue
        try:
            if not predicate(server_name=server_name, remote_name=remote_name):
                continue
            adapter_source = getattr(adapter, "image_source", None)
            if not isinstance(adapter_source, str) or (source is not None and source != adapter_source):
                continue
            adapted = method(
                server_name=server_name, remote_name=remote_name,
                inline_images=[dict(item) for item in inline_images],
                arguments=arguments, structured_content=structured_content,
            )
            if adapted:
                blocks.extend(adapted)
                source = adapter_source
        except Exception:
            _log.debug("MCP image context adapter failed", exc_info=True)
    return blocks or None, source


def adapt_mcp_result_context(
    *, server_name: str, remote_name: str, arguments: dict[str, Any],
    structured_content: dict[str, Any] | None,
) -> str | None:
    """Let scoped adapters project structured MCP data into model context."""
    parts = []
    for adapter in _CURRENT.get():
        method = getattr(adapter, "result_context", None)
        if not callable(method):
            continue
        try:
            context = method(
                server_name=server_name, remote_name=remote_name,
                arguments=arguments, structured_content=structured_content,
            )
            if isinstance(context, str) and context:
                parts.append(context)
        except Exception:
            _log.debug("MCP structured result adapter failed", exc_info=True)
    return "\n".join(parts) or None


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
    # Older adapters retain the transient qualification. New adapters can
    # persist independently of model image capability and follow-up settings.
    eligible = []
    legacy_allowed = adapters
    sources = list(_CURRENT.get())
    for adapter in adapters or ():
        if all(item is not adapter for item in sources):
            sources.append(adapter)
    for adapter in sources:
        if not callable(getattr(adapter, "persist_image_references", None)):
            continue
        predicate = getattr(adapter, "allows_image_persistence", None)
        try:
            if callable(predicate):
                allowed = predicate(server_name=server_name, remote_name=remote_name)
            else:
                if legacy_allowed is None:
                    legacy_allowed = allowed_mcp_result_adapters(
                        server_name=server_name, remote_name=remote_name,
                    )
                allowed = any(item is adapter for item in legacy_allowed)
            if allowed:
                eligible.append(adapter)
        except Exception:
            _log.debug("MCP image persistence eligibility failed", exc_info=True)
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
    "adapt_mcp_result_context",
    "persist_mcp_image_references",
]
