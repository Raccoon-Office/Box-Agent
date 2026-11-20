"""Real Node worker tests with recording backends; no GUI or Driver required."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from box_agent.plugins.repl.runtime import ReplRuntime
from box_agent.plugins.repl.tools import create_tools
from box_agent.tools.base import Tool, ToolInvocationContext, ToolResult
from box_agent.tools.browser_runtime_scope import (
    current_browser_host_binding, set_browser_host_binding, reset_browser_host_binding,
)


@pytest.mark.parametrize("frozen", [True, False])
def test_native_window_helper_selects_executable_entry(monkeypatch, frozen):
    import sys
    from box_agent.plugins.repl.runtime import native_window_command

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    expected = [sys.executable]
    if not frozen:
        expected += ["-m", "box_agent.acp.runtime_entry"]
    assert native_window_command() == [*expected, "--cua-native"]


def test_repl_uses_host_node_without_path_lookup(monkeypatch, tmp_path):
    from box_agent.plugins.repl.runtime import resolve_node

    host = tmp_path / "node"
    host.touch()
    monkeypatch.setenv("BOX_AGENT_NODE", str(host))
    monkeypatch.setattr(shutil, "which", lambda name: pytest.fail("Host Node must take precedence"))
    assert resolve_node(None) == str(host)
    configured = tmp_path / "configured-node"
    configured.touch()
    assert resolve_node(str(configured)) == str(configured)
    host.unlink()
    with pytest.raises(ValueError, match="Node runtime missing"):
        resolve_node(None)


def test_native_adapter_uses_injected_command_and_validates_response(monkeypatch):
    from box_agent._vendor.mac_cua.cua_adapter import cua_adapter as native

    adapter = native.Adapter(SimpleNamespace(tools={}), native_command=["/runtime/acp", "--cua-native"])
    adapter.record = lambda event: None
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"pid": 23, "source": "guest_native_ax", "status": "ok"}))

    monkeypatch.setattr(native.subprocess, "run", run)
    assert adapter.native_window_context({"pid": 23})["structuredContent"]["status"] == "ok"
    assert calls[0][0] == ["/runtime/acp", "--cua-native", "--pid", "23"]
    assert calls[0][1]["timeout"] == 5.0
    assert adapter.native_window_context({"pid": 24})["structuredContent"]["status"] == "helper_failed"


class RecordingTool(Tool):
    description = "record browser operations"
    parameters = {"type": "object", "additionalProperties": True}
    name = ""

    def __init__(self, name, content="", *, success=True):
        self.name, self.content, self.success = name, content, success
        self.calls = []
        self.mcp_generation = 1

    async def execute(self, **arguments):
        self.calls.append((arguments, current_browser_host_binding()))
        return ToolResult(success=self.success, content=self.content,
                          error=None if self.success else "backend refused")


@pytest.fixture
async def runtime(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for real-worker tests")
    value = ReplRuntime(node=node, workspace=tmp_path)
    value.agent = SimpleNamespace(tools={}, messages=[])
    try:
        yield value
    finally:
        await value.close()


async def js(runtime, code, mode="managed", timeout_ms=3000, context=None):
    tool = create_tools(runtime)[2]
    return await tool.invoke({"mode": mode, "code": code, "timeout_ms": timeout_ms}, context=context)


@pytest.mark.asyncio
@pytest.mark.parametrize(("trace", "has_log"), [(False, True), (True, False), (True, True)])
async def test_native_driver_trace_belongs_to_opted_in_session(runtime, tmp_path, monkeypatch, trace, has_log):
    from unittest.mock import AsyncMock
    from box_agent._vendor.mac_cua.cua_adapter import cua_adapter

    monkeypatch.setenv("BOX_AGENT_CUA_TRACE", "1" if trace else "0")
    if has_log:
        runtime.agent.session_log = SimpleNamespace(path=tmp_path / "session.jsonl")
    runtime.cua_service = SimpleNamespace(prepare=AsyncMock(), driver_command=lambda: [])
    tools = {name: {"inputSchema": {"properties": {"session": {}}}}
             for name in ("start_session", "list_apps", "end_session")}
    driver = SimpleNamespace(tools=tools, call=lambda *_: {"structuredContent": {"apps": []}},
                             close=lambda: None)
    monkeypatch.setattr(cua_adapter, "McpClient", lambda *args, **kwargs: driver)
    assert (await js(runtime, "nodeRepl.write(1)")).success
    directory = tmp_path / "cua-evidence"
    assert not directory.exists()
    code = "await cua.listApps()"
    assert (await create_tools(runtime)[0].invoke({"code": code})).success
    if trace and has_log:
        [receipt] = directory.glob("*.jsonl")
        rows = [json.loads(line) for line in receipt.read_text().splitlines()]
        assert rows[0]["code"] == code
        assert [(r["phase"], r["tool"]) for r in rows if r.get("tool") == "list_apps"] == [
            ("dispatch", "list_apps"), ("result", "list_apps")]
        assert rows[-1]["phase"] == "cell_end" and rows[-1]["status"] == "completed"
    else:
        assert not directory.exists()


@pytest.mark.asyncio
async def test_single_axpress_cannot_complete_stops_cell_and_requests_readback(runtime, monkeypatch):
    from unittest.mock import AsyncMock
    from box_agent._vendor.mac_cua.cua_adapter import cua_adapter as native
    from tests.test_cua_adapter_port import FakeDriver

    driver = FakeDriver()
    driver.responses["list_apps"]["structuredContent"]["apps"][0].update(
        name="Microsoft Excel", bundle_id="com.microsoft.Excel")
    driver.responses["list_windows"]["structuredContent"].update(
        active_window_id=7, frontmost_window_id=7, app_active=True)
    button = driver.responses["get_window_state"]["structuredContent"]["elements"][0]
    button.update(role="AXButton", label="排序")
    raw_error = "AX action failed: AXUIElementPerformAction(AXPress) returned -25204"
    driver.responses["click"] = {"isError": True, "content": [{"type": "text", "text": raw_error}]}
    monkeypatch.setattr(native, "McpClient", lambda *args, **kwargs: driver)
    monkeypatch.setattr(native, "native_window_inventory", lambda pid: [])
    runtime.cua_service = SimpleNamespace(prepare=AsyncMock(), driver_command=lambda: [])

    tool = create_tools(runtime)[0]
    initial = await tool.invoke({"code": 'let app = await cua.getApp("com.microsoft.Excel")'})
    assert initial.success and '0 button "排序"' in initial.content
    driver.calls.clear()
    result = await tool.invoke({"code": "await app.click(0); await app.getAXState()"})
    assert not result.success
    assert raw_error in result.error
    assert '"effect": "unverifiable"' in result.error
    assert "动作可能已生效，先读回界面" in result.error
    assert "app.getAXState()" in result.error
    assert [name for name, _ in driver.calls if name in
            {"click", "double_click", "bring_to_front", "get_window_state"}] == ["click"]

    button.update(label="确定", element_token="s2:1")
    observed = await tool.invoke({"code": "await app.getAXState({disableDiffing:true})"})
    assert observed.success and 'button "确定"' in observed.content
    assert sum(name == "click" for name, _ in driver.calls) == 1


@pytest.mark.parametrize(("name", "arguments", "action", "code"), [
    ("click", {"element_token": "s1:1"}, "AXPress", -25202),
    ("click", {"element_token": "s1:1", "action": "pick"}, "AXPick", -25204),
    ("double_click", {"element_token": "s1:1"}, "AXOpen", -25204),
    ("click", {"x": 10, "y": 20}, "AXPress", -25204),
])
def test_other_driver_click_errors_retain_their_original_result(name, arguments, action, code):
    from box_agent._vendor.mac_cua.cua_adapter.cua_adapter import Adapter
    from box_agent._vendor.mac_cua.cua_adapter.cua_driver import ActionError
    from tests.test_cua_adapter_port import FakeDriver

    driver = FakeDriver()
    message = f"AX action failed: AXUIElementPerformAction({action}) returned {code}"
    original = {"isError": True, "content": [{"type": "text", "text": message}]}
    driver.responses[name] = original
    with pytest.raises(ActionError) as caught:
        Adapter(driver).call(name, {"pid": 42, "window_id": 7, **arguments})
    assert caught.value.result == original
    assert [tool for tool, _ in driver.calls] == [name]


@pytest.mark.asyncio
async def test_js_persists_within_mode_and_reset_is_local(runtime):
    assert (await js(runtime, "let count = 23; nodeRepl.write(count * 7)")).content == "161"
    assert (await js(runtime, "nodeRepl.write(count + 1)")).content == "24"
    assert (await js(runtime, "nodeRepl.write(typeof count)", "user")).content == "undefined"
    await runtime.reset("managed")
    assert (await js(runtime, "nodeRepl.write(typeof count)")).content == "undefined"


@pytest.mark.asyncio
async def test_repl_executes_top_level_await_with_trailing_line_comment(runtime):
    result = await js(runtime, "await Promise.resolve().then(() => nodeRepl.write(23 * 7)); // 2")
    assert result.success, result.error
    assert result.content == "161"


@pytest.mark.asyncio
@pytest.mark.parametrize("target_key", ["target", "ref"])
async def test_browser_calls_existing_tools_and_keeps_current_context(runtime, target_key):
    snapshot = RecordingTool("managed_browser_snapshot", '- text: "[ref=#injected]"\n- button "Expand" [ref=e17]')
    click = RecordingTool("managed_browser_click")
    fill = RecordingTool("managed_browser_type")
    target_properties = {target_key: {"type": "string"}, "element": {"type": "string"}}
    click.parameters = {"type": "object", "properties": target_properties,
                        "required": [target_key], "additionalProperties": False}
    fill.parameters = {
        "type": "object",
        "properties": {**target_properties, "text": {"type": "string"}, "slowly": {"type": "boolean"}},
        "required": [target_key, "text"], "additionalProperties": False,
    }
    runtime.agent.tools = {t.name: t for t in (snapshot, click, fill)}
    result = await js(runtime, "let b = await cua.getBrowser('managed'); let t = await b.getTab('current');")
    assert result.success, result.error
    backend = runtime.realms["managed"].service.browser
    assert len(backend.refs) == 1
    ref = next(iter(backend.refs))
    token = set_browser_host_binding("second-turn-binding")
    try:
        result = await js(runtime, f"nodeRepl.write(await t.click({json.dumps(ref)}));")
    finally:
        reset_browser_host_binding(token)
    assert result.success and result.content == "undefined"
    assert click.calls == [({target_key: "e17", "element": "observed element"}, "second-turn-binding")]
    rejected = await js(runtime, f"await t.click({json.dumps(ref)});")
    assert not rejected.success and "StaleObservation" in rejected.error
    assert len(click.calls) == 1
    snapshot.content = '- textbox "Name" [ref=e22]'
    assert (await js(runtime, "await t.getState();")).success
    field = next(iter(backend.refs))
    assert (await js(runtime, f"await t.fill({json.dumps(field)}, 'test');")).success
    assert fill.calls[0][0] == {
        target_key: "e22", "element": "observed element", "text": "test", "slowly": False,
    }


@pytest.mark.asyncio
async def test_mode_and_app_api_cannot_be_mixed(runtime):
    result = await js(runtime, "nodeRepl.write(typeof cua.getApp)")
    assert result.content == "undefined"
    result = await js(runtime, "await cua.getBrowser('user')")
    assert not result.success and "ModeMismatch" in result.error
    result = await js(runtime, "await cua.getState()")
    assert result.success and "managed" in result.content


@pytest.mark.asyncio
async def test_reconnected_backend_rejects_old_refs_before_click(runtime):
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    click = RecordingTool("managed_browser_click")
    runtime.agent.tools = {t.name: t for t in (snapshot, click)}
    await js(runtime, "let b = await cua.getBrowser('managed'); let t = await b.getTab('current');")
    ref = next(iter(runtime.realms["managed"].service.browser.refs))
    click.mcp_generation = 2
    result = await js(runtime, f"await t.click('{ref}')")
    assert not result.success and "reconnected" in result.error
    assert click.calls == []


@pytest.mark.asyncio
async def test_user_backend_still_requires_current_page_intent(runtime):
    snapshot = RecordingTool("user_browser_snapshot", json.dumps({"ok": True, "data": {"elements": [{"ref": "snapshot:e4"}]}}))
    click = RecordingTool("user_browser_click")
    runtime.agent.tools = {t.name: t for t in (snapshot, click)}
    denied = await js(runtime, "let b = await cua.getBrowser('user'); await b.getTab('current')", "user")
    assert not denied.success and snapshot.calls == []
    runtime.turn_text = "点击当前页面中的展开按钮"
    result = await js(runtime, "let t = await b.getTab('current');", "user")
    assert result.success, result.error
    ref = next(iter(runtime.realms["user"].service.browser.refs))
    result = await js(runtime, f"await t.click('{ref}');", "user")
    assert result.success, result.error
    assert click.calls[0][0] == {"ref": "snapshot:e4"}


@pytest.mark.asyncio
async def test_user_confirmed_url_read_returns_gateway_body_without_active_page_access(runtime):
    url = "https://www.iana.org/domains/reserved"
    body = {"url": url, "mode": "full", "title": "Reserved Domains", "content": "测试 測試 テスト"}
    navigate = RecordingTool("user_browser_open_tab_and_read", json.dumps({
        "ok": True, "source": "browser_connector", "data": body,
    }))
    snapshot = RecordingTool("user_browser_snapshot", '{"ok":true,"data":{"elements":[]}}')
    read = RecordingTool("user_browser_read_current_page")
    runtime.agent.tools = {tool.name: tool for tool in (navigate, snapshot, read)}
    runtime.turn_text = "用户选择：“在你的 Chrome 里打开 IANA 页面读取”（option_id=open_in_user_browser）。请按该选择继续原任务。"
    result = await js(runtime, f"var b = await cua.getBrowser('user'); var page = await b.openAndRead('{url}');", "user")
    assert result.success and body["content"] in result.content
    assert navigate.calls == [({"url": url}, None)]
    assert result.raw_output["operations"] == [{"tool": navigate.name, "success": True}]
    assert (await js(runtime, "nodeRepl.write(page.title)", "user")).content == body["title"]
    denied = await js(runtime, "await b.getTab('current')", "user")
    assert "CURRENT_PAGE_INTENT_REQUIRED" in denied.error
    assert not snapshot.calls and not read.calls

    navigate.content = '{"ok":false,"error":"url_mismatch"}'
    mismatch = await js(runtime, f"await b.openAndRead('{url}')", "user")
    assert not mismatch.success and "url_mismatch" in mismatch.error
    assert not snapshot.calls and not read.calls

    managed = await js(runtime, "nodeRepl.write(typeof (await cua.getBrowser('managed')).openAndRead)")
    assert managed.success and managed.content.splitlines()[-1] == "undefined"
    assert len(navigate.calls) == 2

    runtime.turn_text = "读取当前页面"
    assert (await js(runtime, "var tab = await b.getTab('current');", "user")).success
    navigate.content = json.dumps({"ok": True, "source": "browser_connector", "data": body})
    result = await js(runtime, f"var opened = await tab.goto('{url}'); nodeRepl.write(opened.title);", "user")
    assert result.success and body["content"] in result.content and body["title"] in result.content
    assert not read.calls


@pytest.mark.asyncio
async def test_user_read_text_uses_only_the_current_page_connector(runtime):
    snapshot = RecordingTool("user_browser_snapshot", '{"ok":true,"data":{"elements":[{"ref":"snapshot:e1"}]}}')
    body = {"mode": "full", "url": "https://example.test/article", "title": "Article",
            "content": "The body contains a unique phrase.", "truncated": False}
    envelope = {"ok": True, "source": "browser_connector", "session_id": "private",
                "data": body}
    read = RecordingTool("user_browser_read_current_page", json.dumps(envelope))
    read.parameters = {"type": "object", "additionalProperties": False}
    click = RecordingTool("user_browser_click")
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, read, click)}
    runtime.turn_text = "读一下我浏览器当前标签页的正文"
    assert (await js(runtime, "var b = await cua.getBrowser('user'); var t = await b.getTab('current');", "user")).success
    ref = next(iter(runtime.realms["user"].service.browser.refs))

    token = set_browser_host_binding("current-turn")
    try:
        result = await js(runtime, "var article = await t.readText(); nodeRepl.write(article.content);", "user")
    finally:
        reset_browser_host_binding(token)
    assert result.success and body["content"] in result.content and body["url"] in result.content
    assert 'session_id' not in result.content
    assert read.calls == [({}, "current-turn")]
    assert result.raw_output["operations"] == [{"tool": read.name, "success": True}]
    stale = await js(runtime, f"await t.click('{ref}');", "user")
    assert not stale.success and "StaleObservation" in stale.error and not click.calls

    injected = await js(runtime, "await t.readText({url: 'https://other.test', tabId: 2});", "user")
    assert not injected.success and "InvalidArguments" in injected.error
    assert len(read.calls) == 1
    runtime.turn_text = "写一首诗"
    denied = await js(runtime, "await t.readText();", "user")
    assert not denied.success and len(read.calls) == 1

    runtime.turn_text = "继续读当前页面"
    envelope["data"] = {"mode": "chunked", "title": "Long article", "outline": [{"heading": "First part"}],
                        "summaryHint": "Partial material", "truncated": True}
    read.content = json.dumps(envelope)
    partial = await js(runtime, "await t.readText();", "user")
    assert partial.success and "First part" in partial.content and "truncated: true" in partial.content
    envelope["source"] = "playwright"
    read.content = json.dumps(envelope)
    wrong_source = await js(runtime, "await t.readText();", "user")
    assert not wrong_source.success and "InvalidPageText" in wrong_source.error
    read.content = '{"ok":false,"error":"extension_not_connected","retryable":true}'
    disconnected = await js(runtime, "await t.readText();", "user")
    assert not disconnected.success and "extension_not_connected" in disconnected.error

    managed_snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    runtime.agent.tools[managed_snapshot.name] = managed_snapshot
    before = len(read.calls)
    managed = await js(runtime, """
        var t = await (await cua.getBrowser('managed')).getTab('current');
        nodeRepl.write(typeof t.readText);
    """)
    assert managed.success and managed.content.splitlines()[-1] == "undefined"
    assert len(read.calls) == before


@pytest.mark.asyncio
async def test_internal_calls_apply_capability_gates(runtime):
    tool = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    runtime.agent.tools = {tool.name: tool}
    class Deny:
        async def check_invocation(self, name, args):
            return SimpleNamespace(code="DENIED", message="revoked")
    context = ToolInvocationContext(capability_gates=(Deny(),))
    result = await js(runtime, "let b = await cua.getBrowser('managed'); await b.getTab('current')", context=context)
    assert not result.success and "DENIED" in result.error
    assert tool.calls == []


@pytest.mark.asyncio
async def test_backend_failure_is_visible_and_does_not_replay(runtime):
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    click = RecordingTool("managed_browser_click", success=False)
    runtime.agent.tools = {t.name: t for t in (snapshot, click)}
    await js(runtime, "let b = await cua.getBrowser('managed'); let t = await b.getTab('current');")
    ref = next(iter(runtime.realms["managed"].service.browser.refs))
    result = await js(runtime, f"await t.click('{ref}')")
    assert not result.success and "backend refused" in result.error
    assert len(click.calls) == 1
    assert result.raw_output["operations"] == [{"tool": click.name, "success": False}]


@pytest.mark.asyncio
async def test_connector_revocation_prevents_internal_dispatch(runtime):
    from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
    from box_agent.tools.mcp_tool_search import MCPToolExposureManager
    from collections import OrderedDict
    from tests.test_mcp_tool_search import FakeMCPTool
    catalog = MCPToolCatalog()
    backend = FakeMCPTool("user_browser_read_current_page", "browser-gateway", connector_id="user-connector")
    catalog.replace_server("browser-gateway", [backend])
    allowed = {"user-connector"}
    exposure = MCPToolExposureManager(catalog, OrderedDict(), allowed_connector_ids_provider=lambda: frozenset(allowed))
    runtime.agent.mcp_tool_exposure = exposure
    assert runtime._resolve(backend.name)[0] is backend
    allowed.clear()
    with pytest.raises(ValueError, match="CapabilityDenied"):
        runtime._resolve(backend.name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "browser_methods", "tab_methods"),
    [
        (
            "managed",
            {"getTab", "listTabs", "newTab"},
            {
                "getState", "goto", "click", "fill", "activate", "close", "scroll", "pressKey",
                "hover", "uploadFile", "evaluate", "getScreenshot", "getStateAndScreenshot",
                "handleDialog", "waitForText", "back", "selectOption",
            },
        ),
        (
            "user",
            {"getTab", "openAndRead"},
            {"getState", "goto", "click", "fill", "readText"},
        ),
    ],
)
async def test_browser_objects_expose_exactly_the_methods_advertised_by_their_mode(
    runtime, mode, browser_methods, tab_methods,
):
    snapshot = (
        RecordingTool("managed_browser_snapshot", "button [ref=e1]")
        if mode == "managed"
        else RecordingTool("user_browser_snapshot", '{"ok":true,"data":{"elements":[]}}')
    )
    runtime.agent.tools = {snapshot.name: snapshot}
    runtime.turn_text = "查看当前页面"
    result = await js(runtime, f"""
        const browser = await cua.getBrowser('{mode}');
        const tab = await browser.getTab('current');
        const [{{browserMethods, tabMethods}}] = (await cua.getState({{emit: false}})).browsers;
        nodeRepl.write(JSON.stringify({{
            browser: Object.getOwnPropertyNames(Object.getPrototypeOf(browser)),
            tab: Object.getOwnPropertyNames(Object.getPrototypeOf(tab)),
            browserMethods,
            tabMethods,
            unsupported: [typeof tab.typeText, typeof tab.waitForLoad, typeof tab.waitForElement],
        }}));
    """, mode)
    assert result.success, result.error
    state = json.loads(result.content.splitlines()[-1])
    assert set(state["browser"]) == set(state["browserMethods"]) == browser_methods
    assert set(state["tab"]) == set(state["tabMethods"]) == tab_methods
    assert state["unsupported"] == ["undefined", "undefined", "undefined"]
    assert len(snapshot.calls) == 1


@pytest.mark.asyncio
async def test_unsupported_browser_methods_never_call_backend(runtime):
    snapshot = RecordingTool("user_browser_snapshot", '{"ok":true,"data":{"elements":[]}}')
    runtime.agent.tools = {snapshot.name: snapshot}
    runtime.turn_text = "查看当前页面"
    assert (await js(runtime, "let b = await cua.getBrowser('user'); let t = await b.getTab('current');", "user")).success
    for code in ("await b.listTabs()", "await t.evaluate('document.title')", "await t.hover('fake')",
                 "await t.pressKey('Tab')", "await t.scroll({deltaY: 20})", "await t.activate()",
                 "await t.close()", "await t.uploadFile(null, 'x.txt')", "await t.back()",
                 "await t.selectOption('fake', 'a')", "await t.waitForText('Ready')",
                 "await t.handleDialog({accept: false})"):
        result = await js(runtime, code, "user")
        assert not result.success and "is not a function" in result.error
    for code in ("await b.getTab('tab_other')", "await t.click('fake', {button: 'right'})"):
        result = await js(runtime, code, "user")
        assert not result.success and "UnsupportedCapability" in result.error
    assert len(snapshot.calls) == 1


@pytest.mark.asyncio
async def test_managed_tab_handles_route_and_expire_when_inventory_changes(runtime):
    pages = ["https://example.test/a", "https://example.test/b"]
    active = 0
    snapshots = []

    class Tabs(RecordingTool):
        async def execute(self, action, index=None, url=None):
            nonlocal active
            self.calls.append((action, index))
            if action == "select":
                active = index
            elif action == "new":
                pages.append(url)
                active = len(pages) - 1
            elif action == "close":
                pages.pop(index)
                active = 0
            rows = [f"- {i}:{' (current)' if active == i else ''} [Page {i}]({page})"
                    for i, page in enumerate(pages)]
            return ToolResult(success=True, content="### Result\n" + "\n".join(rows))

    class Snapshot(RecordingTool):
        async def execute(self):
            snapshots.append(pages[active])
            return ToolResult(success=True, content="button [ref=e1]")

    tabs, snapshot = Tabs("managed_browser_tabs"), Snapshot("managed_browser_snapshot")
    click = RecordingTool("managed_browser_click")
    runtime.agent.tools = {t.name: t for t in (tabs, snapshot, click)}
    result = await js(runtime, """
        const browser = await cua.getBrowser('managed');
        const tabs = await browser.listTabs();
        const a = await browser.getTab(tabs[0].id);
        const b = await browser.getTab(tabs[1].id);
        const state = await b.getState();
        const ref = state.dom.text.match(/r_[a-zA-Z0-9_]+/)[0];
        await a.activate();
    """)
    assert result.success, result.error
    assert active == 0
    rejected = await js(runtime, "await a.click(ref)")
    assert not rejected.success and "StaleObservation" in rejected.error and click.calls == []
    assert snapshots == pages + [pages[1]]
    result = await js(runtime, """
        const c = await browser.newTab('https://example.test/c');
        if (typeof c.close !== 'function' || typeof c.readText !== 'undefined' ||
            typeof c.waitForLoad !== 'undefined') {
            throw Error('New tabs must expose only managed methods');
        }
        await c.close();
        const fresh = await browser.listTabs();
        const second = await browser.getTab(fresh[1].id);
    """)
    assert result.success, result.error
    assert ("close", 2) in tabs.calls
    rejected = await js(runtime, "await a.getState()")
    assert not rejected.success and "HandleScopeMismatch" in rejected.error
    pages.pop(0)  # external inventory change reuses the old index
    active = 0
    rejected = await js(runtime, "await second.pressKey('Tab')")
    assert not rejected.success and "StaleTab" in rejected.error


@pytest.mark.asyncio
async def test_browser_skill_bindings_work_across_modes_and_evaluate_requires_a_new_ref(runtime):
    from box_agent.plugins.repl.documentation import manual

    skill = Path(__file__).resolve().parents[1] / "box_agent/skills/browser-use/SKILL.md"
    example = json.loads(skill.read_text().split("```json\n", 1)[1].split("\n```", 1)[0])
    init = json.loads(example["arguments_json"])["code"]
    for mode in ("user", "managed"):
        code = init.replace("'managed'", repr(mode))
        assert code in manual("browser", mode)
        result = await js(runtime, code, mode)
        assert result.success, result.error

    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    evaluate = RecordingTool("managed_browser_evaluate", "fixture")
    click = RecordingTool("managed_browser_click")
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, evaluate, click)}
    assert (await js(runtime, "tab = await browser.getTab('current');")).success
    backend = runtime.realms["managed"].service.browser
    old_ref = next(iter(backend.refs))
    assert (await js(runtime, "await tab.evaluate('() => document.title');")).success
    rejected = await js(runtime, f"await tab.click('{old_ref}');")
    assert not rejected.success and "StaleObservation" in rejected.error and not click.calls
    assert (await js(runtime, "await tab.getState();")).success
    fresh_ref = next(iter(backend.refs))
    assert fresh_ref != old_ref
    assert (await js(runtime, f"await tab.click('{fresh_ref}');")).success
    assert len(click.calls) == 1


@pytest.mark.asyncio
async def test_refresh_in_same_cell_cannot_retarget_a_previous_ref(runtime):
    import re

    snapshot = RecordingTool("managed_browser_snapshot", "button Price [ref=e1]")
    click = RecordingTool("managed_browser_click")
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, click)}
    first = await js(runtime, "var b = await cua.getBrowser('managed'); var t = await b.getTab('current');")
    assert first.success, first.error
    old = re.search(r"button Price \[ref=(r_\w+)\]", first.content)[1]
    snapshot.content = "button Other [ref=e1]\nbutton Price [ref=e2]"
    rejected = await js(runtime, f"await t.getState(); await t.click('{old}')")
    assert not rejected.success and "StaleObservation" in rejected.error
    assert not click.calls

    refreshed = await js(runtime, "await t.getState()")
    assert refreshed.success, refreshed.error
    current = re.search(r"button Price \[ref=(r_\w+)\]", refreshed.content)[1]
    assert (await js(runtime, f"await t.click('{current}')")).success
    assert [args["target"] for args, _ in click.calls] == ["e2"]


@pytest.mark.asyncio
async def test_managed_manual_price_example_preserves_page_escapes(runtime):
    from box_agent.plugins.repl.documentation import manual

    guide = manual("browser", "managed")
    code = next(block.split("\n```", 1)[0] for block in guide.split("```js\n")
                if block.startswith("await tab.evaluate(String.raw"))
    snapshot = RecordingTool("managed_browser_snapshot", "text [ref=e1]")
    evaluate = RecordingTool("managed_browser_evaluate", "[]")
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, evaluate)}
    assert (await js(runtime, "var b = await cua.getBrowser('managed'); var tab = await b.getTab('current');")).success
    result = await js(runtime, code)
    assert result.success, result.error
    # Parse and execute the exact string received by the mock Playwright tool in a second JS context.
    page_expression = evaluate.calls[0][0]["function"]
    fixture = "价格\n10999 元\n12s元\n其他\n999元"
    script = ("const document = {body: {innerText: " + json.dumps(fixture) + "}};\n"
              + "process.stdout.write(JSON.stringify((" + page_expression + ")()));")
    page = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, check=True)
    assert json.loads(page.stdout) == ["10999 元", "999元"]


@pytest.mark.parametrize("target,mode", [(0, "background"), ([1, 1], "foreground"), ({"x": 1, "y": 1}, None)])
def test_native_scroll_accepts_observed_targets_and_rejects_object_before_delivery(monkeypatch, target, mode):
    from box_agent._vendor.mac_cua.cua_adapter import cua_adapter as native
    from tests.test_cua_adapter_port import FakeDriver, result

    driver = FakeDriver()
    api = native.Adapter(driver)
    monkeypatch.setattr(native, "native_window_inventory", lambda pid: [])
    monkeypatch.setattr(api, "native_window_context", lambda args: result())
    state = {"pid": 42}
    api.handles["app"] = state
    api.observe(state, "getAXStateAndScreenshot")
    args = {"target": target, "direction": "up", "pages": 4}
    if mode is None:
        with pytest.raises(ValueError, match=r"Expected \[x,y\]"):
            api.control("scroll", "app", args)
    else:
        api.control("scroll", "app", args)
    sent = [args["delivery_mode"] for name, args in driver.calls if name == "scroll"]
    assert sent == ([] if mode is None else [mode])


@pytest.mark.asyncio
async def test_managed_redirect_requires_rebinding_and_allows_reusing_manual_example(runtime):
    url = "https://example.test/search"
    tabs = RecordingTool("managed_browser_tabs", f"- 0: (current) [Search]({url})")
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot)}
    init = await js(runtime, "let browser = await cua.getBrowser('managed'); let tab;")
    assert init.success, init.error
    from box_agent.plugins.repl.documentation import manual

    guide = manual("browser", "managed").split("## Managed browser workflow", 1)[1]
    example = guide.split("```js\n", 1)[1].split("\n```", 1)[0]
    skill = Path(__file__).resolve().parents[1] / "box_agent/skills/browser-use/SKILL.md"
    assert "list-and-rebind example" in skill.read_text()
    first = await js(runtime, example.replace("https://example.com/", url))
    assert first.success, first.error
    tabs.content = f"- 0: (current) [Search]({url}?redirect=1)"
    rejected = await js(runtime, "await tab.getState()")
    assert not rejected.success and "StaleTab" in rejected.error
    assert len(snapshot.calls) == 1
    no_match = await js(runtime, example.replace("https://example.com/", url))
    assert no_match.success and len(snapshot.calls) == 1
    rebound = await js(runtime, example.replace("https://example.com/", url + "?redirect=1"))
    assert rebound.success, rebound.error
    assert len(snapshot.calls) == 2


@pytest.mark.asyncio
async def test_managed_actions_use_existing_tools_and_check_upload_paths(runtime):
    (runtime.workspace / "fixture.txt").write_text("fixture")
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    tools = {name: RecordingTool("managed_browser_" + name) for name in
             ("hover", "press_key", "mouse_move_xy", "mouse_wheel", "click", "file_upload", "evaluate")}
    tools["evaluate"].content = '### Result\n{"title": "fixture"}\n### Ran Playwright code\nignored'
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, *tools.values())}
    result = await js(runtime, """
        const browser = await cua.getBrowser('managed');
        const tab = await browser.getTab('current');
        const state = await tab.getState();
        const ref = state.dom.text.match(/r_[a-zA-Z0-9_]+/)[0];
        await tab.hover(ref);
        await tab.pressKey('Control+a');
        await tab.scroll({x: 2, y: 3, deltaY: 450});
        await tab.evaluate('() => document.title');
    """)
    assert result.success, result.error
    assert 'fixture' in result.content
    expected = {"hover": {"target": "e1", "element": "observed element"},
                "press_key": {"key": "Control+a"}, "mouse_move_xy": {"x": 2, "y": 3},
                "mouse_wheel": {"deltaX": 0, "deltaY": 450},
                "evaluate": {"function": "() => document.title"}}
    for name, args in expected.items():
        assert tools[name].calls == [(args, None)]
    rejects = await js(runtime, "await tab.uploadFile(null, '../outside.txt')")
    assert not rejects.success and "InvalidUpload" in rejects.error
    assert tools["file_upload"].calls == []
    result = await js(runtime, """
        const next = await tab.getState();
        await tab.uploadFile(next.dom.text.match(/r_[a-zA-Z0-9_]+/)[0], 'fixture.txt');
    """)
    assert result.success, result.error
    assert tools["click"].calls[0][0]["target"] == "e1"
    assert tools["file_upload"].calls[0][0] == {"paths": [str(runtime.workspace / "fixture.txt")]}

    host = set_browser_host_binding("desktop-test-binding")
    try:
        result = await js(runtime, "await tab.uploadFile(null, 'fixture.txt');")
    finally:
        reset_browser_host_binding(host)
    assert result.success, result.error
    copied = Path(tools["file_upload"].calls[-1][0]["paths"][0])
    assert copied != runtime.workspace / "fixture.txt"
    assert copied.name == "fixture.txt" and copied.read_text() == "fixture"
    assert copied.parent.parent.stat().st_mode & 0o777 == 0o700
    await runtime.reset("managed")
    assert copied.is_file()
    await runtime.close()
    assert not copied.exists()


@pytest.mark.asyncio
async def test_managed_bind_displays_the_end_of_long_snapshots(runtime):
    tabs = RecordingTool("managed_browser_tabs", "- 0: (current) [Page](https://example.test/)")
    snapshot = RecordingTool("managed_browser_snapshot", "a" * 20000 + " LAST CONTROL [ref=e2]")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot)}
    assert (await js(runtime, "var b = await cua.getBrowser('managed'); var t;")).success
    for bind in ("b.getTab('current')", "b.newTab()", "b.getTab((await b.listTabs())[0].id)"):
        bound = await js(runtime, f"t = await {bind}")
        assert bound.success and " LAST CONTROL [ref=r_" in bound.content
        assert "more characters" not in bound.content
    missing = await js(runtime, "await b.getTab()")
    assert not missing.success and "InvalidArguments: getTab requires" in missing.error


@pytest.mark.asyncio
@pytest.mark.parametrize("method,operation", [("click", "click"), ("selectOption", "select_option")])
async def test_managed_action_navigation_preserves_only_the_reported_tab(runtime, method, operation):
    start = "https://example.test/compare"
    landing = start + "?model=pro"
    before = f"- 0: (current) [Compare]({start})\n- 1: [Help](https://example.test/help)"
    after = before.replace(start, landing)
    tabs = RecordingTool("managed_browser_tabs", before)
    snapshot = RecordingTool("managed_browser_snapshot", "control [ref=e1]")
    action = RecordingTool("managed_browser_" + operation, f"### Open tabs\n{after}\n### Page\n- Page URL: {landing}")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot, action)}
    code = r"""var b = await cua.getBrowser('managed');
        var t = await b.getTab((await b.listTabs())[0].id);
        var ref = (await t.getState()).dom.text.match(/r_\w+/)[0];"""
    assert (await js(runtime, code)).success
    args = "ref" if method == "click" else "ref, 'pro'"
    assert (await js(runtime, f"await t.{method}({args})")).success
    tabs.content = after
    old = await js(runtime, f"await t.{method}({args})")
    assert not old.success and "StaleObservation" in old.error and len(action.calls) == 1
    assert (await js(runtime, "await t.getState()")).success
    tabs.content = after.replace("https://example.test/help", "https://example.test/elsewhere")
    rejected = await js(runtime, "await t.getState()")
    assert not rejected.success and "StaleTab" in rejected.error


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_page", [False, True])
async def test_current_dialog_verifies_origin_or_recovers_by_id_without_snapshot(runtime, switch_page):
    page = "https://example.test/form"
    original = f"- 0: (current) [Form]({page})\n- 1: [Help](https://example.test/help)"
    other = original.replace(" (current)", "").replace("1:", "1: (current)")
    notice = "### Modal state\n- [confirm: Apply change?]: can be handled by browser_handle_dialog"
    class Tabs(RecordingTool):
        async def execute(self, action, index=None):
            if action == "select":
                assert index == 0
                self.content = "### Result\n" + original + "\n" + notice
            return await super().execute(action=action, index=index)

    tabs = Tabs("managed_browser_tabs", "### Result\n" + original + "\n" + notice)
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    click = RecordingTool("managed_browser_click", f"### Open tabs\n{original}\n### Page\n- Page URL: {page}\n{notice}")
    dialog = RecordingTool("managed_browser_handle_dialog")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot, click, dialog)}
    opened = await js(runtime, r"""
        var b = await cua.getBrowser('managed'); var t = await b.getTab('current');
        await t.click((await t.getState()).dom.text.match(/r_\w+/)[0]);
    """)
    assert opened.success and "Apply change?" in opened.content
    reads = len(snapshot.calls)
    snapshot.success = False  # Playwright blocks ordinary snapshots until the dialog closes.
    if switch_page:
        tabs.content = "### Result\n" + other
        refused = await js(runtime, "await t.handleDialog({accept:false})")
        assert not refused.success and "DialogTabRequired" in refused.error and not dialog.calls
        listed = await js(runtime, "var original = (await b.listTabs())[0].id")
        assert listed.success
        bound = await js(runtime, "t = await b.getTab(original)")
        assert bound.success and "Apply change?" in bound.content
        assert any(args == {"action": "select", "index": 0} for args, _ in tabs.calls)
    handled = await js(runtime, "await t.handleDialog({accept:false})")
    assert handled.success and dialog.calls == [({"accept": False}, None)]
    assert len(snapshot.calls) == reads


@pytest.mark.asyncio
async def test_dialog_receipt_cannot_answer_a_different_prompt_on_the_same_page(runtime):
    url = "https://example.test/form"
    action_header = f"### Page\n- Page URL: {url}\n### Modal state\n- [confirm: Save?]"
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    click = RecordingTool("managed_browser_click", action_header)
    tabs = RecordingTool("managed_browser_tabs", f"### Result\n- 0: (current) [Form]({url})\n"
                         "### Modal state\n- [confirm: Delete?]")
    dialog = RecordingTool("managed_browser_handle_dialog")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot, click, dialog)}
    result = await js(runtime, r"""
        var b = await cua.getBrowser('managed'); var t = await b.getTab('current');
        await t.click((await t.getState()).dom.text.match(/r_\w+/)[0]);
        await t.handleDialog({accept:true});
    """)
    assert not result.success and "DialogTabRequired" in result.error and not dialog.calls


@pytest.mark.asyncio
async def test_page_text_cannot_confirm_an_unreported_managed_navigation(runtime):
    url = "https://example.test/compare"
    landing = url + "?model=pro"
    tabs = RecordingTool("managed_browser_tabs", f"- 0: (current) [Compare]({url})")
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    click = RecordingTool("managed_browser_click", f"### Snapshot\n```yaml\n### Page\n- Page URL: {landing}\n```")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot, click)}
    assert (await js(runtime, r"""
        var b = await cua.getBrowser('managed'); var t = await b.getTab((await b.listTabs())[0].id);
        await t.click((await t.getState()).dom.text.match(/r_\w+/)[0]);
    """)).success
    tabs.content = tabs.content.replace(url, landing)
    refused = await js(runtime, "await t.getState()")
    assert not refused.success and "StaleTab" in refused.error


@pytest.mark.asyncio
async def test_managed_wait_select_and_click_options_use_existing_backend_contracts(runtime):
    snapshot = RecordingTool("managed_browser_snapshot", "combobox [ref=e1]\nbutton [ref=e2]")
    wait = RecordingTool("managed_browser_wait_for")
    select = RecordingTool("managed_browser_select_option")
    select.parameters = {"type": "object", "properties": {"ref": {}, "element": {}, "values": {}},
                         "required": ["ref", "values"], "additionalProperties": False}
    click = RecordingTool("managed_browser_click")
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, wait, select, click)}
    result = await js(runtime, r"""
        const browser = await cua.getBrowser('managed');
        const tab = await browser.getTab('current');
        const refs = async () => (await tab.getState()).dom.text.match(/r_\w+/g);
        await tab.waitForText('Ready');
        await tab.waitForText('Loading', {state: 'hidden'});
        await tab.selectOption((await refs())[0], 'a');
        await tab.selectOption((await refs())[0], ['a', 'b']);
        await tab.click((await refs())[1], {button: 'right', doubleClick: true, modifiers: ['Alt']});
    """)
    assert result.success, result.error
    assert [args for args, _ in wait.calls] == [{"text": "Ready"}, {"textGone": "Loading"}]
    assert [args for args, _ in select.calls] == [
        {"ref": "e1", "element": "observed element", "values": ["a"]},
        {"ref": "e1", "element": "observed element", "values": ["a", "b"]},
    ]
    assert [args for args, _ in click.calls] == [
        {"target": "e2", "element": "observed element", "button": "right",
         "doubleClick": True, "modifiers": ["Alt"]},
    ]
    for code, error in (
        ("await tab.waitForText('', {state: 'other'})", "InvalidWaitForText"),
        ("await tab.selectOption((await refs())[0], [])", "InvalidSelectOption"),
        ("await tab.click((await refs())[1], {button: 'unsupported'})", "InvalidClickOptions"),
        ("await tab.click((await refs())[1], {target: 'e1'})", "InvalidClickOptions"),
        ("await tab.waitForText('Ready', {timeoutMs: 1000})", "InvalidArguments"),
        ("await tab.scroll({dy: 400})", "InvalidArguments"),
        ("await tab.scroll({x: 2, y: 3})", "InvalidScroll"),
        ("await tab.scroll({deltaY: Infinity})", "InvalidScroll"),
        ("await tab.handleDialog({accept: true, text: 'value'})", "InvalidArguments"),
    ):
        rejected = await js(runtime, code)
        assert not rejected.success and error in rejected.error
    assert (len(wait.calls), len(select.calls), len(click.calls)) == (2, 2, 1)


@pytest.mark.asyncio
async def test_managed_dialog_targets_original_tab_and_back_invalidates_old_ref(runtime):
    pages = ["https://example.test/a/next", "https://example.test/b"]
    active = 0
    pending_dialog = False

    class Tabs(RecordingTool):
        async def execute(self, action, index=None):
            nonlocal active
            if action == "select":
                active = index
            rows = [f"- {i}:{' (current)' if active == i else ''} [Page {i}]({url})"
                    for i, url in enumerate(pages)]
            return ToolResult(success=True, content="### Result\n" + "\n".join(rows))

    class Click(RecordingTool):
        async def execute(self, **args):
            nonlocal pending_dialog
            await super().execute(**args)
            if len(self.calls) == 1:
                pending_dialog = True
                return ToolResult(success=True, content="### Modal state\n- [prompt: Input]\n### Snapshot\nignored")
            return ToolResult(success=True, content="")

    class Dialog(RecordingTool):
        async def execute(self, **args):
            nonlocal pending_dialog
            assert active == 0 and pending_dialog
            pending_dialog = False
            return await super().execute(**args)

    class Back(RecordingTool):
        async def execute(self):
            assert active == 0
            pages[0] = "https://example.test/a"
            return await super().execute()

    tabs = Tabs("managed_browser_tabs")
    snapshot = RecordingTool("managed_browser_snapshot", "button [ref=e1]")
    click, dialog = Click("managed_browser_click"), Dialog("managed_browser_handle_dialog")
    back = Back("managed_browser_navigate_back")
    runtime.agent.tools = {tool.name: tool for tool in (tabs, snapshot, click, dialog, back)}
    opened = await js(runtime, r"""
        const browser = await cua.getBrowser('managed');
        const rows = await browser.listTabs();
        const a = await browser.getTab(rows[0].id);
        const b = await browser.getTab(rows[1].id);
        const current = await browser.getTab('current');
        const ref = async () => (await a.getState()).dom.text.match(/r_\w+/)[0];
        await a.click(await ref());
    """)
    assert opened.success and opened.content.endswith("### Modal state\n- [prompt: Input]")
    active = 1  # Another page becomes active while A owns the dialog.
    rejected = await js(runtime, "await current.handleDialog({accept: false})")
    assert not rejected.success and "DialogTabRequired" in rejected.error and not dialog.calls
    rejected = await js(runtime, "await a.handleDialog({accept: 'no'})")
    assert not rejected.success and "InvalidDialog" in rejected.error and not dialog.calls
    assert (await js(runtime, "await a.handleDialog({accept: true, promptText: 'answer'})")).success
    assert [args for args, _ in dialog.calls] == [{"accept": True, "promptText": "answer"}]

    assert (await js(runtime, "const old = await ref(); await a.back()")).success
    rejected = await js(runtime, "await a.click(old)")
    assert not rejected.success and "StaleObservation" in rejected.error and len(click.calls) == 1
    assert pages == ["https://example.test/a", "https://example.test/b"] and len(back.calls) == 1
    assert (await js(runtime, "await a.click(await ref())")).success and len(click.calls) == 2


@pytest.mark.asyncio
async def test_managed_press_key_converts_valid_chords_and_rejects_invalid_before_dispatch(runtime):
    snapshot = RecordingTool("managed_browser_snapshot", "textbox [ref=e1]")
    press = RecordingTool("managed_browser_press_key")
    runtime.agent.tools = {tool.name: tool for tool in (snapshot, press)}
    assert (await js(runtime, "let b = await cua.getBrowser('managed'); let t = await b.getTab('current')")).success
    for call in ["'a', {modifiers:4}", "'F1'", "'fn+a'", "'ctrl'", "'no-such-key'", "null", ""]:
        result = await js(runtime, f"await t.pressKey({call})")
        assert not result.success and "InvalidArguments: Tab.pressKey" in result.error
    assert press.calls == []
    for key, chord in [("super+a", "Meta+a"), ("shift+Left", "Shift+ArrowLeft"),
                       ("ctrl+alt+shift+a", "Alt+Control+Shift+A"), ("plus", "Shift++"),
                       ("PgDn", "PageDown"), (" ", " ")]:
        result = await js(runtime, f"await t.pressKey({json.dumps(key)})")
        assert result.success, result.error
        assert press.calls[-1] == ({"key": chord}, None)
    assert len(press.calls) == 6


@pytest.mark.asyncio
async def test_infinite_js_times_out_and_restarts_worker(runtime):
    result = await js(runtime, "while (true) {}", timeout_ms=100)
    assert not result.success and "timed out" in result.error
    assert (await js(runtime, "nodeRepl.write(161)")).content == "161"


@pytest.mark.asyncio
async def test_cancelled_cell_does_not_leave_running_worker(runtime):
    task = asyncio.create_task(js(runtime, "await new Promise(r => setTimeout(r, 10000));"))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.realms["managed"].service is None
    assert (await js(runtime, "nodeRepl.write('new cell')")).content == "new cell"


@pytest.mark.asyncio
async def test_two_sessions_never_share_js_state(tmp_path, runtime):
    other = ReplRuntime(node=runtime.node, workspace=tmp_path)
    other.agent = SimpleNamespace(tools={}, messages=[])
    try:
        await js(runtime, "let secret = 7")
        assert (await js(other, "nodeRepl.write(typeof secret)")).content == "undefined"
    finally:
        await other.close()


def test_native_recovery_manual_uses_box_agent_reset_and_keeps_app_observations():
    from box_agent.plugins.repl.documentation import manual

    native = manual("cua", None)
    assert "`cua_repl_reset`" in native
    assert "`Session reset`" in native
    assert "Window: none" in native and "AXOpen" in native
    assert "`js_reset`" not in native
    assert "rediscover browser tabs" not in native


@pytest.mark.asyncio
async def test_native_ax_lookup_manual_example_can_run_twice_with_the_documented_app_binding(runtime):
    from box_agent.plugins.repl.documentation import manual

    guide = manual("cua", None)
    example = guide.split("```javascript\n", 1)[1].split("\n```", 1)[0]
    stub = """var hits = []; var app = {
        getAXState: async () => '  12 menu bar item "Window"',
        click: async id => hits.push(id)
    };"""
    assert (await js(runtime, stub)).success
    for _ in range(2):
        result = await js(runtime, example)
        assert result.success, result.error
    assert (await js(runtime, "nodeRepl.write(hits.join(','))")).content == "12,12"


def test_press_key_manual_matches_each_realm():
    from box_agent.plugins.repl.documentation import manual

    native = manual("cua", None)
    managed = manual("browser", "managed")
    assert "exactly one argument" in native and "Use `typeText` for exact text" in native
    assert "exactly one argument" in managed and "Use `tab.fill(ref, text)` for exact text" in managed
    assert "Use `typeText` for exact text" not in managed
    assert "pressKey(" not in manual("browser", "user")


def test_vendor_matches_its_source_and_patch_manifest():
    import hashlib
    root = Path(__file__).resolve().parents[1]
    vendor = root / "box_agent/_vendor/mac_cua"
    manifest = json.loads((vendor / "manifest.json").read_text())
    assert manifest["source_commit"] == "9ef2fa2604968f056326b2cbcc9ca8a3addc5c31"
    assert manifest["driver_commit"].startswith("80bcb204")
    for name, hashes in manifest["files"].items():
        assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == hashes["sha256"]
    for patch in manifest["patches"]:
        assert hashlib.sha256((root / "scripts/mac_cua_patches" / patch["file"]).read_bytes()).hexdigest() == patch["sha256"]
    assert not (vendor / "cua_adapter/cua_browser.py").exists()
