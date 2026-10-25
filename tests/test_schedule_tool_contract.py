"""The Tool refactor preserves the original schedule and host contract."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from box_agent.events import ToolCallResult, ToolCallStart
from box_agent.runtime import run_agent_loop
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import build_tool_name_index
from box_agent.tools.schedule_tool import CreateScheduledTaskTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.sub_agent_capabilities import (
    CapabilityFailure, CapabilityResolver, DelegationSpec, parse_delegation_spec,
)
from tests.test_tool_engine_compatibility import _apply_schedule_schema_changes


ROOT = Path(__file__).resolve().parent.parent
SCHEDULE_NAME = "create_scheduled_task"
DRAFT_ARGUMENTS = {
    "name": "工作日报", "prompt": "汇总当天已完成工作和后续事项。", "cron_expr": "0 9 * * 1-5",
}


class ScheduleLLM:
    def __init__(self, requested_name: str | None = None):
        self.requested_name = requested_name
        self.offered_names = []

    async def generate_stream(self, messages, tools=None, **kwargs):
        self.offered_names.append([tool.name for tool in tools or []])
        if self.requested_name is not None:
            requested_name, self.requested_name = self.requested_name, None
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                ToolCall(id="schedule-original-call", type="function", function=FunctionCall(
                    name=requested_name, arguments=DRAFT_ARGUMENTS,
                )),
            ])
        else:
            yield StreamEvent(type="text", delta="请核对草稿后保存。")
            yield StreamEvent(type="finish", finish_reason="stop")


def test_schedule_keeps_original_python_class_name_and_complete_schema():
    tool = CreateScheduledTaskTool()
    baseline = json.loads((ROOT / "tests/fixtures/tool_engine/c1_schemas.json").read_text())["tools"][SCHEDULE_NAME]
    assert type(tool).__name__ == "CreateScheduledTaskTool"
    assert tool.name == SCHEDULE_NAME
    assert tool.to_schema() == _apply_schedule_schema_changes(baseline["schema"])
    assert tool.to_openai_schema()["function"]["name"] == SCHEDULE_NAME
    assert tool.aliases == ()
    index = build_tool_name_index([tool])
    assert {id(target) for target in index.values()} == {id(tool)}
    assert set(index) == {
        SCHEDULE_NAME, SCHEDULE_NAME.replace("_", "-"),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_name", [SCHEDULE_NAME, "create-scheduled-task"])
async def test_original_schedule_name_preserves_call_id_and_host_draft(requested_name):
    class CountingScheduleTool(CreateScheduledTaskTool):
        calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            return await super().execute(**kwargs)

    tool = CountingScheduleTool()
    llm = ScheduleLLM(requested_name)
    messages = [Message(role="system", content="system"), Message(role="user", content="已确认任务与时间。")]
    events = [event async for event in run_agent_loop(
        llm=llm, messages=messages, tools={tool.name: tool}, max_steps=3,
    )]
    assert all(names == [SCHEDULE_NAME] for names in llm.offered_names)
    assert tool.calls == 1
    starts = [event for event in events if isinstance(event, ToolCallStart)]
    results = [event for event in events if isinstance(event, ToolCallResult)]
    assert len(starts) == len(results) == 1
    assert starts[0].tool_call_id == results[0].tool_call_id == "schedule-original-call"
    assert starts[0].tool_name == results[0].tool_name == SCHEDULE_NAME
    assert results[0].raw_output == {
        "kind": "officev3_schedule_draft",
        "draft": {**DRAFT_ARGUMENTS, "trigger_type": "cron", "fire_at": None},
    }
    assert "是否最终保存由用户决定" in results[0].content
    assert messages[2].tool_calls[0].function.name == requested_name


def test_schedule_retains_external_side_effect_delegation_rejection():
    requested_name = SCHEDULE_NAME
    spec = parse_delegation_spec(task="准备任务草稿", required_tools=[requested_name])
    assert isinstance(spec, DelegationSpec)
    result = CapabilityResolver().resolve(
        spec, parent_tools={requested_name: CreateScheduledTaskTool()},
    )
    assert isinstance(result, CapabilityFailure)
    assert result.code == "CAPABILITY_CONSTRAINT_CONFLICT"
    assert result.details["denied_reason"] == "external_side_effect_disabled"


def test_scheduled_task_skill_remains_byte_identical_to_original():
    skill_path = ROOT / "box_agent/skills/scheduled-task/SKILL.md"
    text = skill_path.read_text(encoding="utf-8")
    assert hashlib.sha256(text.encode()).hexdigest() == "a370df5f9edc2afcebd9458b63af777b2d335877533dd6e42d01a33850d9ce70"
    assert SCHEDULE_NAME in text
    loader = SkillLoader(sources=[(ROOT / "box_agent/skills", "builtin")])
    loader.discover_skills()
    skill = loader.get_skill("scheduled-task")
    assert skill is not None
    assert SCHEDULE_NAME in skill.description
    assert SCHEDULE_NAME in skill.to_prompt()
    prompt = (ROOT / "box_agent/config/system_prompt.md").read_text(encoding="utf-8")
    # The stable prompt has no schedule-specific instruction to migrate;
    # the Tool description and this Skill own the draft/save semantics.
    assert SCHEDULE_NAME not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_schedule_history_restores_without_reissuing_draft(tmp_path, completed):
    from box_agent.agent import Agent
    from box_agent.session_log import SessionLog

    call = ToolCall(id="historical-schedule", type="function", function=FunctionCall(
        name=SCHEDULE_NAME, arguments=DRAFT_ARGUMENTS,
    ))
    historical_draft = {
        "kind": "officev3_schedule_draft",
        "draft": {**DRAFT_ARGUMENTS, "trigger_type": "cron", "fire_at": None},
    }
    root = tmp_path / "sessions"
    log = SessionLog.create(root, session_id="old-schedule", cwd=tmp_path)
    log.append("turn/start", {"turn": 1})
    log.append("step/start", {"turn": 1, "step": 1})
    historical_messages = [
        Message(role="user", content="请准备日报任务。"),
        Message(role="assistant", content="", tool_calls=[call]),
    ]
    log.append_unlogged_messages(historical_messages, turn=1, step=1)
    log.append("tool/call", {
        "turn": 1, "step": 1, "callId": call.id,
        "name": SCHEDULE_NAME, "arguments": DRAFT_ARGUMENTS,
    })
    if completed:
        historical_messages.append(
            Message(role="tool", tool_call_id=call.id, content="已发送草稿，请核对保存。"),
        )
        log.append_unlogged_messages(historical_messages, turn=1, step=1, tool_result_metadata={call.id: {
            "toolName": SCHEDULE_NAME, "success": True, "rawOutput": historical_draft,
        }})
        log.append("step/end", {"turn": 1, "step": 1})
        log.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
    log.flush()
    log.close()

    class NeverReissuedSchedule(CreateScheduledTaskTool):
        calls = 0

        async def execute(self, **kwargs):
            self.calls += 1
            raise AssertionError("restoring history must never issue another draft")

    restored_log = SessionLog.open(root, session_id="old-schedule", cwd=tmp_path)
    restored_log.prepare_resume()
    tool = NeverReissuedSchedule()
    llm = ScheduleLLM()
    agent = Agent(
        llm_client=llm, system_prompt="system", tools=[tool],
        workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
        session_log=restored_log,
    )
    assert any(
        message.tool_calls and message.tool_calls[0].function.name == SCHEDULE_NAME
        for message in agent.messages
    )
    assert any(message.tool_call_id == call.id for message in agent.messages)
    agent.add_user_message("只查看先前记录，不重新发送草稿。")
    try:
        events = [event async for event in agent.run_events()]
        assert tool.calls == 0
        assert not any(isinstance(event, (ToolCallStart, ToolCallResult)) for event in events)
        calls = [event for event in restored_log.events if event["type"] == "tool/call"]
        assert len(calls) == 1
        assert calls[0]["data"]["callId"] == call.id
        assert calls[0]["data"]["name"] == SCHEDULE_NAME
        result = next(event for event in restored_log.events if event["type"] == "tool/result")
        if completed:
            assert result["data"]["result"]["rawOutput"] == historical_draft
        else:
            assert result["data"]["error"]["code"] == "TOOL_OUTCOME_UNKNOWN"
        # The original schedule tool remains directly offered; restoring its
        # historical call does not execute it or alter its public name.
        assert llm.offered_names[0] == [SCHEDULE_NAME, "tool_search"]
        assert agent.tools[SCHEDULE_NAME] is tool
    finally:
        restored_log.close()
