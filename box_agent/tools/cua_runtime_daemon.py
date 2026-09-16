"""Lifecycle for the standalone Cua Driver daemon owned by Box-Agent."""

from __future__ import annotations

import asyncio
import atexit
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


def _new_endpoint() -> str:
    token = f"{os.getpid()}-{uuid4().hex[:8]}"
    if sys.platform == "win32":
        return rf"\\.\pipe\box-agent-cua-{token}"
    return str(Path(tempfile.gettempdir()) / f"box-agent-cua-{token}.sock")


STANDALONE_CUA_SOCKET = _new_endpoint()
_owned_process: subprocess.Popen[bytes] | None = None
_owned_command: str | None = None
_owned_socket: str | None = None
_atexit_registered = False
_lifecycle_lock = asyncio.Lock()


def _daemon_is_alive() -> bool:
    return _owned_process is not None and _owned_process.poll() is None


def get_standalone_cua_daemon_runtime() -> tuple[str, str] | None:
    """Return the runtime owned by this process, including one still starting."""
    if _owned_process is None or not _owned_command or not _owned_socket:
        return None
    return _owned_command, _owned_socket


async def _status_ready(command: str, socket_path: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            command,
            "status",
            "--socket",
            socket_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await process.wait() == 0
    except (OSError, asyncio.SubprocessError):
        return False


def _stop_owned_daemon_at_exit() -> None:
    global _owned_command, _owned_process, _owned_socket
    process = _owned_process
    if process is None:
        return
    if _owned_command and _owned_socket:
        try:
            subprocess.run(
                [_owned_command, "stop", "--socket", _owned_socket],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if process.poll() is None:
        process.terminate()
    _owned_command = None
    _owned_process = None
    _owned_socket = None


async def ensure_standalone_cua_daemon(
    command: str,
    *,
    socket_path: str = STANDALONE_CUA_SOCKET,
    timeout: float = 15.0,
) -> tuple[bool, str | None]:
    """Start the process-owned daemon and wait until its socket is healthy."""
    async with _lifecycle_lock:
        return await _ensure_standalone_cua_daemon_locked(
            command,
            socket_path=socket_path,
            timeout=timeout,
        )


async def _ensure_standalone_cua_daemon_locked(
    command: str,
    *,
    socket_path: str,
    timeout: float,
) -> tuple[bool, str | None]:
    global _atexit_registered, _owned_command, _owned_process, _owned_socket

    owned_runtime = get_standalone_cua_daemon_runtime()
    if owned_runtime is not None and owned_runtime != (command, socket_path):
        await _stop_standalone_cua_daemon_locked()

    if await _status_ready(command, socket_path):
        return True, None

    if not _daemon_is_alive():
        try:
            _owned_process = subprocess.Popen(
                [command, "serve", "--socket", socket_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None,
                env={**os.environ, "CUA_DRIVER_RS_UPDATE_CHECK": "false"},
            )
        except OSError as exc:
            return False, f"Failed to start Cua Driver daemon: {exc}"
        _owned_command = command
        _owned_socket = socket_path
        if not _atexit_registered:
            atexit.register(_stop_owned_daemon_at_exit)
            _atexit_registered = True

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await _status_ready(command, socket_path):
            return True, None
        if _owned_process is not None and _owned_process.poll() is not None:
            return False, (
                "Cua Driver daemon exited before becoming ready "
                f"(code {_owned_process.returncode})"
            )
        await asyncio.sleep(0.1)
    return False, f"Cua Driver daemon did not become ready within {timeout:g}s"


async def stop_standalone_cua_daemon() -> None:
    """Stop only the daemon launched by this Box-Agent process."""
    async with _lifecycle_lock:
        await _stop_standalone_cua_daemon_locked()


async def _stop_standalone_cua_daemon_locked() -> None:
    global _owned_command, _owned_process, _owned_socket
    process = _owned_process
    if process is None:
        return

    if _owned_command and _owned_socket:
        try:
            stopper = await asyncio.create_subprocess_exec(
                _owned_command,
                "stop",
                "--socket",
                _owned_socket,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(stopper.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                stopper.kill()
                await stopper.wait()
        except (OSError, asyncio.SubprocessError):
            pass

    if process.poll() is None:
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)
    _owned_command = None
    _owned_process = None
    _owned_socket = None


__all__ = [
    "STANDALONE_CUA_SOCKET",
    "ensure_standalone_cua_daemon",
    "get_standalone_cua_daemon_runtime",
    "stop_standalone_cua_daemon",
]
