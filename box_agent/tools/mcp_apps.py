"""MCP Apps metadata and host-only result handoff (no durable state)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from mcp import ClientSession, types

from ..session_log import SessionLog, run_session_write


class McpAppError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


UI_EXTENSION = "io.modelcontextprotocol/ui"
UI_MIME_TYPE = "text/html;profile=mcp-app"
MAX_APP_PAYLOAD_BYTES = 1024 * 1024
MCP_APP_MODEL_GUIDANCE = """MCP App workflow:
This tool offers an interactive card. Judge the business state from the actual
result, not from tool-call success or the presence of a UI reference. Do not
assume that the card has rendered or that the user's task is complete.
- Final business result or failure: report the verified outcome and relevant
  result truthfully; this takes precedence over the handoff wording below.
  A failed status query alone does not prove the business operation failed.
- User action required: discover a model-visible read-only status query that
  can observe the action using an ID returned by the service. If available,
  first invite the user to act in the card in a visible progress message and
  say you will briefly check for an update. Give them a 30-second interaction
  window from that invitation before handing off for lack of progress.
  Actually wait between checks using an available tool; an immediate status
  query followed by a final reply is not sufficient. Space checks at least
  5 seconds apart, or longer if the service specifies a larger interval.
  Do not restart the window for unchanged or intermediate results. When the
  required user action completes, continue from the new result; a terminal
  result, cancellation or an existing global limit ends the wait early.
  If the window expires, or no suitable query, returned ID or permitted wait
  mechanism is available, hand off. Never approve on the user's behalf,
  bypass App-only visibility, or replace the card with a second confirmation.
- Already submitted and running: if useful, discover and use model-visible
  status-query tools with IDs returned by the service. Continue through the
  existing Agent tool loop; honor any stated polling interval and existing
  step, time and cancellation limits. Avoid tight or unproductive polling.
  Do not repeat a submission or mutation merely to check its status.
- Unclear state: use an available read-only status query when it can resolve
  the uncertainty. Never invent a tool, ID, state or completion claim.
A normal card handoff is not a status report. Use one short action-oriented
sentence in the user's language identifying the card and how to continue there.
Do not repeat transient request statuses such as "not generated", "not completed"
or "still pending"; do not repeat quoted prices, fees, credits, request parameters
or prompt details. Refer the user to the card's current display for those details.
For example: "The card is ready; review or adjust it and confirm there, using
its current display for details and updates." Adapt this to the supported action;
do not invent a confirmation step for a display-only card.
These wording limits apply only to a normal handoff, not a verified final result
or failure, or an explicit user question about status, price or parameters.
Do not frame a handoff as failure or abandonment, claim a pending operation
succeeded, or promise background monitoring after this reply. Later card
interactions do not automatically update your context; use new observations
when answering follow-up questions."""
_host_enabled = False
_capture: ContextVar[tuple[Callable[[dict[str, Any]], None], SessionLog | None] | None] = ContextVar(
    "mcp_app_capture", default=None,
)
_invocation: ContextVar[tuple[str, dict[str, Any]] | None] = ContextVar("mcp_app_invocation", default=None)
_raw_capture: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar("mcp_app_raw_capture", default=None)
_dispatch_admission: ContextVar[Callable[[], str | None] | None] = ContextVar("mcp_app_dispatch_admission", default=None)


def set_mcp_apps_host_capability(enabled: bool) -> None:
    """Set the owning host's capability before starting MCP discovery."""
    global _host_enabled
    _host_enabled = bool(enabled)


def mcp_apps_host_enabled() -> bool:
    return _host_enabled


