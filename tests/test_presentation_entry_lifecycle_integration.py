"""Real bundled entry, ToolEngine and host receipt; only the LLM is scripted.

This verifies routing and full-task continuation, not real-model slide delivery.
The model owns domain workflow; no hidden classifier or completion controller.
"""

import json
from pathlib import Path

import pytest

from box_agent.acp import _user_decision_response_from_meta
from box_agent.agent import Agent
from box_agent.events import DoneEvent, LLMOutputEvent, StopReason, ToolCallResult
from box_agent.schema import FunctionCall, LLMResponse, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog
from box_agent.tools.file_tools import WriteTool
from box_agent.tools.request_user_decision_tool import RequestUserDecisionTool
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


def call(name, args, identifier):
    return ToolCall(type="function", id=identifier,
                    function=FunctionCall(name=name, arguments=args))


class Model:
    def __init__(self, original, workspace):
        self.original, self.workspace = original, workspace
        self.main = []
        self.auxiliary = []

    async def generate(self, messages, **kwargs):
        assert kwargs.get("call_kind") != "presentation_intent"
        if kwargs.get("call_kind") == "turn_continuation_judge":
            return LLMResponse(finish_reason="stop", content='{"continue": false}')
        self.auxiliary.append((messages, kwargs))
        raise AssertionError("Unexpected auxiliary call")

    async def generate_stream(self, messages, **kwargs):
        self.main.append([m.model_copy(deep=True) for m in messages])
        index = len(self.main)
        if index == 1:
            calls = [call("get_skill", {"skill_name": "pptx"}, "entry-read")]
        elif index == 2:
            calls = [call("request_user_decision", {"question": "选择制作模式",
                "decision_kind": "presentation_mode", "options": [
                    {"id": "fast", "label": "快速模式"},
                    {"id": "design", "label": "设计模式"}]}, "mode-card")]
        elif index == 3:
            # First model continuation after the host selects design must see
            # the complete original task, not just the short mode reply.
            context = "\n".join(str(m.content) for m in messages)
            assert self.original in context
            assert '"selected_option_id": "design"' in context
            calls = [call("get_skill", {"skill_name": "sn-ppt-entry"}, "design-method")]
        elif index == 4:
            calls = [call("write_file", {"path": str(self.workspace / "requirements.txt"),
                                          "content": self.original}, "resumed-write")]
        else:
            yield StreamEvent(type="text", delta="Only the requirements note exists.")
            yield StreamEvent(type="finish", finish_reason="stop")
            return
        yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=calls)


