"""Tests for bounded recovery from announced work without tool calls."""

from __future__ import annotations

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, InjectedMessageEvent, StopReason
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.turn_continuation import (
    TurnContinuationController,
    announces_unfinished_action,
)


@pytest.mark.parametrize(
    "text",
    [
        "我会创建一个网页小游戏，完成检查，然后启动服务并提供链接。",
        "检查完成。接下来我会修改文件并运行测试。",
        "I'll now update the implementation and run the tests.",
        "Found the config. Let me now edit it.",
    ],
)
def test_announced_action_is_detected(text: str) -> None:
    assert announces_unfinished_action(text)


@pytest.mark.parametrize(
    "text",
    [
        "文件已创建，测试通过，链接如下：http://127.0.0.1:8000/。",
        "如果你愿意，我可以继续优化这个页面。",
        "请先告诉我你希望使用哪个端口？",
        "The answer is 42.",
        "",
    ],
)
def test_normal_final_or_user_wait_is_not_detected(text: str) -> None:
    assert not announces_unfinished_action(text)


def test_controller_requires_tools_budget_and_normal_finish() -> None:
    cases = (
        {"tools_available": False},
        {"step": 1, "max_steps": 2},
        {"finish_reason": "length"},
        {"cancelled": True},
    )
    for override in cases:
        controller = TurnContinuationController()
        arguments = {
            "content": "我会创建文件并运行测试。",
            "finish_reason": "stop",
            "tools_available": True,
            "step": 0,
            "max_steps": 5,
            "cancelled": False,
        }
        arguments.update(override)
        assert controller.evaluate(**arguments) is None


def test_controller_is_bounded_to_two_continuations() -> None:
    controller = TurnContinuationController(max_continuations=2)
    arguments = {
        "content": "我会创建文件并运行测试。",
        "finish_reason": "stop",
        "tools_available": True,
        "step": 0,
        "max_steps": 5,
        "cancelled": False,
    }

    first = controller.evaluate(**arguments)
    second = controller.evaluate(**arguments)

    assert first is not None and first.attempt == 1
    assert second is not None and second.attempt == 2
    assert controller.evaluate(**arguments) is None


class MockLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        response = self._responses[self.calls]
        self.calls += 1
        if response.content:
            yield StreamEvent(type="text", delta=response.content)
        yield StreamEvent(
            type="finish",
            finish_reason=response.finish_reason,
            tool_calls=response.tool_calls,
        )


class SearchTool(Tool):
    calls = 0

    @property
    def name(self) -> str:
        return "search_files"

    @property
    def description(self) -> str:
        return "Search for files."

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self) -> ToolResult:
        self.calls += 1
        return ToolResult(success=True, content="No matches found.")


async def _collect_events(generator):
    return [event async for event in generator]


@pytest.mark.asyncio
async def test_loop_continues_after_model_announces_work_without_tool_call() -> None:
    search_call = ToolCall(
        id="search-1",
        type="function",
        function=FunctionCall(name="search_files", arguments={}),
    )
    llm = MockLLM(
        [
            LLMResponse(
                content="我先检查当前目录。",
                finish_reason="tool_calls",
                tool_calls=[search_call],
            ),
            LLMResponse(
                content=(
                    "我会创建一个可直接玩的单文件网页小游戏，完成基础交互与"
                    "本地检查，然后实际启动本地服务并提供可点击链接。"
                ),
                finish_reason="stop",
            ),
            LLMResponse(content="文件已创建，服务已启动并验证通过。", finish_reason="stop"),
        ]
    )
    tool = SearchTool()
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="创建 game-hub/index.html 并启动服务。"),
    ]

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={tool.name: tool},
            max_steps=5,
        )
    )

    assert llm.calls == 3
    assert tool.calls == 1
    injected = [event for event in events if isinstance(event, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].user_visible is False
    assert "execute the announced work" in injected[0].content
    assert messages[-2].role == "user"
    assert messages[-1].role == "assistant"
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.stop_reason is StopReason.END_TURN


@pytest.mark.asyncio
async def test_loop_continuation_is_bounded() -> None:
    llm = MockLLM(
        [
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
            LLMResponse(content="我会创建文件并运行测试。", finish_reason="stop"),
        ]
    )
    tool = SearchTool()

    events = await _collect_events(
        run_agent_loop(
            llm=llm,
            messages=[
                Message(role="system", content="system"),
                Message(role="user", content="创建文件并运行测试。"),
            ],
            tools={tool.name: tool},
            max_steps=5,
        )
    )

    assert llm.calls == 3
    assert len(
        [event for event in events if isinstance(event, InjectedMessageEvent)]
    ) == 2
    done = [event for event in events if isinstance(event, DoneEvent)][-1]
    assert done.stop_reason is StopReason.END_TURN
