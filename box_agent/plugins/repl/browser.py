"""Browser REPL API translated onto existing, host-authorized MCP Tools.

Managed mode also binds tabs from the Playwright inventory. User mode keeps
the current-page connector contract.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import uuid
from typing import Any, Callable

from box_agent.tools.base import ToolResult


_TAB_ROW = re.compile(r"- (\d+):(?P<current> \(current\))? \[(?P<title>.*?)\]\((?P<url>.*)\)(?: \[crashed\])?")
_COMMON_TAB_METHODS = ("getState", "goto", "click", "fill")
_BROWSER_API = {
    "managed": {
        "browserMethods": ("getTab", "listTabs", "newTab"),
        "tabMethods": (*_COMMON_TAB_METHODS, "activate", "close", "scroll", "pressKey",
                       "hover", "uploadFile", "evaluate", "getScreenshot", "getStateAndScreenshot",
                       "handleDialog", "waitForText", "back", "selectOption"),
    },
    "user": {
        "browserMethods": ("getTab", "openAndRead"),
        "tabMethods": (*_COMMON_TAB_METHODS, "readText"),
    },
}
_CLICK_BUTTONS = ("left", "right", "middle")
_CLICK_MODIFIERS = ("Alt", "Control", "ControlOrMeta", "Meta", "Shift")


def _result_text(content: str) -> str:
    # Playwright 0.0.76 renders tool return values in a Result section.
    _, marker, body = content.partition("### Result\n")
    if not marker:
        return content.strip()
    return re.split(r"(?m)^### ", body, maxsplit=1)[0].strip()


def _response_header(content: str) -> str:
    # Page DOM and generated Playwright code are not tab or modal metadata.
    header = content.partition("### Snapshot\n")[0]
    return re.sub(r"(?ms)^```[^\n]*\n.*?^```[^\n]*$", "", header)


def _modal_notice(header: str) -> dict | None:
    match = re.search(r"(?m)^### Modal state\n([\s\S]*?)(?=^### |\Z)", header)
    if match:
        return {"notice": "### Modal state\n" + match[1].strip()}
    return None


def _tab_rows(body: str) -> list[dict]:
    if body.startswith("No open tabs."):
        return []
    tabs = []
    for index, line in enumerate(body.splitlines()):
        match = _TAB_ROW.fullmatch(line)
        if match is None or int(match[1]) != index:
            raise ValueError("InvalidTabInventory: Playwright tab list could not be read")
        tabs.append({"index": index, "title": match["title"], "url": match["url"],
                     "active": bool(match["current"])})
    if not tabs or sum(tab["active"] for tab in tabs) != 1:
        raise ValueError("InvalidTabInventory: Playwright did not identify one current tab")
    return tabs


def _action_inventory(header: str) -> tuple[tuple[str, ...], int] | None:
    """Read the active Page and corroborating Open tabs from the same MCP result."""
    page = re.search(r"(?m)^### Page\n- Page URL: ([^\n]+)$", header)
    if not page:
        return None
    rows = re.search(r"(?ms)^### Open tabs\n(.+?)(?=^### |\Z)", header)
    if not rows:  # Playwright omits Open tabs for a single page.
        return (page[1],), 0
    try:
        tabs = _tab_rows(rows[1].strip())
    except ValueError:
        return None
    for tab in tabs:
        if tab["active"] and tab["url"] == page[1]:
            return tuple(row["url"] for row in tabs), tab["index"]
    return None


def _click_options(options: Any) -> dict:
    if not isinstance(options, dict) or options.keys() - {"button", "doubleClick", "modifiers"}:
        raise ValueError("InvalidClickOptions: use button, doubleClick or modifiers")
    if "button" in options and options["button"] not in _CLICK_BUTTONS:
        raise ValueError("InvalidClickOptions: button must be left, right or middle")
    if "doubleClick" in options and type(options["doubleClick"]) is not bool:
        raise ValueError("InvalidClickOptions: doubleClick must be a boolean")
    if "modifiers" in options and (not isinstance(options["modifiers"], list)
            or any(modifier not in _CLICK_MODIFIERS for modifier in options["modifiers"])):
        raise ValueError("InvalidClickOptions: modifiers must be an array of Playwright modifier names")
    return options


class BrowserBackend:
    def __init__(self, mode: str, invoke: Callable, *, workspace: Path | None = None,
                 stage_upload: Callable[[list[str]], list[str]] | None = None):
        if mode not in {"managed", "user"}:
            raise ValueError("Invalid browser mode")
        self.mode, self.invoke = mode, invoke
        self.handle = uuid.uuid4().hex
        self.workspace = workspace
        self.stage_upload = stage_upload
        self.tab_inventory: tuple[str, ...] | None = None
        self.tab_handles: dict[str, int] = {}
        self.ref_handle: str | None = None
        self.refs: dict[str, tuple[str, str]] = {}
        self.generation: tuple | None = None
        self.current_dialog: tuple | None = None

    def invalidate(self):
        self.refs.clear()
        self.ref_handle = None

    def close(self):
        # Connections and user-owned tabs belong to the host, not this facade.
        self.invalidate()
        self.handle = uuid.uuid4().hex
        self.tab_handles.clear()
        self.tab_inventory = None
        self.current_dialog = None

    def _call(self, operation, args, deadline, *, expected=None):
        result, generation = self.invoke(f"{self.mode}_browser_{operation}", args, deadline, expected)
        if self.generation is not None and generation != self.generation:
            self.invalidate()
            self.tab_inventory = None
            self.tab_handles.clear()
            self.current_dialog = None
        self.generation = generation
        if not result.success or result.permission_request:
            self.invalidate()
            raise ValueError(result.error or "PermissionRequired: stop and obtain host authorization; do not replay the cell")
        try:
            payload = json.loads(result.content)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict) and (payload.get("ok") is False or payload.get("isError") is True):
            self.invalidate()
            raise ValueError(json.dumps(payload, ensure_ascii=False))
        return result, payload

    def _user_page(self, operation, args, deadline):
        self.invalidate()
        _, payload = self._call(operation, args, deadline)
        if (not isinstance(payload, dict) or payload.get("ok") is not True
                or payload.get("source") != "browser_connector"
                or not isinstance(payload.get("data"), dict)):
            raise ValueError("InvalidPageText: expected a user browser connector result")
        return payload["data"]

    def _observe(self, deadline, handle=None):
        expected = self.generation if handle in self.tab_handles else None
        result, payload = self._call("snapshot", {}, deadline, expected=expected)
        self.invalidate()
        self.ref_handle = handle or self.handle
        observation = uuid.uuid4().hex
        if self.mode == "managed":
            def replace(match):
                ref = f"r_{observation}_{len(self.refs)}"
                self.refs[ref] = (match.group(1), "observed element")
                return f"[ref={ref}]"
            # Playwright's target field also accepts selectors. Only wrap its AX refs.
            text = re.sub(r"\[ref=((?:f[0-9]+)?e[0-9]+)\]", replace, result.content)
        else:
            # Gateway emits a JSON outcome containing its snapshot data.
            def convert(value):
                if isinstance(value, list):
                    return [convert(item) for item in value]
                if not isinstance(value, dict):
                    return value
                value = dict(value)
                if isinstance(value.get("ref"), str):
                    ref = f"r_{observation}_{len(self.refs)}"
                    self.refs[ref] = (value["ref"], str(value.get("name") or value.get("text") or "observed element"))
                    value["ref"] = ref
                return {key: convert(item) for key, item in value.items()}
            if not isinstance(payload, dict):
                raise ValueError("InvalidSnapshot: user backend did not return structured snapshot JSON")
            text = json.dumps(convert(payload), ensure_ascii=False)
        return {"state": {"mode": self.mode, "binding": "managed-tab" if handle in self.tab_handles else "current-page-only",
                          "dom": {"mode": "full", "observationId": observation,
                                  "baseId": None, "scope": "backend snapshot",
                                  "complete": False, "text": text}}}

    def _tabs(self, action, deadline, **args):
        result, _ = self._call("tabs", {"action": action, **args}, deadline,
                               expected=self.generation if action in {"select", "close"} else None)
        body = _result_text(result.content)
        # Playwright's tabs tool can report the selected tab's modal even when
        # browser_snapshot is blocked. Keep that notice with this exact inventory.
        return _tab_rows(body), _modal_notice(_response_header(result.content))

    def _record_tabs(self, tabs, *, topology_changed=False, navigated_index=None):
        inventory = tuple(tab["url"] for tab in tabs)
        previous = self.tab_inventory
        if navigated_index is not None and previous is not None and len(inventory) == len(previous):
            # Keep the selected handle on a navigation we just dispatched.
            previous = tuple(url if i != navigated_index else inventory[i] for i, url in enumerate(previous))
        if topology_changed or inventory != previous:
            self.tab_handles.clear()
            self.invalidate()
        self.tab_inventory = inventory
        return tabs

    def _tab_handle(self, index):
        for handle, existing in self.tab_handles.items():
            if existing == index:
                return handle
        handle = "tab_" + uuid.uuid4().hex
        self.tab_handles[handle] = index
        return handle

    def _select(self, handle, deadline):
        tabs, notice = self._tabs("list", deadline)
        self._record_tabs(tabs)
        if handle not in self.tab_handles:
            raise ValueError("StaleTab: list tabs again and acquire a fresh handle")
        index = self.tab_handles[handle]
        if not tabs[index]["active"]:
            self.invalidate()
            tabs, notice = self._tabs("select", deadline, index=index)
            self._record_tabs(tabs)
            if handle not in self.tab_handles or not tabs[index]["active"]:
                raise ValueError("StaleTab: Playwright did not select the requested tab")
        return index, notice

    def _managed_result(self, handle, content, *, may_navigate=False):
        header = _response_header(content)
        notice = _modal_notice(header)
        identity = _action_inventory(header) if may_navigate or notice else None
        if handle == self.handle:
            self.current_dialog = (identity, notice, self.generation) if identity and notice else None
        elif may_navigate and identity and self.tab_inventory is not None:
            urls, index = identity
            previous = self.tab_inventory
            if (self.tab_handles.get(handle) == index and len(previous) == len(urls)
                    and previous[:index] == urls[:index] and previous[index + 1:] == urls[index + 1:]):
                self.tab_inventory = urls
        return notice

    def _verify_current_dialog(self, deadline):
        receipt = self.current_dialog
        self.current_dialog = None
        if receipt:
            tabs, notice = self._tabs("list", deadline)
            urls = tuple(tab["url"] for tab in tabs)
            for tab in tabs:
                if (tab["active"] and urls.count(tab["url"]) == 1
                        and receipt == ((urls, tab["index"]), notice, self.generation)):
                    return
        raise ValueError("DialogTabRequired: listTabs(), identify the original page, then getTab(id) "
                         "and handle its reported dialog without taking a snapshot")

    def _upload_paths(self, path):
        values = [path] if isinstance(path, str) else path
        if self.workspace is None or not isinstance(values, list) or not values:
            raise ValueError("InvalidUpload: supply one or more files in the session workspace")
        workspace = self.workspace.resolve()
        paths = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise ValueError("InvalidUpload: every path must be a non-empty string")
            candidate = (workspace / value).resolve()
            if not candidate.is_relative_to(workspace) or not candidate.is_file():
                raise ValueError("InvalidUpload: upload only existing files in the session workspace")
            paths.append(str(candidate))
        return paths

    def _managed_action(self, method, handle, args, deadline):
        if method == "browser.scroll":
            deltas = {key: args.get(key, 0) for key in ("deltaX", "deltaY")}
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in deltas.values()):
                raise ValueError("InvalidScroll: wheel deltas must be finite numbers")
            coordinates = {key: args.get(key) for key in ("x", "y")}
            if any(value is not None for value in coordinates.values()):
                if any(type(value) not in (int, float) or not math.isfinite(value)
                       for value in coordinates.values()):
                    raise ValueError("InvalidScroll: supply both finite x and y coordinates")
                self.invalidate()
                self._call("mouse_move_xy", coordinates, deadline, expected=self.generation)
            operation, params = "mouse_wheel", deltas
        elif method == "browser.pressKey":
            key = args.get("key")
            mask = args.get("modifiers", 0)
            if not isinstance(key, str) or not key or type(mask) is not int or not 0 <= mask <= 15:
                raise ValueError("InvalidKey: expected a normalized key and modifier mask")
            # The shared worker emits a CDP-style mask. Playwright takes one chord.
            modifiers = [name for bit, name in ((1, "Alt"), (2, "Control"), (4, "Meta"), (8, "Shift"))
                         if mask & bit]
            operation, params = "press_key", {"key": "+".join([*modifiers, key])}
        elif method == "browser.hover":
            ref, description = self._element_ref(handle, args.get("target"))
            operation, params = "hover", {"target": ref, "element": description}
        elif method == "browser.selectOption":
            values = args.get("values")
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
                raise ValueError("InvalidSelectOption: supply a string or a nonempty array of strings")
            ref, description = self._element_ref(handle, args.get("target"))
            operation, params = "select_option", {"target": ref, "element": description, "values": values}
        elif method == "browser.waitForText":
            text, state = args.get("text"), args.get("state")
            if not isinstance(text, str) or not text.strip() or state not in ("visible", "hidden"):
                raise ValueError("InvalidWaitForText: supply nonempty text and state visible or hidden")
            operation, params = "wait_for", {"text" if state == "visible" else "textGone": text}
        elif method == "browser.handleDialog":
            if type(args.get("accept")) is not bool or (
                    "promptText" in args and not isinstance(args["promptText"], str)):
                raise ValueError("InvalidDialog: supply a boolean accept and optional string promptText")
            operation, params = "handle_dialog", args
        elif method == "browser.uploadFile":
            paths = self._upload_paths(args.get("path"))
            target = args.get("selector")
            if target is not None:
                ref, description = self._element_ref(handle, target)
            if self.stage_upload is not None:
                paths = self.stage_upload(paths)
            if target is not None:
                self.invalidate()
                self._call("click", {"target": ref, "element": description}, deadline, expected=self.generation)
            operation, params = "file_upload", {"paths": paths}
        else:
            expression = args.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                raise ValueError("InvalidEvaluate: supply a page expression or function")
            operation, params = "evaluate", {"function": expression}

        self.invalidate()
        result, _ = self._call(operation, params, deadline, expected=self.generation)
        if operation == "evaluate":
            value = _result_text(result.content)
            try:
                return json.loads(value)
            except ValueError:
                return None if value == "undefined" else value
        return self._managed_result(handle, result.content, may_navigate=operation == "select_option")

    def _element_ref(self, handle, target):
        if not isinstance(target, str) or self.ref_handle != handle or target not in self.refs:
            raise ValueError("StaleObservation: read current page again and use its ref")
        return self.refs[target]

    def control(self, method: str, handle: str | None, args: dict | None, *, deadline: float) -> Any:
        args = args or {}
        # A floating handle may resolve only its own last observed dialog.
        if method not in {"browser.availability", "browser.listTabs", "browser.getBrowser",
                          "browser.handleDialog"} or handle in self.tab_handles:
            self.current_dialog = None
        if method == "browser.availability":
            api = _BROWSER_API[self.mode]
            methods = (*api["browserMethods"], *api["tabMethods"])
            return [{
                "id": self.mode,
                "binding": "managed-tab" if self.mode == "managed" else "current-page-only",
                "connection": "not-probed",
                "capabilities": ["getTab(current)" if name == "getTab" else name for name in methods],
                **api,
            }]
        if method == "browser.getBrowser":
            if args.get("browser") != self.mode:
                raise ValueError("ModeMismatch: getBrowser must match this tool's mode")
            return {
                "tabs": [{"id": "current", "binding": "current-page-only"}],
                **_BROWSER_API[self.mode],
            }
        if method == "browser.openAndRead":
            if self.mode != "user":
                raise ValueError("UnsupportedCapability: openAndRead requires user mode")
            return self._user_page("open_tab_and_read", {"url": args.get("url")}, deadline)
        target = args.get("targetId")
        if method == "browser.getTab" and target == "current":
            return {"handle": self.handle, **self._observe(deadline)}
        if method in {"browser.listTabs", "browser.newTab", "browser.getTab"}:
            if self.mode != "managed":
                raise ValueError("UnsupportedCapability: user browser only supports getTab('current')")
            if method == "browser.listTabs":
                tabs, _ = self._tabs("list", deadline)
                self._record_tabs(tabs)
                return [{**tab, "id": self._tab_handle(tab["index"])} for tab in tabs]
            if method == "browser.newTab":
                tabs, notice = self._tabs("new", deadline, url=args.get("url") or "about:blank")
                self._record_tabs(tabs, topology_changed=True)
                if not tabs:
                    raise ValueError("InvalidTabInventory: Playwright did not return the new tab")
                index = next(tab["index"] for tab in tabs if tab["active"])
                target = self._tab_handle(index)
            elif target is None:
                raise ValueError("InvalidArguments: getTab requires 'current' or an ID returned by listTabs()")
            elif not isinstance(target, str) or target not in self.tab_handles:
                raise ValueError("StaleTab: select a tab ID returned by the current inventory")
            else:
                _, notice = self._select(target, deadline)
            if notice:
                self.invalidate()
                return {"handle": target, **notice}
            return {"handle": target, **self._observe(deadline, target)}
        if handle != self.handle and handle not in self.tab_handles:
            raise ValueError("HandleScopeMismatch: list tabs again or reacquire the current page")
        if self.mode == "managed" and method == "browser.handleDialog" and handle not in self.tab_handles:
            self._verify_current_dialog(deadline)
        index = self._select(handle, deadline)[0] if handle in self.tab_handles else None
        if self.mode == "managed" and method == "browser.activate":
            self.invalidate()
            return None
        if self.mode == "managed" and method == "browser.close":
            args = {} if index is None else {"index": index}
            tabs, _ = self._tabs("close", deadline, **args)
            self._record_tabs(tabs, topology_changed=True)
            return None
        if self.mode == "managed" and method in {
            "browser.scroll", "browser.pressKey", "browser.hover", "browser.uploadFile", "browser.evaluate",
            "browser.selectOption", "browser.waitForText", "browser.handleDialog",
        }:
            return self._managed_action(method, handle, args, deadline)
        if method == "browser.getState":
            return self._observe(deadline, handle)
        if self.mode == "user" and method == "browser.readText":
            if args:
                raise ValueError("InvalidArguments: readText takes no arguments")
            return self._user_page("read_current_page", {}, deadline)
        if method in {"browser.getScreenshot", "browser.getStateAndScreenshot"}:
            if self.mode != "managed":
                raise ValueError("UnsupportedCapability: user screenshots are not exposed by Gateway")
            state = self._observe(deadline, handle) if method.endswith("AndScreenshot") else {"state": {}}
            expected = self.generation if index is not None else None
            result, _ = self._call("take_screenshot", {"type": "png"}, deadline, expected=expected)
            images = (result.raw_output or {}).get("mcp_inline_images", [])
            if not images:
                images = result.image_content or []
            if not images:
                raise ValueError("ScreenshotUnavailable: backend returned no inline image")
            image = images[0]
            from box_agent.plugins.cua.image_encoding import encode_canonical_cua_image
            canonical = encode_canonical_cua_image(
                image.get("data", ""), image.get("mime_type") or image.get("media_type") or "image/png",
                preserve_geometry=True,
            )
            if not canonical:
                raise ValueError("ScreenshotUnavailable: invalid backend image")
            state["state"]["pixels"] = {"width": canonical["width"], "height": canonical["height"]}
            state["image"] = {"type": "image", "data": canonical["data"], "mimeType": canonical["media_type"]}
            return state
        if method == "browser.goto" or (self.mode == "managed" and method == "browser.back"):
            if method == "browser.back":
                operation, params = "navigate_back", {}
            else:
                params = {"url": args["url"]}
                if self.mode == "user":
                    return {"page": self._user_page("open_tab_and_read", params, deadline)}
                operation = "navigate"
            self.invalidate()
            result, _ = self._call(operation, params, deadline,
                                   expected=self.generation if index is not None else None)
            if index is not None:
                tabs, _ = self._tabs("list", deadline)
                self._record_tabs(tabs, navigated_index=index)
            return self._managed_result(handle, result.content)
        if method in {"browser.click", "browser.fill"}:
            options = args.get("options", {})
            if self.mode == "user" and options:
                raise ValueError("UnsupportedCapability: click options are not implemented")
            if self.mode == "managed" and method == "browser.click":
                options = _click_options(options)
            raw_ref, description = self._element_ref(handle, args.get("target"))
            self.invalidate()
            if self.mode == "managed":
                params = {"target": raw_ref, "element": description}
                if method == "browser.click":
                    params.update(options)
            else:
                params = {"ref": raw_ref}
            if method == "browser.fill":
                if self.mode == "managed":
                    params.update(text=args["text"], slowly=False)
                    operation = "type"
                else:
                    params["value"] = args["text"]
                    operation = "fill"
            else:
                operation = "click"
            result, _ = self._call(operation, params, deadline, expected=self.generation)
            if self.mode == "managed":
                return self._managed_result(handle, result.content, may_navigate=operation == "click")
            return None
        raise ValueError(f"UnsupportedCapability: {method}")
