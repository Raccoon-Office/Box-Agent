"""Anthropic message conversion: consecutive same-role turns are merged.

Anthropic requires strictly alternating user/assistant turns. Tool results are
emitted as user turns, so a following user message (e.g. a vision_review native
image attachment or an injected note) must be merged into the same user turn
rather than sent as a second consecutive user turn (which Anthropic rejects).
"""

from box_agent.llm import AnthropicClient
from box_agent.schema import FunctionCall, Message, ToolCall


def _client() -> AnthropicClient:
    return AnthropicClient(
        api_key="test-key",
        api_base="https://api.anthropic.com",
        model="claude-sonnet-4-20250514",
    )


def _no_consecutive_same_role(api_messages) -> bool:
    roles = [m["role"] for m in api_messages]
    return all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


def test_tool_result_and_following_user_image_merge_into_one_turn():
    client = _client()
    image_blocks = [
        {"type": "text", "text": "Inspect this."},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
        },
    ]
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="t1",
                    type="function",
                    function=FunctionCall(name="vision_review", arguments={}),
                )
            ],
        ),
        Message(role="tool", content="attached", tool_call_id="t1", name="vision_review"),
        Message(role="user", content=image_blocks),
    ]

    system, api = client._convert_messages(messages)

    assert system == "sys"
    assert _no_consecutive_same_role(api), [m["role"] for m in api]
    # The tool_result and the image blocks live in ONE user turn.
    last = api[-1]
    assert last["role"] == "user"
    block_types = [b.get("type") for b in last["content"]]
    assert "tool_result" in block_types
    assert "image" in block_types


def test_plain_alternating_history_is_unchanged():
    client = _client()
    messages = [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
        Message(role="assistant", content="hello"),
        Message(role="user", content="bye"),
    ]

    system, api = client._convert_messages(messages)

    assert system == "sys"
    assert [m["role"] for m in api] == ["user", "assistant", "user"]
    # String content is preserved as-is when no merge is needed.
    assert api[0]["content"] == "hi"
    assert api[1]["content"] == "hello"
    assert api[2]["content"] == "bye"
