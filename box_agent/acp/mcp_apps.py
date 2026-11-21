"""Host-only MCP App snapshots and deterministic ACP interactions.

The Session Log owns durable facts. Each request resolves the original call
against the current connection; no persistent connection handle is required.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import logging
from traceback import walk_tb
from typing import Any

from acp import RequestError
import httpx
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from box_agent.composition import standalone_tool_hooks
from box_agent.kernel.hook_types import copy_data
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.chat_references import ChatReferences
from box_agent.tools.completion_checks import CompletionChecks, CompletionContext
from box_agent.tools.model_tool_context import set_model_tool_context, reset_model_tool_context

log = logging.getLogger(__name__)
VERSION = 1
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_ARGUMENT_BYTES = 256 * 1024
SNAPSHOT_EVENT = "mcp-app/snapshot"
INTERACTION_EVENT = "mcp-app/interaction"


def _error(code: str) -> RequestError:
    return RequestError(-32000, code, {"code": code})


def _log_resource_failure(exc: Exception, *, session_id: str, snapshot: dict[str, Any],
                          resource_uri: str, initial_ui: bool, fallback: bool) -> None:
    """Keep protocol codes and stack locations without remote messages or payloads."""
    from box_agent.tools.mcp_apps import McpAppError

    errors = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(errors) < 4:
        seen.add(id(current))
        detail: dict[str, Any] = {
            "type": f"{type(current).__module__}.{type(current).__name__}",
            # Formatting the exception itself would include response bodies,
            # validation inputs or credentials. Do not include source lines/locals.
            "frames": [{"module": frame.f_globals.get("__name__", ""),
                        "function": frame.f_code.co_name, "line": line}
                       for frame, line in walk_tb(current.__traceback__)][-8:],
        }
        if isinstance(current, McpError):
            detail["mcp_code"] = current.error.code
        if isinstance(current, McpAppError):
            detail["app_code"] = current.code
        if isinstance(current, httpx.HTTPStatusError):
            detail["http_status"] = current.response.status_code
        if isinstance(current, ValidationError):
            detail["validation_types"] = [item["type"] for item in current.errors(
                include_url=False, include_context=False, include_input=False)][:8]
        errors.append(detail)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    log.warning("MCP App resource read failed %s", json.dumps({
        "session_id": session_id,
        "tool_call_id": snapshot["toolCallId"],
        "server_name": snapshot["serverName"],
        "tool_name": snapshot["tool"]["name"],
        # Even a ui:// URI may embed private path/query/fragment values.
        "resource_uri_hash": sha256(resource_uri.encode()).hexdigest(),
        "initial_ui": initial_ui,
        "fallback": fallback,
        "errors": errors,
        "payload_redacted": True,
    }, ensure_ascii=True))


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

    def __init__(self, sessions: dict[str, Any], on_reconnect: Callable[[], None] | None = None) -> None:
        self.sessions = sessions
        self._on_reconnect = on_reconnect
        from box_agent.tools.mcp_apps import set_mcp_apps_host_capability
        set_mcp_apps_host_capability(True)
        self._interactions: dict[tuple[str, str], tuple[str, asyncio.Task]] = {}

    def capture(self, state: Any):
        def save(snapshot: dict[str, Any]) -> None:
            try:
                value = _bounded_copy(snapshot, MAX_SNAPSHOT_BYTES)
                _string(value.get("toolCallId"))
                _string(value.get("serverName"))
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
    def _allowed_connectors(state: Any, params: dict[str, Any], *, selection_only: bool = False) -> frozenset[str]:
        if "connectorContext" not in params:
            return frozenset(getattr(state, "selected_connector_ids", set()))
        # Only the native host supplies this request-local snapshot. It is not
        # model/session state and never changes other requests.
        from . import (
            _connector_ids_from_meta, _connector_statuses_from_meta,
            _connector_status_unavailable_from_meta,
        )
        context = params["connectorContext"]
        if not isinstance(context, dict):
            raise _error("MCP_APP_INVALID_REQUEST")
        try:
            selected = _connector_ids_from_meta(context)
            statuses = _connector_statuses_from_meta(context)
            unavailable = _connector_status_unavailable_from_meta(context)
        except ValueError as exc:
            raise _error("MCP_APP_INVALID_REQUEST") from exc
        if selected is None or len(selected) > 100:
            raise _error("MCP_APP_INVALID_REQUEST")
        if selection_only and not unavailable:
            return frozenset(selected)
        return frozenset(connector_id for connector_id, _, status in statuses or ()
                         if not unavailable and status == "connected" and connector_id in selected)

    def _history(self, params: dict[str, Any]):
        session_id, state = self._state(params)
        call_id = _string(params.get("toolCallId"))
        snapshot = _saved_snapshot(state, call_id)
        if snapshot is None:
            raise _error("MCP_APP_SNAPSHOT_UNAVAILABLE")
        return session_id, state, snapshot

    def _resolve(self, params: dict[str, Any], *, opening: bool = False):
        from box_agent.tools.mcp_apps import McpAppError
        from box_agent.tools.mcp_loader import current_mcp_app_binding
        session_id, state, snapshot = self._history(params)
        try:
            binding = current_mcp_app_binding(
                snapshot["serverName"], lambda: self._allowed_connectors(state, params),
            )
        except McpAppError as exc:
            # A credential update may revoke the old epoch just before its
            # replacement is published. A new open can retry this transition.
            if opening and exc.code == "APP_SERVER_CHANGED":
                raise _error("APP_SERVER_UNAVAILABLE") from exc
            raise
        # Capture the current connection for this request only. An open App
        # continues on the replacement connection after a reconnect.
        return session_id, state, snapshot, binding

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
            if method == "mcp/apps/snapshot":
                from box_agent.tools.mcp_loader import prepare_mcp_app_connection
                session_id, state, saved = self._history(params)

                def selected_connectors():
                    self._assert_current(session_id, state)
                    self._history(params)
                    return self._allowed_connectors(state, params, selection_only=True)

                await prepare_mcp_app_connection(saved["serverName"], selected_connectors, self._on_reconnect)
                self._assert_current(session_id, state)
            session_id, state, snapshot, binding = self._resolve(params, opening=method == "mcp/apps/snapshot")
            if method == "mcp/apps/snapshot":
                return {"version": VERSION,
                        "resourceUri": snapshot["resourceUri"], "tool": snapshot["tool"],
                        "arguments": snapshot["arguments"], "result": snapshot["result"]}
            if method == "mcp/apps/resource":
                from box_agent.tools.mcp_apps import McpAppError
                from box_agent.tools.mcp_loader import read_mcp_app_resource, resolve_mcp_app_tool
                uri = _string(params.get("uri"), limit=2048)
                initial_ui = uri == snapshot["resourceUri"]

                def allowed_connectors():
                    return self._allowed_connectors(state, params)

                async def read(resource_uri: str, *, fallback: bool = False):
                    try:
                        return await read_mcp_app_resource(
                            snapshot["serverName"], resource_uri, server_identity=binding.server_identity,
                            generation=binding.generation, allowed_connector_ids=allowed_connectors,
                            require_ui=initial_ui,
                            # Only the original, persisted call authorizes a historical entry point.
                            saved_ui_uri=snapshot["resourceUri"] if initial_ui else None,
                            ui_tool_name=snapshot["tool"]["name"] if fallback else None,
                        )
                    except Exception as exc:
                        try:
                            _log_resource_failure(exc, session_id=session_id, snapshot=snapshot,
                                                  resource_uri=resource_uri, initial_ui=initial_ui,
                                                  fallback=fallback)
                        except Exception:
                            # Diagnostics must not replace the original error or fallback.
                            pass
                        raise

                try:
                    result = await read(uri)
                except McpAppError as exc:
                    if not initial_ui or exc.code != "APP_RESOURCE_NOT_FOUND":
                        raise
                    self._resolve(params)
                    try:
                        tool = resolve_mcp_app_tool(
                            snapshot["serverName"], snapshot["tool"]["name"], server_identity=binding.server_identity,
                            generation=binding.generation, allowed_connector_ids=allowed_connectors, actor="model",
                        )
                    except McpAppError as lookup_error:
                        if lookup_error.code in {"APP_TOOL_UNAVAILABLE", "APP_TOOL_NOT_ALLOWED"}:
                            raise exc from lookup_error
                        raise
                    if not tool.mcp_ui_resource_uri or tool.mcp_ui_resource_uri == uri:
                        raise
                    uri = tool.mcp_ui_resource_uri
                    result = await read(uri, fallback=True)
                self._assert_current(session_id, state)
                self._resolve(params)
                if initial_ui:
                    # Host-owned metadata; never trust a server-supplied resolved URI.
                    return {**result, "resolvedResourceUri": uri}
                return result
            if method == "mcp/apps/call":
                result = await self._call(session_id, state, snapshot, binding, params)
                self._assert_current(session_id, state)
                # Keep completed receipts, but do not deliver a reply across a
                # connection replacement within this request.
                if self._resolve(params)[3] != binding:
                    raise _error("APP_BINDING_EXPIRED")
                return result
            raise _error("MCP_APP_METHOD_UNSUPPORTED")
        except RequestError:
            raise
        except TimeoutError as exc:
            raise _error("MCP_APP_REQUEST_TIMEOUT") from exc
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

    async def _call(self, session_id: str, state: Any, snapshot: dict, binding: Any, params: dict):
        from box_agent.tools.mcp_loader import resolve_mcp_app_tool
        interaction_id = _string(params.get("interactionId"))
        name = _string(params.get("name"))
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            raise _error("MCP_APP_INVALID_REQUEST")
        arguments = _bounded_copy(arguments, MAX_ARGUMENT_BYTES)
        # Resolve before any dispatch: includes connector authorization and the
        # App visibility policy, independent of untrusted readOnlyHint values.
        target = resolve_mcp_app_tool(
            snapshot["serverName"], name, server_identity=binding.server_identity,
            generation=binding.generation,
            allowed_connector_ids=lambda: self._allowed_connectors(state, params), actor="app",
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
        if self._turn_is_preparing(state):
            raise _error("MCP_APP_SESSION_BUSY")
        task = asyncio.create_task(self._execute(session_id, state, snapshot, binding,
                                               interaction_id, signature, name, target.name, arguments,
                                               lambda: self._allowed_connectors(state, params)),
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

    @staticmethod
    def _turn_is_preparing(state: Any) -> bool:
        return state.agent.active_tool_run_context is None and (
            state._prompt_in_progress or state.turn_active or state.run_handle.is_active
        )

    async def _authorize(self, state, session_id, tool_name, interaction_id, arguments, is_cancelled):
        agent = state.agent
        active = agent.active_tool_run_context
        generation = agent.tool_run_context_generation
        if self._turn_is_preparing(state):
            raise _error("MCP_APP_SESSION_BUSY")

        async def apply_hooks(dispatch, base):
            if dispatch is None or base is None:
                raise _error("MCP_APP_AUTHORIZATION_UNAVAILABLE")
            context = replace(
                base, event="tool.before_execution", tool_call_id=interaction_id,
                is_cancelled=is_cancelled,
                payload={"tool_name": tool_name, "tool_call_id": interaction_id, "arguments": arguments},
            )
            decision = await dispatch.before_tool(context, arguments)
            if decision.action == "deny":
                raise _error("MCP_APP_TOOL_DENIED")
            return _bounded_copy(copy_data(decision.arguments), MAX_ARGUMENT_BYTES)

        if active is None:
            options = agent.default_run_options()
            checks = await CompletionChecks.create(
                agent.tools, CompletionContext(
                    tuple(agent.messages), is_cancelled, agent.session_log, resume_scope=agent.messages,
                ), include_inputs=False,
            )
            if checks.failures:
                raise _error("MCP_APP_AUTHORIZATION_UNAVAILABLE")
            async with standalone_tool_hooks({
                "llm": options.llm, "tools": agent.tools, "hooks": options.hooks,
                "plugins": options.plugins, "session_id": options.session_id or session_id,
                "is_cancelled": is_cancelled, "session_log": agent.session_log,
                "permission_negotiator": options.permission_negotiator,
                "tool_exposure_manager": agent.mcp_tool_exposure, "skill_engine": agent.skill_runtime,
            }) as bus:
                arguments = await apply_hooks(bus, bus.context)
            gates = tuple(checks.gates.values())
        else:
            if active.policy_error(tool_name, arguments):
                raise _error("MCP_APP_TOOL_DENIED")
            arguments = await apply_hooks(active.hook_dispatch, active.hook_context)
            gates = tuple(active.completion_gates.values())

        def still_authorized():
            return (agent.tool_run_context_generation == generation
                    and agent.active_tool_run_context is active) and (
                not self._turn_is_preparing(state) if active is None else (
                    tuple(active.completion_gates.values()) == gates
                    and not active.policy_error(tool_name, arguments)
                )
            )

        return arguments, gates, still_authorized

    async def _execute(self, session_id, state, snapshot, binding, interaction_id, signature,
                       name, tool_name, arguments, allowed_connectors):
        from box_agent.tools.mcp_loader import call_mcp_app_tool
        record = {"version": VERSION, "toolCallId": snapshot["toolCallId"],
                  "interactionId": interaction_id, "signature": signature, "name": name, "arguments": arguments}
        session_log = state.agent.session_log
        self._assert_current(session_id, state)
        if _saved_snapshot(state, snapshot["toolCallId"]) is None:
            raise _error("MCP_APP_SNAPSHOT_UNAVAILABLE")
        session_log.append(INTERACTION_EVENT, {**record, "status": "started"}, ignorable=True)
        session_log.flush()

        def revoked() -> bool:
            return (self.sessions.get(session_id) is not state
                    or getattr(state, "_closed", False)
                    or getattr(state, "_mcp_apps_closing", False)
                    or getattr(state, "_history_edit_in_progress", False)
                    or getattr(state, "_history_restore_failed", False)
                    or _saved_snapshot(state, snapshot["toolCallId"]) is None)

        try:
            with self._execution_scope(session_id, state):
                arguments, gates, still_authorized = await self._authorize(
                    state, session_id, tool_name, interaction_id, arguments, revoked,
                )
                context = ToolInvocationContext(
                    parent_tool_call_id=interaction_id,
                    chat_references=ChatReferences(state.agent.workspace_dir, state.agent.messages, session_log),
                    capability_gates=gates,
                )
                result = await call_mcp_app_tool(
                    snapshot["serverName"], name, arguments,
                    server_identity=binding.server_identity, generation=binding.generation,
                    allowed_connector_ids=allowed_connectors, invocation_context=context,
                    is_cancelled=lambda: revoked() or not still_authorized(),
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
