"""SenseNova DSML tool-call fallback: parsing, hook gating, and loop integration."""

import pytest

from box_agent.dsml_fallback import (
    DsmlToolCallRecoveryHook,
    parse_dsml_tool_calls,
    recover_dsml_tool_calls,
)
from box_agent.events import DoneEvent, InjectedMessageEvent, StopReason, ToolCallResult
from box_agent.hooks import HookManager
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult

DSML_SINGLE = (
    "<｜DSML｜tool_calls>\n"
    '<｜DSML｜invoke name="bash">\n'
    '<｜DSML｜parameter name="command" string="true">ls -la</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜tool_calls>"
)

DSML_MULTI = (
    "<｜DSML｜tool_calls>\n"
    '<｜DSML｜invoke name="bash">\n'
    '<｜DSML｜parameter name="command" string="true">ls</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    '<｜DSML｜invoke name="read_file">\n'
    '<｜DSML｜parameter name="path" string="true">/tmp/a.txt</｜DSML｜parameter>\n'
    '<｜DSML｜parameter name="limit">10</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜tool_calls>"
)


@pytest.fixture(autouse=True)
def _stable_sensenova_prefixes(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_SENSENOVA_MODEL_PREFIXES", raising=False)


# ── Parser unit tests ─────────────────────────────────────────


def test_parse_single_invoke_from_thinking_text():
    calls = parse_dsml_tool_calls(f"我先想一下。\n{DSML_SINGLE}\n然后执行。")

    assert len(calls) == 1
    assert calls[0].name == "bash"
    assert calls[0].arguments == {"command": "ls -la"}


def test_parse_multiple_invokes():
    calls = parse_dsml_tool_calls(DSML_MULTI)

    assert [c.name for c in calls] == ["bash", "read_file"]
    assert calls[0].arguments == {"command": "ls"}
    # Non-string parameters are JSON-coerced when possible.
    assert calls[1].arguments == {"path": "/tmp/a.txt", "limit": 10}


def test_parse_accepts_ascii_pipe_delimiter():
    text = DSML_SINGLE.replace("｜", "|")
    calls = parse_dsml_tool_calls(text)

    assert len(calls) == 1
    assert calls[0].name == "bash"


@pytest.mark.parametrize("value", ["    return 1\n", "\tline\r\n", " \n", ""])
def test_recovery_preserves_string_parameter_whitespace(value):
    response = LLMResponse(content="", finish_reason="stop", thinking=(
        '<｜DSML｜invoke name="write_file">'
        f'<｜DSML｜parameter name="content" string="true">{value}</｜DSML｜parameter>'
        '</｜DSML｜invoke>'
    ))
    assert recover_dsml_tool_calls(response) == ["write_file"]
    assert response.tool_calls[0].function.arguments == {"content": value}


@pytest.mark.parametrize("body", [
    '<｜DSML｜parameter name="command" string="true">echo one'
    '<｜DSML｜parameter name="timeout">30</｜DSML｜parameter>',
    '<｜DSML｜parameter name="command" string="true">first</｜DSML｜parameter>'
    '<｜DSML｜parameter name="command" string="true">second</｜DSML｜parameter>',
])
def test_recovery_leaves_ambiguous_parameters_untouched(body):
    response = LLMResponse(content="", thinking=(
        f'<｜DSML｜invoke name="bash">{body}</｜DSML｜invoke>'
    ), finish_reason="stop")
    original = response.model_copy(deep=True)
    assert recover_dsml_tool_calls(response) == []
    assert response == original


def test_parse_does_not_rescan_inside_an_unclosed_invoke():
    text = '<｜DSML｜invoke name="broken">unfinished\n' + DSML_SINGLE
    assert parse_dsml_tool_calls(text) == []


def test_parse_does_not_execute_dsml_example_inside_string_parameter():
    text = (
        '<｜DSML｜invoke name="write_file">'
        '<｜DSML｜parameter name="content" string="true">'
        + DSML_SINGLE
        + '</｜DSML｜parameter></｜DSML｜invoke>'
    )
    assert parse_dsml_tool_calls(text) == []


@pytest.mark.parametrize(
    "text",
    [
        # Unclosed invoke block.
        '<｜DSML｜invoke name="bash">\n'
        '<｜DSML｜parameter name="command" string="true">ls</｜DSML｜parameter>\n',
        # Missing name.
        "<｜DSML｜invoke>\n"
        '<｜DSML｜parameter name="command" string="true">ls</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>",
        # Unclosed parameter inside an otherwise closed invoke.
        '<｜DSML｜invoke name="bash">\n'
        '<｜DSML｜parameter name="command" string="true">ls -la\n'
        "</｜DSML｜invoke>",
        # Plain reasoning text without any DSML markup.
        "让我想想，应该先列出目录。",
        "",
        None,
    ],
)
def test_parse_skips_malformed_or_absent_blocks(text):
    assert parse_dsml_tool_calls(text) == []


def test_parse_keeps_valid_invoke_when_sibling_is_malformed():
    text = (
        DSML_SINGLE
        + "\n"
        + '<｜DSML｜invoke name="bash">\n'
        + '<｜DSML｜parameter name="command" string="true">broken\n'
        + "</｜DSML｜invoke>"
    )
    calls = parse_dsml_tool_calls(text)

    assert len(calls) == 1
    assert calls[0].arguments == {"command": "ls -la"}


def test_parse_never_raises_on_pathological_input():
    assert parse_dsml_tool_calls("<｜DSML｜" * 500) == []


# ── Response-level recovery ───────────────────────────────────


def test_recover_fills_tool_calls_and_cleans_thinking():
    response = LLMResponse(
        content="",
        thinking=f"先确认目录。\n{DSML_SINGLE}\n马上执行。",
        finish_reason="stop",
    )

    names = recover_dsml_tool_calls(response)

    assert names == ["bash"]
    assert response.finish_reason == "tool_calls"
    assert response.tool_calls is not None and len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id.startswith("call_")
    assert call.type == "function"
    assert call.function.name == "bash"
    assert call.function.arguments == {"command": "ls -la"}
    # Parsed DSML markup is removed; surrounding reasoning text is kept.
    assert response.thinking is not None
    assert "DSML" not in response.thinking
    assert "先确认目录。" in response.thinking
    assert "马上执行。" in response.thinking


def test_recover_multiple_invokes_generate_unique_ids():
    response = LLMResponse(content="", thinking=DSML_MULTI, finish_reason="stop")

    names = recover_dsml_tool_calls(response)

    assert names == ["bash", "read_file"]
    ids = [call.id for call in response.tool_calls or []]
    assert len(ids) == len(set(ids)) == 2
    assert all(call.id.startswith("call_") for call in response.tool_calls or [])


def test_recover_from_content_when_thinking_has_no_dsml():
    response = LLMResponse(content=DSML_SINGLE, thinking=None, finish_reason="stop")

    names = recover_dsml_tool_calls(response)

    assert names == ["bash"]
    assert response.tool_calls[0].function.arguments == {"command": "ls -la"}
    assert response.content == ""


def test_recover_prefers_thinking_over_content():
    response = LLMResponse(
        content='<｜DSML｜invoke name="read_file">\n'
        '<｜DSML｜parameter name="path" string="true">/x</｜DSML｜parameter>\n'
        "</｜DSML｜invoke>",
        thinking=DSML_SINGLE,
        finish_reason="stop",
    )

    names = recover_dsml_tool_calls(response)

    assert names == ["bash"]
    # Content is untouched when thinking already yielded valid calls.
    assert "read_file" in response.content


def test_recover_leaves_response_untouched_without_valid_dsml():
    response = LLMResponse(
        content="",
        thinking='<｜DSML｜invoke name="bash">\n<｜DSML｜parameter name="command" string="true">ls\n',
        finish_reason="stop",
    )

    assert recover_dsml_tool_calls(response) == []
    assert response.tool_calls is None
    assert response.finish_reason == "stop"
    assert "DSML" in (response.thinking or "")


# ── Hook-level gating (HookManager + DsmlToolCallRecoveryHook) ──


async def _fire(hook: DsmlToolCallRecoveryHook, response: LLMResponse) -> None:
    mgr = HookManager([hook])
    await mgr.fire_llm_response(response=response)


@pytest.mark.asyncio
async def test_hook_recovers_sensenova_response_in_place():
    response = LLMResponse(
        content="",
        thinking=f"思考一下。\n{DSML_SINGLE}",
        finish_reason="stop",
        provider_response_id="chatcmpl-test",
    )
    hook = DsmlToolCallRecoveryHook(model_getter=lambda: "sensenova-flash-284b-vision-v16")

    await _fire(hook, response)

    assert response.finish_reason == "tool_calls"
    assert response.tool_calls is not None and len(response.tool_calls) == 1
    assert response.tool_calls[0].id.startswith("call_")
    assert response.tool_calls[0].function.name == "bash"
    assert response.tool_calls[0].function.arguments == {"command": "ls -la"}
    assert "DSML" not in (response.thinking or "")
    assert "思考一下。" in (response.thinking or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"finish_reason": "length"},
    {"finish_reason": "max_tokens"},
    {"finish_reason": "tool_argument_limit"},
    {"finish_reason": "provider_stale"},
    {"finish_reason": "content_filter"},
    {"truncated_tool_calls": [{"name": "bash"}]},
    {"oversized_tool_calls": [{"name": "write_file", "arguments_len": 10000}]},
    {"stream_dropped_mid_tool": True},
])
async def test_hook_preserves_provider_failure_and_truncation(fields):
    response = LLMResponse(**{
        "content": "", "thinking": DSML_SINGLE, "finish_reason": "stop", **fields,
    })
    original = response.model_copy(deep=True)
    await _fire(DsmlToolCallRecoveryHook(model_getter=lambda: "sensenova-test"), response)
    assert response == original


