"""Each logical call retains its own final arguments and execution facts."""

import json

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import ArtifactEvent, ToolCallResult
from box_agent.hooks import BaseHook
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


class CallsThenDone:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments
        self.requests = 0

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests += 1
        if self.requests == 1:
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id=f"call-{index}", type="function", function=FunctionCall(
                    name=self.name, arguments=args,
                )) for index, args in enumerate(self.arguments)
            ])
        else:
            yield StreamEvent(type="text", delta="Done.")
            yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_parallel_search_results_use_their_own_site_filters():
    class Search(Tool):
        name = "web_search"
        description = "Find sources."
        parallel_safe = True
        parameters = {"type": "object", "properties": {"query": {"type": "string"}}}

        async def execute(self, query):
            return ToolResult(success=True, content=json.dumps({"refs": [
                {"title": "Alpha documentation", "url": "https://alpha.example/docs"},
                {"title": "Beta release notes", "url": "https://beta.example/releases"},
            ]}))

    tool = Search()
    events = [event async for event in run_agent_loop(
        llm=CallsThenDone(tool.name, [
            {"query": "alpha documentation site:alpha.example"},
            {"query": "beta release notes site:beta.example"},
        ]),
        tools={tool.name: tool}, messages=[Message(role="user", content="Research both sources.")],
        max_steps=2,
    )]
    results = {event.tool_call_id: event for event in events if isinstance(event, ToolCallResult)}
    assert "https://alpha.example/docs" in results["call-0"].content
    assert "https://beta.example/releases" not in results["call-0"].content
    assert "https://beta.example/releases" in results["call-1"].content


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_hook_changed_browser_output_path_is_checked_before_execution(tmp_path, parallel):
    class Snapshot(Tool):
        name = "managed_browser_snapshot"
        description = "Capture a page snapshot."
        parallel_safe = parallel
        parameters = {"type": "object", "properties": {"filename": {"type": "string"}}}
        executions = 0

        async def execute(self, filename=None):
            self.executions += 1
            return ToolResult(success=True, content="page contents")

    class ChangePath(BaseHook):
        async def on_tool_start(self, **kwargs):
            return {"filename": "../outside.md"}

    tool = Snapshot()
    events = [event async for event in run_agent_loop(
        llm=CallsThenDone(tool.name, [{"filename": "original.md"}]),
        tools={tool.name: tool}, messages=[Message(role="user", content="Save the web page snapshot.")],
        hooks=[ChangePath()], workspace_dir=str(tmp_path), max_steps=2,
    )]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 1
    assert not results[0].success
    assert "BROWSER_SNAPSHOT_OUTPUT_PATH_INVALID" in results[0].error
    assert tool.executions == 0
    assert not (tmp_path / "original.md").exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("capture_kind", ["snapshot", "screenshot"])
async def test_engine_persists_browser_outputs_under_cwd_with_legacy_root_ignored(
    tmp_path, parallel, capture_kind,
):
    filename = "task/qa/page.md" if capture_kind == "snapshot" else "task/qa/page.png"
    expected = b"page contents" if capture_kind == "snapshot" else b"image"

    class Capture(Tool):
        name = "managed_browser_snapshot" if capture_kind == "snapshot" else "managed_browser_take_screenshot"
        description = "Capture the current page."
        parallel_safe = parallel
        parameters = {"type": "object", "properties": {"filename": {"type": "string"}}}

        async def execute(self, filename=None):
            assert filename is None
            return ToolResult(
                success=True, content="page contents",
                raw_output={"mcp_inline_images": [{"data": "aW1hZ2U=", "mime_type": "image/png"}]}
                if capture_kind == "screenshot" else None,
            )

    tool = Capture()
    events = [event async for event in run_agent_loop(
        llm=CallsThenDone(tool.name, [{"filename": filename}]),
        tools={tool.name: tool}, messages=[Message(role="user", content="Save a browser page capture.")],
        workspace_dir=str(tmp_path), artifact_root_dir=str(tmp_path / "legacy-output"),
        max_steps=2,
    )]

    assert (tmp_path / filename).read_bytes() == expected
    assert [event.rel_path for event in events if isinstance(event, ArtifactEvent)] == [filename]
    assert not (tmp_path / "legacy-output").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "result"])
async def test_tool_hook_durability_failure_stops_the_call_chain(phase):
    from box_agent.session_log import SessionLogDurabilityError

    class Effect(Tool):
        name = "effect"
        description = "Record an effect."
        parameters = {"type": "object", "properties": {}}
        effects = 0

        async def execute(self):
            self.effects += 1
            return ToolResult(success=True, content="effect done")

    class BrokenLogHook(BaseHook):
        async def on_tool_start(self, **kwargs):
            if phase == "start":
                raise SessionLogDurabilityError("fixture disk full")

        async def on_tool_result(self, **kwargs):
            if phase == "result":
                raise SessionLogDurabilityError("fixture disk full")

    tool, model, events = Effect(), CallsThenDone("effect", [{}]), []
    with pytest.raises(SessionLogDurabilityError, match="fixture disk full"):
        async for event in run_agent_loop(
            llm=model, tools={tool.name: tool}, messages=[Message(role="user", content="do it")],
            hooks=[BrokenLogHook()], max_steps=2,
        ):
            events.append(event)
    assert tool.effects == (1 if phase == "result" else 0)
    assert model.requests == 1
    assert not any(isinstance(event, ToolCallResult) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [False, True])
