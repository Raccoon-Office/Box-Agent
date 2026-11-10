"""Parent execution permissions and child-owned Python kernel regressions."""

from __future__ import annotations

import asyncio
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.events import DoneEvent, ErrorEvent, StopReason
from box_agent.runtime import invoke_tool_with_permissions
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools import jupyter_tool
from box_agent.tools.base import ToolResult
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.jupyter_tool import (
    InProcessKernelSession, JupyterKernelSession, JupyterSandboxTool,
    SandboxEnvironment, SandboxStatusTool,
)
from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine
from box_agent.tools.sub_agent_tool import SubAgentTool


async def _run_child_loop_events(monkeypatch, events):
    from box_agent import runtime

    async def run(**kwargs):
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event

    monkeypatch.setattr(runtime, "run_agent_loop", run)
    child = SubAgentTool(llm=None, parent_tools={})
    return await child._run_general_loop(
        llm=None, messages=[Message(role="user", content="task")], child_tools={},
        max_steps=1, max_tool_calls=None, diagnostic={"strategy": "general_loop"},
        queue=None, parent_tool_call_id="test-child", task_preview="task",
        sub_agent_id="child", title="child",
    )


@pytest.mark.parametrize("stop_reason", [StopReason.ERROR, StopReason.CANCELLED, StopReason.INTERRUPTED])
async def test_child_error_or_interruption_keeps_partial_output_without_reporting_success(monkeypatch, stop_reason):
    result = await _run_child_loop_events(monkeypatch, [
        DoneEvent(stop_reason=stop_reason, final_content="partial work"),
    ])
    assert not result.success
    assert result.content == "partial work"
    assert stop_reason.value in result.error
    assert result.raw_output["stop_reason"] == stop_reason.value


@pytest.mark.parametrize("stop_reason", [StopReason.END_TURN, StopReason.MAX_TOKENS, None])
async def test_child_fatal_error_is_not_success_even_with_final_output(monkeypatch, stop_reason):
    events = [ErrorEvent(message="Input budget exceeded", is_fatal=True,
                         error_code="CONTEXT_INPUT_BUDGET_EXCEEDED")]
    if stop_reason is not None:
        events.append(DoneEvent(stop_reason=stop_reason, final_content="partial work"))
    result = await _run_child_loop_events(monkeypatch, events)
    assert not result.success
    assert result.error == "Input budget exceeded"
    assert result.content == ("partial work" if stop_reason is not None else "")
    assert result.raw_output["error_code"] == "CONTEXT_INPUT_BUDGET_EXCEEDED"


@pytest.mark.parametrize("stop_reason", [
    StopReason.END_TURN, StopReason.MAX_STEPS, StopReason.MAX_TOKENS, StopReason.WAITING_FOR_USER,
])
async def test_child_nonfatal_outcomes_keep_output_and_expose_the_stop_reason(monkeypatch, stop_reason):
    result = await _run_child_loop_events(monkeypatch, [
        ErrorEvent(message="Optional image unavailable", is_fatal=False),
        DoneEvent(stop_reason=stop_reason, final_content="useful output"),
    ])
    assert result.success
    assert result.content == "useful output"
    assert result.error is None
    assert result.raw_output["stop_reason"] == stop_reason.value


async def test_child_loop_task_cancellation_still_propagates(monkeypatch):
    with pytest.raises(asyncio.CancelledError):
        await _run_child_loop_events(monkeypatch, [asyncio.CancelledError()])


class SuccessfulProcess:
    returncode = 0

    async def communicate(self):
        return b"executed\n", b""


class Broker:
    def __init__(self, store: GrantStore, *, approve: bool):
        self.store = store
        self.approve = approve
        self.requests: list[dict] = []

    async def negotiate(self, request: dict) -> bool:
        self.requests.append(request.copy())
        grant_scope = "prompt" if request.get("scope") == "safety" else "session"
        return self.approve and self.store.apply_permission_grant(request, grant_scope)