@pytest.mark.asyncio
async def test_hook_ignores_non_sensenova_model():
    response = LLMResponse(content="", thinking=DSML_SINGLE, finish_reason="stop")
    hook = DsmlToolCallRecoveryHook(model_getter=lambda: "deepseek-v4")

    await _fire(hook, response)

    assert response.tool_calls is None
    assert response.finish_reason == "stop"
    assert response.thinking == DSML_SINGLE


@pytest.mark.asyncio
async def test_hook_ignores_response_with_tool_calls_or_content():
    hook = DsmlToolCallRecoveryHook(model_getter=lambda: "sensenova-flash-284b-vision-v16")

    with_tools = LLMResponse(
        content="",
        thinking=DSML_SINGLE,
        tool_calls=[ToolCall(id="t0", type="function",
                             function=FunctionCall(name="echo", arguments={"text": "x"}))],
        finish_reason="tool_calls",
    )
    await _fire(hook, with_tools)
    assert [c.id for c in with_tools.tool_calls] == ["t0"]
    assert with_tools.thinking == DSML_SINGLE

    with_content = LLMResponse(content="正常答复", thinking=DSML_SINGLE, finish_reason="stop")
    await _fire(hook, with_content)
    assert with_content.tool_calls is None
    assert with_content.thinking == DSML_SINGLE


