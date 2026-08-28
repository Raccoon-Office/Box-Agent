"""End-to-end coverage for the public Agent context-compaction path."""

from __future__ import annotations

import pytest

from box_agent.agent import Agent
from box_agent.events import DoneEvent, StopReason, SummarizationEvent
from box_agent.schema import LLMResponse, Message, StreamEvent


class CompactionE2ELLM:
    """Exercise one non-streaming summary call followed by a normal stream."""

    def __init__(self) -> None:
        self.summary_messages: list[Message] = []
        self.normal_messages: list[Message] = []

    async def generate(self, messages, tools=None, **_kwargs):
        assert tools is None
        self.summary_messages = list(messages)
        return LLMResponse(
            content=(
                "<summary>"
                "1. Primary Request and Intent:\nContinue the compaction E2E.\n\n"
                "6. All User Messages:\n"
                "- old user request\n"
                "- latest user request\n\n"
                "8. Current Work:\nContext compaction is being verified."
                "</summary>"
            ),
            finish_reason="stop",
        )

    async def generate_stream(self, messages, tools=None, **_kwargs):
        self.normal_messages = list(messages)
        yield StreamEvent(type="text", delta="E2E resumed answer")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_agent_compacts_above_derived_limit_and_resumes_from_synthetic_user_message(
    tmp_path,
) -> None:
    llm = CompactionE2ELLM()
    agent = Agent(
        llm_client=llm,
        system_prompt="E2E system prompt",
        tools=[],
        max_steps=2,
        workspace_dir=str(tmp_path),
        token_limit=104_400,
        deferred_mcp_loading_enabled=False,
    )
    old_user = Message(role="user", content="old user request")
    large_execution = Message(role="assistant", content="x" * 420_000)
    latest_user = Message(role="user", content="latest user request")
    agent.messages.extend([old_user, large_execution, latest_user])
    original_prefix = list(agent.messages)

    events = [event async for event in agent.run_events()]

    compaction = next(event for event in events if isinstance(event, SummarizationEvent))
    assert compaction.token_limit == 104_400
    assert compaction.estimated_tokens >= 104_400
    assert compaction.mode == "summary"
    assert compaction.summary_calls == 1

    assert len(llm.summary_messages) == len(original_prefix) + 1
    assert all(
        sent is original
        for sent, original in zip(llm.summary_messages[:-1], original_prefix)
    )
    assert llm.summary_messages[-1].role == "user"

    compacted_summary = llm.normal_messages[1]
    assert compacted_summary.role == "user"
    assert "Summary:\n1. Primary Request and Intent:" in str(
        compacted_summary.content
    )
    assert "<summary>" not in str(compacted_summary.content)
    assert "</summary>" not in str(compacted_summary.content)
    assert "pick up the last task as if the break never happened." in str(
        compacted_summary.content
    )
    assert old_user not in llm.normal_messages
    assert large_execution not in llm.normal_messages
    assert latest_user in llm.normal_messages

    assert agent.messages[-1].role == "assistant"
    assert agent.messages[-1].content == "E2E resumed answer"
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == StopReason.END_TURN