async def invoke_child_bash(parent: BashTool, command: str, broker=None):
    child = SubAgentTool(llm=None, parent_tools={"bash": parent})
    child.set_permission_negotiator(broker)

    async def run(**kwargs):
        assert kwargs["child_tools"]["bash"] is parent
        result, _ = await invoke_tool_with_permissions(
            kwargs["child_tools"]["bash"], {"command": command},
            permission_negotiator=broker,
        )
        return result

    child._run_general_loop = run
    return await child.execute(task="Run the assigned command", required_tools=["bash"])


def make_bash(tmp_path, monkeypatch, *, bypass=False):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = GrantStore()
    engine = PermissionEngine(
        CapabilityPolicy(filesystem_scope="session_workspace", session_workspace_root=str(workspace)),
        workspace, grant_store=store,
    )
    engine._home_dir = tmp_path.resolve()
    tool = BashTool(
        workspace_dir=str(workspace), permission_engine=engine,
        non_interactive=True, bypass_dangerous_command_approval=bypass,
    )
    spawn = AsyncMock(side_effect=lambda *args, **kwargs: SuccessfulProcess())
    monkeypatch.setattr(tool, "_create_subprocess", spawn)
    return tool, store, spawn


@pytest.mark.parametrize("with_broker", [False, True])
async def test_repeated_normal_child_commands_need_no_extra_approval(
    tmp_path, monkeypatch, with_broker,
):
    tool, store, spawn = make_bash(tmp_path, monkeypatch)
    broker = Broker(store, approve=True) if with_broker else None
    for command in ["pwd", "printf probe", "pwd"]:
        parent, _ = await invoke_tool_with_permissions(
            tool, {"command": command}, permission_negotiator=broker,
        )
        child = await invoke_child_bash(tool, command, broker)
        assert parent.success and child.success
        assert parent.content == child.content
    assert spawn.await_count == 6
    if broker is not None:
        assert broker.requests == []


@pytest.mark.parametrize("approve", [None, False, True])
@pytest.mark.parametrize("bypass", [False, True])
async def test_child_dangerous_command_matches_parent_approval_and_bypass(
    tmp_path, monkeypatch, posix_command_parser, approve, bypass,
):
    tool, store, spawn = make_bash(tmp_path, monkeypatch, bypass=bypass)
    broker = Broker(store, approve=approve) if approve is not None else None
    command = "rm -f missing.txt"
    parent, _ = await invoke_tool_with_permissions(
        tool, {"command": command}, permission_negotiator=broker,
    )
    child = await invoke_child_bash(tool, command, broker)
    repeated_child = await invoke_child_bash(tool, command, broker)

    expected = bypass or approve is True
    assert parent.success == child.success == repeated_child.success == expected
    assert spawn.await_count == (3 if expected else 0)
    if broker is not None:
        assert len(broker.requests) == (0 if bypass else 3)
        assert all(request["requested_scope"] == "dangerous_command" for request in broker.requests)
    if approve is None and not bypass:
        assert child.permission_request == parent.permission_request


@pytest.mark.parametrize("approve", [False, True])
async def test_child_filesystem_checks_and_grants_use_parent_store(
    tmp_path, monkeypatch, posix_command_parser, approve,
):
    tool, store, spawn = make_bash(tmp_path, monkeypatch)
    outside = tmp_path / "outside" / "input.txt"
    outside.parent.mkdir()
    outside.write_text("input", encoding="utf-8")
    command = f"cat {shlex.quote(str(outside))}"
    broker = Broker(store, approve=approve)
    parent, _ = await invoke_tool_with_permissions(
        tool, {"command": command}, permission_negotiator=broker,
    )
    child = await invoke_child_bash(tool, command, broker)

    assert parent.success == child.success == approve
    assert spawn.await_count == (2 if approve else 0)
    assert len(broker.requests) == (1 if approve else 2)
    assert all(request["scope"] == "filesystem" for request in broker.requests)


