"""Host time-budget notifications preserve task state and cancellation semantics."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.injections import InjectionKind
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


class Conn:
    def __init__(self):
        self.updates = []

    async def sessionUpdate(self, payload):
        self.updates.append(payload)


class CheckTool(Tool):
    name = "check_result"
    description = "Check the existing result"
    parameters = {"type": "object", "properties": {}}

    async def execute(self):
        return ToolResult(success=True, content="Existing file verified")


class Model:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if len(self.requests) == 1:
            self.started.set()
            await self.release.wait()
            yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[
                ToolCall(id="check-1", type="function", function=FunctionCall(
                    name="check_result", arguments={},
                )),
            ])
        else:
            yield StreamEvent(type="text", delta="Saved results; remaining work is documented.")
            yield StreamEvent(type="finish", finish_reason="stop")


def make_agent(tmp_path):
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(max_steps=4, workspace_dir=str(tmp_path), goal_autopilot_max_turns=2),
        tools=ToolsConfig(enable_sub_agent=False, enable_mcp=False),
    )
    model, conn = Model(), Conn()
    return BoxACPAgent(conn, config, model, [CheckTool()], "system"), model, conn


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_status", [None, "active", "complete"])
async def test_wrap_up_reaches_model_as_hidden_budget_and_keeps_unfinished_goal(tmp_path, goal_status):
    agent, model, conn = make_agent(tmp_path)
    meta = {"goal": {"objective": "Verify every deliverable", "status": "active"}} if goal_status else {}
    session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta=meta))
    state = agent._sessions[session.sessionId]
    pending = asyncio.create_task(agent.prompt(SimpleNamespace(
        sessionId=session.sessionId, prompt=[{"text": "Finish the report"}],
        field_meta={"turn_id": "turn-1"},
    )))
    try:
        await asyncio.wait_for(model.started.wait(), 5)
        params = {"sessionId": session.sessionId, "remainingSeconds": 600, "turnId": "turn-1"}
        assert await agent.extMethod("request_wrap_up", params) == {"ok": True}
        assert await agent.extMethod("request_wrap_up", params) == {"ok": True, "deduplicated": True}
        if goal_status == "complete":
            state.agent.complete_goal(evidence=["fixture checks passed"], progress="Finished")
        model.release.set()
        response = await asyncio.wait_for(pending, 5)
        assert len(model.requests) == 2, "wrap-up must suppress automatic goal continuations"
        budget = [m for m in model.requests[-1] if "host execution time budget" in str(m.content)]
        assert len(budget) == 1
        assert budget[0].source == "runtime"
        assert "600 seconds" in budget[0].content
        assert "bounded verification" in budget[0].content
        assert "Do not mark an unfinished goal complete" in budget[0].content
        assert "The user sent" not in budget[0].content
        assert "host execution time budget" not in str(conn.updates)
        incomplete = goal_status == "active"
        assert response.field_meta["wrapUp"] == {"requested": True, "goalIncomplete": incomplete}
        registry = json.loads((tmp_path / ".box-agent" / "task-registry" / "tasks" / f"{state.current_task_id}.json").read_text())
        assert registry["execution_status"] == ("incomplete" if incomplete else "completed")
        if incomplete:
            assert state.agent.goal.status == "active"
            assert response.stopReason == "max_turn_requests"
            assert response.field_meta["completed"] is False
            assert response.field_meta["runStatus"] == "incomplete"
            assert response.field_meta["messageDisplay"]["finalMessageIds"] == []
        else:
            assert response.stopReason == "end_turn"
            assert response.field_meta["completed"] is True
            resumed = await agent.prompt(SimpleNamespace(
                sessionId=session.sessionId, prompt=[{"text": "Continue"}],
            ))
            assert "wrapUp" not in resumed.field_meta
            assert not state.wrap_up_requested
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await agent.aclose()


@pytest.mark.asyncio
async def test_wrap_up_validates_active_turn_and_deduplicates_without_touching_user_injections(tmp_path):
    agent, _, _ = make_agent(tmp_path)
    try:
        session = await agent.newSession(SimpleNamespace(cwd=str(tmp_path), field_meta={}))
        state = agent._sessions[session.sessionId]
        params = {"sessionId": session.sessionId, "remainingSeconds": 600}
        assert await agent.extMethod("request_wrap_up", params) == {"error": "no_active_turn"}
        state.turn_active = True
        state.current_turn_id = "current"
        assert await agent.extMethod("request_wrap_up", {**params, "turnId": "old"}) == {"error": "turn_mismatch"}
        for remaining in (None, True, 0, -1, 1.5, "600", 10801):
            assert await agent.extMethod("request_wrap_up", {**params, "remainingSeconds": remaining}) == {"error": "invalid_remaining_seconds"}
        assert not state.wrap_up_requested
        assert state.inject_queue.empty()
        await agent.extMethod("inject", {"sessionId": session.sessionId, "text": "Keep the appendix", "injectionId": "user-1"})
        assert await agent.extMethod("request_wrap_up", params) == {"ok": True}
        assert await agent.extMethod("request_wrap_up", params) == {"ok": True, "deduplicated": True}
        history = []
        user = state.inject_queue.apply_next(history)[1]
        budget = state.inject_queue.apply_next(history)[1]
        assert user.user_visible is True
        assert budget.user_visible is False
        assert budget.kind == InjectionKind.BUDGET.value
        assert [message.source for message in history] == ["user", "runtime"]
        assert state.inject_queue.empty()
    finally:
        await agent.aclose()
