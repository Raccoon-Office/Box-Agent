"""Thin test adapter over the existing acp_eval stdio transport."""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, monotonic_ns
from types import SimpleNamespace
from typing import Any

# acp_eval is a repository-local developer package, not a runtime dependency.
_EVAL_SRC = Path(__file__).resolve().parents[2] / "test_workspace/acp_eval/src"
if str(_EVAL_SRC) not in sys.path:
    sys.path.insert(0, str(_EVAL_SRC))
from acp_eval.transport import _ACPStdoutReader, _send, _read_until_response
from acp_eval.lifecycle import ProcessRecorder, drain_stream, stop_process
from acp_eval.protocol import ACPAccumulator, ProtocolRecorder


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        self.code, self.message, self.data = code, message, data
        super().__init__(f"RpcError({code!r}): {message}")


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    ok: bool
    expected: str
    actual: str
    logs: str
    repro_steps: list[str]


def default_acp_command() -> list[str]:
    return [sys.executable, "-m", "box_agent.acp.server"]


class _Updates(ACPAccumulator):
    def __init__(self):
        super().__init__()
        self.notifications = []

    def consume(self, message):
        super().consume(message)
        if "method" in message:
            self.notifications.append(dict(message))


class AcpHostProbe:
    """Single-flight requests with independent cancellation notifications."""
    def __init__(self, *, command, cwd, env=None, timeout_s=60.0):
        self.command, self.cwd = list(command), Path(cwd)
        self.env, self.timeout_s = env or {}, timeout_s
        self._proc = None
        self._temp = None
        self._stderr_task = None
        self._stderr = ""
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._updates = _Updates()

    @property
    def stderr_text(self) -> str:
        if self._temp:
            path = Path(self._temp.name) / "stderr.log"
            return path.read_text(errors="replace") if path.exists() else ""
        return self._stderr

    async def start(self) -> None:
        self.cwd.mkdir(parents=True, exist_ok=True)
        self._temp = TemporaryDirectory(prefix="acp-host-probe-")
        directory = Path(self._temp.name)
        self._protocol = ProtocolRecorder(directory, lambda: datetime.now(timezone.utc), monotonic_ns)
        self._lifecycle = ProcessRecorder(directory / "process.jsonl")
        # Run source from this checkout, even when the session workspace is temporary.
        root = str(Path(__file__).resolve().parents[2])
        env = os.environ | {"PYTHONPATH": root, "BOX_AGENT_SESSION_TRACE_ENABLED": "0"} | self.env
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            self._temp.cleanup()
            self._temp = None
            raise RpcError("spawn_failed", str(exc)) from exc
        self._reader = _ACPStdoutReader(self._proc.stdout, self._protocol)
        self._stderr_task = asyncio.create_task(drain_stream(
            self._proc.stderr, directory / "stderr.log", self._lifecycle, "stderr"))

    async def stop(self) -> None:
        if self._proc:
            await stop_process(self._proc, self._lifecycle, natural_exit_seconds=0.2, term_seconds=2)
            await self._stderr_task
            self._proc = None
        if self._temp:
            self._stderr = self.stderr_text
            self._temp.cleanup()
            self._temp = None

    async def kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()

    async def request(self, method: str, params: dict | None = None) -> object:
        async with self._lock:
            if self._proc is None or self._proc.returncode is not None:
                raise RpcError("eof", "ACP process is not running")
            self._next_id += 1
            try:
                await _send(self._proc, self._protocol, {
                    "jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}})
                response = await _read_until_response(
                    self._proc, self._reader, self._protocol, self._updates, self._lifecycle,
                    SimpleNamespace(stdout_terminal=False), self._next_id, monotonic() + self.timeout_s,
                    strict=True)
            except asyncio.TimeoutError as exc:
                raise RpcError("timeout", f"Timed out waiting for {method}") from exc
            except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
                raise RpcError("eof", str(exc)) from exc
            except ValueError as exc:
                raise RpcError("protocol_error", str(exc)) from exc
            if "error" in response:
                error = response["error"]
                raise RpcError(error.get("code"), error.get("message"), error.get("data"))
            return response.get("result")

    async def notify(self, method: str, params: dict | None = None) -> None:
        await _send(self._proc, self._protocol, {"jsonrpc": "2.0", "method": method, "params": params or {}})

    def drain_notifications(self) -> list[dict]:
        notes, self._updates.notifications = self._updates.notifications, []
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
