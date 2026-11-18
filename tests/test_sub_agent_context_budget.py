"""Exercise complete-request budgets through the real isolated child loop."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.context_input import DefaultContextEngine
from box_agent.config import AgentConfig, LLMConfig, derive_context_token_limit
from box_agent.events import DoneEvent, StopReason
from box_agent.kernel.context_engine import (
    _SUMMARY_MARKER, _validate_transient_followup_result, request_input_tokens,
)
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.file_tools import ReadTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.sub_agent_tool import SubAgentTool


class BudgetModel:
    context_window = 1_000_000
    max_output_tokens = 100_000
    model = "budget-fixture"
    provider = "fixture"
    estimate_text_tokens = LLMClient.estimate_text_tokens

    def __init__(self, *, followup=False):
        self.followup = followup
        self.summary_calls = 0
        self.request_sizes = []

    async def generate(self, **kwargs):
        return LLMResponse(content='{"continue":false}', finish_reason="stop")

    async def generate_stream(self, messages, tools=None, **kwargs):
        if kwargs.get("call_kind") == "context_summary":
            self.summary_calls += 1
            text = "<summary>" + ("Task progress. " * 580)[:8000] + "</summary>"
        else:
            self.request_sizes.append(request_input_tokens(messages, tools, llm=self))
            if self.followup and len(self.request_sizes) == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="followup", type="function",
                    function=FunctionCall(name="request_followup", arguments={}),
                )])
                return
            text = "Completed verification."
        yield StreamEvent(type="text", delta=text)
        yield StreamEvent(type="finish", finish_reason="stop")


class FollowupTool(Tool):
    transient_followup_allowed = True

    @property
    def name(self):
        return "request_followup"

    @property
    def description(self):
        return "Provide content for the next request only."

    @property
    def parameters(self):
        return {"type": "object", "properties": {}, "additionalProperties": False}

    async def execute(self):
        return ToolResult(success=True, content="Ready.", transient_followup_content=[
            {"type": "text", "text": "T" * 27000},
        ])


async def run_child(model, history, tools=None, *, session_log=None, token_limit=50000):
    tools = tools or {}
    # Recovery fixtures deliberately exercise a smaller explicit input cap.
    child = SubAgentTool(llm=model, parent_tools=tools, token_limit=token_limit)
    return await child._run_general_loop(
        llm=model, messages=history, child_tools=tools,
        max_steps=3, max_tool_calls=3, diagnostic={"strategy": "general_loop"},
        queue=None, parent_tool_call_id="budget-test", task_preview="verify",
        sub_agent_id="budget-child", title="verify",
        session_log=session_log,
    )


def child_history(execution_chars=30000):
    return [
        Message(role="system", content="S" * 64000),
        Message(role="user", content="old task"),
        Message(role="assistant", content="X" * execution_chars),
        Message(role="user", content="Check the supplied content and finish."),
    ]


@pytest.mark.parametrize("blocks", [
    [{"type": "text", "text": '中文与转义"\\\n' * 40}],
    [{"type": "text", "text": "image"},
     {"type": "input_image", "media_type": "image/png", "data": "aGVsbG8=", "width": 1, "height": 1}],
])
def test_followup_admission_uses_the_complete_request_counter(blocks):
    model = BudgetModel()
    result, accepted, tokens = _validate_transient_followup_result(
        result=ToolResult(success=True, transient_followup_content=blocks),
        tool=FollowupTool(), llm=model, token_limit=50000, pending_token_estimate=0,
    )
    assert result.success and accepted == blocks
    assert tokens == request_input_tokens(
        [Message(role="user", source="runtime", content=blocks)], {}, llm=model,
    )
    history = [Message(role="system", content="system"), Message(role="user", content="task")]
    transient = Message(role="user", source="runtime", content=blocks)
    expected = request_input_tokens([*history, transient], {}, llm=model) + 100
    engine = DefaultContextEngine(llm=model)
    for limit in (expected - 1, expected):
        projection = engine.prepare_request(
            history, prepared_tools=prepare_tools([]), token_limit=limit,
            transient_message=transient, transient_tokens=tokens + 100,
        )
        assert projection.total_input_tokens == expected
        assert projection.budget_blocked is (expected > limit)


async def test_child_compacts_history_with_the_full_followup_budget():
    model = BudgetModel(followup=True)
    history = child_history()
    system, latest_user = history[0], history[-1]
    result = await run_child(model, history, {"request_followup": FollowupTool()})
    assert result.success, result.error
    assert model.summary_calls == 1
    assert len(model.request_sizes) == 2 and max(model.request_sizes) <= 50000
    assert system in history and latest_user in history
    assert not any(isinstance(message.content, list) for message in history)
    assert any(message.tool_calls and message.tool_calls[0].id == "followup" for message in history)
    assert any(message.role == "tool" and message.tool_call_id == "followup" for message in history)


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("system_chars, overlay_chars, fits", [
    (64000, 30000, True), (64000, 50000, False), (83000, 14000, True),
])
async def test_child_tightens_budget_after_reprojecting_compacted_history(
    monkeypatch, tmp_path, system_chars, overlay_chars, fits, persisted,
):
    original_prepare = DefaultContextEngine._prepare_request
    projections = []

    def prepare(self, messages, **kwargs):
        # Request-only material can change when references/images are restored.
        if any(_SUMMARY_MARKER in str(message.content) for message in messages):
            kwargs["extra_messages"] = (*kwargs.get("extra_messages", ()),
                                        Message(role="user", source="runtime", content="R" * overlay_chars))
        projection = original_prepare(self, messages, **kwargs)
        projections.append(projection)
        return projection

    monkeypatch.setattr(DefaultContextEngine, "_prepare_request", prepare)
    model = BudgetModel()
    history = child_history(execution_chars=60000)
    history[0].content = "S" * system_chars
    system, latest_user = history[0], history[-1]
    session_log = None
    if persisted:
        session_log = SessionLog.create(tmp_path / "sessions", session_id="budget-child", cwd=tmp_path)
        session_log.append("turn/start", {"turn": 1})
        session_log.append_unlogged_messages(history[1:], turn=1, step=None)
    result = await run_child(model, history, session_log=session_log)
    assert result.success is fits, result.error
    assert 2 <= model.summary_calls <= 3
    assert system in history and latest_user in history
    assert projections[0].budget_blocked and projections[1].budget_blocked
    if fits:
        assert len(model.request_sizes) == 1 and model.request_sizes[0] <= 50000
        assert not projections[-1].blocked_reason
    else:
        assert model.request_sizes == []
        assert result.raw_output["error_code"] == "CONTEXT_INPUT_BUDGET_EXCEEDED"
    if persisted:
        reopened = SessionLog.open(tmp_path / "sessions", session_id="budget-child", cwd=tmp_path)
        try:
            assert latest_user in reopened.replay().messages
            assert sum(event["type"] == "compaction/end" for event in reopened.events) == model.summary_calls
        finally:
            reopened.close()


async def test_child_budget_retries_are_bounded_when_projection_never_fits(monkeypatch):
    original_prepare = DefaultContextEngine._prepare_request
    projections = []

    def prepare(self, messages, **kwargs):
        projection = original_prepare(self, messages, **kwargs)
        # A changing projection keeps its deficit positive after each compaction.
        projections.append(projection)
        return replace(projection, blocked_reason="Model input exceeds the safe context budget.",
                       budget_blocked=True)

    monkeypatch.setattr(DefaultContextEngine, "_prepare_request", prepare)
    model = BudgetModel()
    result = await run_child(model, child_history(execution_chars=60000))
    assert not result.success and model.request_sizes == []
    assert 1 < model.summary_calls <= 3
    assert len(projections) <= 4
    assert result.raw_output["error_code"] == "CONTEXT_INPUT_BUDGET_EXCEEDED"


async def test_cancellation_after_reprojection_stops_before_another_summary(monkeypatch):
    original_prepare = DefaultContextEngine._prepare_request
    cancelled = False

    def prepare(self, messages, **kwargs):
        nonlocal cancelled
        if any(_SUMMARY_MARKER in str(message.content) for message in messages):
            kwargs["extra_messages"] = (Message(role="user", source="runtime", content="R" * 30000),)
            cancelled = True
        return original_prepare(self, messages, **kwargs)

    monkeypatch.setattr(DefaultContextEngine, "_prepare_request", prepare)
    model = BudgetModel()
    events = [event async for event in run_agent_loop(
        llm=model, messages=child_history(execution_chars=60000), tools={}, token_limit=50000,
        max_steps=1, is_cancelled=lambda: cancelled,
    )]
    assert model.summary_calls == 1 and model.request_sizes == []
    assert any(isinstance(event, DoneEvent) and event.stop_reason == StopReason.CANCELLED
               for event in events)


@pytest.fixture
def child_runs(monkeypatch):
    calls = []

    async def capture(**kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr("box_agent.runtime.run_agent_loop", capture)
    return calls


def test_child_budget_is_automatic_unless_explicitly_capped():
    assert AgentConfig().sub_agent_token_limit is None
    assert AgentConfig(sub_agent_token_limit=None).sub_agent_token_limit is None
    assert AgentConfig(sub_agent_token_limit=12345).sub_agent_token_limit == 12345


@pytest.mark.parametrize("window, output", [(1_000_000, 100_000), (128_000, 16_000), (32_000, 8_000)])
@pytest.mark.parametrize("cap", [None, 25000, 1_000_000])
async def test_child_loop_receives_its_model_budget_after_routing(child_runs, window, output, cap):
    model = BudgetModel()
    model.context_window, model.max_output_tokens = window, output
    tool = SubAgentTool(llm=model, parent_tools={}, token_limit=cap)
    result = await tool.execute(task="Summarize the assigned material", required_tools=[])
    expected = derive_context_token_limit(window, output)
    assert result.success, result.error
    assert child_runs[0]["token_limit"] == (expected if cap is None else min(expected, cap))
    assert tool._token_limit == cap


async def test_child_with_unknown_model_window_uses_main_configuration_default(child_runs):
    tool = SubAgentTool(llm=SimpleNamespace(), parent_tools={})
    result = await tool.execute(task="Summarize", required_tools=[])
    assert result.success, result.error
    assert child_runs[0]["token_limit"] == LLMConfig().context_token_limit


class RoutingBudgetModel(BudgetModel):
    def for_model(self, model, *, max_output_tokens=None):
        child = BudgetModel()
        child.model = model
        if max_output_tokens is not None:
            child.max_output_tokens = max_output_tokens
        return child


async def test_parallel_children_derive_separate_budgets_from_their_selected_models(child_runs):
    model = RoutingBudgetModel()
    model.auto_model_candidates = [
        {"model": "small", "contextWindow": 32000, "maxTokens": 8000,
         "tags": ["summary", "rewrite", "general", "chat", "fast"], "abilityLevel": 1},
        {"model": "large", "contextWindow": 1_000_000, "maxTokens": 100_000,
         "tags": ["code", "analysis", "reasoning"], "abilityLevel": 3},
    ]
    tool = SubAgentTool(llm=model, parent_tools={})
    results = await asyncio.gather(
        tool.execute(task="Summarize this short note", required_tools=[]),
        tool.execute(task="Analyze and debug the complex database code", required_tools=[]),
    )
    assert all(result.success for result in results)
    assert {call["llm"].model: call["token_limit"] for call in child_runs} == {
        "small": 21600, "large": 810000,
    }
    assert tool._token_limit is None
    assert (model.context_window, model.max_output_tokens) == (1_000_000, 100_000)


async def test_child_recomputes_budget_when_its_inherited_binding_changes(child_runs):
    model = BudgetModel()
    tool = SubAgentTool(llm=model, parent_tools={})
    assert (await tool.execute(task="First task", required_tools=[])).success
    model.context_window, model.max_output_tokens = 32000, 8000
    assert (await tool.execute(task="Second task", required_tools=[])).success
    assert [call["token_limit"] for call in child_runs] == [810000, 21600]


@pytest.mark.parametrize("window, output, fits", [(1_000_000, 100_000, True), (32000, 8000, False)])
async def test_skill_preflight_uses_the_routed_child_budget(tmp_path, child_runs, window, output, fits):
    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: review\ndescription: Review method\n---\n" + "R" * 110000)
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    model = RoutingBudgetModel()
    model.auto_model_candidates = [{"model": "routed", "contextWindow": window, "maxTokens": output,
                                    "tags": ["general"], "abilityLevel": 3}]
    tool = SubAgentTool(llm=model, parent_tools={})
    tool.set_skill_provider(lambda: loader)
    result = await tool.execute(task="Apply the delegated review method", skills=["review"], required_tools=[])
    assert result.success is fits, result.error
    if fits:
        assert child_runs[0]["token_limit"] == 810000
        assert "R" * 110000 in child_runs[0]["messages"][1].content
    else:
        assert not child_runs
        assert result.raw_output["code"] == "SKILL_REFERENCE_BUDGET_EXCEEDED"
        assert result.raw_output["token_limit"] == 21600


async def test_batch_files_checks_its_selected_model_budget_without_skills(tmp_path):
    (tmp_path / "input.txt").write_text("X" * 20000)
    model = BudgetModel()
    model.context_window, model.max_output_tokens = 8000, 1000
    model.generate = AsyncMock(return_value=LLMResponse(content="summary", finish_reason="stop"))
    tool = SubAgentTool(llm=model, parent_tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
                        workspace_dir=str(tmp_path))
    result = await tool.execute(task="Summarize this file", files=["input.txt"], required_tools=["read_file"])
    assert not result.success
    assert result.raw_output["code"] == "CONTEXT_INPUT_BUDGET_EXCEEDED"
    assert result.raw_output["token_limit"] == 6300
    model.generate.assert_not_called()


async def test_large_model_child_sends_input_above_fifty_thousand_without_compaction():
    from tests.test_main_input_budget import wire_llm

    async with wire_llm(context_window=1_000_000) as (model, requests):
        history = [Message(role="system", content="system"), Message(role="user", content="U" * 120000)]
        result = await run_child(model, history, token_limit=None)
        assert result.success, result.error
        assert len(requests) == 1
        assert requests[0]["messages"][-1]["content"] == "U" * 120000


async def test_large_model_batch_uses_the_derived_budget_for_skills_and_prefetched_content(tmp_path):
    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: review\ndescription: Review method\n---\n" + "R" * 110000)
    (tmp_path / "input.txt").write_text("X" * 60000)
    loader = SkillLoader(tmp_path)
    loader.discover_skills()
    model = BudgetModel()
    model.generate = AsyncMock(return_value=LLMResponse(content="summary", finish_reason="stop"))
    tool = SubAgentTool(llm=model, parent_tools={"read_file": ReadTool(workspace_dir=str(tmp_path))},
                        workspace_dir=str(tmp_path))
    tool.set_skill_provider(lambda: loader)
    result = await tool.execute(task="Apply the review method", skills=["review"],
                                files=["input.txt"], required_tools=["read_file"])
    assert result.success, result.error
    messages = model.generate.call_args.kwargs["messages"]
    assert 50000 < request_input_tokens(messages, None, llm=model) <= 810000
