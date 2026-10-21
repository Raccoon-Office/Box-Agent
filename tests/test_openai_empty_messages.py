"""Empty assistant compatibility at the OpenAI SDK HTTP boundary."""

import json

import httpx
from openai import AsyncOpenAI
import pytest

from box_agent.events import DoneEvent, StopReason, ToolCallResult
from box_agent.llm.openai_client import OpenAIClient
from box_agent.retry import RetryConfig
from box_agent.runtime import run_agent_loop
from box_agent.schema import Message
from box_agent.tools.file_tools import ReadTool
from box_agent.turn_continuation import model_says_continue


PLACEHOLDER = "（该轮模型未返回可见回复。）"


@pytest.fixture
async def wire_client():
    requests = []
    stream_replies = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        common = {"id": "empty-message-test", "created": 0, "model": body["model"]}
        if body.get("stream"):
            delta, finish = stream_replies.pop(0) if stream_replies else (
                {"content": "Completed."}, "stop",
            )
            chunk = {
                **common, "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
            )
        return httpx.Response(200, json={
            **common, "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": '{"continue":false}',
            }}],
        })

    client = OpenAIClient(
        api_key="test", api_base="https://inference.example/v1", model="test-model",
        retry_config=RetryConfig(enabled=False),
    )
    await client.client.close()
    client.client = AsyncOpenAI(
        api_key="test", base_url=client.api_base, max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )
    try:
        yield client, requests, stream_replies
    finally:
        await client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("content", "thinking"), [
    ("", None),
    (" \n\t", None),
    ([], None),
    ([{"type": "text", "text": ""}], None),
    ([{"type": "text", "text": " "}, {"type": "text", "text": "\t"}], None),
    ("", " \n\t"),
])
async def test_pure_empty_assistant_gets_placeholder_only_on_wire(
    wire_client, stream, content, thinking,
):
    client, requests, _ = wire_client
    messages = [
        Message(role="user", content="First request."),
        Message(role="assistant", content=content, thinking=thinking, tool_calls=[]),
        Message(role="user", content="Continue."),
    ]
    before = [message.model_copy(deep=True) for message in messages]

    if stream:
        _ = [event async for event in client.generate_stream(messages)]
    else:
        await client.generate(messages)

    assert len(requests) == 1
    assert [message["role"] for message in requests[0]["messages"]] == [
        "user", "assistant", "user",
    ]
    assert requests[0]["messages"][1]["content"] == PLACEHOLDER
    assert messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("content", ["", " \n\t"])
async def test_empty_text_with_tool_call_keeps_content_and_tool_pair(wire_client, stream, content):
    client, requests, _ = wire_client
    messages = [
        Message(role="user", content="Look up the answer."),
        Message(role="assistant", content=content, tool_calls=[{
            "id": "lookup-1", "type": "function",
            "function": {"name": "lookup", "arguments": {"query": "answer"}},
        }]),
        Message(role="tool", content="42", tool_call_id="lookup-1"),
    ]
    before = [message.model_copy(deep=True) for message in messages]

    if stream:
        _ = [event async for event in client.generate_stream(messages)]
    else:
        await client.generate(messages)

    assert requests[0]["messages"][1:] == [
        {"role": "assistant", "content": content, "tool_calls": [{
            "id": "lookup-1", "type": "function",
            "function": {"name": "lookup", "arguments": '{"query": "answer"}'},
        }]},
        {"role": "tool", "content": "42", "tool_call_id": "lookup-1"},
    ]
    assert messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    "  Visible answer.\n",
    [{"type": "text", "text": " "}, {"type": "text", "text": "Answer."}],
    [{"type": "refusal", "refusal": "Unable to help."}],
    [{"type": "extension", "payload": "Keep unknown blocks."}],
])
async def test_assistant_payload_is_not_replaced(wire_client, content):
    client, requests, _ = wire_client
    messages = [Message(role="assistant", content=content)]
    before = [message.model_copy(deep=True) for message in messages]

    await client.generate(messages)

    assert requests[0]["messages"] == [{"role": "assistant", "content": content}]
    assert messages == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("api_base", "replay_field"), [
    ("https://xiaohuanxiong.com/api/web/llm/v2", "reasoning_content"),
    ("https://inference.example/v1", "reasoning_details"),
])
async def test_thinking_only_assistant_is_not_replaced(wire_client, stream, api_base, replay_field):
    client, requests, _ = wire_client
    client.api_base = api_base
    client.model = "sn-glm-5-2"
    thinking = "  Original reasoning.\nKeep whitespace.\n"
    messages = [Message(role="assistant", content="", thinking=thinking)]
    before = [message.model_copy(deep=True) for message in messages]

    if stream:
        _ = [event async for event in client.generate_stream(messages)]
    else:
        await client.generate(messages)

    assert requests[0]["messages"] == [{
        "role": "assistant", "content": "",
        replay_field: thinking if replay_field == "reasoning_content" else [{"text": thinking}],
    }]
    assert messages == before


@pytest.mark.asyncio
async def test_judge_fills_assistant_made_empty_by_reasoning_redaction(wire_client):
    client, requests, _ = wire_client
    messages = [
        Message(role="user", content="Old request."),
        Message(role="assistant", content="", thinking="Original reasoning."),
        Message(role="user", content="Finish the task."),
        Message(role="assistant", content="I will continue working."),
    ]
    before = [message.model_copy(deep=True) for message in messages]

    assert not await model_says_continue(client, messages=messages)

    assert len(requests) == 1
    assert requests[0]["messages"][1] == {"role": "assistant", "content": PLACEHOLDER}
    assert requests[0]["messages"][3] == {
        "role": "assistant", "content": "I will continue working.",
    }
    assert requests[0]["messages"][-1]["role"] == "user"
    assert messages == before


@pytest.mark.asyncio
async def test_tool_empty_final_retry_fills_wire_without_changing_history(wire_client, tmp_path):
    client, requests, stream_replies = wire_client
    path = tmp_path / "answer.txt"
    path.write_text("42", encoding="utf-8")
    stream_replies.extend([
        ({"tool_calls": [{
            "index": 0, "id": "read-1", "type": "function", "function": {
                "name": "read_file", "arguments": json.dumps({"path": str(path)}),
            },
        }]}, "tool_calls"),
        ({"content": ""}, "stop"),
        ({"content": "The answer is 42."}, "stop"),
    ])
    messages = [Message(role="user", content="Read answer.txt and report the answer.")]
    tool = ReadTool(workspace_dir=str(tmp_path))

    events = [event async for event in run_agent_loop(
        llm=client, messages=messages, tools={tool.name: tool}, max_steps=5,
        workspace_dir=str(tmp_path), artifact_detection_enabled=False,
    )]

    assert len(requests) == 4  # Tool call, empty final, retry, continuation judge.
    assert requests[2]["messages"][3] == {"role": "assistant", "content": PLACEHOLDER}
    assert requests[3]["messages"][3] == {"role": "assistant", "content": PLACEHOLDER}
    for request in requests[1:]:
        assert request["messages"][1]["content"] == ""
        assert request["messages"][1]["tool_calls"][0]["id"] == "read-1"
        assert request["messages"][2]["tool_call_id"] == "read-1"
    assert len([event for event in events if isinstance(event, ToolCallResult)]) == 1
    assert messages[3].role == "assistant" and messages[3].content == ""
    assert all(message.content != PLACEHOLDER for message in messages)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert done[-1].stop_reason == StopReason.END_TURN
    assert done[-1].final_content == "The answer is 42."
