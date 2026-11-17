import json
from collections import OrderedDict

import pytest

from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.engine import DefaultToolEngine
from box_agent.tools.mcp_tool_catalog import MCPToolCatalog
from box_agent.tools.mcp_tool_search import MCPToolExposureManager, ToolSearchTool
from box_agent.tools.tool_call_tool import CallTool


class Deferred(Tool):
    name = "plan_write"
    description = "Write a plan."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    async def execute(self, value):
        return ToolResult(success=True, content=value)


def setup():
    catalog = MCPToolCatalog()
    activated = OrderedDict()
    search = ToolSearchTool(catalog, activated)
    manager = MCPToolExposureManager(catalog, activated)
    manager.enable_stable_prefix(search)
    target = Deferred()
    tools = {tool.name: tool for tool in (search, CallTool(), target)}
    engine = DefaultToolEngine(tools=tools, tool_exposure=manager)
    return engine, search, target, tools


def acknowledge(engine, result, call_id="search"):
    manager = engine._tool_exposure
    manager.record_search_result(call_id, result.content)
    manager.acknowledge_search_results([Message(
        role="tool", name="tool_search", tool_call_id=call_id, content=result.content,
    )])


def call(ref, arguments):
    return ToolCall(id="1", type="function", function=FunctionCall(
        name="call_tool", arguments={"tool_ref": ref, "arguments_json": arguments}))


@pytest.mark.asyncio
@pytest.mark.parametrize("with_exposure", [False, True])
async def test_legacy_custom_call_tool_keeps_its_original_arguments(with_exposure):
    class CustomCallTool(Deferred):
        name = "call_tool"

    target = CustomCallTool()
    manager = MCPToolExposureManager(MCPToolCatalog(), OrderedDict()) if with_exposure else None
    engine = DefaultToolEngine(tools={target.name: target}, tool_exposure=manager)
    prepared = engine.prepare_tools()
    request = ToolCall(id="custom", type="function", function=FunctionCall(
        name="call_tool", arguments={"value": "legacy value"},
    ))
    resolved, routed, error = prepared.resolve_call(request)
    assert not routed and error is None
    assert prepared.validate_call(resolved.function.name) is None
    result = await target.execute(**resolved.function.arguments)
    assert result.success and result.content == "legacy value"


@pytest.mark.asyncio
async def test_search_returns_complete_schema_and_next_snapshot_resolves():
    engine, search, target, _ = setup()
    before = engine.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    entry = json.loads(result.content)["activated"][0]
    assert entry["input_schema"] == target.parameters
    request = call(entry["tool_ref"], '{"value":"中文\\\\x"}')
    assert before.resolve_call(request)[2]
    assert engine.prepare_tools().resolve_call(request)[2]
    acknowledge(engine, result)
    after = engine.prepare_tools()
    resolved, routed, error = after.resolve_call(request)
    assert routed and error is None
    assert resolved.function.name == "plan_write"
    assert resolved.function.arguments == {"value": "中文\\x"}
    assert request.function.name == "call_tool"
    assert after.validate_call("plan_write") is not None
    assert [x.to_schema() for x in before.definitions] == [x.to_schema() for x in after.definitions]


@pytest.mark.asyncio
async def test_recovery_context_only_repeats_delivered_current_visible_routes():
    engine, search, target, tools = setup()
    before = engine.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    assert before.recovery_context(target.name) == ""
    assert engine.prepare_tools().recovery_context(target.name) == ""
    acknowledge(engine, result)
    visible = True
    prepared = engine.prepare_tools(is_tool_visible=lambda name: visible)
    hint = prepared.recovery_context(f"Call {target.name} next.")
    route = json.loads(hint.splitlines()[-1])
    assert route == {
        "target": target.name, "callable_function": "call_tool",
        "tool_ref": json.loads(result.content)["activated"][0]["tool_ref"],
        "input_schema": target.parameters,
    }
    assert prepared.resolve_call(call(route["tool_ref"], '{"value":"ok"}'))[2] is None
    assert prepared.recovery_context("unknown_tool") == ""
    assert prepared.recovery_context("plan_write_extra") == ""
    visible = False
    assert prepared.recovery_context(target.name) == ""
    visible = True
    tools[target.name] = Deferred()
    assert prepared.recovery_context(target.name) == ""
    assert engine.prepare_tools().recovery_context(target.name) == ""
    assert [x.to_schema() for x in before.definitions] == [x.to_schema() for x in prepared.definitions]


