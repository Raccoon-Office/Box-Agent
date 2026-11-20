"""Session-owned shared REPL; the Node worker never receives host credentials."""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any

from box_agent.tools.base import ToolInvocationContext, ToolResult, check_tool_cancellation
from box_agent.tools.browser_intent import BrowserToolIntentPolicy
from box_agent.tools.browser_runtime_scope import current_browser_host_binding
from box_agent.tools.engine.execution import invoke_tool_once
from .browser import BrowserBackend


def native_window_command() -> list[str]:
    """Use the dedicated entry in frozen builds, preserving bounded subprocess IO."""
    from box_agent.acp.runtime_entry import CUA_NATIVE_ARG

    if getattr(sys, "frozen", False):
        return [sys.executable, CUA_NATIVE_ARG]
    return [sys.executable, "-m", "box_agent.acp.runtime_entry", CUA_NATIVE_ARG]


@dataclass
class Realm:
    service: Any = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cell: Any = None


class ReplRuntime:
    def __init__(self, *, node: str, workspace: Path, cua_service=None):
        self.node, self.workspace, self.cua_service = node, workspace, cua_service
        self.realms: dict[str, Realm] = {}
        self.agent = None
        self.turn_text = None
        self.closed = False
        self.uploads = ExitStack()
        # Serializes current-page operations across both modes in this session.
        self.operation_lock = asyncio.Lock()

    def _resolve(self, name):
        if self.agent is None:
            raise ValueError("REPL requires an active owning Agent run")
        exposure = getattr(self.agent, "mcp_tool_exposure", None)
        if exposure is not None:
            server = "playwright" if name.startswith("managed_browser_") else "browser-gateway"
            target = exposure.resolve_internal_mcp(name, server)
            if target is None:
                raise ValueError(f"CapabilityDenied: backend {name} is unavailable in this session")
            tool, generation = target
            return tool, (server, generation)
        tool = self.agent.tools.get(name)
        if tool is None:
            raise ValueError(f"CapabilityDenied: backend {name} is unavailable")
        return tool, (name.split('_browser_')[0], getattr(tool, "mcp_generation", None))

    def _stage_upload(self, paths: list[str]) -> list[str]:
        if current_browser_host_binding() is None:
            return paths
        # Desktop Playwright runs from the host temp directory. Keep copies alive
        # until session close because a selected File can be read much later.
        with ExitStack() as staging:
            root = Path(staging.enter_context(tempfile.TemporaryDirectory(prefix="box-repl-upload-"))).resolve()
            staged = []
            for index, path in enumerate(paths):
                parent = root / str(index)
                parent.mkdir()
                target = parent / Path(path).name
                shutil.copyfile(path, target)
                staged.append(str(target))
            self.uploads.enter_context(staging.pop_all())
        return staged

    async def _invoke(self, realm, name, args, deadline, expected):
        cell = realm.cell
        async with self.operation_lock:
            if cell is None or cell["cancelled"].is_set() or time.monotonic() >= deadline:
                raise ValueError("CellEnded: operation was not dispatched")
            check_tool_cancellation()
            tool, generation = self._resolve(name)
            if expected is not None and expected != generation:
                raise ValueError("StaleObservation: backend reconnected; observe again")
            if name in {"managed_browser_click", "managed_browser_type", "managed_browser_hover",
                        "managed_browser_select_option"}:
                properties = tool.parameters.get("properties", {})
                if "ref" in properties and "target" not in properties:
                    args = {**args}
                    args["ref"] = args.pop("target")
            policy = BrowserToolIntentPolicy.for_turn(
                current_turn_text=self.turn_text, messages=self.agent.messages,
            )
            issue = policy.tool_call_error(name, args)
            if issue:
                raise ValueError(issue)
            # No recursive call_tool, no raw MCP client: retain tool validation,
            # capability gates, leases, cancellation and private host binding.
            async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
                result = await invoke_tool_once(tool, args, cell["context"])
            cell["receipts"].append({"tool": name, "success": result.success})
            return result, generation

    def _service(self, capability, mode, realm):
        from box_agent._vendor.mac_cua.cua_adapter import cua_adapter as native
        from box_agent._vendor.mac_cua.cua_adapter.cua_service import ReplService
        from .documentation import manual

        def invoke(name, args, deadline, expected):
            cell = realm.cell
            if cell is None or cell["cancelled"].is_set():
                raise ValueError("CellEnded: operation was not dispatched")
            # Submission occurs with the current cell's context, never the
            # context from the first persistent REPL call.
            future = cell["contextvars"].copy().run(
                asyncio.run_coroutine_threadsafe,
                self._invoke(realm, name, args, deadline, expected), cell["loop"],
            )
            try:
                return future.result(timeout=max(0.001, deadline - time.monotonic()))
            except TimeoutError:
                future.cancel()
                raise ValueError("ActionOutcomeUnknown: backend timed out; observe before retrying")

        def driver(timeout):
            if self.cua_service is None:
                raise ValueError("CUA runtime is not configured")
            return native.McpClient(self.cua_service.driver_command(), timeout=timeout)

        def check(method, handle, args):
            cell = realm.cell
            if cell is None or cell["cancelled"].is_set() or self.agent is None:
                raise ValueError("CellEnded: operation was not dispatched")
            try:
                check_tool_cancellation()
            except RuntimeError as exc:
                raise ValueError(str(exc)) from exc

        # The host can opt into the upstream per-cell Driver trace for this
        # launch. Keep raw AX and screenshot evidence in the owning session.
        evidence_dir = None
        if capability == "cua" and os.environ.get("BOX_AGENT_CUA_TRACE") == "1":
            log_path = getattr(getattr(self.agent, "session_log", None), "path", None)
            if log_path is not None:
                evidence_dir = Path(log_path).parent / "cua-evidence"

        return ReplService(
            driver, self.node, Path(native.__file__).with_name("cua_repl.mjs"),
            evidence_dir=evidence_dir,
            capability=capability, documentation=manual(capability, mode),
            browser_backend_factory=(lambda: BrowserBackend(
                mode, invoke, workspace=self.workspace, stage_upload=self._stage_upload,
            )) if mode else None,
            dispatch_check=check, worker_cwd=str(self.workspace),
            worker_env={key: os.environ[key] for key in ("PATH", "LANG", "TMPDIR") if key in os.environ},
            adapter_factory=lambda *args, **kwargs: native.Adapter(
                *args, native_command=native_window_command(), **kwargs),
            action_error=native.ActionError, result_data=native.data,
            evidence_contract=native.evidence_contract, mcp_client=native.McpClient,
            driver_timeout_seconds=native.DRIVER_TIMEOUT_SECONDS,
        )

    async def execute(self, capability, mode, code, title, timeout_ms, context):
        if self.closed or self.agent is None:
            return ToolResult(success=False, error="REPL has no active owning session")
        realm = self.realms.setdefault(mode or capability, Realm())
        async with realm.lock:
            if capability == "cua":
                if self.cua_service is None:
                    return ToolResult(success=False, error="CUA runtime is not configured; prepare Desktop control first")
                await self.cua_service.prepare()
            if realm.service is None:
                realm.service = self._service(capability, mode, realm)
            realm.cell = {"loop": asyncio.get_running_loop(), "contextvars": contextvars.copy_context(),
                          "context": context or ToolInvocationContext(), "cancelled": threading.Event(),
                          "receipts": []}
            task = asyncio.create_task(asyncio.to_thread(realm.service.execute, code, title, timeout_ms))
            try:
                output = await asyncio.shield(task)
                from box_agent.plugins.cua.image_encoding import encode_canonical_cua_image
                parts = output.get("content", [])
                text = "\n".join(part["text"] for part in parts if part.get("type") == "text")
                success = not output.get("isError", False)
                result = ToolResult(success=success, content=text, error=None if success else text,
                                    raw_output={"operations": realm.cell["receipts"]})
                images = [part for part in parts if part.get("type") == "image"]
                if images and capability == "cua":
                    from box_agent.plugins.cua.wiring import adapt_repl_screenshots
                    adapt_repl_screenshots(result, self.cua_service.name, images)
                elif images:
                    result.image_content = [
                        image for part in images
                        if (image := encode_canonical_cua_image(part["data"], part["mimeType"],
                                                               preserve_geometry=True))
                    ] or None
                    result.image_source = capability
                return result
            except asyncio.CancelledError:
                realm.cell["cancelled"].set()
                realm.service.abort()
                # Join before reuse/disposal; cancelling to_thread cannot stop
                # an already dispatched native operation.
                async def finish():
                    try:
                        await task
                    finally:
                        await asyncio.to_thread(realm.service.close)
                        realm.service = None
                cleanup = asyncio.create_task(finish())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                # Retrieve exceptions from the worker, preserving cancellation
                # as the outer outcome after its resources have been released.
                if not cleanup.cancelled():
                    cleanup.exception()
                raise
            finally:
                realm.cell["cancelled"].set()
                realm.cell = None

    async def reset(self, key):
        realm = self.realms.get(key)
        if realm is not None:
            async with realm.lock:
                if realm.service is not None:
                    await asyncio.to_thread(realm.service.close)
                realm.service = None

    async def close(self):
        self.closed = True
        for realm in self.realms.values():
            if realm.cell is not None:
                realm.cell["cancelled"].set()
                realm.service.abort()
        try:
            for key in list(self.realms):
                await self.reset(key)
        finally:
            await asyncio.to_thread(self.uploads.close)


def resolve_node(configured: str | None) -> str:
    configured = configured or os.environ.get("BOX_AGENT_NODE")
    if configured:
        node = str(Path(configured).expanduser())
    else:
        from box_agent.tools.runtime import NodeRuntimeManager
        runtime = NodeRuntimeManager().discover()
        node = runtime.executable_path if runtime.available else shutil.which("node")
    if not node or not Path(node).is_file():
        raise ValueError("Node runtime missing; configure plugins.repl.node_path (no automatic download)")
    return str(node)
