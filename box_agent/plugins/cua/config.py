"""Configuration owned by the optional Cua vision plugin."""

from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CuaRuntimeConfig(BaseModel):
    """Trusted host configuration; never supplied as model tool arguments."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    mode: Literal["standalone", "external"] = "standalone"
    driver_path: str | None = None
    endpoint: str | None = None

    @model_validator(mode="after")
    def require_external_endpoint(self) -> CuaRuntimeConfig:
        if self.enabled and self.mode == "external" and not self.endpoint:
            raise ValueError("external CUA runtime requires a host endpoint")
        return self


class CuaConfig(BaseModel):
    """Run-scoped Cua MCP vision settings."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    server_name: str = Field(default="computer-use", min_length=1)
    tool_interface: Literal["driver", "app"] = Field(
        default_factory=lambda: "app" if sys.platform == "darwin" else "driver"
    )
    feed_screenshots: bool = True
    persist_images: bool = True
    runtime: CuaRuntimeConfig | None = None


__all__ = ["CuaConfig", "CuaRuntimeConfig"]