async def test_permission_result_is_committed_once_without_inline_image_bytes(tmp_path, parallel):
    from box_agent.agent import Agent
    from box_agent.session_log import SessionLog

    image_data = "c2Vuc2l0aXZlLWltYWdlLWJ5dGVz"

    class ImageResult(Tool):
        name = "image_result"
        description = "A gated request-only image with durable text."
        parameters = {"type": "object", "properties": {}}
        transient_followup_allowed = True
        parallel_safe = parallel
        attempts = 0

        async def execute(self):
            self.attempts += 1
            if self.attempts == 1:
                return ToolResult(success=False, permission_request={"scope": "fixture"})
            return ToolResult(
                success=True, content="full visible text", model_context="short model receipt",
                raw_output={"reference": "image-1", "mcp_inline_images": [
                    {"mime_type": "image/png", "data": image_data},
                ]},
                transient_followup_content=[{
                    "type": "input_image", "media_type": "image/png", "data": image_data,
                    "width": 1, "height": 1, "source_bytes": 21, "sha256": "fixture",
                }],
            )

    class Approver:
        async def negotiate(self, request):
            return True

    class Model(CallsThenDone):
        capabilities = {"image_input": True}
        saw_image = False

        async def generate_stream(self, messages, tools=None, **kwargs):
            if self.requests == 1:
                self.saw_image = image_data in str([message.content for message in messages])
            async for event in super().generate_stream(messages, tools=tools, **kwargs):
                yield event

    tool, model = ImageResult(), Model("image_result", [{}])
    log = SessionLog.create(tmp_path / "sessions", session_id="image-once", cwd=tmp_path)
    try:
        agent = Agent(llm_client=model, system_prompt="system", tools=[tool],
                      workspace_dir=str(tmp_path), session_log=log, deferred_mcp_loading_enabled=False)
        agent.add_user_message("Inspect the image.")
        from dataclasses import replace
        options = replace(agent.default_run_options(), permission_negotiator=Approver())
        events = [event async for event in agent.run_events(options=options)]
        assert tool.attempts == 2 and model.saw_image
        results = [event for event in events if isinstance(event, ToolCallResult)]
        assert len(results) == 1
        calls = [event for event in log.events if event["type"] == "tool/call"]
        stored = [event for event in log.events if event["type"] == "tool/result"]
        assert len(calls) == len(stored) == 1
        assert stored[0]["data"]["result"]["policyDecision"]["retry_count"] == 1
        assert stored[0]["data"]["result"]["rawOutput"]["reference"] == "image-1"
        assert "full visible text" in log.path.read_text()
        assert "short model receipt" in log.path.read_text()
        assert image_data not in log.path.read_text()
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_system", [False, True])
@pytest.mark.parametrize("parallel", [False, True])
async def test_direct_loop_persists_each_result_once_with_or_without_system(tmp_path, with_system, parallel):
    from box_agent.session_log import SessionLog

    class Echo(Tool):
        name = "echo"
        description = "Return the fixture value."
        parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
        parallel_safe = parallel

        async def execute(self, value):
            return ToolResult(success=True, content=value, raw_output={"value": value})

    class TwoSteps:
        step = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.step += 1
            if self.step <= 2:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id=f"echo-{self.step}", type="function", function=FunctionCall(
                        name="echo", arguments={"value": f"value-{self.step}"},
                    )),
                ])
            else:
                yield StreamEvent(type="text", delta="Done.")
                yield StreamEvent(type="finish", finish_reason="stop")

    messages = [Message(role="user", content="Echo two values.")]
    if with_system:
        messages.insert(0, Message(role="system", content="Fixture system."))
    log = SessionLog.create(tmp_path / "sessions", session_id="direct-loop", cwd=tmp_path)
    try:
        events = [event async for event in run_agent_loop(
            llm=TwoSteps(), tools={"echo": Echo()}, messages=messages,
            session_log=log, session_turn=1, max_steps=4,
        )]
        results = [event for event in events if isinstance(event, ToolCallResult)]
        calls = [event for event in log.events if event["type"] == "tool/call"]
        stored = [event for event in log.events if event["type"] == "tool/result"]
        assert len(results) == len(calls) == len(stored) == 2
        assert [event["data"]["message"]["tool_call_id"] for event in stored] == ["echo-1", "echo-2"]
        for index, event in enumerate(stored, 1):
            assert event["data"]["result"]["success"] is True
            assert event["data"]["result"]["rawOutput"] == {"value": f"value-{index}"}
        assert log.replay().messages == (messages[1:] if with_system else messages)
    finally:
        log.close()
