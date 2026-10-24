"""Compatibility entry point for session-owned CUA screenshot files."""

from box_agent.image_store import SessionImageStore


class CuaImageSidecar(SessionImageStore):
    """Preserve the plugin API while sharing exact-image durable storage."""


__all__ = ["CuaImageSidecar"]
