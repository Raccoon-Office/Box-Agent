"""Installed-driver adapter. No downloading, shell execution, or global stop."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
from uuid import uuid4

from .config import CuaRuntimeConfig

_MAC_CLEANUP_TIMEOUT = 10.0


def installed_driver() -> str | None:
    from box_agent.tools.cua_install import shared_driver
    shared = shared_driver()
    if shared:
        return str(shared)
    candidates = [shutil.which("cua-driver"), str(Path.home() / ".local/bin/cua-driver")]
    if sys.platform == "darwin":
        candidates.append("/Applications/CuaDriver.app/Contents/MacOS/cua-driver")
    return next((str(Path(p).resolve()) for p in candidates if p and Path(p).is_file()), None)


async def command(*args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
        return process.returncode or 0, (stdout + stderr).decode(errors="replace")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class DriverAdapter:
    def __init__(self, driver: str, endpoint: str | None = None):
        self.driver = driver
        self.directory = None if endpoint else tempfile.TemporaryDirectory(prefix="box-cua-")
        self.endpoint = endpoint or (
            rf"\\.\pipe\box-cua-{uuid4().hex}" if sys.platform == "win32"
            else str(Path(self.directory.name) / "driver.sock")
        )
        self._pending_handle = None

    async def probe(self, endpoint: str) -> bool:
        code, output = await command(self.driver, "status", "--socket", endpoint)
        return code == 0 and "not running" not in output.lower() and "running" in output.lower()

    async def ready(self, endpoint: str) -> bool:
        if not await self.probe(endpoint):
            return False
        if sys.platform == "darwin":
            code, output = await command(
                self.driver, "call", "check_permissions", '{"prompt":false}', "--socket", endpoint,
            )
            if code:
                raise RuntimeError(output[:2000])
            permissions = json.loads(output)
            if not permissions.get("accessibility") or not permissions.get("screen_recording"):
                raise RuntimeError("CUA requires Accessibility and Screen Recording; grant the daemon owner permissions in System Settings, then restart")
        return True

    async def start(self, config: CuaRuntimeConfig):
        if config.endpoint:
            raise RuntimeError("Configured standalone endpoint is unavailable; refusing to replace a shared daemon")
        await self.close_pending()
        handle = DriverHandle(self)
        # Own the launch intent before the first await; LaunchServices may finish
        # after readiness is cancelled. Failed cleanup must retain this handle.
        self._pending_handle = handle
        try:
            if sys.platform == "darwin":
                app = Path(self.driver).parent.parent.parent
                if app.suffix != ".app":
                    app = Path("/Applications/CuaDriver.app")
                if not app.is_dir():
                    raise RuntimeError("Install CuaDriver.app and run cua-driver permissions grant")
                handle.launch_requested = True
                code, output = await command(
                    "/usr/bin/open", "-n", "-g", "-a", str(app), "--args",
                    "serve", "--socket", self.endpoint,
                )
                if code:
                    handle.launch_requested = False
                    raise RuntimeError(output)
            else:
                handle.process = await asyncio.create_subprocess_exec(
                    self.driver, "serve", "--socket", self.endpoint,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
            for _ in range(100):
                if await self.probe(self.endpoint):
                    handle.launch_confirmed = True
                    self._pending_handle = None  # Transferred to CuaRuntimeManager.
                    return handle
                if handle.process and handle.process.returncode is not None:
                    raise RuntimeError("CUA daemon exited before readiness")
                await asyncio.sleep(.1)
            raise RuntimeError("CUA daemon did not become ready")
        except BaseException as error:
            try:
                await self.close_pending()
            except BaseException as cleanup_error:
                if hasattr(error, "add_note"):
                    error.add_note(f"CUA pending launch cleanup failed: {cleanup_error}")
            raise

    async def close_pending(self):
        if self._pending_handle is not None:
            await self._pending_handle.close()
            self._pending_handle = None

    def cleanup(self):
        if self._pending_handle is not None:
            raise RuntimeError("CUA launch cleanup is pending; preserving private endpoint")
        if self.directory:
            self.directory.cleanup()


class DriverHandle:
    def __init__(self, adapter: DriverAdapter):
        self.adapter = adapter
        self.endpoint = adapter.endpoint
        self.process = None
        self.launch_requested = False
        self.launch_confirmed = False

    async def _mac_pid(self) -> int | None:
        # Match the exact executable AND fresh private endpoint, never an app
        # name/global PID. This also sees our app before it starts listening.
        code, output = await command("/bin/ps", "-axo", "pid=,command=")
        if code:
            raise RuntimeError("Cannot verify owned CUA process")
        expected = f"{Path(self.adapter.driver).resolve()} serve --socket {self.endpoint}"
        for line in output.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[0].isdigit() and fields[1] == expected:
                return int(fields[0])
        return None

    async def _close_mac(self):
        async def settle():
            stopped_pid = None
            while True:
                pid = await self._mac_pid()
                if pid is not None:
                    if pid != stopped_pid:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        stopped_pid = pid
                elif stopped_pid is not None:
                    self.launch_requested = False
                    return
                elif await self.adapter.probe(self.endpoint):
                    code, output = await command(self.adapter.driver, "stop", "--socket", self.endpoint)
                    if code or await self.adapter.probe(self.endpoint):
                        raise RuntimeError(f"Owned CUA daemon did not stop: {output}")
                    self.launch_requested = False
                    return
                elif self.launch_confirmed:
                    # A previously observed instance has exited. Pending launch
                    # intents must still wait for a late LaunchServices process.
                    self.launch_requested = False
                    return
                await asyncio.sleep(.1)
        try:
            await asyncio.wait_for(settle(), _MAC_CLEANUP_TIMEOUT)
        except asyncio.TimeoutError as error:
            raise RuntimeError("CUA launch has not settled; retaining its private endpoint for cleanup retry") from error

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        elif self.launch_requested:
            await self._close_mac()
        elif await self.adapter.probe(self.endpoint):
            # This endpoint is inside our fresh private directory; never stop the
            # default/global driver, and never mistake open's PID for ownership.
            code, output = await command(self.adapter.driver, "stop", "--socket", self.endpoint)
            if code or await self.adapter.probe(self.endpoint):
                raise RuntimeError(f"Owned CUA daemon did not stop: {output}")
        if self.adapter.directory is not None and sys.platform != "win32":
            # Keep the private address stable for MCP, but remove our confirmed
            # stopped instance's socket so a later ensure can reuse that address.
            Path(self.endpoint).unlink(missing_ok=True)