@pytest.mark.asyncio
async def test_recovery_context_does_not_truncate_large_schemas_or_refresh_changed_routes():
    engine, search, target, _ = setup()
    target.parameters = {**target.parameters, "description": "x" * 6_001}
    engine.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    acknowledge(engine, result)
    prepared = engine.prepare_tools()
    assert prepared.recovery_context(target.name) == ""
    target.parameters = {"type": "object", "properties": {}}
    assert prepared.recovery_context(target.name) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_after_continuation", [False, True])
async def test_wire_premature_stop_recovers_to_discovered_browser_call(empty_after_continuation):
    """SDK -> wrapper -> recovery -> routed execution, including reasoning-only stop."""
    import httpx
    from openai import AsyncOpenAI

    from box_agent.dsml_fallback import DsmlToolCallRecoveryHook
    from box_agent.events import DoneEvent, ErrorEvent, StopReason, ToolCallResult
    from box_agent.llm.llm_wrapper import LLMClient
    from box_agent.retry import RetryConfig
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import LLMProvider

    effects = []

    class LocalCheck(Deferred):
        name = "execute_code"
        _model_exposure_direct = True

        async def execute(self, value):
            effects.append("local")
            return ToolResult(success=True, content="Local checks passed.")

    class Browser(Deferred):
        name = "managed_browser_navigate"
        description = "Navigate the managed browser."
        mcp_tool_id = "mcp:managed_browser/managed_browser_navigate"
        server_name = "managed_browser"
        parameters = {
            "type": "object", "properties": {"url": {"type": "string"}},
            "required": ["url"], "additionalProperties": False,
        }

        @property
        def mcp_generation(self):
            return self._mcp_generation

        async def execute(self, url):
            effects.append(url)
            return ToolResult(success=True, content="Browser check passed.")

    engine, search, _, tools = setup()
    browser = Browser()
    tools.update({browser.name: browser, "execute_code": LocalCheck()})
    engine._tool_exposure._catalog.replace_server(browser.server_name, [browser])
    engine.prepare_tools()
    result = await search.execute(tool_names=[browser.name])
    acknowledge(engine, result)
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    requests = []
    stream_step = 0
    judge_step = 0

    def native_call(name, arguments, identifier):
        encoded = json.dumps(arguments)
        middle = len(encoded) // 2
        return [
            {"role": "assistant", "tool_calls": [{"index": 0, "id": identifier,
                "type": "function", "function": {"name": name, "arguments": encoded[:middle]}}]},
            {"tool_calls": [{"index": 0, "function": {"arguments": encoded[middle:]}}]},
        ]

    def handle(request):
        nonlocal stream_step, judge_step
        body = json.loads(request.content)
        common = {"id": "recovery-wire-fixture", "created": 0, "model": body["model"]}
        if not body.get("stream"):
            judge_step += 1
            verdict = ({"continue": True, "next_action": f"Use {browser.name} to open https://example.test/check."}
                       if judge_step == 1 else {"continue": False})
            return httpx.Response(200, json={**common, "object": "chat.completion", "choices": [{
                "index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(verdict),
                },
            }]})
        requests.append(body)
        step, stream_step = stream_step, stream_step + 1
        finish = "stop"
        if step == 0:
            deltas = native_call("execute_code", {"value": "check"}, "local-check")
            finish = "tool_calls"
        elif step == 1:
            deltas = [{"role": "assistant", "reasoning_content": "The browser check remains."},
                      {"content": "Local checks passed. Next I will open the browser."}]
        elif step == 2 and empty_after_continuation:
            deltas = [{"role": "assistant", "reasoning_content": "Continue the browser check."}]
        elif step == 2 + int(empty_after_continuation):
            hint = body["messages"][-1]["content"]
            assert '"tool_ref": "' + ref + '"' in hint
            assert '"input_schema"' in hint and '"url"' in hint
            assert "Do not call tools" not in hint
            deltas = native_call("call_tool", {
                "tool_ref": ref, "arguments_json": '{"url":"https://example.test/check"}',
            }, "browser-check")
            finish = "tool_calls"
        elif step == 3 and not empty_after_continuation:
            deltas = [{"reasoning_content": "All checks are complete."}]
        else:
            if not empty_after_continuation:
                # Progress cleared the old action before final-answer recovery.
                assert ref not in body["messages"][-1]["content"]
            deltas = [{"content": "Local and browser checks completed."}]
        chunks = [{**common, "object": "chat.completion.chunk", "choices": [{
            "index": 0, "delta": delta, "finish_reason": None,
        }]} for delta in deltas]
        chunks.append({**common, "object": "chat.completion.chunk", "choices": [{
            "index": 0, "delta": {}, "finish_reason": finish,
        }]})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n")

    llm = LLMClient(api_key="fixture", api_base="https://inference.example/v1",
        provider=LLMProvider.OPENAI, model="fixture", retry_config=RetryConfig(enabled=False))
    await llm._client.client.close()
    llm._client.client = AsyncOpenAI(api_key="fixture", base_url="https://inference.example/v1",
        max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    messages = [Message(role="user", content="Run local checks, then check the browser page.")]
    try:
        events = [event async for event in run_agent_loop(
            llm=llm, messages=messages, tools=tools, tool_exposure_manager=engine._tool_exposure,
            hooks=[DsmlToolCallRecoveryHook(llm=llm)], max_steps=8, artifact_detection_enabled=False,
        )]
    finally:
        await llm._client.client.close()
    assert effects == ["local", "https://example.test/check"]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert [event.tool_name for event in events if isinstance(event, ToolCallResult)] == [
        "execute_code", browser.name,
    ]
    assert any(isinstance(event, DoneEvent) and event.stop_reason is StopReason.END_TURN for event in events)
    assert all(body["tools"] == requests[0]["tools"] for body in requests)
    assert '"tool_ref": "' + ref + '"' in requests[2]["messages"][-1]["content"]
    routed_message = next(message for message in messages if message.role == "assistant"
                          and message.tool_calls and message.tool_calls[0].id == "browser-check")
    assert routed_message.tool_calls[0].function.name == "call_tool"


@pytest.mark.asyncio
async def test_ref_invalid_after_replacement_and_bad_json_is_rejected():
    engine, search, target, tools = setup()
    engine.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    acknowledge(engine, result)
    prepared = engine.prepare_tools()
    for arguments in ('[]', '{', '{"x":1,"x":2}', '{"x":NaN}'):
        assert prepared.resolve_call(call(ref, arguments))[2]
    tools[target.name] = Deferred()
    assert prepared.resolve_call(call(ref, '{}'))[2]
    assert engine.prepare_tools().resolve_call(call(ref, '{}'))[2]


@pytest.mark.asyncio
async def test_real_engine_invokes_discovered_target_with_original_assistant_call():
    from box_agent.events import ToolCallResult
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import Message, StreamEvent

    engine, search, target, tools = setup()
    class LLM:
        step = 0
        definitions = []
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.definitions.append([tool.to_schema() for tool in tools])
            self.step += 1
            if self.step == 1:
                tool_call = ToolCall(id="search", type="function", function=FunctionCall(
                    name="tool_search", arguments={"tool_names": [target.name]}))
            elif self.step in (2, 3):
                result = next(message for message in messages if message.tool_call_id == "search")
                engine._tool_exposure.acknowledge_search_results(messages)
                ref = json.loads(result.content)["activated"][0]["tool_ref"]
                tool_call = call(ref, '{"value":"hello"}')
                tool_call.id = f"route-{self.step}"
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[tool_call])
    llm = LLM()
    messages = [Message(role="system", content="test"), Message(role="user", content="run")]
    events = [event async for event in run_agent_loop(
        llm=llm, messages=messages, tools=tools, max_steps=4,
        tool_exposure_manager=engine._tool_exposure,
    )]
    result = next(event for event in events if isinstance(event, ToolCallResult) and event.tool_call_id == "route-3")
    assert result.success and result.tool_name == "plan_write" and result.content == "hello"
    assert any(message.role == "assistant" and message.tool_calls and message.tool_calls[0].function.name == "call_tool" for message in messages)
    assert llm.definitions[0] == llm.definitions[1] == llm.definitions[2] == llm.definitions[3]