@pytest.mark.asyncio
async def test_hook_leaves_malformed_dsml_untouched_and_never_raises():
    hook = DsmlToolCallRecoveryHook(model_getter=lambda: "sensenova-flash-284b-vision-v16")

    malformed = LLMResponse(
        content="",
        thinking='<｜DSML｜invoke name="bash">\n<｜DSML｜parameter name="command" string="true">ls\n',
        finish_reason="stop",
    )
    await _fire(hook, malformed)
    assert malformed.tool_calls is None
    assert malformed.finish_reason == "stop"

    garbage = LLMResponse(content="", thinking="<｜DSML｜" * 100, finish_reason="stop")
    await _fire(hook, garbage)
    assert garbage.tool_calls is None


@pytest.mark.asyncio
async def test_hook_without_model_source_is_inert():
    """config.yaml-loaded hooks (no-arg construction) never match the gate."""
    response = LLMResponse(content="", thinking=DSML_SINGLE, finish_reason="stop")
    hook = DsmlToolCallRecoveryHook()

    await _fire(hook, response)

    assert response.tool_calls is None
    assert response.thinking == DSML_SINGLE


@pytest.mark.asyncio
async def test_hook_reads_model_from_response_field_when_present():
    class ResponseWithModel(LLMResponse):
        model: str = ""

    response = ResponseWithModel(
        content="", thinking=DSML_SINGLE, finish_reason="stop",
        model="sensenova-flash-284b-vision-v16",
    )
    hook = DsmlToolCallRecoveryHook()

    await _fire(hook, response)

    assert response.tool_calls is not None
    assert response.tool_calls[0].function.name == "bash"


# ── End-to-end loop integration ───────────────────────────────


class CountingEchoTool(Tool):
    def __init__(self):
        self.calls: list[str] = []

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
        self.calls.append(text)
        return ToolResult(success=True, content=f"echo:{text}")


