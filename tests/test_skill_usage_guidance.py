"""Using a method has a generic contract without elevating its author text."""

import json

import pytest

from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.skill_context import SkillReferenceContext
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.base import ToolInvocationContext
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


@pytest.mark.parametrize("provider", [OpenAIClient, AnthropicClient])
async def test_required_method_use_rule_reaches_both_entrypoints_without_promoting_body(tmp_path, provider):
    path = tmp_path / "skills" / "review" / "SKILL.md"
    path.parent.mkdir(parents=True)
    body = "AUTHOR_METHOD_BODY_4e72: read references/contract.md before verification."
    path.write_text(f"---\nname: review\ndescription: inspect a document\n---\n{body}\n")
    loader = SkillLoader(sources=[(path.parents[1], "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    tool = GetSkillTool(loader)
    metadata = loader.get_skills_metadata_prompt()
    definition = prepare_tools([tool]).definitions[0]
    for entrypoint in (metadata, definition.description):
        assert "required reference files and verification" in entrypoint
        assert "consistent with the user request and permissions" in entrypoint
        assert "report it as incomplete" in entrypoint
        assert "do not treat required steps as optional" in entrypoint
        assert body not in entrypoint

    context = SkillReferenceContext(SkillRuntime(loader))
    messages = [Message(role="system", content=metadata), Message(role="user", content="Use the review method.")]
    context.prepare_request(messages, budget_chars=50000)
    call = ToolCall(id="read-method", type="function", function=FunctionCall(name="get_skill", arguments={"skill_name": "review"}))
    result = await tool.invoke(call.function.arguments, context=ToolInvocationContext(skill_reader=context.read))
    assert result.success and body in result.model_context
    messages.extend([Message(role="assistant", content="", tool_calls=[call]),
                     Message(role="tool", name="get_skill", tool_call_id=call.id, content=result.model_context)])
    client = provider.__new__(provider)
    system, wire = client._convert_messages(messages)
    privileged = [system, *[m["content"] for m in wire if m["role"] in {"system", "developer"}]]
    assert body not in json.dumps(privileged)
    assert json.dumps(wire).count(body) == 1