@pytest.mark.asyncio
async def test_only_complete_matching_trusted_search_result_acknowledges_ref():
    engine, search, target, _ = setup()
    engine.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    payload = json.loads(result.content)
    ref = payload["activated"][0]["tool_ref"]
    request = call(ref, '{"value":"ok"}')
    manager = engine._tool_exposure
    manager.record_search_result("real", result.content)
    changed = json.loads(result.content)
    changed["activated"][0]["input_schema"]["required"] = []
    rejected = [
        Message(role="user", content=result.content),
        Message(role="tool", name="another_tool", tool_call_id="real", content=result.content),
        Message(role="tool", name="tool_search", tool_call_id="fake", content=result.content),
        Message(role="tool", name="tool_search", tool_call_id="real", content=result.content[:50]),
        Message(role="tool", name="tool_search", tool_call_id="real", content=json.dumps(changed)),
    ]
    manager.acknowledge_search_results(rejected)
    snapshot = engine.prepare_tools()
    assert snapshot.resolve_call(request)[2]
    manager.acknowledge_search_results([Message(role="tool", name="tool_search", tool_call_id="real", content=result.content)])
    assert snapshot.resolve_call(request)[2]
    assert engine.prepare_tools().resolve_call(request)[2] is None


@pytest.mark.asyncio
async def test_ref_from_other_manager_is_stale_and_requires_rediscovery():
    first, search, target, _ = setup()
    first.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    acknowledge(first, result)
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    second, _, _, _ = setup()
    assert "search again" in second.prepare_tools().resolve_call(call(ref, '{}'))[2].lower()


