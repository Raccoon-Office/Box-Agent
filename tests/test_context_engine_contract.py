"""Compaction injection and projection contracts alongside canonical Context."""
from dataclasses import replace

import pytest

from box_agent.agent import Agent
from box_agent.kernel.compact_engine import DefaultCompactEngine
from box_agent.kernel.context_types import CompactionInput, CompactionOutcome
from box_agent.kernel.ports import CompactEnginePort
from box_agent.schema import Message
from box_agent.session_projection import SessionProjection
from tests.test_managed_kernel_services import DeterministicLLM, managed_services


def test_compaction_outcome_exposes_limit_fields_without_breaking_legacy_tuple():
    outcome = CompactionOutcome(None, 100, 120, mode="blocked", protected_messages=2,
                                still_over_limit=True)
    assert outcome.blocked and outcome.still_over_limit
    assert outcome.protected_messages == 2
    assert tuple(outcome) == (None, False, 100)
    assert CompactionOutcome(None, 100, 100, mode="blocked").still_over_limit
    with pytest.raises(ValueError, match="must match"):
        CompactionOutcome(None, 100, 100, mode="none", still_over_limit=True)


def test_session_projection_returns_defensive_copies():
    messages = [Message(role="user", content="original")]
    goal = {"status": "active"}
    projection = SessionProjection(messages=messages, goal=goal,
                                   plan={"steps": ["one"]}, todos=[{"title": "one"}],
                                   skills=[{"name": "demo"}])
    messages[0].content = "changed before access"
    goal["status"] = "changed before access"
    projection.messages[0].content = "changed"
    projection.goal["status"] = "changed"
    projection.plan["steps"].append("changed")
    projection.todos[0]["title"] = "changed"
    projection.skills[0]["name"] = "changed"
    assert projection.messages[0].content == "original"
    assert projection.goal == {"status": "active"}
    assert projection.plan == {"steps": ["one"]}
    assert projection.todos == [{"title": "one"}]
    assert projection.skills == [{"name": "demo"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_kernel_uses_the_provided_compact_engine_without_replacing_falsey_instances(tmp_path, monkeypatch, managed):
    class Compact:
        def __init__(self):
            self.inputs = []

        def __bool__(self):
            return False

        async def compact_if_needed(self, inputs):
            self.inputs.append(inputs)
            return CompactionOutcome(None, 0, 0)

    compact = Compact()
    llm = DeterministicLLM()
    agent = Agent(llm_client=llm, system_prompt="system", tools=[], workspace_dir=tmp_path)
    agent.add_user_message("use the supplied compaction policy")
    options = None
    if managed:
        services = managed_services(
            llm=llm, tools=agent.tools, tool_exposure=agent.mcp_tool_exposure,
            tool_result_store=agent.tool_result_storage, compact_engine=compact,
        )
        options = replace(agent.default_run_options(), kernel_services=services)
    else:
        import box_agent.plugins.defaults as defaults
        original = defaults.default_plugin_descriptors

        def descriptors(**kwargs):
            return tuple(
                replace(descriptor, factory=lambda: compact)
                if descriptor.capabilities == (CompactEnginePort,) else descriptor
                for descriptor in original(**kwargs)
            )
        monkeypatch.setattr(defaults, "default_plugin_descriptors", descriptors)
    _ = [event async for event in agent.run_events(options=options)]
    assert compact.inputs
    assert all(isinstance(inputs, CompactionInput) for inputs in compact.inputs)
    assert all(inputs.llm is llm and inputs.tools is agent.tools for inputs in compact.inputs)
    assert "use the supplied compaction policy" in str(compact.inputs[0].history)


@pytest.mark.asyncio
async def test_compact_engine_propagates_commit_failure_before_requesting_a_summary():
    class Provider:
        async def generate(self, **kwargs):
            pytest.fail("summary requested before its durable start committed")

    def failed_commit(estimated):
        raise OSError("compaction start failed")

    inputs = CompactionInput(
        history=(Message(role="system", content="BASE"), Message(role="user", content="request")),
        token_limit=10000, llm=Provider(), force=True, before_summary=failed_commit,
        estimate_tools={}, summary_input_token_limit=10000,
    )
    with pytest.raises(OSError, match="compaction start failed"):
        await DefaultCompactEngine().compact_if_needed(inputs)