class MockLLM:
    """Deterministic LLM that replays pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse], model: str = ""):
        self._responses = list(responses)
        self._idx = 0
        self.model = model

    async def generate_stream(self, messages, tools=None, **_):
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


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


async def _collect(gen) -> list:
    return [event async for event in gen]


def _echo_call(text: str) -> ToolCall:
    return ToolCall(
        id="t0",
        type="function",
        function=FunctionCall(name="echo", arguments={"text": text}),
    )


_DSML_ECHO = (
    "<｜DSML｜tool_calls>\n"
    '<｜DSML｜invoke name="echo">\n'
    '<｜DSML｜parameter name="text" string="true">hello-dsml</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜tool_calls>"
)


def _scripted_responses(dsml_response: LLMResponse) -> list[LLMResponse]:
    return [
        # Step 1: a normal visible tool call so visible_tool_call_total > 0,
        # which is the precondition of the empty-final-answer retry path.
        LLMResponse(content="", tool_calls=[_echo_call("first")], finish_reason="tool"),
        # Step 2: the channel-misrouted response under test.
        dsml_response,
        # Step 3: the final answer.
        LLMResponse(content="结论：完成。", finish_reason="stop"),
    ]


@pytest.mark.asyncio
async def test_loop_recovers_sensenova_dsml_into_tool_execution():
    echo = CountingEchoTool()
    llm = MockLLM(
        _scripted_responses(
            LLMResponse(content="", thinking=_DSML_ECHO, finish_reason="stop")
        ),
        model="sensenova-flash-284b-vision-v16",
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo},
            max_steps=6,
        )
    )

    # The recovered call went through the normal tool execution path …
    assert echo.calls == ["first", "hello-dsml"]
    tool_results = [e for e in events if isinstance(e, ToolCallResult)]
    assert any("echo:hello-dsml" in getattr(e, "content", "") for e in tool_results)
    # … instead of the empty-final-answer retry/error path.
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert not any("produced no visible final answer" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].stop_reason == StopReason.END_TURN
    assert done[-1].final_content == "结论：完成。"


@pytest.mark.asyncio
async def test_loop_ignores_dsml_for_non_sensenova_model():
    echo = CountingEchoTool()
    llm = MockLLM(
        _scripted_responses(
            LLMResponse(content="", thinking=_DSML_ECHO, finish_reason="stop")
        ),
        model="deepseek-v4",
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo},
            max_steps=6,
        )
    )

    # Behavior is unchanged: the DSML text is not executed and the existing
    # empty-final-answer retry fires instead.
    assert echo.calls == ["first"]
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("produced no visible final answer" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "结论：完成。"


@pytest.mark.asyncio
async def test_loop_falls_back_to_retry_when_dsml_is_malformed():
    echo = CountingEchoTool()
    llm = MockLLM(
        _scripted_responses(
            LLMResponse(
                content="",
                thinking='<｜DSML｜invoke name="echo">\n'
                '<｜DSML｜parameter name="text" string="true">never-closed\n',
                finish_reason="stop",
            )
        ),
        model="sensenova-flash-284b-vision-v16",
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo},
            max_steps=6,
        )
    )

    assert echo.calls == ["first"]
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("produced no visible final answer" in e.content for e in injected)
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "结论：完成。"


@pytest.mark.asyncio
async def test_loop_untouched_when_sensenova_returns_normal_tool_calls():
    echo = CountingEchoTool()
    llm = MockLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[_echo_call("organic")],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ],
        model="sensenova-flash-284b-vision-v16",
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo},
            max_steps=4,
        )
    )

    assert echo.calls == ["organic"]
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "done"


@pytest.mark.asyncio
async def test_user_hooks_observe_the_recovered_response():
    """The built-in recovery hook is registered ahead of user hooks."""

    observed: list[LLMResponse] = []

    class Observer:
        async def on_llm_response(self, *, response):
            observed.append(response.model_copy(deep=True))

    echo = CountingEchoTool()
    llm = MockLLM(
        _scripted_responses(
            LLMResponse(content="", thinking=_DSML_ECHO, finish_reason="stop")
        ),
        model="sensenova-flash-284b-vision-v16",
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"echo": echo},
            max_steps=6,
            hooks=[Observer()],
        )
    )

    assert echo.calls == ["first", "hello-dsml"]
    # The second observed response already carries the recovered tool call.
    assert len(observed) >= 2
    assert observed[1].tool_calls is not None
    assert observed[1].tool_calls[0].function.name == "echo"
    done = [e for e in events if isinstance(e, DoneEvent)]
    assert done[-1].final_content == "结论：完成。"
