"""Real Kernel recovery for ordinary and bundled methods, with no lifecycle."""

from pathlib import Path

import pytest

from box_agent.context_input import DefaultContextEngine
from box_agent.events import DoneEvent, StopReason, SummarizationEvent
from box_agent.kernel.context_engine import _fallback_context_estimate
from box_agent.runtime import run_agent_loop
from box_agent.schema import LLMResponse, Message, StreamEvent
from box_agent.session_log import SessionLog
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


class Provider:
    max_output_tokens = 64000

    def __init__(self, summary_available):
        self.summary_available = summary_available
        self.requests = []

    async def generate(self, messages, **kwargs):
        if kwargs.get("call_kind") == "turn_continuation_judge":
            return LLMResponse(content='{"continue":false}', finish_reason="stop")
        assert kwargs.get("call_kind") == "context_summary"
        if not self.summary_available:
            raise RuntimeError("summary service unavailable")
        return LLMResponse(content="<summary>Work is in progress.</summary>", finish_reason="stop")

    async def generate_stream(self, messages, **kwargs):
        if kwargs.get("call_kind") == "context_summary":
            result = await self.generate(messages, **kwargs)
            yield StreamEvent(type="text", delta=result.content)
        else:
            self.requests.append([message.model_copy(deep=True) for message in messages])
            yield StreamEvent(type="text", delta="Done.")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
async def test_compaction_before_first_skill_read_preserves_pending_task_on_restart(tmp_path):
    path = tmp_path / "generic-method/SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: generic-method\ndescription: test\n---\nEXACT_GENERIC_METHOD\n")
    loader = SkillLoader(sources=[(tmp_path, "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    store = SessionLog.create(tmp_path / "sessions", session_id="before-read", cwd=tmp_path)
    try:
        runtime = SkillRuntime(loader, session_log=store)
        tool = GetSkillTool(loader)
        original = "Make an 8-page report, preserving every data row."
        history = [Message(role="system", content="System"), Message(role="user", content=original),
                   Message(role="assistant", content="old work " * 24000)]
        history.extend(Message(role="assistant", content=f"recent work {i}") for i in range(6))
        history.append(Message(role="user", content="Continue."))
        events = [event async for event in run_agent_loop(
            llm=Provider(True), messages=history, tools={tool.name: tool}, skill_engine=runtime,
            session_log=store, token_limit=30000, max_steps=2, artifact_detection_enabled=False,
            truncation_continuation_enabled=False)]
        assert any(isinstance(event, SummarizationEvent) for event in events)
        assert not any(message.content == original for message in history)
        restored = SkillRuntime(loader, session_log=store)
        assert original in restored.task.user_inputs
        restored.prime_task_history(store.replay().messages)
        history.append(Message(role="user", content="Use the detailed method."))
        engine = DefaultContextEngine()
        engine.configure_run(skill_engine=restored, session_store=store)
        engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=30000)
        result = await tool.invoke({"skill_name": "generic-method"},
                                   context=ToolInvocationContext(skill_reader=engine.tool_reader))
        assert result.success
        assert original in restored.task.user_inputs
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["generic-method", "sn-ppt-standard"])
@pytest.mark.parametrize("summary_available", [False, True])
async def test_actual_provider_gets_adopted_body_and_original_task_after_compaction(
        tmp_path, name, summary_available):
    path = tmp_path / "generic-method/SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: generic-method\ndescription: test\n---\nEXACT_GENERIC_METHOD\n")
    builtin = Path(__file__).resolve().parents[1] / "box_agent/skills/presentation-suite/skills"
    loader = SkillLoader(sources=[(tmp_path, "user"), (builtin, "builtin")],
                         skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    runtime = SkillRuntime(loader)
    tool = GetSkillTool(loader)
    engine = DefaultContextEngine()
    engine.configure_run(skill_engine=runtime)
    original = "制作 12 页中文演示，使用设计模式，保留全部原始数据。"
    history = [Message(role="system", content="Follow the task."), Message(role="user", content=original)]
    engine.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=60000)
    result = await tool.invoke({"skill_name": name},
                               context=ToolInvocationContext(skill_reader=engine.tool_reader))
    assert result.success
    expected = runtime.resolve_reference(name).prompt
    history.append(Message(role="assistant", content="old work " * 24000))
    history.extend(Message(role="assistant", content=f"recent work {index}") for index in range(6))
    history.append(Message(role="user", content="继续。"))
    provider = Provider(summary_available)
    events = [event async for event in run_agent_loop(
        llm=provider, messages=history, tools={tool.name: tool}, skill_engine=runtime,
        token_limit=30000, max_steps=2, artifact_detection_enabled=False,
        truncation_continuation_enabled=False)]
    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert len(provider.requests) == 1
    actual = provider.requests[0]
    assert any(isinstance(message.content, str) and expected in message.content for message in actual)
    assert original in str(actual)
    assert "继续。" in str(actual)
    assert _fallback_context_estimate(actual, {tool.name: tool}) + 1024 <= 30000
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason != StopReason.ERROR
