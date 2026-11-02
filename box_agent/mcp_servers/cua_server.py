"""Expose the native App adapter as a stdio MCP server for Box-Agent.

mac-cua wraps the same adapter in a persistent JavaScript REPL. Box-Agent has
no REPL tool, so each App method becomes one direct MCP tool keyed by the App
reference name returned from ``get_app``.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import json
import os
import time
import uuid
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from box_agent.plugins.cua.runtime.coordinator import SerialCoordinator
from box_agent.plugins.cua.runtime.connection import DEFAULT_DRIVER, DRIVER_ENV, DriverConnection
from box_agent.plugins.cua.structured_result import cua_structured_payload

from box_agent.mcp_servers.cua_adapter import (
    DRIVER_TIMEOUT_SECONDS,
    ActionError,
    Adapter,
    McpClient,
    clear_image,
    data,
)

# Optional directory for raw driver call/result evidence (one JSONL per server process).
EVIDENCE_ENV = "BOX_AGENT_CUA_EVIDENCE_DIR"
CALL_TIMEOUT_SECONDS = 120

INSTRUCTIONS = """Native macOS App control. Start with get_app("Microsoft Word") (name, bundle ID or
.app path); it binds to a running app or launches it once, and returns the current AX state.
Pass that same app string to every other tool. The adapter resolves the current window, an
open app menu, or an attached Save/Open sheet automatically; there is no window-selection API.
Element IDs are positions in the latest observation only. After any action, dialog, menu or
window change, observe again (get_state) before using an element ID. Screenshot coordinates
refer to the latest screenshot from get_state(screenshot=true); never reuse old coordinates.
Actions return no state: observe to verify. Never replay an action whose outcome is unknown.
Use set_value to replace a writable plain-text field, including filename and path fields in
native Open/Save/Go to Folder dialogs. Use type_text to insert at the cursor, or paste for
clipboard input into a focused control. Choose according to the control and operation.
After setting a field, observe its value and verify the subsequent Open/Save/navigation result;
setting a value does not itself submit the dialog. Do not use set_value to rewrite rich-text documents."""

_APP = {"type": "string", "description": "App reference passed to get_app."}
_TARGET = {
    "description": "Element ID from the latest get_state, or [x, y] in the latest screenshot's pixels.",
    "anyOf": [
        {"type": "integer", "minimum": 0},
        {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
    ],
}
_POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


TOOLS = [
    _tool("list_apps", "List installed and running macOS apps.", {}, []),
    _tool(
        "get_app",
        "Bind to a running app or launch it once in the background, wait for its first window, "
        "and return its current AX state. Does not create or erase documents. " + INSTRUCTIONS,
        {"app": {"type": "string", "description": "App name, bundle ID or absolute .app path."}},
        ["app"],
    ),
    _tool(
        "list_windows",
        "List the app's visible windows (id, title, bounds, layer, isActive). Read-only; "
        "a new supplemental window is verified once, which invalidates earlier element IDs.",
        {"app": _APP},
        ["app"],
    ),
    _tool(
        "get_state",
        "Observe the current app surface: active menu, attached sheet or current window. "
        "Subsequent calls return diffs (~ replaced line, + added line, removed IDs). "
        "screenshot=true returns one window screenshot without activating it; "
        "ax=false returns only the screenshot and keeps existing element IDs.",
        {
            "app": _APP,
            "screenshot": {"type": "boolean", "default": False},
            "ax": {"type": "boolean", "default": True},
            "disable_diffing": {"type": "boolean", "default": False},
        },
        ["app"],
    ),
    _tool(
        "click",
        "Click an element ID or screenshot point. Elements without AXPress are clicked at the "
        "center of their visible frame when screenshot geometry is verified.",
        {
            "app": _APP,
            "target": _TARGET,
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            "click_count": {"type": "integer", "minimum": 1, "default": 1},
            "action": {"type": "string", "enum": ["press", "show_menu", "pick", "confirm", "cancel", "open"], "default": "press"},
        },
        ["app", "target"],
    ),
    _tool(
        "drag",
        "Drag between two points of the latest screenshot.",
        {"app": _APP, "from": _POINT, "to": _POINT},
        ["app", "from", "to"],
    ),
    _tool(
        "scroll",
        "Scroll an element or screenshot point by 1-50 driver page steps.",
        {
            "app": _APP,
            "target": _TARGET,
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "pages": {"type": "integer", "minimum": 1, "maximum": 50, "default": 1},
        },
        ["app", "target", "direction"],
    ),
    _tool(
        "set_value",
        "Replace the whole value of a writable plain-text control, including fields in native "
        "Open/Save/Go to Folder dialogs and sheets. Use a current element ID and observe the result. "
        "This does not submit the dialog or insert text at the cursor; do not use it to rewrite rich-text documents.",
        {"app": _APP, "element": {"type": "integer", "minimum": 0}, "value": {"type": "string"}},
        ["app", "element", "value"],
    ),
    _tool(
        "type_text",
        "Type text into the focused control as keyboard input.",
        {"app": _APP, "text": {"type": "string"}, "element": {"type": "integer", "minimum": 0}},
        ["app", "text"],
    ),
    _tool(
        "paste",
        "Paste plain text into the focused control via the clipboard (Cmd+V). The text stays on "
        "the clipboard. After an error, observe before continuing; do not replay.",
        {"app": _APP, "text": {"type": "string"}, "element": {"type": "integer", "minimum": 0}},
        ["app", "text"],
    ),
    _tool(
        "press_key",
        "Press a key or combination, e.g. Return, Escape, cmd+s, cmd+shift+g.",
        {"app": _APP, "key": {"type": "string"}, "element": {"type": "integer", "minimum": 0}},
        ["app", "key"],
    ),
]

# Explicit foreground is available after observation proves a background action
# did not land. It is never inferred from an unverifiable result alone.
for _definition in TOOLS:
    if _definition.name in {"click", "type_text", "press_key", "paste"}:
        _definition.inputSchema["properties"]["delivery_mode"] = {
            "type": "string", "enum": ["background", "foreground"], "default": "background",
            "description": "Prefer background. Use foreground only when observation confirms the background action was ineffective or unavailable; may interrupt user input.",
        }

_SCREENSHOT_ONLY = "getScreenshot"


class CuaService:
    """Serialize model tool calls onto one adapter and its driver connection."""

    def __init__(self, driver_factory, evidence=None) -> None:
        self.factory = driver_factory
        self.evidence = evidence
        self.adapter: Adapter | None = None
        self.apps: dict[str, str] = {}
        self.requires_requery: set[str] = set()
        self._events: list[dict[str, Any]] = []
        self._value: Any = None
        self._error_category: str | None = None
        self._request_id: str | None = None

    def backend(self, deadline: float) -> Adapter:
        if self.adapter is None:
            timeout = max(0.001, min(DRIVER_TIMEOUT_SECONDS, deadline - time.monotonic()))
            self.adapter = Adapter(self.factory(timeout), self.evidence)
            self.apps.clear()
        self.adapter.event_sink = self._record_event
        self.adapter.invocation_id = self._request_id
        self.adapter.deadline = deadline
        return self.adapter

    def _record_event(self, event: dict[str, Any]) -> None:
        # Image bytes already travel as MCP image content. Preserve backend
        # identifiers and reports without sending the same image twice.
        event = {"request_id": self._request_id, "timestamp": time.time(), **event}
        if isinstance(event.get("result"), dict):
            event["result"] = dict(event["result"])
            event["result"]["content"] = [
                item for item in event["result"].get("content", [])
                if item.get("type") != "image"
            ]
        self._events.append(copy.deepcopy(cua_structured_payload(event)))

    def invalidate_after_cancellation(self) -> None:
        """Called only by the owner worker, after abandoned work settles."""
        self.requires_requery.update(self.apps)
        if self.adapter is not None:
            self.adapter.invalidate()
            for state in self.adapter.handles.values():
                clear_image(state)
            self.adapter.panel_focus.close()

    def call_result(self, name: str, args: dict[str, Any]) -> CallToolResult:
        items, is_error = self.call(name, args)
        context = None
        app = args.get("app") if isinstance(args.get("app"), str) else None
        if self.adapter is not None:
            state = self.adapter.handles.get(self.apps.get(app), {})
            if state:
                context = {key: copy.deepcopy(state[key]) for key in (
                    "pid", "bundle_id", "window", "elements", "element_windows", "fresh",
                ) if key in state}
        return CallToolResult(
            content=_mcp_content(items), isError=is_error,
            structuredContent={
                "schema_version": 1, "request_id": self._request_id,
                "tool": name, "app": args.get("app"),
                "output": cua_structured_payload(
                    {key: value for key, value in self._value.items() if key != "image"}
                    if isinstance(self._value, dict) else self._value),
                "backend_events": self._events, "adapter_context": context,
                "error_category": self._error_category,
                "requires_requery": is_error or name in {
                    "click", "drag", "scroll", "set_value", "type_text", "paste", "press_key",
                } or app in self.requires_requery,
            },
        )

    def handle(self, app: Any) -> str:
        if not isinstance(app, str) or app not in self.apps:
            raise ValueError("Unknown app reference; call get_app first (again after a reset).")
        return self.apps[app]

    def call(self, name: str, args: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        self._events = []
        self._value = None
        self._error_category = None
        self._request_id = uuid.uuid4().hex
        deadline = time.monotonic() + CALL_TIMEOUT_SECONDS
        try:
            return self._dispatch(name, args, deadline), False
        except ActionError as failure:
            self._error_category = "backend_error"
            detail = json.dumps(
                {k: v for k, v in data(failure.result).items()
                 if k in {"effect", "status", "error", "reason", "refusal", "message", "code", "foreground_attempted", "clipboard_may_have_changed"}},
                ensure_ascii=False,
            )
            texts = [c.get("text", "") for c in failure.result.get("content", []) if c.get("type") == "text"]
            return [_text("\n".join([detail, *texts]))], True
        except (ValueError, KeyError, TypeError) as failure:
            self._error_category = "validation_or_precondition"
            return [_text(str(failure))], True
        except (TimeoutError, EOFError, OSError, RuntimeError) as failure:
            self._error_category = "transport_outcome_unknown"
            # The connection is poisoned and effects may be unknown. Drop every
            # reference so the model must re-observe instead of replaying.
            self.close()
            return [_text(f"{failure}\nCUA connection reset; app references cleared. "
                          "Call get_app again and observe before continuing; do not replay.")], True
        finally:
            if self.adapter is not None:
                self.adapter.deadline = None

    def _dispatch(self, name: str, args: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
        adapter = self.backend(deadline)
        def control(*positional, **keywords):
            self._value = adapter.control(*positional, **keywords)
            return self._value

        if name == "list_apps":
            return [_text(json.dumps(control("listApps"), ensure_ascii=False))]
        if name == "get_app":
            app = args.get("app")
            value = control("getApp", None, {"app": app})
            self.apps[app] = value["handle"]
            self.requires_requery.discard(app)
            return [_text(value["state"])]
        handle = self.handle(args.get("app"))
        if name == "list_windows":
            return [_text(json.dumps(control("listWindows", handle), ensure_ascii=False))]
        if name == "get_state":
            ax = args.get("ax", True)
            screenshot = args.get("screenshot", False)
            if not ax and not screenshot:
                raise ValueError("get_state needs ax or screenshot")
            method = ("getAXStateAndScreenshot" if ax and screenshot else
                      "getAXState" if ax else _SCREENSHOT_ONLY)
            value = control(method, handle, {"disableDiffing": args.get("disable_diffing", False)})
            if ax:
                self.requires_requery.discard(args.get("app"))
            content = [_text(value["state"])] if value.get("state") else []
            if value.get("image"):
                content.append(value["image"])
            elif screenshot and ax:
                content.append(_text("Screenshot unavailable; do not choose coordinates."))
            return content
        if args.get("app") in self.requires_requery:
            raise ValueError("RequiresRequery: a previous call was cancelled after dispatch; "
                             "call get_state with ax=true before continuing. Do not replay blindly.")
        if name == "click":
            options = {"mouseButton": args.get("button", "left"), "clickCount": args.get("click_count", 1), "action": args.get("action", "press")}
            value = control("click", handle, {"target": args.get("target"), "options": options,
                **({"deliveryMode": args["delivery_mode"]} if "delivery_mode" in args else {})})
        elif name == "drag":
            value = control("drag", handle, {"from": args.get("from"), "to": args.get("to")})
        elif name == "scroll":
            value = control("scroll", handle, {
                "target": args.get("target"), "direction": args.get("direction"),
                "pages": args.get("pages", 1)})
        elif name == "set_value":
            value = control("setValue", handle, {"target": args.get("element"), "value": args.get("value")})
        elif name == "type_text":
            value = control("typeText", handle, {"text": args.get("text"),
                **({"target": args["element"]} if "element" in args else {}),
                **({"deliveryMode": args["delivery_mode"]} if "delivery_mode" in args else {})})
        elif name == "paste":
            value = control("paste", handle, {"text": args.get("text"),
                **({"target": args["element"]} if "element" in args else {}),
                **({"deliveryMode": args["delivery_mode"]} if "delivery_mode" in args else {})})
        elif name == "press_key":
            value = control("pressKey", handle, {"key": args.get("key"),
                **({"target": args["element"]} if "element" in args else {}),
                **({"deliveryMode": args["delivery_mode"]} if "delivery_mode" in args else {})})
        else:
            raise ValueError(f"Unknown tool: {name}")
        effect = value.get("effect")
        return [_text(f"Done (driver effect: {effect}). Observe to verify." if effect
                      else "Done. Observe to verify.")]

    def close(self) -> None:
        adapter, self.adapter = self.adapter, None
        self.apps.clear()
        self.requires_requery.clear()
        if adapter is None:
            return
        try:
            adapter.close()
        except Exception:
            pass
        finally:
            adapter.driver.close()


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _mcp_content(items: list[dict[str, Any]]) -> list[TextContent | ImageContent]:
    content: list[TextContent | ImageContent] = []
    for item in items:
        if item.get("type") == "image":
            content.append(ImageContent(type="image", data=item["data"], mimeType=item.get("mimeType", "image/png")))
        else:
            content.append(TextContent(type="text", text=item.get("text", "")))
    return content


def build_server(service: CuaService, coordinator: SerialCoordinator | None = None) -> Server:
    coordinator = coordinator or SerialCoordinator(
        service.call_result, invalidate=service.invalidate_after_cancellation, close=service.close)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            await coordinator.aclose()

    server = Server("box-agent-cua", instructions=INSTRUCTIONS, lifespan=lifespan)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        return await coordinator.call(name, arguments or {})

    return server


def main() -> None:
    connection = DriverConnection.from_environment(os.environ)
    evidence = None
    if os.environ.get(EVIDENCE_ENV):
        directory = os.path.expanduser(os.environ[EVIDENCE_ENV])
        os.makedirs(directory, mode=0o700, exist_ok=True)
        evidence = open(os.path.join(directory, f"cua-driver-{os.getpid()}.jsonl"), "a", encoding="utf-8")
    service = CuaService(lambda timeout: McpClient(connection.command(), timeout=timeout), evidence)
    coordinator = SerialCoordinator(service.call_result,
                                    invalidate=service.invalidate_after_cancellation, close=service.close)
    server = build_server(service, coordinator)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    try:
        asyncio.run(run())
    finally:
        coordinator.close()
        if evidence is not None:
            evidence.close()


if __name__ == "__main__":
    main()
