"""Tests for ToolResult.followup_user_content -> main-model injection.

A tool can hand multimodal content (e.g. vision_review's native strategy) to
the main model. The loop appends it as ONE follow-up user message at the next
step boundary — after every tool result is in place, before the next model
call — so tool-call/result closure is never interleaved with it.
"""

import asyncio

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, InjectedMessageEvent
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


class MockLLM:
    def __init__(self, responses):
        self._responses = list(responses)
        self._idx = 0
        self.seen_messages = []

    async def generate_stream(self, messages, tools=None, **_):
        # Snapshot a shallow copy of history at each call for later assertions.
        self.seen_messages.append(list(messages))
        resp = self._responses[self._idx]
        self._idx += 1
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


_IMAGE_BLOCKS = [
    {"type": "text", "text": "Inspect the following screenshot(s)."},
    {"type": "text", "text": "Image 1: slide.png"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
]


class AttachTool(Tool):
    @property
    def name(self):
        return "attach"

    @property
    def description(self):
        return "Attaches images to the main model"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(
            success=True,
            content="Attached 1 image(s) for direct inspection.",
            followup_user_content=list(_IMAGE_BLOCKS),
        )


class PlainTool(Tool):
    @property
    def name(self):
        return "plain"

    @property
    def description(self):
        return "Returns text only"

    @property
    def parameters(self):
        return {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(success=True, content="ok")


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


async def _collect(gen):
    return [ev async for ev in gen]


def _call(name):
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id="t1", type="function", function=FunctionCall(name=name, arguments={}))
        ],
        finish_reason="tool",
    )


@pytest.mark.asyncio
async def test_followup_user_content_injected_as_user_message_after_tool_result():
    msgs = _msgs()
    llm = MockLLM([_call("attach"), LLMResponse(content="done", finish_reason="stop")])

    events = await _collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"attach": AttachTool()}, max_steps=5)
    )

    # A user message carrying exactly the follow-up blocks was appended.
    followup = [
        m
        for m in msgs
        if m.role == "user" and isinstance(m.content, list) and m.content == _IMAGE_BLOCKS
    ]
    assert len(followup) == 1

    # Ordering: it comes after the tool result, not before it.
    roles = [m.role for m in msgs]
    tool_idx = roles.index("tool")
    followup_idx = msgs.index(followup[0])
    assert followup_idx > tool_idx

    # The second model call sees the follow-up user message in its history.
    assert any(
        m.role == "user" and m.content == _IMAGE_BLOCKS for m in llm.seen_messages[1]
    )

    # A hidden injected-message event is emitted for observability.
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert any("attached" in e.content.lower() for e in injected)

    done = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done) == 1
    assert done[0].final_content == "done"


@pytest.mark.asyncio
async def test_no_followup_content_appends_no_extra_user_message():
    msgs = _msgs()
    llm = MockLLM([_call("plain"), LLMResponse(content="done", finish_reason="stop")])

    await _collect(
        run_agent_loop(llm=llm, messages=msgs, tools={"plain": PlainTool()}, max_steps=5)
    )

    # No user message with list content was injected (zero impact).
    assert not any(m.role == "user" and isinstance(m.content, list) for m in msgs)