@pytest.fixture
def isolated_python(tmp_path, monkeypatch):
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {})
    monkeypatch.setattr(JupyterSandboxTool, "_sandbox_env", None)
    monkeypatch.setattr(JupyterSandboxTool, "_sandbox_env_key", None)
    monkeypatch.setattr(jupyter_tool, "SANDBOX_BASE_DIR", tmp_path / "sandbox")
    monkeypatch.setattr(jupyter_tool, "RUNTIME_PACKAGES_DIR", tmp_path / "packages")
    monkeypatch.setattr(SandboxEnvironment, "_verify_packages", AsyncMock())
    return JupyterSandboxTool(
        workspace_dir=str(tmp_path / "workspace"),
        runtime_env={"BOX_AGENT_SANDBOX_PYTHON": sys.executable},
        process_owner_id="parent",
    )


async def test_parallel_child_python_preserves_local_state_and_cleans_only_children(
    isolated_python, monkeypatch,
):
    parent = isolated_python
    initial = await parent.execute("value = 'parent'", session_id="shared")
    assert initial.success, initial.error
    sandboxes = []
    fork = parent.fork_for_child

    def record_fork(child_id):
        sandbox = fork(child_id)
        assert sandbox.workspace_dir == parent.workspace_dir
        assert sandbox.runtime_env == parent.runtime_env
        assert sandbox.session_id is None
        sandboxes.append(sandbox)
        return sandbox

    monkeypatch.setattr(parent, "fork_for_child", record_fork)

    async def stream(*, messages, tools, **kwargs):
        assert {tool.name for tool in tools} == {"execute_code", "sandbox_status"}
        task = next(message.content for message in messages if message.role == "user")
        title = "first" if "first" in task else "second"
        outputs = [message.content for message in messages if message.role == "tool"]
        step = len(outputs)
        if step == 0:
            arguments = {
                "code": f"assert 'value' not in globals()\nvalue = {title!r}\nprint(value)",
                "session_id": "shared",
            }
        elif step == 1:
            assert title in outputs[0]
            arguments = {"code": f"assert value == {title!r}\nprint(value)", "session_id": "shared"}
        elif step == 2:
            assert title in outputs[1]
            arguments = {}
        else:
            assert "Total sessions: 1" in outputs[2]
            yield StreamEvent(type="text", delta=title)
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        yield StreamEvent(
            type="finish", finish_reason="tool_use",
            tool_calls=[ToolCall(
                id=f"{title}-{step}", type="function",
                function=FunctionCall(name="sandbox_status" if step == 2 else "execute_code", arguments=arguments),
            )],
        )

    llm = AsyncMock()
    llm.generate_stream = stream
    child = SubAgentTool(
        llm=llm,
        parent_tools={"execute_code": parent, "sandbox_status": SandboxStatusTool(parent)},
        workspace_dir=parent.workspace_dir,
    )
    try:
        results = await asyncio.gather(*(
            child.execute(
                task=f"Run Python for {title}", title=title,
                required_tools=["execute_code", "sandbox_status"],
            ) for title in ["first", "second"]
        ))
        assert all(result.success for result in results)
        assert {result.content for result in results} == {"first", "second"}
        assert all(result.raw_output["tool_calls"] == 3 for result in results)
        assert sandboxes[0]._session_key("shared") != sandboxes[1]._session_key("shared")
        assert all(sandbox.session_id is None for sandbox in sandboxes)
        assert list(parent._sessions) == [parent._session_key("shared")]
        persisted = await parent.execute("assert value == 'parent'\nprint(value)")
        assert persisted.success, persisted.error
        assert "parent" in persisted.content
    finally:
        await parent.shutdown_owned_sessions()


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
async def test_child_python_cleanup_runs_on_every_exit(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {})
    parent = JupyterSandboxTool(workspace_dir=str(tmp_path), process_owner_id="parent")
    child = SubAgentTool(llm=None, parent_tools={"execute_code": parent})
    parent_session = SimpleNamespace(stop=AsyncMock())
    sibling_session = SimpleNamespace(stop=AsyncMock())
    parent._sessions[parent._session_key("shared")] = parent_session
    parent._sessions["parent:sibling::shared"] = sibling_session
    sessions = []

    async def run(**kwargs):
        sandbox = kwargs["child_tools"]["execute_code"]
        for name in ["first", "second"]:
            session = SimpleNamespace(stop=AsyncMock())
            sessions.append(session)
            sandbox._sessions[sandbox._session_key(name)] = session
        if outcome == "error":
            raise RuntimeError("child failed")
        if outcome == "cancel":
            raise asyncio.CancelledError
        return ToolResult(success=True, content="done")

    child._run_general_loop = run
    if outcome == "success":
        assert (await child.execute(task="Run Python", required_tools=["execute_code"])).success
    else:
        with pytest.raises(RuntimeError if outcome == "error" else asyncio.CancelledError):
            await child.execute(task="Run Python", required_tools=["execute_code"])
    assert all(session.stop.await_count == 1 for session in sessions)
    parent_session.stop.assert_not_awaited()
    sibling_session.stop.assert_not_awaited()
    assert set(parent._sessions) == {parent._session_key("shared"), "parent:sibling::shared"}


