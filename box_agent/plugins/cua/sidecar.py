"""Compatibility entry point for session-owned CUA screenshot files."""

from typing import Any

from box_agent.image_store import SessionImageStore


class CuaImageSidecar(SessionImageStore):
    """Preserve the plugin API while sharing exact-image durable storage."""

    def persist_block(self, block: dict[str, Any]) -> dict[str, Any] | None:
        reference = super().persist_block(block)
        if reference is None:
            return None
        return {**reference, "path": str(self._path(reference).resolve())}


__all__ = ["CuaImageSidecar"]
