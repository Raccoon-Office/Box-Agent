"""Regression tests for backwards-compatible tool aliases."""

from __future__ import annotations

import pytest

from box_agent.events import ErrorEvent, ToolCallResult, ToolCallStart
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.bash_tool import BashTool
from box_agent.tools.base import Tool, ToolResult, build_tool_name_index
from box_agent.tools.file import ReadTool
from box_agent.tools.file.jsonl_tool import JsonlQueryTool
from box_agent.tools.file_tools import EditTool, WriteTool
from box_agent.tools.image_generation_tool import GenerateImageTool
from box_agent.tools.request_user_input_tool import RequestUserInputTool
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.tools.sub_agent_tool import (
    SubAgentTool,
    _PermissionGatedBashTool,
    _WriteScopedTool,
)


class SequenceLLM:
    """Deterministic LLM that records the canonical tools offered per step."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.offered_names: list[list[str]] = []

    async def generate_stream(self, messages, tools=None, **_):
        self.offered_names.append([tool.name for tool in tools or []])
        response = self._responses.pop(0)
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(
            type="finish",
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
            usage=response.usage,
        )


class RecordingTool(Tool):
    aliases = ("legacy_echo",)

    def __init__(self, name: str = "echo") -> None:
        self._name = name
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Record a text value."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    async def execute(self, text: str) -> ToolResult:
        self.calls.append(text)
        return ToolResult(success=True, content=f"echo:{text}")


class NamedTool(Tool):
    def __init__(self, name: str, aliases: tuple[str, ...] = ()) -> None:
        self._name = name
        self.aliases = aliases

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Test tool {self._name}."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        return ToolResult(success=True, content=self._name)


def _messages() -> list[Message]:
    return [
        Message(role="system", content="system"),
        Message(role="user", content="run the tool"),
    ]


def _tool_call(call_id: str, name: str, text: str = "hello") -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments={"text": text}),
    )


def _empty_tool_call(call_id: str, name: str) -> ToolCall:
    return ToolCall(
        id=call_id,
        type="function",
        function=FunctionCall(name=name, arguments={}),
    )


async def _collect(generator) -> list:
    return [event async for event in generator]


def test_aliases_are_not_serialized_in_provider_tool_schemas() -> None:
    tool = RecordingTool()

    assert tool.to_schema() == {
        "name": "echo",
        "description": "Record a text value.",
        "input_schema": tool.parameters,
    }
    assert tool.to_openai_schema() == {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Record a text value.",
            "parameters": tool.parameters,
        },
    }


def test_write_scoped_tool_preserves_wrapped_tool_aliases(tmp_path) -> None:
    wrapped = _WriteScopedTool(RecordingTool(), str(tmp_path), (".",))

    assert build_tool_name_index([wrapped])["legacy_echo"] is wrapped


def test_permission_gated_tool_preserves_wrapped_tool_aliases() -> None:
    wrapped = _PermissionGatedBashTool(
        RecordingTool(),
        title=None,
        scopes=None,
    )

    assert build_tool_name_index([wrapped])["legacy_echo"] is wrapped


def test_builtin_compatibility_names_resolve_to_equivalent_tools() -> None:
    tools = [
        tool_class.__new__(tool_class)
        for tool_class in (
            ReadTool,
            WriteTool,
            EditTool,
            BashTool,
            GenerateImageTool,
            SubAgentTool,
            RequestUserInputTool,
            GetSkillTool,
        )
    ]

    index = build_tool_name_index(tools)

    expected_canonical_names = {
        "read": "read_file",
        "write": "write_file",
        "edit": "edit_file",
        "exec": "bash",
        "terminal": "bash",
        "image_generate": "generate_image",
        "image-generate": "generate_image",
        "sessions_spawn": "sub_agent",
        "sessions-spawn": "sub_agent",
        "delegate_task": "sub_agent",
        "delegate-task": "sub_agent",
        "clarify": "request_user_input",
        "skill_view": "get_skill",
        "skill-view": "get_skill",
    }
    assert {
        call_name: index[call_name].name
        for call_name in expected_canonical_names
    } == expected_canonical_names


def test_distinct_read_subclass_does_not_inherit_read_file_alias() -> None:
    read_tool = ReadTool.__new__(ReadTool)
    jsonl_tool = JsonlQueryTool.__new__(JsonlQueryTool)

    index = build_tool_name_index([read_tool, jsonl_tool])

    assert index["read"] is read_tool
    assert index["query_jsonl"] is jsonl_tool
    assert jsonl_tool.aliases == ()


def test_underscore_names_automatically_accept_hyphenated_calls() -> None:
    tool = NamedTool("read_file", aliases=("legacy_reader_name",))

    index = build_tool_name_index([tool])

    assert index["read-file"] is tool
    assert index["legacy-reader-name"] is tool


@pytest.mark.asyncio
async def test_alias_executes_canonical_tool_without_being_offered_to_model() -> None:
    tool = RecordingTool()
    messages = _messages()
    llm = SequenceLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[_tool_call("alias-call", "legacy_echo")],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={tool.name: tool},
            max_steps=3,
        )
    )

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert llm.offered_names[0] == ["echo"]
    assert tool.calls == ["hello"]
    assert [event.tool_name for event in starts] == ["echo"]
    assert [event.tool_name for event in results] == ["echo"]
    assert messages[2].tool_calls[0].function.name == "legacy_echo"


@pytest.mark.asyncio
async def test_hyphenated_name_executes_underscore_canonical_tool() -> None:
    tool = RecordingTool(name="echo_value")
    llm = SequenceLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[_tool_call("hyphenated-call", "echo-value")],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_messages(),
            tools={tool.name: tool},
            max_steps=3,
        )
    )

    starts = [event for event in events if isinstance(event, ToolCallStart)]
    assert llm.offered_names[0] == ["echo_value"]
    assert tool.calls == ["hello"]
    assert [event.tool_name for event in starts] == ["echo_value"]


@pytest.mark.asyncio
async def test_alias_is_canonicalized_before_duplicate_calls_are_removed() -> None:
    tool = RecordingTool()
    llm = SequenceLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    _tool_call("alias-call", "legacy_echo"),
                    _tool_call("canonical-call", "echo"),
                ],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_messages(),
            tools={tool.name: tool},
            max_steps=3,
        )
    )

    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert tool.calls == ["hello"]
    assert [(event.tool_name, event.user_visible) for event in results] == [
        ("echo", True),
        ("echo", False),
    ]


@pytest.mark.asyncio
async def test_alias_is_canonicalized_before_empty_argument_loop_detection() -> None:
    tool = RecordingTool()
    llm = SequenceLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[_empty_tool_call("alias-call", "legacy_echo")],
                finish_reason="tool",
            ),
            LLMResponse(
                content="",
                tool_calls=[_empty_tool_call("canonical-call", "echo")],
                finish_reason="tool",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_messages(),
            tools={tool.name: tool},
            max_steps=3,
        )
    )

    error = next(event for event in events if isinstance(event, ErrorEvent))
    assert "2x in a row" in error.message
    assert "echo" in error.message


@pytest.mark.asyncio
async def test_conflicting_aliases_fail_before_the_model_is_called() -> None:
    first = NamedTool("first", aliases=("legacy",))
    second = NamedTool("second", aliases=("legacy",))
    llm = SequenceLLM([LLMResponse(content="done", finish_reason="stop")])

    with pytest.raises(ValueError, match="legacy"):
        await _collect(
            run_agent_loop(
                llm=llm,
                messages=_messages(),
                tools={first.name: first, second.name: second},
                max_steps=1,
            )
        )

    assert llm.offered_names == []


@pytest.mark.asyncio
async def test_generated_hyphenated_alias_conflict_fails_before_model_call() -> None:
    underscored = NamedTool("read_file")
    hyphenated = NamedTool("read-file")
    llm = SequenceLLM([LLMResponse(content="done", finish_reason="stop")])

    with pytest.raises(ValueError, match="read-file"):
        await _collect(
            run_agent_loop(
                llm=llm,
                messages=_messages(),
                tools={
                    underscored.name: underscored,
                    hyphenated.name: hyphenated,
                },
                max_steps=1,
            )
        )

    assert llm.offered_names == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aliases",
    [
        ("",),
        ("tool",),
        ("legacy", "legacy"),
    ],
    ids=["empty", "canonical-name", "duplicate"],
)
async def test_invalid_alias_declarations_fail_before_the_model_is_called(
    aliases: tuple[str, ...],
) -> None:
    tool = NamedTool("tool", aliases=aliases)
    llm = SequenceLLM([LLMResponse(content="done", finish_reason="stop")])

    with pytest.raises(ValueError, match="alias"):
        await _collect(
            run_agent_loop(
                llm=llm,
                messages=_messages(),
                tools={tool.name: tool},
                max_steps=1,
            )
        )

    assert llm.offered_names == []