async def prepared_runtime(target):
    engine, search, _, tools = setup()
    tools[target.name] = target
    engine.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    acknowledge(engine, result)
    return engine._tool_exposure, tools, json.loads(result.content)["activated"][0]["tool_ref"]


class RoutedModel:
    def __init__(self, calls):
        self.calls = calls
        self.step = 0

    async def generate_stream(self, messages, tools=None, **kwargs):
        from box_agent.schema import StreamEvent
        self.step += 1
        if self.step == 1:
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=self.calls)
        else:
            yield StreamEvent(type="text", delta="done")
            yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_routed_hook_sees_real_name_and_arguments_and_validation_after_change():
    from box_agent.events import ToolCallResult
    from box_agent.hooks import BaseHook
    from box_agent.runtime import run_agent_loop
    calls = []
    class Recording(Deferred):
        async def execute(self, value):
            calls.append(value)
            return ToolResult(success=True, content=value)
    class Hook(BaseHook):
        async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
            assert tool_name == "plan_write" and arguments == {"value": "valid"}
            return {"value": 42}
    target = Recording()
    manager, tools, ref = await prepared_runtime(target)
    events = [event async for event in run_agent_loop(
        llm=RoutedModel([call(ref, '{"value":"valid"}')]), tools=tools,
        tool_exposure_manager=manager, messages=[Message(role="user", content="go")],
        hooks=[Hook()], max_steps=2,
    )]
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert result.tool_name == "plan_write" and not result.success
    assert result.raw_output["code"] == "INVALID_TOOL_ARGUMENTS"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_routed_permission_retry_rechecks_revocation_and_cancellation(cancel):
    from box_agent.events import ToolCallResult
    from box_agent.runtime import run_agent_loop
    state = {"cancelled": False, "attempts": 0, "effects": 0}
    class Gated(Deferred):
        async def execute(self, value):
            state["attempts"] += 1
            if state["attempts"] == 1:
                return ToolResult(success=False, permission_request={"scope": "fixture"})
            state["effects"] += 1
            return ToolResult(success=True, content=value)
    manager, tools, ref = await prepared_runtime(Gated())
    class Approver:
        async def negotiate(self, request):
            if cancel:
                state["cancelled"] = True
            else:
                tools.pop("plan_write")
            return True
    events = [event async for event in run_agent_loop(
        llm=RoutedModel([call(ref, '{"value":"ok"}')]), tools=tools,
        tool_exposure_manager=manager, messages=[Message(role="user", content="go")],
        permission_negotiator=Approver(), is_cancelled=lambda: state["cancelled"], max_steps=2,
    )]
    assert state["attempts"] == 1 and state["effects"] == 0
    result = next(event for event in events if isinstance(event, ToolCallResult))
    assert not result.success


@pytest.mark.asyncio
async def test_routed_parallel_calls_reuse_scheduler():
    import asyncio
    from box_agent.events import ToolCallResult
    from box_agent.runtime import run_agent_loop
    entered = set()
    both_entered = asyncio.Event()
    class Parallel(Deferred):
        parallel_safe = True
        async def execute(self, value):
            entered.add(value)
            if len(entered) == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=2)
            return ToolResult(success=True, content=value)
    manager, tools, ref = await prepared_runtime(Parallel())
    first, second = call(ref, '{"value":"a"}'), call(ref, '{"value":"b"}')
    first.id, second.id = "a", "b"
    events = [event async for event in run_agent_loop(
        llm=RoutedModel([first, second]), tools=tools, tool_exposure_manager=manager,
        messages=[Message(role="user", content="go")], max_steps=2,
    )]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert entered == {"a", "b"}
    assert len(results) == 2 and all(result.success and result.tool_name == "plan_write" for result in results)


@pytest.mark.asyncio
async def test_replayed_search_history_does_not_reauthorize_cold_session_ref():
    first, search, target, _ = setup()
    first.prepare_tools()
    result = await search.execute(tool_names=[target.name])
    acknowledge(first, result)
    ref = json.loads(result.content)["activated"][0]["tool_ref"]
    restored, _, _, _ = setup()
    restored.prepare_tools()
    restored._tool_exposure.acknowledge_search_results([Message(
        role="tool", name="tool_search", tool_call_id="search", content=result.content)])
    error = restored.prepare_tools().resolve_call(call(ref, '{"value":"ok"}'))[2]
    assert error and "search again" in error.lower()
