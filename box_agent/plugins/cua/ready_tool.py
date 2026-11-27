"""CUA readiness seam registered alongside the existing MCP runtime owner."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .config import CuaConfig, CuaRuntimeConfig
from .driver import DriverAdapter
from .lifecycle import CuaRuntimeManager
from .runtime.connection import DriverConnection, HOST_RUNTIME_ENV


class CuaService:
    def __init__(self, settings: CuaConfig):
        self.settings = settings
        runtime = settings.runtime
        assert runtime is not None
        from .pinned_release import installed_pinned_driver
        driver = runtime.driver_path or installed_pinned_driver()
        self.adapter = DriverAdapter(driver or "cua-driver", runtime.endpoint)
        self.config = runtime.model_copy(update={"driver_path": driver})
        self.manager = CuaRuntimeManager(self.config, probe=self._ready, start=self.adapter.start)

    async def _ready(self, endpoint):
        if await self.adapter.probe(endpoint):
            from .driver import command
            from .pinned_release import require_build
            code, output = await command(self.adapter.driver, "call", "health_report",
                                         '{"include":["binary_version"]}', "--socket", endpoint)
            if code:
                raise RuntimeError("Cannot verify host Driver identity; restart with the pinned release")
            require_build(json.loads(output).get("build", {}), Path(self.adapter.driver))
        return await self.adapter.ready(endpoint)

    @property
    def name(self):
        return self.settings.server_name

    def driver_command(self) -> list[str]:
        return DriverConnection(self.config.driver_path or "cua-driver", self.adapter.endpoint,
                                self.config.mode == "external").command()

    async def prepare(self):
        if self.config.enabled:
            from .pinned_release import verify_source
            if not self.config.driver_path:
                raise RuntimeError("Matching CUA Driver is not installed; install the pinned REPL release")
            await asyncio.to_thread(verify_source, Path(self.config.driver_path))
        result = await self.manager.ensure_ready()
        if result.state != "ready":
            raise RuntimeError(result.detail or f"CUA runtime is {result.state}")

    async def close(self):
        await self.manager.close()
        await self.adapter.close_pending()
        self.adapter.cleanup()


def create_cua_service(config):
    raw = config.plugins.get("cua")
    host = os.environ.get(HOST_RUNTIME_ENV, "").strip()
    if host:
        runtime = json.loads(host)
        user_runtime = (raw or {}).get("runtime") or {}
        user_enabled = CuaRuntimeConfig(enabled=user_runtime.get("enabled", True)).enabled
        if not user_enabled:
            runtime["enabled"] = False
        settings = CuaConfig.model_validate({**(raw or {}), "runtime": runtime})
    elif raw is not None:
        settings = CuaConfig.model_validate(raw)
        if settings.runtime is None:
            settings.runtime = CuaRuntimeConfig(enabled=True)
    else:
        settings = CuaConfig(runtime=CuaRuntimeConfig(enabled=True))
    config.plugins["cua"] = settings.model_dump()
    return CuaService(settings)
