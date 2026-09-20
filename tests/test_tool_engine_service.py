"""The model and kernel use the same run-scoped tool preparation service."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest

from box_agent.composition import compose_default_kernel_services
from box_agent.core import run_agent_loop
from box_agent.events import ArtifactEvent, DoneEvent, ToolCallResult
from box_agent.kernel.loop import AgentLoopKernel
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


class MutableTool(Tool):
    name = "write_record"
    description = "Record a value."

    def __init__(self):
        self.schema = {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }
        self.values = []

    @property
    def parameters(self):
        return self.schema

    async def execute(self, value):
        self.values.append(value)
        return ToolResult(success=True, content=value)


def messages():
    return [Message(role="system", content="sys"), Message(role="user", content="record it")]


class MutatingLLM:
    def __init__(self, tool):
        self.tool = tool
        self.requests = 0
        self.request_schema = None

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests += 1
        if self.requests == 1:
            self.request_schema = deepcopy(tools[0].to_schema())
            self.tool.schema["properties"]["value"]["type"] = "integer"
            # The request view itself must remain unchanged while execution
            # validates whether its real target still implements that contract.
            assert tools[0].to_schema() == self.request_schema
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id="record-1", type="function", function=FunctionCall(
                    name="write_record", arguments={"value": "original"},
                )),
            ])
        else:
            yield StreamEvent(type="text", delta="The tool definition changed.")
            yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_model_request_keeps_its_schema_and_rejects_changed_definition():
    tool = MutableTool()
    llm = MutatingLLM(tool)
    events = [event async for event in run_agent_loop(
        llm=llm, tools={tool.name: tool}, messages=messages(), max_steps=3,
    )]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(results) == 1
    assert results[0].success is False
    assert "definition" in (results[0].error or "").lower()
    assert tool.values == []


@pytest.mark.asyncio
async def test_explicit_engine_is_used_and_borrowed_registry_remains_live():
    from box_agent.tools.engine.engine import DefaultToolEngine

    tool = MutableTool()
    registry = {tool.name: tool}

    class QuietLLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            assert [item.name for item in tools] == ["write_record", "other"]
            yield StreamEvent(type="text", delta="Ready.")
            yield StreamEvent(type="finish", finish_reason="stop")

    class RecordingEngine(DefaultToolEngine):
        calls = 0

        def prepare_tools(self, **kwargs):
            self.calls += 1
            return super().prepare_tools(**kwargs)

    engine = RecordingEngine(tools=registry)
    services = replace(compose_default_kernel_services({"llm": QuietLLM(), "tools": registry}),
                       tool_engine=engine)
    other = MutableTool()
    other.name = "other"
    registry[other.name] = other
    kernel = AgentLoopKernel(_services=services, messages=messages(), max_steps=1)
    _events = [event async for event in kernel.run()]
    assert engine.calls == 1
    assert registry[tool.name] is tool
    assert registry[other.name] is other


def test_default_composition_builds_separate_run_engines_over_same_tools():
    tool = MutableTool()
    registry = {tool.name: tool}
    arguments = {"llm": object(), "tools": registry}
    first = compose_default_kernel_services(arguments)
    second = compose_default_kernel_services(arguments)
    assert first.tool_engine is not second.tool_engine
    assert first.tool_engine.prepare_tools().targets[tool.name] is tool
    assert second.tool_engine.prepare_tools().targets[tool.name] is tool


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True])
@pytest.mark.parametrize("legacy_entrypoint", ["loop", "engine"])
async def test_engine_discovers_cwd_files_and_warns_for_ignored_legacy_root(
    tmp_path, caplog, parallel_safe, legacy_entrypoint,
):
    from box_agent.tools.engine.engine import DefaultToolEngine

    report = tmp_path / "task" / "report.txt"
    legacy_root = tmp_path / "legacy-output"
    (tmp_path / "existing.txt").write_text("unrelated input", encoding="utf-8")

    class WriteArtifact(MutableTool):
        async def execute(self, value):
            report.parent.mkdir()
            report.write_text(value, encoding="utf-8")
            return ToolResult(success=True, content=f"written {report}")

    class WriteThenDone:
        requests = 0

        async def generate_stream(self, **kwargs):
            self.requests += 1
            if self.requests == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id="write-1", type="function", function=FunctionCall(
                        name="write_record", arguments={"value": "report"},
                    )),
                ])
            else:
                yield StreamEvent(type="text", delta="Done.")
                yield StreamEvent(type="finish", finish_reason="stop")

    class LegacyContextEngine(DefaultToolEngine):
        def configure_run(self, context, options):
            if legacy_entrypoint == "engine":
                context = replace(context, artifact_root_dir=str(legacy_root))
            super().configure_run(context, options)

    tool = WriteArtifact()
    tool.parallel_safe = parallel_safe
    registry = {tool.name: tool}
    services = replace(
        compose_default_kernel_services({"llm": WriteThenDone(), "tools": registry}),
        tool_engine=LegacyContextEngine(tools=registry),
    )
    kernel = AgentLoopKernel(
        _services=services, messages=messages(), max_steps=2,
        workspace_dir=str(tmp_path),
        artifact_root_dir=str(legacy_root) if legacy_entrypoint == "loop" else None,
    )
    events = [event async for event in kernel.run()]

    assert [event.rel_path for event in events if isinstance(event, ArtifactEvent)] == [
        "task/report.txt",
    ]
    assert any(isinstance(event, DoneEvent) for event in events)
    assert report.read_text(encoding="utf-8") == "report"
    assert not legacy_root.exists()
    assert "artifact_root_dir is deprecated and ignored" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel_safe", [False, True], ids=["serial", "parallel"])
@pytest.mark.parametrize("existing", [False, True], ids=["new-file", "revision"])
async def test_waiting_task_does_not_claim_another_tasks_published_file(
    tmp_path, parallel_safe, existing,
):
    from box_agent.artifact_publication import write_metadata
    from box_agent.task_context import TaskContext
    from box_agent.task_registry import register_artifact_revision

    waiting = asyncio.Event()
    published = asyncio.Event()
    report = tmp_path / "shared-report.html"
    if existing:
        report.write_text("old revision")
        write_metadata(report, {"type": "artifact"})

    class WaitTool(MutableTool):
        name = "wait_for_choice"

        async def execute(self, value):
            waiting.set()
            await published.wait()
            return ToolResult(success=True, content="Still waiting for a choice.")

    class ProducerTool(MutableTool):
        name = "produce_report"

        async def execute(self, value):
            await waiting.wait()
            report.write_text("new report from task B")
            write_metadata(report, {"type": "artifact"})
            published.set()
            return ToolResult(success=True, content="Published report", raw_output={
                "type": "artifact", "abs_path": str(report),
            })

    class OneCall:
        def __init__(self, name):
            self.name, self.called = name, False

        async def generate_stream(self, **kwargs):
            if not self.called:
                self.called = True
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id=self.name, type="function", function=FunctionCall(
                        name=self.name, arguments={"value": "run"},
                    )),
                ])
            else:
                yield StreamEvent(type="finish", finish_reason="stop")

    async def run(tool, task_id):
        artifacts = []
        async for event in run_agent_loop(
            llm=OneCall(tool.name), tools={tool.name: tool}, messages=messages(),
            workspace_dir=str(tmp_path), max_steps=2,
        ):
            if isinstance(event, ArtifactEvent):
                register_artifact_revision(tmp_path, TaskContext(
                    session_id=task_id, task_id=task_id, turn_id=task_id,
                ), event)
                artifacts.append(event)
        return artifacts

    waiter = WaitTool()
    waiter.parallel_safe = parallel_safe
    a, b = await asyncio.wait_for(asyncio.gather(
        run(waiter, "task-A"), run(ProducerTool(), "task-B"),
    ), timeout=5)
    assert a == []
    assert [(event.rel_path, event.placement) for event in b] == [
        (report.name, "primary"),
    ]
    registry = tmp_path / ".box-agent/task-registry/tasks"
    assert not (registry / "task-A.json").exists()
    assert (registry / "task-B.json").exists()
    assert report.read_text() == "new report from task B"


@pytest.mark.asyncio
async def test_parallel_outputs_keep_their_producing_tool_call_ids(tmp_path):
    class Producer(MutableTool):
        parallel_safe = True

        async def execute(self, value):
            path = tmp_path / f"{value}.txt"
            path.write_text(value)
            return ToolResult(success=True, content=f"written {path}")

    class TwoCalls:
        called = False

        async def generate_stream(self, **kwargs):
            if not self.called:
                self.called = True
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id=value, type="function", function=FunctionCall(
                        name="write_record", arguments={"value": value},
                    )) for value in ["first", "second"]
                ])
            else:
                yield StreamEvent(type="finish", finish_reason="stop")

    tool = Producer()
    events = [event async for event in run_agent_loop(
        llm=TwoCalls(), tools={tool.name: tool}, messages=messages(),
        workspace_dir=str(tmp_path), max_steps=2,
    )]
    assert {(event.tool_call_id, event.rel_path) for event in events
            if isinstance(event, ArtifactEvent)} == {
        ("first", "first.txt"), ("second", "second.txt"),
    }
