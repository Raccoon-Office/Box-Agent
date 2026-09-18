"""ACP Host-like probe: spawn real ``box_agent.acp.server`` over stdio JSON-RPC."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RpcError(Exception):
    """JSON-RPC error or transport failure surfaced to the host probe."""

    code: int | str
    message: str
    data: object | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = f"RpcError({self.code!r}): {self.message}"
        if self.data is not None:
            return f"{base} data={self.data!r}"
        return base

    def __post_init__(self) -> None:
        Exception.__init__(self, str(self))


@dataclass(frozen=True)
class CaseResult:
    case_id: str  # T1-01 ...
    ok: bool
    expected: str
    actual: str
    logs: str
    repro_steps: list[str]


def default_acp_command() -> list[str]:
    """Default spawn command matching ``scripts/test_acp_streaming.py``."""
    return [sys.executable, "-m", "box_agent.acp.server"]


class AcpHostProbe:
    """Minimal Host-like client for a real ACP agent subprocess.

    Protocol traffic is NDJSON on stdout; diagnostics stay on stderr.
    Incoming agent reverse-RPCs (e.g. ``session/request_permission``) are
    answered automatically with an approve outcome so probes can proceed.
    """

    def __init__(
        self,
        *,
        command: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._command = list(command)
        self._cwd = Path(cwd)
        self._env = env
        self._timeout_s = float(timeout_s)
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int | str, asyncio.Future[Any]] = {}
        self._notifications: list[dict[str, Any]] = []
        self._stderr_chunks: list[str] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._closed = False
        self._permission_option: str = "approve"
        self._auto_approve_permissions: bool = True

    @property
    def command(self) -> list[str]:
        return list(self._command)

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def stderr_text(self) -> str:
        return "".join(self._stderr_chunks)

    def set_permission_policy(self, *, auto_approve: bool = True, option_id: str = "approve") -> None:
        self._auto_approve_permissions = auto_approve
        self._permission_option = option_id

    async def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("AcpHostProbe already started")
        self._cwd.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        if self._env:
            env.update(self._env)
        # Keep session-trace out of the real home unless a test opts in.
        env.setdefault("BOX_AGENT_SESSION_TRACE_ENABLED", "0")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd),
                env=env,
            )
        except FileNotFoundError as exc:
            raise RpcError(
                code="spawn_failed",
                message=f"Failed to spawn ACP command {self._command!r}: {exc}",
                data={"command": self._command, "cwd": str(self._cwd)},
            ) from exc
        except OSError as exc:
            raise RpcError(
                code="spawn_failed",
                message=f"OS error spawning ACP command {self._command!r}: {exc}",
                data={"command": self._command, "cwd": str(self._cwd), "errno": getattr(exc, "errno", None)},
            ) from exc
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_stdout(), name="acp-host-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="acp-host-stderr")

    async def stop(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._closed = True
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
        except Exception:
            pass
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    RpcError(code="stopped", message="ACP probe stopped while request pending")
                )
        self._pending.clear()
        self._proc = None
        self._reader_task = None
        self._stderr_task = None

    async def kill(self) -> None:
        """Hard-kill the subprocess (T2-03). Pending requests must fail clearly."""
        proc = self._proc
        self._closed = True
        if proc is None:
            return
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(
                    RpcError(
                        code="process_killed",
                        message="ACP subprocess was killed; request did not complete",
                        data={"returncode": proc.returncode, "stderr_tail": self.stderr_text[-2000:]},
                    )
                )
        self._pending.clear()
        self._proc = None

    async def request(self, method: str, params: dict | None = None) -> object:
        proc = self._require_proc()
        if proc.stdin is None or proc.stdin.is_closing():
            raise RpcError(code="stdin_closed", message="ACP stdin is closed; cannot send request")
        self._next_id += 1
        req_id = self._next_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        await self._write(payload)
        try:
            return await asyncio.wait_for(fut, timeout=self._timeout_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise RpcError(
                code="timeout",
                message=f"Timed out after {self._timeout_s}s waiting for {method!r} (id={req_id})",
                data={
                    "method": method,
                    "id": req_id,
                    "stderr_tail": self.stderr_text[-4000:],
                    "notifications": self._notifications[-20:],
                },
            ) from exc

    async def notify(self, method: str, params: dict | None = None) -> None:
        proc = self._require_proc()
        if proc.stdin is None or proc.stdin.is_closing():
            raise RpcError(code="stdin_closed", message="ACP stdin is closed; cannot send notification")
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        await self._write(payload)

    def drain_notifications(self) -> list[dict]:
        notes = list(self._notifications)
        self._notifications.clear()
        return notes

    async def initialize(self) -> object:
        return await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "acp-host-probe", "version": "1.0"},
                "clientCapabilities": {},
            },
        )

    async def session_new(self, *, cwd: str, meta: dict | None = None) -> str:
        params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
        if meta:
            params["_meta"] = meta
        result = await self.request("session/new", params)
        if not isinstance(result, dict):
            raise RpcError(
                code="bad_result",
                message=f"session/new returned non-object result: {type(result).__name__}",
                data=result,
            )
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RpcError(
                code="missing_session_id",
                message="session/new response missing sessionId",
                data=result,
            )
        return session_id

    async def session_prompt(
        self,
        session_id: str,
        prompt: object,
        *,
        meta: dict | None = None,
    ) -> object:
        if isinstance(prompt, str):
            prompt_blocks: object = [{"type": "text", "text": prompt}]
        else:
            prompt_blocks = prompt
        params: dict[str, Any] = {
            "sessionId": session_id,
            "prompt": prompt_blocks,
        }
        if meta:
            params["_meta"] = meta
        return await self.request("session/prompt", params)

    async def session_cancel(self, session_id: str) -> None:
        await self.notify("session/cancel", {"sessionId": session_id})

    async def ext_request(self, method: str, params: dict | None = None) -> object:
        """Call an ACP extension method (wire name ``_<method>``)."""
        wire = method if method.startswith("_") else f"_{method}"
        return await self.request(wire, params or {})

    # ── internals ─────────────────────────────────────────────

    def _require_proc(self) -> asyncio.subprocess.Process:
        if self._proc is None:
            raise RpcError(code="not_started", message="AcpHostProbe.start() was not called")
        return self._proc

    async def _write(self, payload: dict[str, Any]) -> None:
        proc = self._require_proc()
        assert proc.stdin is not None
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(raw)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise RpcError(
                code="stdin_broken",
                message=f"Broken pipe writing to ACP stdin: {exc}",
                data={"stderr_tail": self.stderr_text[-2000:]},
            ) from exc

    async def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                self._stderr_chunks.append(line.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._stderr_chunks.append(f"\n[probe stderr reader error: {exc}]\n")

    async def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    self._fail_all_pending(
                        RpcError(
                            code="eof",
                            message="ACP subprocess closed stdout (EOF)",
                            data={"stderr_tail": self.stderr_text[-4000:]},
                        )
                    )
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as exc:
                    self._stderr_chunks.append(f"\n[probe bad JSON on stdout: {text[:200]} ({exc})]\n")
                    continue
                await self._dispatch_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail_all_pending(
                RpcError(
                    code="reader_error",
                    message=f"ACP stdout reader failed: {exc}",
                    data={"stderr_tail": self.stderr_text[-2000:]},
                )
            )

    def _fail_all_pending(self, err: RpcError) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    async def _dispatch_message(self, msg: dict[str, Any]) -> None:
        # Response to our request
        if "id" in msg and "method" not in msg:
            req_id = msg["id"]
            fut = self._pending.pop(req_id, None)
            if fut is None or fut.done():
                return
            if "error" in msg:
                err = msg["error"] or {}
                fut.set_exception(
                    RpcError(
                        code=err.get("code", "rpc_error"),
                        message=str(err.get("message", "unknown RPC error")),
                        data=err.get("data"),
                    )
                )
            else:
                fut.set_result(msg.get("result"))
            return

        # Reverse RPC request from agent
        if "id" in msg and "method" in msg:
            await self._handle_agent_request(msg)
            return

        # Notification
        if "method" in msg:
            self._notifications.append(msg)
            return

    async def _handle_agent_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method", "")
        req_id = msg["id"]
        params = msg.get("params") or {}
        self._notifications.append(msg)  # keep for drain / assertions

        result: dict[str, Any]
        if method == "session/request_permission" and self._auto_approve_permissions:
            # Prefer an option that exists on the request when possible.
            options = []
            if isinstance(params, dict):
                options = params.get("options") or []
            option_id = self._permission_option
            option_ids = {
                o.get("optionId")
                for o in options
                if isinstance(o, dict) and isinstance(o.get("optionId"), str)
            }
            if option_id not in option_ids:
                for candidate in ("approve", "approve_once", "allow_once", "allow_session", "approve_session"):
                    if candidate in option_ids:
                        option_id = candidate
                        break
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        elif method in {"fs/read_text_file", "fs/write_text_file"}:
            # Host filesystem helpers are not implemented in this probe.
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Host probe does not implement {method}",
                    },
                }
            )
            return
        else:
            # Unknown reverse RPC — return empty ok-ish result to avoid hanging the agent.
            result = {}

        await self._write({"jsonrpc": "2.0", "id": req_id, "result": result})


def collect_session_updates(notifications: list[dict]) -> list[dict]:
    """Extract ``update`` objects from ``session/update`` notifications."""
    updates: list[dict] = []
    for note in notifications:
        if note.get("method") != "session/update":
            continue
        params = note.get("params") or {}
        update = params.get("update")
        if isinstance(update, dict):
            updates.append(update)
    return updates


def tool_events_from_updates(updates: list[dict]) -> list[dict]:
    """Return tool-related session updates (start / progress / completed)."""
    events: list[dict] = []
    for update in updates:
        kind = update.get("sessionUpdate") or update.get("kind") or ""
        if "tool" in str(kind).lower() or update.get("toolCallId") or update.get("title"):
            # Prefer explicit tool sessionUpdate kinds.
            if kind in {
                "tool_call",
                "tool_call_update",
                "tool_call_start",
                "tool_call_progress",
            } or str(kind).startswith("tool"):
                events.append(update)
            elif update.get("toolCallId") and kind not in {
                "agent_message_chunk",
                "agent_thought_chunk",
            }:
                events.append(update)
    return events


def message_text_from_updates(updates: list[dict]) -> str:
    parts: list[str] = []
    for update in updates:
        kind = update.get("sessionUpdate") or update.get("kind") or ""
        if kind in {"agent_message_chunk", "agent_message"}:
            content = update.get("content")
            if isinstance(content, dict):
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(content, str):
                parts.append(content)
    return "".join(parts)
