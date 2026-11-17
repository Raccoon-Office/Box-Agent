"""Host-only MCP App snapshots and deterministic ACP interactions.

The Session Log owns durable facts. Bindings are derived from the original
call and the current connection; no independent persistent App registry exists.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from hmac import compare_digest
import json
import logging
from secrets import token_hex
from typing import Any

from acp import RequestError

from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.chat_references import ChatReferences
from box_agent.tools.model_tool_context import set_model_tool_context, reset_model_tool_context

log = logging.getLogger(__name__)
VERSION = 1
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_ARGUMENT_BYTES = 256 * 1024
SNAPSHOT_EVENT = "mcp-app/snapshot"
INTERACTION_EVENT = "mcp-app/interaction"


def _error(code: str) -> RequestError:
    return RequestError(-32000, code, {"code": code})


def _string(value: Any, *, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _error("MCP_APP_INVALID_REQUEST")
    return value


def _bounded_copy(value: Any, limit: int) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _error("MCP_APP_INVALID_PAYLOAD") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise _error("MCP_APP_PAYLOAD_TOO_LARGE")
    return json.loads(encoded)


def _active_events(state: Any) -> list[dict[str, Any]]:
    events = state.agent.session_log._active_events()
    reset = max((i for i, item in enumerate(events) if item["type"] == "surface/reset"), default=-1)
    return events[reset + 1:]


def _saved_snapshot(state: Any, call_id: str) -> dict[str, Any] | None:
    events = _active_events(state)
    # A host cannot manufacture a snapshot that was never attached to a real call.
    if not any(item["type"] == "tool/call" and item["data"].get("callId") == call_id for item in events):
        return None
    for item in reversed(events):
        if item["type"] == SNAPSHOT_EVENT and item["data"].get("toolCallId") == call_id:
            return deepcopy(item["data"])
    return None



class McpAppsACP:
    """Thin, session-bound adapter over the owning MCP runtime."""

    def __init__(self, sessions: dict[str, Any]) -> None:
        self.sessions = sessions
        from box_agent.tools.mcp_apps import set_mcp_apps_host_capability
        set_mcp_apps_host_capability(True)
        self._epoch = token_hex(32)
        self._interactions: dict[tuple[str, str], tuple[str, asyncio.Task]] = {}

    def capture(self, state: Any):
        def save(snapshot: dict[str, Any]) -> None:
            try:
                value = _bounded_copy(snapshot, MAX_SNAPSHOT_BYTES)
                _string(value.get("toolCallId"))
                _string(value.get("serverName"))
                _string(value.get("serverIdentity"), limit=2048)
                _string(value.get("resourceUri"), limit=2048)
                if not isinstance(value.get("arguments"), dict) or not isinstance(value.get("result"), dict):
                    raise _error("MCP_APP_INVALID_PAYLOAD")
                state.agent.session_log.append(SNAPSHOT_EVENT, value, ignorable=True)
                state.agent.session_log.flush()
            except Exception:
                # The remote tool already executed. A UI delivery/storage failure
                # must not turn its real result into an apparently retryable call.
                log.error("MCP App snapshot unavailable", extra={"code": "MCP_APP_SNAPSHOT_UNAVAILABLE"})
        return save

    def marker(self, state: Any, call_id: str) -> dict[str, Any] | None:
        snapshot = _saved_snapshot(state, call_id)
        if snapshot is None:
            return None
        marker = {"version": VERSION, "resourceUri": snapshot["resourceUri"]}
        title = snapshot.get("tool", {}).get("title")
        if isinstance(title, str) and title.strip():
            marker["title"] = title[:256]
        return marker

    def _state(self, params: dict[str, Any]) -> tuple[str, Any]:
        session_id = _string(params.get("sessionId"), limit=1024)
        state = self.sessions.get(session_id)
        if state is None or getattr(state, "_closed", False) or getattr(state, "_mcp_apps_closing", False):
            raise _error("MCP_APP_SESSION_UNAVAILABLE")
        if getattr(state, "_history_edit_in_progress", False) or getattr(state, "_history_restore_failed", False):
            raise _error("MCP_APP_SESSION_BUSY")
        return session_id, state

    @staticmethod
    def _allowed_connectors(state: Any) -> frozenset[str]:
        return frozenset(getattr(state, "selected_connector_ids", set()))

    def _resolve(self, params: dict[str, Any], *, bound: bool):
        from box_agent.tools.mcp_loader import resolve_mcp_app_tool
        session_id, state = self._state(params)
        call_id = _string(params.get("toolCallId"))
        snapshot = _saved_snapshot(state, call_id)
        if snapshot is None:
            raise _error("MCP_APP_SNAPSHOT_UNAVAILABLE")
        tool = resolve_mcp_app_tool(
            snapshot["serverName"], snapshot["tool"]["name"],
            server_identity=snapshot["serverIdentity"],
            allowed_connector_ids=lambda: self._allowed_connectors(state), actor="model",
        )
        if tool.mcp_ui_resource_uri != snapshot["resourceUri"]:
            raise _error("MCP_APP_RESOURCE_CHANGED")
        material = json.dumps([
            self._epoch, session_id, state._mcp_apps_nonce, call_id,
            tool.mcp_server_identity, tool.mcp_generation, snapshot["resourceUri"],
        ], separators=(",", ":"))
        binding_id = sha256(material.encode()).hexdigest()
        if bound and not compare_digest(_string(params.get("bindingId")), binding_id):
            raise _error("MCP_APP_BINDING_EXPIRED")
        return session_id, state, snapshot, tool, binding_id

    def _assert_current(self, session_id: str, state: Any) -> None:
        if (self.sessions.get(session_id) is not state or getattr(state, "_closed", False)
                or getattr(state, "_mcp_apps_closing", False)):
            raise _error("MCP_APP_SESSION_UNAVAILABLE")

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method not in {"mcp/apps/snapshot", "mcp/apps/resource", "mcp/apps/call", "mcp/apps/cancel"}:
                raise _error("MCP_APP_METHOD_UNSUPPORTED")
            if method == "mcp/apps/cancel":
                session_id, _ = self._state(params)
                key = session_id, _string(params.get("interactionId"))
                running = self._interactions.get(key)
                if running is None or running[1].done():
                    return {"cancelled": False}
                running[1].cancel()
                return {"cancelled": True}
            session_id, state, snapshot, tool, binding_id = self._resolve(params, bound=method != "mcp/apps/snapshot")
            if method == "mcp/apps/snapshot":
                return {"version": VERSION, "bindingId": binding_id,
                        "resourceUri": snapshot["resourceUri"], "tool": snapshot["tool"],
                        "arguments": snapshot["arguments"], "result": snapshot["result"]}
            if method == "mcp/apps/resource":
                from box_agent.tools.mcp_loader import read_mcp_app_resource
                uri = _string(params.get("uri"), limit=2048)
                result = await read_mcp_app_resource(
                    snapshot["serverName"], uri, server_identity=tool.mcp_server_identity,
                    generation=tool.mcp_generation, allowed_connector_ids=lambda: self._allowed_connectors(state),
                    require_ui=uri == snapshot["resourceUri"],
                )
                self._assert_current(session_id, state)
                return result
            if method == "mcp/apps/call":
                return await self._call(session_id, state, snapshot, tool, params)
            raise _error("MCP_APP_METHOD_UNSUPPORTED")
        except RequestError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", "MCP_APP_UNAVAILABLE")
            raise _error(code if isinstance(code, str) else "MCP_APP_UNAVAILABLE") from exc

    def _interaction_record(self, state: Any, interaction_id: str):
        # History edits can hide receipts, but cannot undo remote side effects.
        # Card eligibility still uses active history; deduplication uses all facts.
        latest = None
        for event in state.agent.session_log.events_of_type(INTERACTION_EVENT):
            if event["data"].get("interactionId") == interaction_id:
                latest = event["data"]
        return latest

    async def _call(self, session_id: str, state: Any, snapshot: dict, original_tool: Any, params: dict):
        from box_agent.tools.mcp_loader import resolve_mcp_app_tool
        interaction_id = _string(params.get("interactionId"))
        name = _string(params.get("name"))
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise _error("MCP_APP_INVALID_REQUEST")
        arguments = _bounded_copy(arguments, MAX_ARGUMENT_BYTES)
        # Resolve before any dispatch: includes connector authorization and the
        # App visibility policy, independent of untrusted readOnlyHint values.
        resolve_mcp_app_tool(
            snapshot["serverName"], name, server_identity=original_tool.mcp_server_identity,
            generation=original_tool.mcp_generation,
            allowed_connector_ids=lambda: self._allowed_connectors(state), actor="app",
        )
        signature = sha256(json.dumps([snapshot["toolCallId"], name, arguments],
                                     sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        key = session_id, interaction_id
        running = self._interactions.get(key)
        if running is not None:
            if running[0] != signature:
                raise _error("MCP_APP_INTERACTION_CONFLICT")
            return await asyncio.shield(running[1])
        previous = self._interaction_record(state, interaction_id)
        if previous is not None:
            if previous.get("signature") != signature:
                raise _error("MCP_APP_INTERACTION_CONFLICT")
            if previous.get("status") == "completed":
                return deepcopy(previous["result"])
            raise _error(previous.get("error", "MCP_APP_OUTCOME_UNKNOWN"))
        task = asyncio.create_task(self._execute(session_id, state, snapshot, original_tool,
                                               interaction_id, signature, name, arguments),
                                   name="mcp-app-interaction")
        self._interactions[key] = signature, task
        def retire(completed):
            if self._interactions.get(key, (None, None))[1] is completed:
                self._interactions.pop(key, None)
            if not completed.cancelled():
                completed.exception()  # Observe abandoned request failures too.
        task.add_done_callback(retire)
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._interactions.get(key, (None, None))[1] is task:
                self._interactions.pop(key, None)

    @contextmanager
    def _execution_scope(self, session_id: str, state: Any):
        from box_agent.tools.browser_runtime_scope import (
            set_browser_session_key, reset_browser_session_key,
        )
        llm = state.agent.llm
        model_token = set_model_tool_context(model=getattr(llm, "model", ""),
            max_output_tokens=getattr(llm, "max_output_tokens", 0),
            session_id=getattr(state, "upstream_session_id", "") or session_id)
        browser_token = set_browser_session_key(session_id)
        try:
            yield
        finally:
            reset_browser_session_key(browser_token)
            reset_model_tool_context(model_token)

    async def _execute(self, session_id, state, snapshot, original_tool, interaction_id, signature, name, arguments):
        from box_agent.tools.mcp_loader import call_mcp_app_tool
        record = {"version": VERSION, "toolCallId": snapshot["toolCallId"],
                  "interactionId": interaction_id, "signature": signature, "name": name, "arguments": arguments}
        session_log = state.agent.session_log
        self._assert_current(session_id, state)
        if _saved_snapshot(state, snapshot["toolCallId"]) is None:
            raise _error("MCP_APP_SNAPSHOT_UNAVAILABLE")
        session_log.append(INTERACTION_EVENT, {**record, "status": "started"}, ignorable=True)
        session_log.flush()
        try:
            context = ToolInvocationContext(parent_tool_call_id=interaction_id,
                chat_references=ChatReferences(state.agent.workspace_dir, state.agent.messages, session_log))
            with self._execution_scope(session_id, state):
                result = await call_mcp_app_tool(
                    snapshot["serverName"], name, arguments,
                    server_identity=original_tool.mcp_server_identity, generation=original_tool.mcp_generation,
                    allowed_connector_ids=lambda: self._allowed_connectors(state), invocation_context=context,
                    is_cancelled=lambda: (self.sessions.get(session_id) is not state
                        or getattr(state, "_closed", False)
                        or getattr(state, "_mcp_apps_closing", False)
                        or getattr(state, "_history_edit_in_progress", False)
                        or _saved_snapshot(state, snapshot["toolCallId"]) is None),
                )
            self._assert_current(session_id, state)
            if _saved_snapshot(state, snapshot["toolCallId"]) is None:
                raise _error("MCP_APP_SNAPSHOT_UNAVAILABLE")
            result = _bounded_copy(result, MAX_SNAPSHOT_BYTES)
            session_log.append(INTERACTION_EVENT, {**record, "status": "completed", "result": result}, ignorable=True)
            session_log.flush()
            return result
        except BaseException as exc:
            code = "MCP_APP_OUTCOME_UNKNOWN" if isinstance(exc, asyncio.CancelledError) else getattr(exc, "code", "MCP_APP_CALL_FAILED")
            if isinstance(exc, RequestError):
                code = (exc.data or {}).get("code", "MCP_APP_CALL_FAILED")
            if not isinstance(code, str):
                code = "MCP_APP_CALL_FAILED"
            try:
                session_log.append(INTERACTION_EVENT, {**record, "status": "failed", "error": code}, ignorable=True)
                session_log.flush()
            except Exception:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise _error("MCP_APP_OUTCOME_UNKNOWN") from exc
            raise

    async def close_session(self, session_id: str) -> None:
        # Revoke admission before cancellation cleanup can yield. The flag is
        # owned by this state, so a replacement with the same ID starts fresh.
        state = self.sessions.get(session_id)
        if state is not None:
            state._mcp_apps_closing = True
        tasks = [item[1] for key, item in self._interactions.items() if key[0] == session_id]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        for state in self.sessions.values():
            state._mcp_apps_closing = True
        tasks = [item[1] for item in self._interactions.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._interactions.clear()