@pytest.mark.asyncio
async def test_real_entry_card_host_design_reply_preserves_full_task_and_resumes_work(tmp_path):
    root = Path(__file__).resolve().parents[1]
    loader = SkillLoader(root / "box_agent/skills", skill_settings_path=tmp_path / "skill-settings.json")
    loader.discover_skills()
    reader = GetSkillTool(loader)
    original = "请为瑞幸咖啡制作12页商业分析PPT，保留竞争风险、财务数据和全部来源。"
    model = Model(original, tmp_path)
    log = SessionLog.create(tmp_path / "sessions", session_id="entry-lifecycle", cwd=tmp_path)
    instance = Agent(llm_client=model, system_prompt="Complete the actual task.",
                     tools=[reader, RequestUserDecisionTool(), WriteTool(workspace_dir=str(tmp_path))],
                     workspace_dir=str(tmp_path), max_steps=6, thinking_enabled=True,
                     enable_builtin_tools=False, deferred_mcp_loading_enabled=False,
                     session_log=log)
    try:
        instance.add_user_message(original)
        first = [event async for event in instance.run_events()]
        assert next(e for e in first if isinstance(e, DoneEvent)).stop_reason == StopReason.WAITING_FOR_USER
        entry = next(e for e in first if isinstance(e, ToolCallResult) and e.tool_name == "get_skill")
        assert entry.success and "制作模式" in entry.content
        card = next(e for e in first if isinstance(e, ToolCallResult) and e.tool_name == "request_user_decision")
        assert card.success and card.origin == "model"
        assert len(model.main) == 2 and model.auxiliary == []
        assert len([e for e in first if isinstance(e, LLMOutputEvent)]) == 2
        assert not (tmp_path / "requirements.txt").exists()

        response = _user_decision_response_from_meta({"userDecision": {
            "requestId": card.raw_output["requestId"], "decisionKind": "presentation_mode",
            "selectedOptionId": "design", "trigger": "user",
        }})
        instance.add_user_message("[HOST_USER_DECISION_RESPONSE]\n" + json.dumps(response) + "\n[/HOST_USER_DECISION_RESPONSE]\n设计模式")
        second = [event async for event in instance.run_events()]
        assert (tmp_path / "requirements.txt").read_text() == original
        assert any(e.success and e.tool_name == "get_skill" for e in second if isinstance(e, ToolCallResult))
        assert not any(e.tool_name == "request_user_decision" for e in second if isinstance(e, ToolCallResult))
        assert not hasattr(instance, "_presentation_runtime")
        assert "presentation_delivery" not in instance.tools
        assert next(e for e in second if isinstance(e, DoneEvent)).final_content == "Only the requirements note exists."
        assert model.auxiliary == []
        assert not any(event["type"].startswith("presentation/") for event in log.events)
    finally:
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["fast", "static", "dynamic"])
async def test_documented_backend_handoff_retains_public_contract_through_compaction(tmp_path, route):
    """Execute real documented calls and recover both whole-task obligations and method."""
    import ast
    import re

    from box_agent.context_input import DefaultContextEngine
    from box_agent.events import SummarizationEvent
    from box_agent.runtime import run_agent_loop
    from box_agent.skill_runtime import SkillRuntime
    from box_agent.tools.base import ToolInvocationContext
    from box_agent.tools.engine.preparation import prepare_tools
    from tests.test_adopted_skill_loop_recovery import Provider

    root = Path(__file__).resolve().parents[1]
    loader = SkillLoader(root / "box_agent/skills", skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    runtime = SkillRuntime(loader)
    tool = GetSkillTool(loader)
    context = DefaultContextEngine()
    context.configure_run(skill_engine=runtime)
    original = "制作 12 页中文课件，保留附件内容、全部数据来源及已确认的文件交付。"
    history = [Message(role="system", content="Follow the actual task."), Message(role="user", content=original)]
    context.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=60000)

    async def read(arguments):
        context.prepare_request(history, prepared_tools=prepare_tools([tool]), token_limit=60000)
        result = await tool.invoke(arguments, context=ToolInvocationContext(skill_reader=context.tool_reader))
        assert result.success, result.error

    await read({"skill_name": "pptx"})
    public = runtime.resolve_reference("pptx").prompt
    backend = "ppt-fast" if route == "fast" else "sn-ppt-entry"
    # The public Skill is the actual author of the handoff contract. Parsing its
    # literal example prevents a test-only call shape from concealing retirement.
    expression = re.search(r'get_skill\(skill_name="' + backend + r'"[^)]*\)', public).group()
    call_ast = ast.parse(expression, mode="eval").body
    await read({item.arg: ast.literal_eval(item.value) for item in call_ast.keywords})
    if route != "fast":
        await read({"skill_name": "sn-ppt-story", "usage": "use", "replace": ["sn-ppt-entry"]})
        backend = "sn-ppt-dazzle" if route == "dynamic" else "sn-ppt-standard"
        await read({"skill_name": backend, "usage": "use", "replace": ["sn-ppt-story"]})
    history.append(Message(role="assistant", content="old work " * 24000))
    history.extend(Message(role="assistant", content=f"recent work {n}") for n in range(6))
    history.append(Message(role="user", content="继续。"))
    provider = Provider(True)
    events = [event async for event in run_agent_loop(
        llm=provider, messages=history, tools={tool.name: tool}, skill_engine=runtime,
        token_limit=30000, max_steps=2, artifact_detection_enabled=False,
        truncation_continuation_enabled=False)]
    assert any(isinstance(event, SummarizationEvent) for event in events)
    assert len(provider.requests) == 1
    actual = str(provider.requests[0])
    assert public in "\n".join(str(message.content) for message in provider.requests[0])
    assert original in actual and "继续。" in actual
    assert "--requirements" in actual and "finalize-receipt.json" in actual
    assert runtime.resolve_reference(backend).prompt in "\n".join(str(m.content) for m in provider.requests[0])
