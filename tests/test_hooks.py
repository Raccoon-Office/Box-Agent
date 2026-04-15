"""Tests for the agent lifecycle hooks system (box_agent.hooks)."""

import asyncio

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import (
    ContentEvent,
    DoneEvent,
    ErrorEvent,
    StepEnd,
    StepStart,
    StopReason,
    ToolCallResult,
    ToolCallStart,
)
from box_agent.hooks import BaseHook, HookManager, load_hooks
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


# ── Helpers (mirrors test_core.py patterns) ────────────────────


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0

    async def generate_stream(self, messages, tools=None):
        resp = self._responses[self._idx]
        self._idx += 1
        if resp.thinking:
            yield StreamEvent(type="thinking", delta=resp.thinking)
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echoes text back"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = ""):
        return ToolResult(success=True, content=f"echo:{text}")


class FailTool(Tool):
    @property
    def name(self):
        return "fail"

    @property
    def description(self):
        return "Always fails"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        raise RuntimeError("boom")


async def collect(gen) -> list:
    return [ev async for ev in gen]


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


class RecordingHook(BaseHook):
    """Records all hook method calls for assertion."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def on_agent_start(self, *, messages, tools, max_steps):
        self.calls.append(("on_agent_start", {"max_steps": max_steps, "tool_count": len(tools)}))

    async def on_step_start(self, *, step, max_steps):
        self.calls.append(("on_step_start", {"step": step, "max_steps": max_steps}))

    async def on_llm_response(self, *, response):
        self.calls.append(("on_llm_response", {"content": response.content}))

    async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
        self.calls.append(("on_tool_start", {"tool_name": tool_name, "arguments": arguments}))
        return None

    async def on_tool_result(self, *, tool_call_id, tool_name, success, content, error):
        self.calls.append(("on_tool_result", {"tool_name": tool_name, "success": success, "content": content}))
        return None

    async def on_step_end(self, *, step, elapsed_seconds, total_elapsed_seconds):
        self.calls.append(("on_step_end", {"step": step}))

    async def on_done(self, *, stop_reason, final_content):
        self.calls.append(("on_done", {"stop_reason": stop_reason, "final_content": final_content}))

    async def on_error(self, *, message, is_fatal, exception):
        self.calls.append(("on_error", {"message": message, "is_fatal": is_fatal}))


def _tool_call_response(tool_name: str, args: dict, content: str = "done"):
    """LLM response that calls a tool, followed by a final text response."""
    return [
        LLMResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCall(
                    id="tc1",
                    type="function",
                    function=FunctionCall(name=tool_name, arguments=args),
                )
            ],
        ),
        LLMResponse(content=content, finish_reason="stop"),
    ]


# ── Tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hook_simple_conversation():
    """No tool calls — verify lifecycle order."""
    hook = RecordingHook()
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    await collect(run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5, hooks=[hook]))

    names = [c[0] for c in hook.calls]
    assert names == [
        "on_agent_start",
        "on_step_start",
        "on_llm_response",
        "on_step_end",
        "on_done",
    ]
    # Verify done reason
    done_call = hook.calls[-1]
    assert done_call[1]["stop_reason"] == StopReason.END_TURN
    assert done_call[1]["final_content"] == "hello"


@pytest.mark.asyncio
async def test_hook_tool_call_cycle():
    """Full lifecycle with one tool call."""
    hook = RecordingHook()
    llm = MockLLM(_tool_call_response("echo", {"text": "hi"}, content="done"))
    echo = EchoTool()
    await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={"echo": echo}, max_steps=5, hooks=[hook])
    )

    names = [c[0] for c in hook.calls]
    assert "on_tool_start" in names
    assert "on_tool_result" in names

    # Verify tool_start has correct args
    ts = next(c for c in hook.calls if c[0] == "on_tool_start")
    assert ts[1]["tool_name"] == "echo"
    assert ts[1]["arguments"] == {"text": "hi"}

    # Verify tool_result
    tr = next(c for c in hook.calls if c[0] == "on_tool_result")
    assert tr[1]["tool_name"] == "echo"
    assert tr[1]["success"] is True
    assert tr[1]["content"] == "echo:hi"


@pytest.mark.asyncio
async def test_hook_error():
    """LLM exception triggers on_error and on_done(ERROR)."""
    hook = RecordingHook()

    class BrokenLLM:
        async def generate_stream(self, messages, tools=None):
            raise RuntimeError("LLM exploded")
            yield  # make it a generator  # noqa: E501

    await collect(
        run_agent_loop(llm=BrokenLLM(), messages=_msgs(), tools={}, max_steps=5, hooks=[hook])
    )

    names = [c[0] for c in hook.calls]
    assert "on_error" in names
    assert "on_done" in names

    err = next(c for c in hook.calls if c[0] == "on_error")
    assert err[1]["is_fatal"] is True

    done = next(c for c in hook.calls if c[0] == "on_done")
    assert done[1]["stop_reason"] == StopReason.ERROR


@pytest.mark.asyncio
async def test_hook_cancellation():
    """Cancellation triggers on_done(CANCELLED)."""
    hook = RecordingHook()
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    await collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            is_cancelled=lambda: True,
            hooks=[hook],
        )
    )

    names = [c[0] for c in hook.calls]
    assert "on_done" in names
    done = next(c for c in hook.calls if c[0] == "on_done")
    assert done[1]["stop_reason"] == StopReason.CANCELLED


@pytest.mark.asyncio
async def test_hook_tool_arg_modification():
    """on_tool_start can modify arguments."""

    class ArgModHook(BaseHook):
        async def on_tool_start(self, *, tool_call_id, tool_name, arguments):
            return {"text": "modified"}

    hook = ArgModHook()
    llm = MockLLM(_tool_call_response("echo", {"text": "original"}, content="done"))
    echo = EchoTool()
    events = await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={"echo": echo}, max_steps=5, hooks=[hook])
    )

    # The tool should have received "modified", not "original"
    result_events = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(result_events) == 1
    assert result_events[0].content == "echo:modified"


@pytest.mark.asyncio
async def test_hook_tool_result_modification():
    """on_tool_result can modify content (safety filtering)."""

    class SafetyHook(BaseHook):
        async def on_tool_result(self, *, tool_call_id, tool_name, success, content, error):
            if "secret" in content:
                return ("[REDACTED]", error)
            return None

    hook = SafetyHook()

    class SecretTool(Tool):
        @property
        def name(self):
            return "leak"

        @property
        def description(self):
            return "Leaks secrets"

        @property
        def parameters(self):
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=True, content="secret_data_here")

    llm = MockLLM(_tool_call_response("leak", {}, content="done"))
    events = await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={"leak": SecretTool()}, max_steps=5, hooks=[hook])
    )

    result_events = [e for e in events if isinstance(e, ToolCallResult)]
    assert len(result_events) == 1
    assert result_events[0].content == "[REDACTED]"


@pytest.mark.asyncio
async def test_hook_error_swallowed():
    """Hook exception doesn't crash the agent loop."""

    class BrokenHook(BaseHook):
        async def on_step_start(self, *, step, max_steps):
            raise RuntimeError("hook exploded")

    hook = BrokenHook()
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    events = await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5, hooks=[hook])
    )

    # Loop should complete normally despite hook error
    types = [type(e) for e in events]
    assert DoneEvent in types
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert done.stop_reason == StopReason.END_TURN


