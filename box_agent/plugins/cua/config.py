"""Configuration owned by the optional Cua vision plugin."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CuaConfig(BaseModel):
    """Run-scoped Cua MCP vision settings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    server_name: str = Field(default="computer-use", min_length=1)
    feed_screenshots: bool = True


__all__ = ["CuaConfig"]
