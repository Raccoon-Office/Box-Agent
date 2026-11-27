"""Anthropic prompt-cache breakpoints: stable prefix + rolling conversation tail."""
import copy
import json

import pytest

from box_agent.llm.anthropic_client import AnthropicClient, apply_cache_breakpoints, prompt_cache_enabled
from box_agent.schema import FunctionCall, Message, ToolCall

EPHEMERAL = {"type": "ephemeral"}
TOOLS = [{"name": "read_file", "description": "Read", "parameters": {"type": "object"}},
         {"name": "bash", "description": "Run", "parameters": {"type": "object"}}]


def _client():
    return AnthropicClient(api_key="test", model="claude-sonnet-4-5", max_output_tokens=4096)


def _markers(params):
    return json.dumps(params, ensure_ascii=False).count('"cache_control"')


def _conversation():
    call = ToolCall(id="t1", type="function", function=FunctionCall(name="read_file", arguments={"path": "a"}))
    return [
        Message(role="system", content="系统指令"),
        Message(role="user", content=[{"type": "text", "text": "读一下 a"}]),
        Message(role="assistant", content="", thinking="想一想", tool_calls=[call]),
        Message(role="tool", content="文件内容", tool_call_id="t1"),
    ]


def test_system_and_tail_are_marked_and_history_is_not_mutated(monkeypatch):
    monkeypatch.delenv("BOX_AGENT_PROMPT_CACHE", raising=False)
    client = _client()
    messages = _conversation()
    before = [m.model_dump() for m in messages]
    system, api_messages = client._convert_messages(messages)
    params = client._request_body_params(system, api_messages, TOOLS, thinking_enabled=False)

    assert params["system"] == [{"type": "text", "text": "系统指令", "cache_control": EPHEMERAL}]
    assert all("cache_control" not in tool for tool in params["tools"])  # covered by the system breakpoint
    tail = params["messages"][-1]["content"][-1]
    assert tail["type"] == "tool_result" and tail["cache_control"] == EPHEMERAL
    assert _markers(params) == 2  # well under Anthropic's limit of four
    assert [m.model_dump() for m in messages] == before


def test_breakpoint_skips_thinking_and_empty_text_blocks():
    params = {"messages": [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "答"}, {"type": "thinking", "thinking": "x"},
                                          {"type": "text", "text": ""}]},
    ]}
    apply_cache_breakpoints(params)
    blocks = params["messages"][-1]["content"]
    assert blocks[0]["cache_control"] == EPHEMERAL
    assert "cache_control" not in blocks[1] and "cache_control" not in blocks[2]


def test_string_user_content_becomes_one_marked_block_and_empty_string_is_skipped():
    params = {"messages": [{"role": "user", "content": "你好"}, {"role": "assistant", "content": ""}]}
    apply_cache_breakpoints(params)
    assert params["messages"][0]["content"] == [{"type": "text", "text": "你好", "cache_control": EPHEMERAL}]
    assert params["messages"][1]["content"] == ""


def test_without_system_the_last_tool_carries_the_prefix_breakpoint():
    tools = [{"name": "a"}, {"name": "b"}]
    original = copy.deepcopy(tools)
    params = {"tools": tools, "messages": []}
    apply_cache_breakpoints(params)
    assert params["tools"][-1]["cache_control"] == EPHEMERAL and "cache_control" not in params["tools"][0]
    assert tools == original


def test_previous_step_prefix_is_unchanged_in_the_next_step(monkeypatch):
    # The rolling breakpoint moves forward; every earlier block stays
    # byte-identical (apart from the old marker), so step N+1 reads step N's cache.
    monkeypatch.delenv("BOX_AGENT_PROMPT_CACHE", raising=False)
    client = _client()
    first = _conversation()
    second = [*first, Message(role="assistant", content="读完了")]
    p1 = client._request_body_params(*client._convert_messages(first), TOOLS, thinking_enabled=False)
    p2 = client._request_body_params(*client._convert_messages(second), TOOLS, thinking_enabled=False)
    assert p1["system"] == p2["system"] and p1["tools"] == p2["tools"]

    def strip(value):
        return json.loads(json.dumps(value).replace(', "cache_control": {"type": "ephemeral"}', ""))
    assert strip(p2["messages"][: len(p1["messages"])]) == strip(p1["messages"])


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no"])
def test_environment_opt_out_sends_the_historical_request(monkeypatch, value):
    monkeypatch.setenv("BOX_AGENT_PROMPT_CACHE", value)
    assert not prompt_cache_enabled()
    client = _client()
    params = client._request_body_params(*client._convert_messages(_conversation()), TOOLS, thinking_enabled=False)
    assert params["system"] == "系统指令"
    assert _markers(params) == 0


ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}


def _params_for(content, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_PROMPT_CACHE", raising=False)
    client = _client()
    messages = [Message(role="system", content="系统指令"), Message(role="user", content=content)]
    return client._request_body_params(*client._convert_messages(messages), TOOLS, thinking_enabled=False)


def test_caller_with_four_breakpoints_is_left_at_four(monkeypatch):
    blocks = [{"type": "text", "text": f"doc{i}", "cache_control": EPHEMERAL} for i in range(4)]
    params = _params_for([*blocks, {"type": "text", "text": "问题"}], monkeypatch)
    assert _markers(params) == 4
    assert params["system"] == "系统指令"


def test_caller_one_hour_tail_is_not_overwritten(monkeypatch):
    params = _params_for([{"type": "text", "text": "问题", "cache_control": ONE_HOUR}], monkeypatch)
    assert params["messages"][-1]["content"][-1]["cache_control"] == ONE_HOUR
    assert _markers(params) == 1


def test_no_shorter_ttl_is_inserted_before_a_caller_one_hour_breakpoint(monkeypatch):
    params = _params_for([{"type": "text", "text": "文档", "cache_control": ONE_HOUR},
                          {"type": "text", "text": "问题"}], monkeypatch)
    assert params["system"] == "系统指令"
    assert [block.get("cache_control") for block in params["messages"][-1]["content"]] == [ONE_HOUR, None]


def test_mentioning_cache_control_in_text_does_not_disable_breakpoints(monkeypatch):
    params = _params_for([{"type": "text", "text": 'Explain the "cache_control" field'}], monkeypatch)
    assert _markers(params) == 2
