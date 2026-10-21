"""CUA-specific lifecycle ownership, independent of MCP and Agent assembly.

Platform launch adapters must return a handle only for a verified owned
instance. LaunchServices PIDs and shared standalone services are not handles.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol

from .config import CuaRuntimeConfig


class OwnedDaemon(Protocol):
    endpoint: str

    async def close(self) -> None: ...


@dataclass(frozen=True)
class CuaReadiness:
    state: Literal["disabled", "ready", "unavailable", "closed"]
    endpoint: str | None = None
    owned: bool = False
    detail: str | None = None


class CuaRuntimeManager:
    """One runtime owner, reused across runs, with serialized transitions.

    The probe verifies permissions as well as endpoint liveness. Connection
    and task control leases remain separate from owning the daemon.
    """

    def __init__(
        self,
        config: CuaRuntimeConfig,
        *,
        probe: Callable[[str], Awaitable[bool]],
        start: Callable[[CuaRuntimeConfig], Awaitable[OwnedDaemon]],
        timeout: float = 30,
    ) -> None:
        if timeout <= 0:
            raise ValueError("CUA readiness timeout must be positive")
        self.config = config.model_copy(deep=True)
        self._probe = probe
        self._start = start
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._owned: OwnedDaemon | None = None
        self._closed = False

    async def ensure_ready(self) -> CuaReadiness:
        async with self._lock:
            if self._closed:
                return CuaReadiness("closed")
            if not self.config.enabled:
                return CuaReadiness("disabled")
            try:
                return await asyncio.wait_for(self._ensure_ready(), self._timeout)
            except (OSError, RuntimeError, ValueError, TimeoutError, asyncio.TimeoutError) as error:
                return CuaReadiness("unavailable", detail=str(error))

    async def _ensure_ready(self) -> CuaReadiness:
        if self._owned is not None:
            if await self._probe(self._owned.endpoint):
                if self._closed:
                    return CuaReadiness("closed")
                return CuaReadiness("ready", self._owned.endpoint, owned=True)
            # Reap only our known instance before requesting a replacement.
            await self._owned.close()
            self._owned = None

        if self.config.endpoint and await self._probe(self.config.endpoint):
            if self._closed:
                return CuaReadiness("closed")
            return CuaReadiness("ready", self.config.endpoint)
        if self.config.mode == "external":
            return CuaReadiness("unavailable", detail="Host CUA daemon is unavailable")
        if not self.config.driver_path:
            return CuaReadiness("unavailable", detail="Install and configure a trusted CuaDriver")
        if self._closed:
            return CuaReadiness("closed")

        daemon = await self._start(self.config.model_copy(deep=True))
        # Retain ownership before the next await, including on cancellation.
        self._owned = daemon
        try:
            if not await self._probe(daemon.endpoint):
                raise RuntimeError("CUA daemon failed its readiness check")
            if self._closed:
                raise RuntimeError("CUA runtime is closing")
        except BaseException as error:
            # Cleanup failures leave the handle retained for close() to retry.
            try:
                await daemon.close()
            except BaseException as cleanup_error:
                add_note = getattr(error, "add_note", None)
                if callable(add_note):
                    add_note(f"CUA daemon cleanup failed: {cleanup_error}")
                raise error
            self._owned = None
            raise
        return CuaReadiness("ready", daemon.endpoint, owned=True)

    async def close(self) -> None:
        self._closed = True
        async with self._lock:
            if self._owned is not None:
                await self._owned.close()
                self._owned = None


__all__ = ["CuaReadiness", "CuaRuntimeManager", "OwnedDaemon"]
