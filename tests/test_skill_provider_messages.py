"""Offline wire conversion of real disk Skill references and attachments.

The provider converters are real. No SDK transport or model generation is used.
"""

import base64
import json

import pytest

from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient
from box_agent.schema import FunctionCall, Message, ToolCall
from box_agent.skill_context import SkillReferenceContext
from box_agent.skill_runtime import SkillRuntime
from box_agent.tools.base import ToolInvocationContext, build_tool_name_index
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool


HOST_BODY = "HOST_SELECTED_RULE_7a33"
TOOL_BODY = "TOOL_READ_RULE_2d17"
PNG_DATA = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6yzkAAAAASUVORK5CYII="
PROVIDERS = [pytest.param(OpenAIClient, id="openai"), pytest.param(AnthropicClient, id="anthropic")]


@pytest.fixture
def disk_context(tmp_path):
    for name, body in (("selected-guide", HOST_BODY), ("read-guide", TOOL_BODY)):
        path = tmp_path / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\ndescription: local example\n---\n{body}\n", encoding="utf-8")
    loader = SkillLoader(sources=[(tmp_path / "skills", "user")],
                         skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    runtime = SkillRuntime(loader)
    runtime.select(["selected-guide"])
    image_path = tmp_path / "attached.png"
    image_path.write_bytes(base64.b64decode(PNG_DATA))
    note_path = tmp_path / "附件.txt"
    note_path.write_text("ATTACHMENT_NOTE: preserve this user fact.", encoding="utf-8")
    blocks = [
        {"type": "text", "text": "Use the attached image and notes."},
        {"type": "input_image", "media_type": "image/png",
         "data": base64.b64encode(image_path.read_bytes()).decode("ascii")},
        {"type": "text", "text": f"Attachment: {note_path}\n{note_path.read_text(encoding='utf-8')}"},
    ]
    return SkillReferenceContext(runtime), blocks


def convert(provider, messages):
    # Conversion needs no initialized SDK transport. This calls the production
    # serializer without substituting any message or content transformation.
    client = provider.__new__(provider)
    return client._convert_messages(messages)


def user_blocks(wire):
    return [block for message in wire if message["role"] == "user"
            and isinstance(message["content"], list) for block in message["content"]]


def assert_reference_locations(system, wire):
    privileged = [system, *[message["content"] for message in wire
                            if message["role"] in {"system", "developer"}]]
    assert HOST_BODY not in json.dumps(privileged)
    assert TOOL_BODY not in json.dumps(privileged)
    assert sum(HOST_BODY in block.get("text", "") for block in user_blocks(wire)) == 1
    assert json.dumps(wire).count(HOST_BODY) == 1


def assert_attachments_survive(provider, wire):
    blocks = user_blocks(wire)
    if provider is OpenAIClient:
        images = [block for block in blocks if block["type"] == "image_url"]
        assert images == [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_DATA}"}}]
    else:
        images = [block for block in blocks if block["type"] == "image"]
        assert images == [{"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": PNG_DATA,
        }}]
    assert sum("ATTACHMENT_NOTE" in block.get("text", "") for block in blocks) == 1
    assert any("Use the attached image and notes." == block.get("text") for block in blocks)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_explicit_selection_serializes_as_ordinary_reference_without_fake_tool_result(disk_context, provider):
    context, blocks = disk_context
    messages = [Message(role="system", content="BASE_POLICY"), Message(role="user", content=blocks)]
    before = [message.model_dump() for message in messages]

    projection = context.prepare_request(messages, budget_chars=50000)
    system, wire = convert(provider, projection.messages)

    assert_reference_locations(system, wire)
    assert_attachments_survive(provider, wire)
    assert all(message["role"] != "tool" for message in wire)
    assert all(block["type"] != "tool_result" for block in user_blocks(wire))
    assert all(not message.get("tool_calls") for message in wire)
    assert [message.model_dump() for message in messages] == before
    assert messages[0].content == "BASE_POLICY"
    if provider is AnthropicClient:
        assert system == "BASE_POLICY"
    else:
        assert wire[0] == {"role": "system", "content": "BASE_POLICY"}


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("call_name", ["get_skill", "skill_view"])
async def test_real_skill_tool_result_keeps_call_id_beside_ordinary_attachment_reference(disk_context, provider, call_name):
    context, blocks = disk_context
    messages = [Message(role="system", content="BASE_POLICY"), Message(role="user", content="Read the method first.")]
    context.prepare_request(messages, budget_chars=50000)
    call_id = "skill-call-42"
    call = ToolCall(id=call_id, type="function",
                    function=FunctionCall(name=call_name, arguments={"skill_name": "read-guide"}))
    tool = build_tool_name_index([GetSkillTool(context.runtime.loader)])[call_name]
    result = await tool.invoke(call.function.arguments, context=ToolInvocationContext(
        parent_tool_call_id=call_id, skill_reader=context.read,
    ))
    assert result.success and TOOL_BODY in result.model_context
    messages.extend([
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", name=call_name, tool_call_id=call_id, content=result.model_context),
        Message(role="user", content=blocks),
    ])
    before = [message.model_dump() for message in messages]

    projection = context.prepare_request(messages, budget_chars=50000)
    system, wire = convert(provider, projection.messages)

    assert_reference_locations(system, wire)
    assert_attachments_survive(provider, wire)
    if provider is OpenAIClient:
        calls = [item for message in wire for item in message.get("tool_calls", [])]
        results = [message for message in wire if message["role"] == "tool"]
        assert [item["id"] for item in calls] == [call_id]
        assert calls[0]["function"]["name"] == call_name
        assert json.loads(calls[0]["function"]["arguments"]) == call.function.arguments
        assert results == [{"role": "tool", "tool_call_id": call_id, "content": result.model_context}]
    else:
        calls = [block for message in wire if message["role"] == "assistant"
                 for block in message["content"] if block["type"] == "tool_use"]
        results = [block for block in user_blocks(wire) if block["type"] == "tool_result"]
        assert calls == [{"type": "tool_use", "id": call_id, "name": call_name, "input": call.function.arguments}]
        assert results == [{"type": "tool_result", "tool_use_id": call_id, "content": result.model_context}]
        # The provider coalesces the adjacent tool result and attachment turn;
        # the host reference must remain a separate ordinary text block.
        assert any(message["role"] == "user" and isinstance(message["content"], list)
                   and any(block["type"] == "tool_result" for block in message["content"])
                   and any(HOST_BODY in block.get("text", "") for block in message["content"])
                   for message in wire)
    assert json.dumps(wire).count(TOOL_BODY) == 1
    assert HOST_BODY not in result.model_context
    assert [message.model_dump() for message in messages] == before