@pytest.mark.asyncio
async def test_multiple_hooks_order():
    """Multiple hooks called in registration order."""
    order = []

    class HookA(BaseHook):
        async def on_step_start(self, *, step, max_steps):
            order.append("A")

    class HookB(BaseHook):
        async def on_step_start(self, *, step, max_steps):
            order.append("B")

    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5, hooks=[HookA(), HookB()])
    )

    assert order == ["A", "B"]


@pytest.mark.asyncio
async def test_no_hooks_no_overhead():
    """hooks=None produces identical event stream."""
    llm1 = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    llm2 = MockLLM([LLMResponse(content="hello", finish_reason="stop")])

    events_no_hook = await collect(run_agent_loop(llm=llm1, messages=_msgs(), tools={}, max_steps=5))
    events_none = await collect(run_agent_loop(llm=llm2, messages=_msgs(), tools={}, max_steps=5, hooks=None))

    types1 = [type(e).__name__ for e in events_no_hook]
    types2 = [type(e).__name__ for e in events_none]
    assert types1 == types2


@pytest.mark.asyncio
async def test_duck_typing():
    """A plain class (no BaseHook inheritance) with only on_done works."""

    class MinimalHook:
        def __init__(self):
            self.called = False

        async def on_done(self, *, stop_reason, final_content):
            self.called = True

    hook = MinimalHook()
    llm = MockLLM([LLMResponse(content="hello", finish_reason="stop")])
    await collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5, hooks=[hook])
    )

    assert hook.called is True


@pytest.mark.asyncio
async def test_hook_via_agent_class():
    """Hooks work through the Agent wrapper."""
    from box_agent.agent import Agent
    from box_agent.llm import LLMClient

    hook = RecordingHook()

    # We can't easily mock LLMClient, so test that Agent stores and passes hooks
    agent = Agent(
        llm_client=MockLLM([LLMResponse(content="hello", finish_reason="stop")]),
        system_prompt="sys",
        tools=[],
        max_steps=5,
        hooks=[hook],
    )
    agent.add_user_message("hi")
    await agent.run()

    names = [c[0] for c in hook.calls]
    assert "on_agent_start" in names
    assert "on_done" in names


def test_load_hooks_valid():
    """load_hooks can import and instantiate BaseHook."""
    hooks = load_hooks(["box_agent.hooks.BaseHook"])
    assert len(hooks) == 1
    assert isinstance(hooks[0], BaseHook)


def test_load_hooks_invalid_skipped():
    """Invalid dotted path is skipped (not raised)."""
    hooks = load_hooks(["nonexistent.module.Hook", "box_agent.hooks.BaseHook"])
    assert len(hooks) == 1  # only the valid one


def test_hook_manager_empty():
    """Empty HookManager has falsy hooks list."""
    mgr = HookManager()
    assert not mgr.hooks
    mgr2 = HookManager(None)
    assert not mgr2.hooks


def test_load_hooks_from_user_dir(tmp_path, monkeypatch):
    """Hooks can be loaded from ~/.box-agent/hooks/ directory."""
    import box_agent.hooks as hooks_mod

    # Create a temporary hooks dir with a hook module
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "my_hook.py").write_text(
        "from box_agent.hooks import BaseHook\n"
        "class MyHook(BaseHook):\n"
        "    pass\n"
    )

    # Patch USER_HOOKS_DIR to point to our temp dir
    monkeypatch.setattr(hooks_mod, "USER_HOOKS_DIR", hooks_dir)

    hooks = load_hooks(["my_hook.MyHook"])
    assert len(hooks) == 1
    assert type(hooks[0]).__name__ == "MyHook"
