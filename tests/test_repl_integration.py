import asyncio
import base64
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest

from box_agent.plugins.repl import prepare_repl, RunBinding
from box_agent.plugins.repl.config import ReplConfig
from box_agent.plugins.repl.runtime import ReplRuntime
from box_agent.plugins.repl.tools import create_tools
from box_agent.plugins.builtins import SessionCleanup
from box_agent.tools.base import ToolResult
from box_agent.tools.local_tool_exposure import SKILL_TOOL_HINTS, DISCOVERABLE_LOCAL_NAMES


@pytest.mark.parametrize(("legacy", "trust", "expected"), [
    (True, False, False), (True, True, True), (False, True, False), ("false", True, False),
])
def test_retired_switch_cannot_grant_local_js_or_override_prior_opt_out(legacy, trust, expected):
    config = ReplConfig.model_validate({"enabled": legacy, "trusted_local_code": trust})
    assert config.trusted_local_code is expected
    assert "enabled" not in config.model_dump()
    assert not ReplConfig().trusted_local_code


@pytest.mark.asyncio
async def test_shared_session_registration_binding_and_cleanup(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    resources = SimpleNamespace(
        context=SimpleNamespace(config=SimpleNamespace(plugins={"repl": {
            "trusted_local_code": True, "node_path": node,
        }}), workspace=tmp_path), tools=[], cleanup=SessionCleanup(),
    )
    service = SimpleNamespace(config=SimpleNamespace(enabled=True))
    prepare_repl(resources, allow_full_access=False, cua_service=service)
    assert resources.tools == []
    prepare_repl(resources, allow_full_access=True, cua_service=service)
    assert {t.name for t in resources.tools} == {
        "cua_repl_js", "cua_repl_reset", "browser_repl_js", "browser_repl_reset",
    }
    agent = SimpleNamespace(tools={t.name: t for t in resources.tools}, messages=[])
    binding = RunBinding(SimpleNamespace(session=resources.context, options=SimpleNamespace(current_turn_text="当前页面")))
    binding.install_agent(agent)
    tool = agent.tools["browser_repl_js"]
    result = await tool.invoke({"mode": "managed", "code": "nodeRepl.write(23 * 7)"})
    assert result.success and result.content == "161"
    binding.close()
    assert tool.runtime.agent is None
    binding.install_agent(agent)
    result = await tool.invoke({"mode": "user", "code": "nodeRepl.write(typeof cua.getApp)"})
    assert result.content == "undefined"
    binding.close()
    await resources.cleanup.aclose()
    assert tool.runtime.closed
    assert all(realm.service is None for realm in tool.runtime.realms.values())


@pytest.mark.asyncio
async def test_standard_permission_session_chats_with_repl_configured(tmp_path):
    from box_agent.agent_session import AgentSession
    from box_agent.session_context import HostBindings, SessionOptions
    from tests.test_agent_session import session_config
    from tests.test_session_plugins import RecordingLLM

    config = session_config(tmp_path)
    config.plugins["repl"] = {"trusted_local_code": True}
    llm = RecordingLLM("你好")
    session = await AgentSession.open(
        config=config, host=HostBindings(llm_client=llm, base_tools=[], system_prompt="test"),
        options=SessionOptions(workspace_dir=tmp_path, allow_full_access=False),
    )
    try:
        session.agent.add_user_message("你好")
        events = [event async for event in session.run_events()]
        assert any(getattr(event, "final_content", None) == "你好" for event in events)
        assert "browser_repl_js" not in session.agent.tools
        assert "cua_repl_js" not in session.agent.tools
        assert "ensure_cua_ready" not in session.agent.tools
    finally:
        await session.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("cua_enabled", [True, False])
async def test_trusted_session_always_registers_browser_repl(tmp_path, monkeypatch, cua_enabled):
    monkeypatch.setattr("box_agent.plugins.repl.sys.platform", "darwin")
    config = SimpleNamespace(plugins={"repl": {"trusted_local_code": True, "node_path": sys.executable}})
    resources = SimpleNamespace(context=SimpleNamespace(config=config, workspace=tmp_path),
                                tools=[], cleanup=SessionCleanup())
    service = SimpleNamespace(config=SimpleNamespace(enabled=cua_enabled))
    try:
        prepare_repl(resources, allow_full_access=True, cua_service=service)
        expected = {"browser_repl_js", "browser_repl_reset"}
        if cua_enabled:
            expected.update({"cua_repl_js", "cua_repl_reset"})
        assert {tool.name for tool in resources.tools} == expected
        assert all(tool.runtime.cua_service is service for tool in resources.tools)
    finally:
        await resources.cleanup.aclose()


def test_skills_discover_distinct_tool_groups():
    assert SKILL_TOOL_HINTS["computer-use"] == {"cua_repl_js", "cua_repl_reset"}
    assert {"browser_repl_js", "browser_repl_reset"} <= SKILL_TOOL_HINTS["browser-use"]
    assert not SKILL_TOOL_HINTS["computer-use"] & SKILL_TOOL_HINTS["browser-use"]
    assert {tool.name for tool in create_tools(None)} <= DISCOVERABLE_LOCAL_NAMES


@pytest.mark.asyncio
async def test_repl_groups_are_independently_found_by_tool_search():
    from tests.test_local_tool_search import setup_search
    tools = create_tools(None)
    search, exposure, _ = setup_search(tools)
    assert exposure.prepare_tools(tools).offered_names == frozenset()
    result = await search.execute(tool_names=["browser_repl_js", "browser_repl_reset"])
    assert result.success
    assert exposure.prepare_tools(tools).offered_names == {"browser_repl_js", "browser_repl_reset"}
    result = await search.execute(tool_names=["cua_repl_js"])
    assert result.success
    assert "cua_repl_js" in exposure.prepare_tools(tools).offered_names


def test_repl_is_not_implicitly_delegated():
    from box_agent.tools.sub_agent_capabilities import _tool_denial_reason, DelegationConstraints
    for tool in create_tools(None):
        assert _tool_denial_reason(tool.name, tool, strategy="general_loop",
                                  constraints=DelegationConstraints()) == "gui_delegation_disabled"


@pytest.mark.asyncio
async def test_managed_agent_session_assembles_repl_and_disposes_workers(tmp_path):
    from box_agent.agent_session import AgentSession
    from box_agent.plugins.runtime import PluginRuntime
    from box_agent.session_context import HostBindings, SessionOptions
    from tests.test_agent_session import session_config
    from box_agent.schema import StreamEvent

    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    config = session_config(tmp_path)
    config.plugins["repl"] = {"trusted_local_code": True, "node_path": node}
    config.tools.allow_full_access = True
    outputs = []
    class Model:
        async def generate_stream(self, **kwargs):
            # Exercise the assembled run binding while the model is active.
            tool = session.agent.tools["browser_repl_js"]
            outputs.append(await tool.invoke({"mode": "managed", "code": "nodeRepl.write(23 * 7)"}))
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")

    plugins = PluginRuntime()
    session = await AgentSession.open(
        config=config, runtime=plugins,
        host=HostBindings(llm_client=Model(), system_prompt="test", base_tools=[]),
        options=SessionOptions(workspace_dir=tmp_path),
    )
    manager = session.agent.tools["browser_repl_js"].runtime
    try:
        session.agent.add_user_message("compute 23 * 7")
        _ = [event async for event in session.run_events()]
        assert outputs and outputs[0].success and outputs[0].content == "161"
        assert manager.agent is None
    finally:
        await session.aclose()
        await plugins.aclose()
    assert manager.closed and manager.realms["managed"].service is None


def test_supervisor_injection_cua_api_and_no_browser_fallback(tmp_path):
    from box_agent._vendor.mac_cua.cua_adapter import cua_adapter as native
    from box_agent._vendor.mac_cua.cua_adapter.cua_service import ReplService
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    calls = []

    class Adapter:
        def __init__(self, *args):
            pass
        def set_context(self, **kwargs):
            pass
        def control(self, method, handle, args):
            calls.append((method, args))
            if method == "getApp":
                return {"handle": "calculator", "state": "AX: result 161"}
            return {"state": "AX: result 161"}
        def counts(self, cell):
            return {}
        def close(self):
            calls.append("close")

    def make(capability):
        return ReplService(
            lambda timeout: object(), node, Path(native.__file__).with_name("cua_repl.mjs"),
            capability=capability, documentation="Native App manual",
            adapter_factory=Adapter, action_error=native.ActionError, result_data=native.data,
            evidence_contract=native.evidence_contract, mcp_client=native.McpClient,
            driver_timeout_seconds=10,
        )
    service = make("cua")
    try:
        result = service.execute("let app = await cua.getApp('Calculator');")
        assert not result["isError"] and calls == [("getApp", {"app": "Calculator"})]
        result = service.execute("nodeRepl.write(typeof cua.getBrowser)")
        assert result["content"] == [{"type": "text", "text": "undefined"}]
        for name in ("selectText", "performSecondaryAction"):
            result = service.execute(f"nodeRepl.write('{name}' in app)")
            assert result["content"] == [{"type": "text", "text": "false"}]
            result = service.execute(f"await app.{name}(0, 'text')")
            assert result["isError"] and "is not a function" in str(result)
        for arguments in ["'a', {modifiers:4}", "'NotaKey'", "42", ""]:
            invalid = service.execute(f"await app.pressKey({arguments})")
            assert invalid["isError"] and "InvalidArguments: App.pressKey" in str(invalid)
        assert calls == [("getApp", {"app": "Calculator"})]
        assert not service.execute("await app.pressKey('super+a')")["isError"]
        assert calls[-1] == ("pressKey", {"key": "cmd+a"})
        assert not service.execute("await app.getAXState()")['isError']
    finally:
        service.close()
    browser = make("browser")
    try:
        result = browser.execute("await cua.getBrowser('managed')")
        assert result["isError"] and "injection is required" in str(result)
    finally:
        browser.close()


@pytest.mark.asyncio
async def test_real_worker_emits_actual_browser_image(tmp_path):
    from PIL import Image
    from box_agent.tools.base import Tool
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    buffer = io.BytesIO()
    Image.new("RGB", (23, 7)).save(buffer, "PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    class BackendTool(Tool):
        name = "managed_browser_snapshot"
        description = "test backend"
        parameters = {"type": "object", "additionalProperties": True}
        async def execute(self, **kwargs):
            return ToolResult(success=True, content="button [ref=e1]", raw_output={
                "mcp_inline_images": [{"data": data, "mime_type": "image/png"}],
            })
    snapshot, screenshot = BackendTool(), BackendTool()
    screenshot.name = "managed_browser_take_screenshot"
    runtime = ReplRuntime(node=node, workspace=tmp_path)
    runtime.agent = SimpleNamespace(tools={t.name: t for t in (snapshot, screenshot)}, messages=[])
    try:
        result = await create_tools(runtime)[2].invoke({"mode": "managed", "code":
            "let b = await cua.getBrowser('managed'); let t = await b.getTab('current'); await t.getScreenshot();"})
        assert result.success, result.error
        assert result.image_content[0]["width"] == 23
        assert result.image_content[0]["height"] == 7
        assert result.image_content[0]["data"] == data
    finally:
        await runtime.close()
