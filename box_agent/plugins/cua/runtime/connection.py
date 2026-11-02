"""Resolve the exact Driver MCP connection from trusted host configuration."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

from box_agent.plugins.cua.config import CuaRuntimeConfig


HOST_RUNTIME_ENV = "BOX_AGENT_CUA_RUNTIME_CONFIG"
DRIVER_ENV = "BOX_AGENT_CUA_DRIVER"
DEFAULT_DRIVER = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"


@dataclass(frozen=True)
class DriverConnection:
    driver_path: str
    endpoint: str | None = None
    embedded: bool = False

    def command(self) -> list[str]:
        command = [self.driver_path, "mcp"]
        if self.endpoint:
            command.extend(["--socket", self.endpoint])
        if self.embedded:
            if not self.endpoint:
                raise ValueError("External CUA connection requires a resolved host endpoint")
            command.append("--embedded")
        return command

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> DriverConnection:
        host = environment.get(HOST_RUNTIME_ENV, "").strip()
        if not host:
            # Compatibility for the existing standalone wrapper launcher.
            return cls(environment.get(DRIVER_ENV) or DEFAULT_DRIVER)
        runtime = CuaRuntimeConfig.model_validate(json.loads(host))
        if not runtime.enabled:
            raise ValueError("CUA runtime is disabled by the host")
        if not runtime.driver_path or not runtime.endpoint:
            raise ValueError("App CUA wrapper requires the host's resolved driver_path and endpoint; "
                             "refusing to connect to a different default Driver instance")
        return cls(runtime.driver_path, runtime.endpoint, runtime.mode == "external")