class MCPAppsClientSession(ClientSession):
    """Add the UI extension while retaining the pinned SDK's handshake logic."""

    async def send_request(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(request, types.ClientRequest) and isinstance(request.root, types.InitializeRequest):
            params = request.root.params
            capabilities = params.capabilities.model_dump(by_alias=True, exclude_none=True)
            extensions = dict(capabilities.get("extensions") or {})
            extensions[UI_EXTENSION] = {"mimeTypes": [UI_MIME_TYPE]}
            capabilities["extensions"] = extensions
            request = types.ClientRequest(request.root.model_copy(update={
                "params": params.model_copy(update={
                    "capabilities": types.ClientCapabilities.model_validate(capabilities),
                }),
            }))
        return await super().send_request(request, *args, **kwargs)


def ui_resource_uri(descriptor: dict[str, Any]) -> str | None:
    meta = descriptor.get("_meta")
    ui = meta.get("ui") if isinstance(meta, dict) else None
    uri = (ui.get("resourceUri") if isinstance(ui, dict) and "resourceUri" in ui
           else meta.get("ui/resourceUri") if isinstance(meta, dict) else None)
    if (isinstance(uri, str) and uri.startswith("ui://") and 5 < len(uri) <= 2048
            and not any(char.isspace() or ord(char) < 32 for char in uri)):
        return uri
    return None


def tool_visibility(descriptor: dict[str, Any]) -> frozenset[str]:
    meta = descriptor.get("_meta")
    ui = meta.get("ui") if isinstance(meta, dict) else None
    if not isinstance(ui, dict) or "visibility" not in ui:
        return frozenset(("model", "app"))
    value = ui["visibility"]
    if not isinstance(value, list) or any(item not in ("model", "app") for item in value):
        return frozenset()
    return frozenset(value)


def mcp_tool_allows(tool: Any, actor: str) -> bool:
    return actor in getattr(tool, "mcp_visibility", ("model", "app"))


def bounded_json(value: Any) -> Any:
    """Copy JSON without retaining SDK objects or unbounded host payloads."""
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_APP_PAYLOAD_BYTES:
        raise ValueError("MCP App payload exceeds the host size limit")
    return json.loads(encoded)


def _content_json(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(item, dict):
        return item
    # Test doubles and older SDK content objects expose the same public fields.
    fields = ("type", "text", "data", "mimeType", "uri", "name", "resource", "annotations")
    value = {name: getattr(item, name) for name in fields if hasattr(item, name)}
    if "type" not in value and "text" in value:
        value["type"] = "text"
    return value


def call_result_json(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        return bounded_json(result.model_dump(mode="json", by_alias=True, exclude_none=True))
    value: dict[str, Any] = {"content": [_content_json(item) for item in result.content]}
    for name in ("structuredContent", "isError"):
        field = getattr(result, name, None)
        if field is not None:
            value[name] = field
    meta = getattr(result, "meta", getattr(result, "_meta", None))
    if meta is not None:
        value["_meta"] = meta
    return bounded_json(value)


def set_mcp_app_capture(
    callback: Callable[[dict[str, Any]], None] | None, session_log: SessionLog | None = None,
) -> Token:
    return _capture.set((callback, session_log) if callback is not None else None)


def reset_mcp_app_capture(token: Token) -> None:
    _capture.reset(token)


@contextmanager
def mcp_app_invocation(call_id: str, arguments: dict[str, Any]) -> Iterator[None]:
    token = _invocation.set((call_id, arguments))
    try:
        yield
    finally:
        _invocation.reset(token)


@contextmanager
def capture_raw_app_result(callback: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    token = _raw_capture.set(callback)
    try:
        yield
    finally:
        _raw_capture.reset(token)


@contextmanager
def bind_mcp_app_admission(check: Callable[[], str | None]) -> Iterator[None]:
    """Carry App authorization to dispatch, across tool-internal async waits."""
    token = _dispatch_admission.set(check)
    try:
        yield
    finally:
        _dispatch_admission.reset(token)


def check_mcp_app_dispatch() -> None:
    check = _dispatch_admission.get()
    if check is not None:
        error = check()
        if error is not None:
            raise McpAppError(error)


def mcp_app_result_is_host_only(tool: Any) -> bool:
    return _raw_capture.get() is not None or bool(tool.mcp_ui_resource_uri and _capture.get() is not None)


def capture_mcp_app_result(tool: Any, result: Any) -> bool:
    """Hand a complete result to the host without putting it in ToolResult."""
    raw_callback = _raw_capture.get()
    capture = _capture.get()
    callback = capture[0] if capture else None
    invocation = _invocation.get()
    uri = tool.mcp_ui_resource_uri
    if raw_callback is None and (callback is None or invocation is None or not invocation[0] or
                                 not uri or not tool.mcp_server_identity):
        return False
    payload = call_result_json(result)
    if raw_callback is not None:
        raw_callback(payload)
        return True
    call_id, arguments = invocation
    snapshot = bounded_json({
        "version": 1,
        "toolCallId": call_id,
        "toolId": tool.mcp_tool_id,
        "serverName": tool.server_name,
        "generation": tool.mcp_generation,
        "tool": tool.mcp_descriptor,
        "resourceUri": uri,
        "arguments": arguments,
        "result": payload,
    })
    callback(snapshot)
    return True


async def acapture_mcp_app_result(tool: Any, result: Any) -> bool:
    capture = _capture.get()
    store = capture[1] if capture and tool.mcp_ui_resource_uri and _raw_capture.get() is None else None
    return await run_session_write(store, capture_mcp_app_result, tool, result)