@pytest.mark.parametrize("outcome", [RuntimeError("startup failed"), asyncio.CancelledError()])
async def test_child_kernel_start_failure_and_cancellation_close_pending_kernel(
    tmp_path, monkeypatch, outcome,
):
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {})
    child = JupyterSandboxTool(process_owner_id="parent").fork_for_child("child")
    session = SimpleNamespace(start=AsyncMock(side_effect=outcome), stop=AsyncMock())
    monkeypatch.setattr(child, "_create_session", lambda *args: session)

    with pytest.raises(type(outcome)):
        await child._start_session(child._session_key("shared"), tmp_path, None)
    session.stop.assert_awaited_once()
    assert child._sessions == {}


async def test_child_status_without_python_execution_cannot_enumerate_parent(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {})
    monkeypatch.setattr(JupyterSandboxTool, "_get_sandbox_env", lambda self: SimpleNamespace(
        venv_dir=tmp_path, is_created=False,
    ))
    parent = JupyterSandboxTool(workspace_dir=str(tmp_path), process_owner_id="parent")
    parent._sessions[parent._session_key("parent-secret-kernel")] = SimpleNamespace(is_alive=lambda: True, workspace=tmp_path)
    child = SubAgentTool(llm=None, parent_tools={"execute_code": parent, "sandbox_status": SandboxStatusTool(parent)})

    async def run(**kwargs):
        assert set(kwargs["child_tools"]) == {"sandbox_status"}
        status = await kwargs["child_tools"]["sandbox_status"].execute()
        assert "parent-secret-kernel" not in status.content
        assert "Total sessions: 0" in status.content
        return status

    child._run_general_loop = run
    assert (await child.execute(task="Inspect Python", required_tools=["sandbox_status"])).success


