from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import box_agent.agent as agent_module
from box_agent.agent import Agent
from box_agent.config import ToolLimitsConfig
from box_agent.context_resources import ResourceClass, ResourceDescriptor
from box_agent.events import DoneEvent, StopReason
from box_agent.tools.skill_preload import ACTIVE_SKILLS_HEADING, strip_active_skills


class DummyLLM:
    pass


@pytest.mark.asyncio
async def test_agent_run_forwards_core_execution_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def fake_run_agent_loop(**kwargs):
        captured.update(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr(agent_module, "run_agent_loop", fake_run_agent_loop)

    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
        thinking_enabled=True,
        tool_limits=ToolLimitsConfig(web_search={"total_calls": 31}),
    )

    result = await agent.run(
        force_plan_start=True,
        artifact_detection_enabled=False,
        current_turn_text="current user request",
    )

    assert result == "done"
    assert captured["force_plan_start"] is True
    assert "completion_gate" not in captured
    assert captured["artifact_detection_enabled"] is False
    assert captured["thinking_enabled"] is True
    assert captured["tool_limits"].web_search.total_calls == 31
    assert captured["active_skill_activator"] == agent.activate_skill_instructions
    assert captured["current_turn_text"] == "current user request"
    assert captured["context_resource_ledger"] is agent.context_resource_ledger
    assert captured["context_resource_dedup_enabled"] is True


@pytest.mark.asyncio
async def test_agent_run_events_forwards_host_run_options(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    async def fake_run_agent_loop(**kwargs):
        captured.update(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="done")

    monkeypatch.setattr(agent_module, "run_agent_loop", fake_run_agent_loop)
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    host_llm = object()
    artifact_root = tmp_path / "host-output"
    def fingerprint_sink(_payload: dict) -> None:
        pass

    options = replace(
        agent.default_run_options(),
        llm=host_llm,
        session_id="host-session",
        memory_turn_id="turn-1",
        turn_id="turn-1",
        title="Quarterly review",
        max_tool_calls=9,
        max_delegated_tool_calls=12,
        web_search_total_limit=36,
        no_progress_limit=2,
        artifact_root_dir=artifact_root,
        cache_fingerprint_sink=fingerprint_sink,
        current_turn_text="host current turn",
    )

    events = [event async for event in agent.run_events(options=options)]

    assert len(events) == 1
    assert captured["llm"] is host_llm
    assert captured["session_id"] == "host-session"
    assert captured["memory_turn_id"] == "turn-1"
    assert captured["turn_id"] == "turn-1"
    assert captured["title"] == "Quarterly review"
    assert captured["max_tool_calls"] == 9
    assert captured["max_delegated_tool_calls"] == 12
    assert captured["web_search_total_limit"] == 36
    assert captured["no_progress_limit"] == 2
    assert captured["artifact_root_dir"] == artifact_root
    assert captured["cache_fingerprint_sink"] is fingerprint_sink
    assert "workflow_policy" not in captured
    assert captured["current_turn_text"] == "host current turn"
    assert captured["context_resource_ledger"] is agent.context_resource_ledger


def test_agent_public_runtime_configuration_and_history_reset(tmp_path: Path) -> None:
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    permission_negotiator = object()
    memory_extractor = object()
    proposal_negotiator = object()

    agent.set_permission_negotiator(permission_negotiator)
    agent.set_memory_extractor(memory_extractor)
    agent.set_memory_proposal_negotiator(proposal_negotiator)
    agent.messages.extend(
        [
            agent.messages[0].__class__(role="user", content="hello"),
            agent.messages[0].__class__(role="assistant", content="hi"),
        ]
    )
    agent.context_resource_ledger.register_full_source(
        "read-1",
        ResourceDescriptor(
            resource_id=str(tmp_path / "reference.md"),
            content_version="a" * 64,
            start_line=1,
            end_line=1,
            total_lines=1,
            resource_class=ResourceClass.RECONSTRUCTABLE,
        ),
        "full body",
    )

    options = agent.default_run_options()
    assert options.permission_negotiator is permission_negotiator
    assert options.memory_extractor is memory_extractor
    assert agent.clear_history() == 2
    assert len(agent.messages) == 1
    assert agent.context_resource_ledger.epoch == 1
    assert agent.context_resource_ledger.source_ids == ()


def test_agent_preserves_deduplicated_active_skills_across_prompt_updates(
    tmp_path: Path,
) -> None:
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )

    agent.activate_skill_instructions("pptx", "# Skill: pptx\n\nOld instructions.")
    agent.activate_skill_instructions("pptx", "# Skill: pptx\n\nOld instructions.")

    assert agent.system_prompt.count(ACTIVE_SKILLS_HEADING) == 1
    assert agent.system_prompt.count("Old instructions.") == 1

    base_prompt = strip_active_skills(agent.system_prompt).replace("system", "updated", 1)
    agent.set_system_prompt(base_prompt)
    agent.activate_skill_instructions("pptx", "# Skill: pptx\n\nNew instructions.")

    assert agent.messages[0].content == agent.system_prompt
    assert agent.system_prompt.startswith("updated")
    assert "Old instructions." not in agent.system_prompt
    assert agent.system_prompt.endswith("New instructions.")


def test_agent_reports_active_skill_budget_without_truncating_and_can_clear(
    tmp_path: Path,
) -> None:
    agent = Agent(
        llm_client=DummyLLM(),
        system_prompt="system",
        tools=[],
        workspace_dir=str(tmp_path),
    )
    first = "FIRST_REQUIRED_RULE\n" + "a" * 70_000
    second = "SECOND_REQUIRED_RULE\n" + "b" * 70_000

    agent.activate_skill_instructions("first", first)
    agent.activate_skill_instructions("second", second)
    diagnostics = agent.active_skill_diagnostics()

    assert diagnostics["names"] == ("first", "second")
    assert diagnostics["budget_exceeded"] is True
    assert "FIRST_REQUIRED_RULE" in agent.system_prompt
    assert "SECOND_REQUIRED_RULE" in agent.system_prompt

    assert agent.deactivate_skill_instructions("first") is True
    assert "FIRST_REQUIRED_RULE" not in agent.system_prompt
    assert "SECOND_REQUIRED_RULE" in agent.system_prompt
    assert agent.deactivate_skill_instructions("missing") is False

    agent.clear_active_skill_instructions()
    assert ACTIVE_SKILLS_HEADING not in agent.system_prompt
    assert agent.active_skill_diagnostics()["names"] == ()
