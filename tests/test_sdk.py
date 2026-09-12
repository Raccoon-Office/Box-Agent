from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from box_agent.api import ControlCommand, RunRequest
from box_agent.events import ContentEvent, DoneEvent, StopReason
from box_agent.run_control import PermissionBroker
from box_agent.sdk import AgentClient
from box_agent.tools.permissions import GrantStore


class _Agent:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add_user_message(self, content: str) -> None:
        self.messages.append(content)


@dataclass
class _Options:
    run_control: object | None = None
    permission_negotiator: object | None = None


class _Session:
    def __init__(self) -> None:
        self.agent = _Agent()
        self.inject_queue: asyncio.Queue[object] = asyncio.Queue()
        self.cancelled = False
        self.grant_store = GrantStore()

    def build_run_options(self, **overrides: object) -> _Options:
        return _Options()

    async def run_events(self, *, options=None):
        if options.run_control is not None:
            await options.run_control.checkpoint()
        yield ContentEvent(content="answer")
        yield DoneEvent(
            stop_reason=StopReason.END_TURN,
            final_content="answer",
        )


@pytest.mark.asyncio
async def test_sdk_client_starts_and_completes_a_run() -> None:
    session = _Session()
    client = AgentClient(session)

    result = await client.run(
        RunRequest("sdk-run", "sdk-session", "Explain this"),
    )

    assert session.agent.messages == ["Explain this"]
    assert result.run_id == "sdk-run"
    assert result.status == "completed"
    assert result.final_content == "answer"


@pytest.mark.asyncio
async def test_sdk_client_exposes_handle_for_streaming_and_control() -> None:
    session = _Session()
    handle = await AgentClient(session).start(
        RunRequest("sdk-stream", "sdk-session", "Stream this"),
    )
    await handle.send(ControlCommand.cancel())

    events = [event async for event in handle.events()]

    assert [type(event.payload) for event in events] == [ContentEvent, DoneEvent]
    assert session.cancelled is True
    assert (await handle.result()).run_id == "sdk-stream"


@pytest.mark.asyncio
async def test_sdk_handle_pauses_and_resumes_before_the_next_kernel_action() -> None:
    session = _Session()
    handle = await AgentClient(session).start(
        RunRequest("sdk-pause", "sdk-session", "Pause this"),
    )

    await handle.send(ControlCommand("pause"))
    stream = handle.events()
    next_event = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    assert handle.control_state == "paused"
    assert not next_event.done()

    await handle.send(ControlCommand("resume"))
    assert (await next_event).payload.content == "answer"
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("option_id", ["approve", "approve_session", "reject"])
async def test_sdk_permission_response_controls_real_file_access(tmp_path, monkeypatch, option_id):
    from pathlib import Path
    from box_agent.agent_session import AgentSession
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.events import PermissionRequestEvent, ToolCallResult
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.tools.file_tools import ReadTool
    from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "outside" / "data.txt"
    source.parent.mkdir()
    source.write_text("permission proof", encoding="utf-8")
    store = GrantStore()
    engine = PermissionEngine(CapabilityPolicy(), workspace, grant_store=store)
    reader = ReadTool(str(workspace), permission_engine=engine)

    class Model:
        calls = 0

        async def generate_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id="read-1", type="function", function=FunctionCall(
                        name="read_file", arguments={"path": str(source)},
                    )),
                ])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(workspace), max_steps=2, enable_memory_extraction=False),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False),
    )
    session = AgentSession.create(
        config=config, llm_client=Model(), system_prompt="system", tools=[reader],
        grant_store=store, permission_engine=engine,
    )
    broker = PermissionBroker(run_id="file-run", on_request=lambda _: None)
    handle = await AgentClient(session).start(
        RunRequest("file-run", "session", "read the file"),
        options=session.build_run_options(permission_negotiator=broker, logger=None),
    )
    results = []
    try:
        async with handle:
            async for envelope in handle.events():
                event = envelope.payload
                if isinstance(event, PermissionRequestEvent) and event.request_id:
                    await handle.send(ControlCommand.permission_response(event.request_id, option_id=option_id))
                if isinstance(event, ToolCallResult):
                    results.append(event)
        assert len(results) == 1
        assert results[0].success is (option_id != "reject")
        if option_id != "reject":
            assert "permission proof" in results[0].content
        assert not store.has_filesystem_dir_grant(tmp_path / "unrelated.txt")
        await AgentClient(session).run(
            RunRequest("continuation", "session"),
            options=session.build_run_options(logger=None),
        )
        assert store.has_filesystem_dir_grant(source) is (option_id != "reject")
        # A fresh SDK user turn clears only temporary grants in this same store.
        await AgentClient(session).run(
            RunRequest("next", "session", "next turn"),
            options=session.build_run_options(logger=None),
        )
        assert store.has_filesystem_dir_grant(source) is (option_id == "approve_session")
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_sdk_handle_routes_permission_response_to_pending_request() -> None:
    session = _Session()

    async def run_events(*, options=None):
        assert isinstance(options.permission_negotiator, PermissionBroker)
        approved = await options.permission_negotiator.negotiate({
            "scope": "filesystem",
            "requested_scope": "workspace",
        })
        yield DoneEvent(
            stop_reason=StopReason.END_TURN,
            final_content="approved" if approved else "denied",
        )

    session.run_events = run_events
    broker = PermissionBroker(run_id="sdk-permission", on_request=lambda _request: None)
    options = _Options(permission_negotiator=broker)
    handle = await AgentClient(session).start(
        RunRequest("sdk-permission", "sdk-session", "Check permission"),
        options=options,
    )

    stream = handle.events()
    permission = await stream.__anext__()
    request_id = permission.payload.request_id
    assert request_id
    await handle.send(ControlCommand(
        "permission_response",
        request_id=request_id,
        payload={"approved": True},
    ))
    done = await stream.__anext__()
    assert done.payload.final_content == "approved"
    await stream.aclose()