def test_frozen_child_requires_subprocess_kernel_and_keeps_parent_fallback(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(jupyter_tool, "IS_FROZEN", True)
    parent = JupyterSandboxTool(process_owner_id="parent")
    child = parent.fork_for_child("child")
    env = SimpleNamespace(_bundled_override=False)
    assert isinstance(parent._create_session("parent", tmp_path, env), InProcessKernelSession)
    with pytest.raises(RuntimeError, match="ISOLATED_KERNEL_UNAVAILABLE"):
        child._create_session("child", tmp_path, env)
    env._bundled_override = True
    assert isinstance(child._create_session("child", tmp_path, env), JupyterKernelSession)


async def test_parallel_children_initialize_shared_python_environment_once(
    isolated_python, monkeypatch,
):
    env = isolated_python._get_sandbox_env()

    async def verify(on_progress):
        await asyncio.sleep(0)

    probe = AsyncMock(side_effect=verify)
    monkeypatch.setattr(env, "_verify_packages", probe)
    first = isolated_python.fork_for_child("first")
    second = isolated_python.fork_for_child("second")
    assert first._get_sandbox_env() is second._get_sandbox_env() is env

    await asyncio.gather(first._get_sandbox_env().ensure_ready(), second._get_sandbox_env().ensure_ready())

    probe.assert_awaited_once()
    assert env._ready


async def test_python_environment_initialization_can_retry_after_failure(
    isolated_python, monkeypatch,
):
    env = isolated_python._get_sandbox_env()
    probe = AsyncMock(side_effect=[RuntimeError("not ready"), None])
    monkeypatch.setattr(env, "_verify_packages", probe)

    with pytest.raises(RuntimeError, match="not ready"):
        await env.ensure_ready()
    assert not env._ready
    await env.ensure_ready()
    assert env._ready
    assert probe.await_count == 2


async def test_frozen_child_reports_missing_isolated_runtime_without_starting_kernel(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(jupyter_tool, "IS_FROZEN", True)
    monkeypatch.setattr(JupyterSandboxTool, "_sessions", {})
    child = JupyterSandboxTool(workspace_dir=str(tmp_path)).fork_for_child("child")
    env = SimpleNamespace(_bundled_override=False, ensure_ready=AsyncMock())
    monkeypatch.setattr(child, "_get_sandbox_env", lambda: env)

    result = await child.execute("print('child')")

    assert not result.success
    assert "ISOLATED_KERNEL_UNAVAILABLE" in result.error
    assert child._sessions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("required_tools", [None, [], ["read_file"]])
@pytest.mark.parametrize("with_hook", [False, True])
async def test_read_only_children_still_overlap_with_hosted_image_billing(tmp_path, required_tools, with_hook):
    from box_agent.core import run_agent_loop
    from box_agent.events import ToolCallResult
    from box_agent.tools.billing import confirmation
    from box_agent.tools.file_tools import ReadTool
    from box_agent.tools.image_generation_tool import GenerateImageTool

    from box_agent.hooks import BaseHook

    class TitleHook(BaseHook):
        async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
            return {**arguments, "title": "Analysis"} if tool_name == "sub_agent" else arguments

    both_started = asyncio.Event()
    started = 0
    class ChildLLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            nonlocal started
            assert "generate_image" not in {tool.name for tool in tools}
            started += 1
            if started == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            yield StreamEvent(type="text", delta="Read-only analysis complete.")
            yield StreamEvent(type="finish", finish_reason="stop")

    class ParentLLM:
        calls = 0
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                    ToolCall(id=f"child-{index}", type="function", function=FunctionCall(
                        name="sub_agent", arguments={
                            "task": f"Analyze independent topic {index}",
                            **({"required_tools": required_tools} if required_tools is not None else {}),
                        },
                    )) for index in range(2)
                ])
            else:
                yield StreamEvent(type="text", delta="Done.")
                yield StreamEvent(type="finish", finish_reason="stop")

    image = GenerateImageTool(workspace_dir=str(tmp_path), endpoint="https://xiaohuanxiong.com/images/gen")
    registry = {"generate_image": image, "read_file": ReadTool(workspace_dir=str(tmp_path))}
    child = SubAgentTool(llm=ChildLLM(), parent_tools=registry, workspace_dir=str(tmp_path))
    registry[child.name] = child
    callback = AsyncMock(return_value=False)
    token = confirmation.set(callback)
    try:
        events = [event async for event in run_agent_loop(
            llm=ParentLLM(), tools=registry,
            messages=[Message(role="user", content="Analyze these independent topics in parallel")],
            max_steps=3, workspace_dir=str(tmp_path), hooks=[TitleHook()] if with_hook else [],
        )]
    finally:
        confirmation.reset(token)
    results = [event for event in events if isinstance(event, ToolCallResult) and event.tool_name == "sub_agent"]
    assert len(results) == 2
    assert all(result.success for result in results), [result.error for result in results]
    assert both_started.is_set()
    callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rewrite_at,hook_route", [(None, "legacy"), (0, "legacy"), (1, "legacy"), (0, "plugin"), (1, "plugin")])
@pytest.mark.parametrize("approved", [True, False])
async def test_image_child_waits_in_current_turn_without_parallel_batch_timeout(tmp_path, monkeypatch, rewrite_at, hook_route, approved):
    from box_agent.core import run_agent_loop
    from box_agent.tools.billing import confirmation
    from box_agent.tools.image_generation_tool import GenerateImageTool

    from box_agent.hooks import BaseHook

    hook_calls = []
    class AddImageHook(BaseHook):
        async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
            if tool_name == "sub_agent":
                hook_calls.append(tool_call_id)
                if int(tool_call_id.removeprefix("child-")) >= rewrite_at:
                    return {**arguments, "required_tools": ["generate_image"]}
            return arguments

    hook_options = {}
    if rewrite_at is not None:
        hook = AddImageHook()
        if hook_route == "legacy":
            hook_options["hooks"] = [hook]
        else:
            from box_agent.kernel.hook_types import BeforeToolDecision, copy_data
            from box_agent.plugins.hooks import HookSpec
            from tests.test_hook_plugins import plugin

            async def before(ctx):
                arguments = await hook.on_tool_start(
                    tool_call_id=ctx.tool_call_id, tool_name=ctx.payload["tool_name"],
                    arguments=copy_data(ctx.payload["arguments"]),
                )
                return BeforeToolDecision.modify(arguments)
            hook_options["plugins"] = (plugin([
                HookSpec("add-image", ("tool.before_execution",), "before_tool", before),
            ]),)

    child_starts = []
    class ImageLLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            if any(message.role == "tool" for message in messages):
                yield StreamEvent(type="text", delta="Image attempt finished.")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            child_starts.append(True)
            if "generate_image" not in {tool.name for tool in tools}:
                yield StreamEvent(type="text", delta="Analysis complete.")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[ToolCall(
                id="image", type="function", function=FunctionCall(
                    name="generate_image", arguments={"prompt": "cat", "output_path": "cat.png"},
                ),
            )])

    class ParentLLM:
        calls = 0
        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls > 1:
                yield StreamEvent(type="text", delta="Done.")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id=f"child-{index}", type="function", function=FunctionCall(
                    name="sub_agent", arguments={
                        "task": f"Analyze topic {index}",
                        "required_tools": ["generate_image"] if rewrite_at is None else [],
                    },
                )) for index in range(3)
            ])

    image = GenerateImageTool(workspace_dir=str(tmp_path), endpoint="https://xiaohuanxiong.com/images/gen")
    from tests.test_image_generation_tool import PNG_BYTES
    network = AsyncMock(return_value=(PNG_BYTES, "image/png"))
    monkeypatch.setattr(image, "_request_image", network)
    monkeypatch.setattr("box_agent.tools.image_generation_tool.apply_text_watermark", lambda data, *a, **k: (data, {}))
    registry = {"generate_image": image}
    child = SubAgentTool(llm=ImageLLM(), parent_tools=registry, workspace_dir=str(tmp_path))
    registry[child.name] = child
    waiting = asyncio.Event()
    reply = asyncio.Event()
    async def approve(capability):
        waiting.set()
        await reply.wait()
        return approved

    async def run():
        return [event async for event in run_agent_loop(
            llm=ParentLLM(), tools=registry,
            messages=[Message(role="user", content="Complete the delegated work")],
            max_steps=3, workspace_dir=str(tmp_path), parallel_tool_timeout_seconds=0.01,
            **hook_options,
        )]

    callback = AsyncMock(side_effect=approve)
    token = confirmation.set(callback)
    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        # Human approval waits on the current invocation, beyond the ordinary
        # parallel batch deadline. Later children must not start while waiting.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.2)
        assert len(child_starts) == (rewrite_at or 0) + 1
        assert callback.await_count == 1
        network.assert_not_awaited()
        reply.set()
        events = await asyncio.wait_for(task, 3)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        confirmation.reset(token)
    assert len(child_starts) == 3
    if rewrite_at is not None:
        assert hook_calls == [f"child-{index}" for index in range(3)]
    assert callback.await_count == 3 - (rewrite_at or 0)
    assert network.await_count == (callback.await_count if approved else 0)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done[-1].stop_reason is StopReason.END_TURN
