"""Tests for session-level deep-think / extended thinking passthrough."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import AsyncOpenAI

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.llm.anthropic_client import AnthropicClient
from box_agent.llm.openai_client import OpenAIClient, _tool_parameter_types
from box_agent.schema import Message, StreamEvent
from box_agent.tools.base import Tool, ToolResult


# ───────────────────────── Anthropic ─────────────────────────


def test_sensenova_parameter_type_index_rejects_generated_alias_collision():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read-file",
                "parameters": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                },
            },
        },
    ]

    with pytest.raises(ValueError, match="read-file.*read_file"):
        _tool_parameter_types(tools)

@pytest.mark.asyncio
async def test_anthropic_request_has_thinking_when_enabled(monkeypatch):
    """AnthropicClient injects the ``thinking`` param when ``thinking_enabled=True``."""
    client = AnthropicClient(api_key="k", api_base="https://x.example", model="claude-3")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        return SimpleNamespace(content=[], usage=None, stop_reason="stop")

    monkeypatch.setattr(client.client.messages, "create", fake_create)

    await client._make_api_request(
        system_message="hi",
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
        thinking_enabled=True,
    )

    assert captured["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert captured["max_tokens"] > 8000  # budget must be strictly less than max_tokens


@pytest.mark.asyncio
async def test_anthropic_request_no_thinking_by_default(monkeypatch):
    client = AnthropicClient(api_key="k", api_base="https://x.example", model="claude-3")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        return SimpleNamespace(content=[], usage=None, stop_reason="stop")

    monkeypatch.setattr(client.client.messages, "create", fake_create)

    await client._make_api_request(
        system_message=None,
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
    )

    assert "thinking" not in captured


# ───────────────────────── OpenAI ─────────────────────────

@pytest.mark.asyncio
async def test_openai_request_sends_high_reasoning_effort_when_enabled(monkeypatch):
    """Generic OpenAI-compatible models receive high reasoning effort."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="gpt-5")

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        choice = SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=None, reasoning_details=None),
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(client.client.chat.completions, "create", fake_create)

    await client._make_api_request(
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
        thinking_enabled=True,
    )

    assert "extra_body" not in captured
    assert captured["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thinking_enabled", "expected_effort"),
    [(True, "high"), (False, None)],
)
async def test_sensenova_request_uses_compatible_reasoning_control(
    thinking_enabled,
    expected_effort,
    monkeypatch,
):
    """SenseNova models receive their documented top-level reasoning control."""
    client = OpenAIClient(
        api_key="k",
        api_base="https://token.sensenova.cn/v1",
        model="SenseNova-Flash-Lite-20260727-v39-fp8-step4k-dpov2-mtp",
    )
    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        choice = SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=None, reasoning_details=None),
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(client.client.chat.completions, "create", fake_create)

    await client._make_api_request(
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
        thinking_enabled=thinking_enabled,
    )

    assert "extra_body" not in captured
    if expected_effort is None:
        assert "reasoning_effort" not in captured
    else:
        assert captured["reasoning_effort"] == expected_effort


@pytest.mark.asyncio
async def test_openai_request_no_extra_body_by_default(monkeypatch):
    """Default path sends no ``extra_body`` — especially no ``reasoning_split`` (deleted)."""
    client = OpenAIClient(
        api_key="k",
        api_base="https://x.example",
        model="custom-chat-model",
    )

    captured: dict = {}

    async def fake_create(**params):
        captured.update(params)
        choice = SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=None, reasoning_details=None),
        )
        return SimpleNamespace(choices=[choice], usage=None)

    monkeypatch.setattr(client.client.chat.completions, "create", fake_create)

    await client._make_api_request(
        api_messages=[{"role": "user", "content": "go"}],
        tools=None,
    )

    assert "extra_body" not in captured, "extra_body must not be sent by default"
    assert "reasoning_effort" not in captured, "reasoning_effort must not be sent by default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "thinking_enabled", "expected_extra_body", "expected_effort"),
    [
        ("gpt-5", True, None, "high"),
        ("custom-chat-model", False, None, None),
        ("SenseNova-Flash-Lite-test", False, None, None),
        ("SenseNova-Flash-Lite-test", True, None, "high"),
        ("SN-SenseNova-6-8-Flash-Lite", True, None, "high"),
        (
            "SN-DeepSeek-V4-Pro",
            True,
            {"extra_body": {"thinking": {"type": "enabled"}}},
            None,
        ),
        (
            "deepseek-v4-flash",
            False,
            {"extra_body": {"thinking": {"type": "disabled"}}},
            None,
        ),
        ("QWEN3-235B", True, {"enable_thinking": True}, None),
        ("vendor/qwen3-coder", False, {"enable_thinking": False}, None),
        ("Gemini-2.5-Flash", False, None, "none"),
        ("Gemini-2.5-Pro", False, None, None),
        ("GEMINI-3.1-PRO-preview", False, None, None),
        ("gemini-3-flash", True, None, "high"),
        (
            "Doubao-Seed",
            True,
            {"extra_body": {"thinking": {"type": "enabled"}}},
            None,
        ),
        (
            "ark/doubao-pro",
            False,
            {"extra_body": {"thinking": {"type": "disabled"}}},
            None,
        ),
    ],
)
async def test_openai_stream_request_maps_thinking_to_provider_dialect(
    model,
    thinking_enabled,
    expected_extra_body,
    expected_effort,
    monkeypatch,
):
    """Streaming requests use the same model-specific mapping as completions."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model=model)
    captured: dict = {}

    delta = SimpleNamespace(
        content="ok",
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    chunk = SimpleNamespace(
        id="resp-123",
        usage=None,
        choices=[SimpleNamespace(finish_reason="stop", delta=delta)],
    )

    async def response_stream():
        yield chunk

    async def fake_create(**params):
        captured.update(params)
        return SimpleNamespace(
            request_id="req-123",
            headers={},
            parse=response_stream,
        )

    monkeypatch.setattr(
        client.client.chat.completions.with_raw_response,
        "create",
        fake_create,
    )

    events = [
        event
        async for event in client.generate_stream(
            [Message(role="user", content="go")],
            thinking_enabled=thinking_enabled,
        )
    ]

    assert [event.delta for event in events if event.type == "text"] == ["ok"]
    if expected_extra_body is None:
        assert "extra_body" not in captured
    else:
        assert captured["extra_body"] == expected_extra_body
    if expected_effort is not None:
        assert captured["reasoning_effort"] == expected_effort
    else:
        assert "reasoning_effort" not in captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "returned_name",
    ["staged_file_write", "staged-file-write"],
    ids=["canonical", "generated-hyphenated-alias"],
)
async def test_sensenova_stream_recovers_allowed_tool_call_from_thinking(
    returned_name,
    monkeypatch,
):
    client = OpenAIClient(
        api_key="k",
        api_base="https://token.sensenova.cn/v1",
        model="sn-sensenova-6-8-flash-lite",
    )
    pseudo_call = f"""
<tool_call>
<function={returned_name}>
<parameter=action>
append_text
</parameter>
<parameter=chunk_index>
0
</parameter>
<parameter=content>
<html>recovered</html>
</parameter>
</function>
</tool_call>
"""
    delta = SimpleNamespace(
        content=None,
        tool_calls=None,
        reasoning=pseudo_call,
        reasoning_content=None,
        reasoning_details=None,
    )
    chunk = SimpleNamespace(
        id="resp-recovered",
        usage=None,
        choices=[SimpleNamespace(finish_reason="stop", delta=delta)],
    )

    async def response_stream():
        yield chunk

    async def fake_create(**_params):
        return SimpleNamespace(request_id="req-recovered", headers={}, parse=response_stream)

    monkeypatch.setattr(
        client.client.chat.completions.with_raw_response,
        "create",
        fake_create,
    )
    tool = {
        "type": "function",
        "function": {
            "name": "staged_file_write",
            "description": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "chunk_index": {"type": "integer"},
                    "content": {"type": "string"},
                },
            },
        },
    }

    events = [
        event
        async for event in client.generate_stream(
            [Message(role="user", content="go")],
            tools=[tool],
            thinking_enabled=True,
        )
    ]
    finish = next(event for event in events if event.type == "finish")

    assert finish.raw_finish_reason == "stop"
    assert finish.finish_reason == "tool_calls"
    assert finish.tool_calls is not None
    assert finish.tool_calls[0].function.name == returned_name
    assert finish.tool_calls[0].function.arguments == {
        "action": "append_text",
        "chunk_index": 0,
        "content": "<html>recovered</html>",
    }


@pytest.mark.asyncio
async def test_sensenova_stream_recovers_declared_alias_from_tool_only_content(
    monkeypatch,
):
    client = OpenAIClient(
        api_key="k",
        api_base="https://token.sensenova.cn/v1",
        model="SenseNova-Flash-Lite-test",
    )
    captured: dict = {}
    pseudo_call = """
<tool_call>
<function=legacy_alias_probe>
<parameter=value>
flash-lite-live
</parameter>
</function>
</tool_call>
"""
    delta = SimpleNamespace(
        content=pseudo_call,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    chunk = SimpleNamespace(
        id="resp-alias-recovered",
        usage=None,
        choices=[SimpleNamespace(finish_reason="stop", delta=delta)],
    )

    async def response_stream():
        yield chunk

    async def fake_create(**params):
        captured.update(params)
        return SimpleNamespace(
            request_id="req-alias-recovered",
            headers={},
            parse=response_stream,
        )

    monkeypatch.setattr(
        client.client.chat.completions.with_raw_response,
        "create",
        fake_create,
    )

    class AliasProbeTool(Tool):
        aliases = ("legacy_alias_probe",)

        @property
        def name(self) -> str:
            return "alias_probe"

        @property
        def description(self) -> str:
            return "Probe alias recovery."

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }

        async def execute(self, value: str) -> ToolResult:
            return ToolResult(success=True, content=value)

    events = [
        event
        async for event in client.generate_stream(
            [Message(role="user", content="go")],
            tools=[AliasProbeTool()],
            thinking_enabled=True,
        )
    ]
    finish = next(event for event in events if event.type == "finish")

    offered_names = [tool["function"]["name"] for tool in captured["tools"]]
    assert offered_names == ["alias_probe"]
    assert [event for event in events if event.type == "text"] == []
    assert finish.raw_finish_reason == "stop"
    assert finish.finish_reason == "tool_calls"
    assert finish.tool_calls is not None
    assert finish.tool_calls[0].function.name == "legacy_alias_probe"
    assert finish.tool_calls[0].function.arguments == {
        "value": "flash-lite-live"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aliases", "prefix"),
    [
        ((), ""),
        (("legacy_alias_probe",), "This is explanatory text, not a tool call.\n"),
    ],
    ids=["undeclared-alias", "mixed-visible-text"],
)
async def test_sensenova_stream_keeps_unsafe_pseudo_calls_as_visible_text(
    aliases,
    prefix,
    monkeypatch,
):
    client = OpenAIClient(
        api_key="k",
        api_base="https://token.sensenova.cn/v1",
        model="SenseNova-Flash-Lite-test",
    )
    pseudo_call = (
        f"{prefix}<tool_call>\n"
        "<function=legacy_alias_probe>\n"
        "<parameter=value>flash-lite-live</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    delta = SimpleNamespace(
        content=pseudo_call,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    chunk = SimpleNamespace(
        id="resp-unsafe-pseudo-call",
        usage=None,
        choices=[SimpleNamespace(finish_reason="stop", delta=delta)],
    )

    async def response_stream():
        yield chunk

    async def fake_create(**_params):
        return SimpleNamespace(
            request_id="req-unsafe-pseudo-call",
            headers={},
            parse=response_stream,
        )

    monkeypatch.setattr(
        client.client.chat.completions.with_raw_response,
        "create",
        fake_create,
    )

    class AliasProbeTool(Tool):
        def __init__(self) -> None:
            self.aliases = aliases

        @property
        def name(self) -> str:
            return "alias_probe"

        @property
        def description(self) -> str:
            return "Probe alias recovery."

        @property
        def parameters(self) -> dict:
            return {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            }

        async def execute(self, value: str) -> ToolResult:
            return ToolResult(success=True, content=value)

    events = [
        event
        async for event in client.generate_stream(
            [Message(role="user", content="go")],
            tools=[AliasProbeTool()],
            thinking_enabled=True,
        )
    ]
    finish = next(event for event in events if event.type == "finish")

    streamed_text = "".join(
        event.delta or "" for event in events if event.type == "text"
    )
    assert streamed_text == pseudo_call
    assert finish.finish_reason == "stop"
    assert finish.tool_calls is None


@pytest.mark.asyncio
async def test_generic_openai_stream_does_not_execute_tool_markup_from_thinking(monkeypatch):
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="qwen")
    delta = SimpleNamespace(
        content=None,
        tool_calls=None,
        reasoning="<tool_call><function=echo></function></tool_call>",
        reasoning_content=None,
        reasoning_details=None,
    )
    chunk = SimpleNamespace(
        id="resp-generic",
        usage=None,
        choices=[SimpleNamespace(finish_reason="stop", delta=delta)],
    )

    async def response_stream():
        yield chunk

    async def fake_create(**_params):
        return SimpleNamespace(request_id="req-generic", headers={}, parse=response_stream)

    monkeypatch.setattr(
        client.client.chat.completions.with_raw_response,
        "create",
        fake_create,
    )
    tool = {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "echo",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    events = [
        event
        async for event in client.generate_stream(
            [Message(role="user", content="go")],
            tools=[tool],
            thinking_enabled=True,
        )
    ]
    finish = next(event for event in events if event.type == "finish")

    assert finish.finish_reason == "stop"
    assert finish.tool_calls is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thinking_enabled", "expected_effort"),
    [(True, "high"), (False, None)],
)
async def test_sensenova_sdk_uses_compatible_reasoning_control_in_wire_body(
    thinking_enabled,
    expected_effort,
):
    """The SDK sends SenseNova reasoning control at the HTTP body top level."""
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "SenseNova-Flash-Lite-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIClient(
        api_key="k",
        api_base="https://token.sensenova.cn/v1",
        model="SenseNova-Flash-Lite-test",
    )
    await client.client.close()
    client.client = AsyncOpenAI(
        api_key="k",
        base_url="https://token.sensenova.cn/v1",
        http_client=http_client,
    )

    try:
        await client._make_api_request(
            api_messages=[{"role": "user", "content": "go"}],
            tools=None,
            thinking_enabled=thinking_enabled,
        )
    finally:
        await client.client.close()

    assert "extra_body" not in captured
    assert "chat_template_kwargs" not in captured
    if expected_effort is None:
        assert "reasoning_effort" not in captured
    else:
        assert captured["reasoning_effort"] == expected_effort


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "thinking_enabled", "expected_control"),
    [
        ("raccoon-8c4485", True, {"reasoning_effort": "high"}),
        ("raccoon-8c4485", False, {}),
        ("raccoon-19b265", True, {"reasoning_effort": "high"}),
        ("raccoon-19b265", False, {}),
        ("raccoon-405a1c", True, {"reasoning_effort": "high"}),
        ("raccoon-405a1c", False, {}),
        ("sn-sensenova-6-8-flash-lite", True, {"reasoning_effort": "high"}),
        ("sn-sensenova-6-8-flash-lite", False, {}),
        ("sn-glm-5-2", True, {"reasoning_effort": "high"}),
        ("sn-glm-5-2", False, {}),
        (
            "SN-DeepSeek-V4-Pro",
            True,
            {"extra_body": {"thinking": {"type": "enabled"}}},
        ),
        (
            "sn-deepseek-v4-pro",
            False,
            {"extra_body": {"thinking": {"type": "disabled"}}},
        ),
        ("QWEN3-235B", True, {"enable_thinking": True}),
        ("vendor/qwen3-coder", False, {"enable_thinking": False}),
        ("Gemini-2.5-Flash", True, {"reasoning_effort": "high"}),
        ("Gemini-2.5-Flash", False, {"reasoning_effort": "none"}),
        ("gemini-2.5-pro-preview", False, {}),
        ("GEMINI-3.1-PRO", False, {}),
        (
            "Doubao-Seed",
            True,
            {"extra_body": {"thinking": {"type": "enabled"}}},
        ),
        (
            "ark/doubao-pro",
            False,
            {"extra_body": {"thinking": {"type": "disabled"}}},
        ),
    ],
)
async def test_model_family_thinking_control_wire_body(
    model,
    thinking_enabled,
    expected_control,
):
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIClient(
        api_key="k",
        api_base="https://xiaohuanxiong.com/api/web/llm/v2",
        model=model,
    )
    await client.client.close()
    client.client = AsyncOpenAI(
        api_key="k",
        base_url="https://xiaohuanxiong.com/api/web/llm/v2",
        http_client=http_client,
    )

    try:
        await client._make_api_request(
            api_messages=[{"role": "user", "content": "go"}],
            tools=None,
            thinking_enabled=thinking_enabled,
        )
    finally:
        await client.client.close()

    assert "chat_template_kwargs" not in captured
    actual_control = {
        key: captured[key]
        for key in ("reasoning_effort", "extra_body", "enable_thinking")
        if key in captured
    }
    assert actual_control == expected_control


@pytest.mark.asyncio
async def test_openai_raw_response_parse_may_be_sync():
    """OpenAI SDK raw responses can parse to a direct ChatCompletion object."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="qwen")

    parsed = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None, reasoning_details=None),
            )
        ],
        usage=None,
    )

    class FakeRawResponse:
        request_id = "req-123"
        headers = {}

        def parse(self):
            return parsed

    class FakeRawCompletions:
        async def create(self, **params):
            return FakeRawResponse()

    class FakeCompletions:
        with_raw_response = FakeRawCompletions()

    class FakeChat:
        completions = FakeCompletions()

    client.client.chat = FakeChat()

    assert await client._make_api_request([{"role": "user", "content": "go"}]) is parsed


@pytest.mark.parametrize(
    "reasoning_fields",
    [
        {"reasoning": "private reasoning"},
        {"reasoning_content": "private reasoning"},
        {"reasoning_details": [SimpleNamespace(text="private reasoning")]},
    ],
    ids=["reasoning", "reasoning_content", "reasoning_details"],
)
def test_openai_response_parses_reasoning_aliases(reasoning_fields):
    """Provider-specific response reasoning fields are preserved as thinking."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="qwen")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="answer",
                    tool_calls=None,
                    **reasoning_fields,
                ),
            )
        ],
        usage=None,
    )

    parsed = client._parse_response(response)

    assert parsed.content == "answer"
    assert parsed.thinking == "private reasoning"


@pytest.mark.parametrize("reasoning_field", ["reasoning", "reasoning_content"])
def test_openai_reasoning_alias_round_trip_uses_canonical_details(reasoning_field):
    """Inbound aliases normalize to the existing outbound history contract."""
    client = OpenAIClient(api_key="k", api_base="https://x.example", model="qwen")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content="answer",
                    tool_calls=None,
                    **{reasoning_field: "private reasoning"},
                ),
            )
        ],
        usage=None,
    )

    parsed = client._parse_response(response)
    _, api_messages = client._convert_messages(
        [Message(role="assistant", content=parsed.content, thinking=parsed.thinking)]
    )

    assert api_messages == [
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_details": [{"text": "private reasoning"}],
        }
    ]


# ───────────────────────── Core plumbing ─────────────────────────

@pytest.mark.asyncio
async def test_run_agent_loop_forwards_thinking_flag():
    """``run_agent_loop(thinking_enabled=True)`` must thread the flag into ``generate_stream``."""
    from box_agent.core import run_agent_loop

    captured: dict = {}

    class _LLM:
        async def generate_stream(self, *, messages, tools, thinking_enabled=False, session_id="", **_):
            captured["thinking_enabled"] = thinking_enabled
            captured["session_id"] = session_id
            yield StreamEvent(type="text", delta="hi")
            yield StreamEvent(type="finish", finish_reason="stop")

        async def generate(self, messages, tools=None, *, thinking_enabled=False, session_id="", **_):
            return SimpleNamespace(content="", thinking=None, tool_calls=None)

    events = []
    async for ev in run_agent_loop(
        llm=_LLM(),
        messages=[Message(role="user", content="ping")],
        tools={},
        max_steps=1,
        thinking_enabled=True,
    ):
        events.append(ev)

    assert captured["thinking_enabled"] is True


# ───────────────────────── ACP wiring ─────────────────────────

@pytest.mark.asyncio
async def test_acp_new_session_reads_deep_think(tmp_path):
    class _Conn:
        async def sessionUpdate(self, payload):
            pass

    class _LLM:
        async def generate(self, *args, **kwargs):
            from box_agent.schema import LLMResponse
            return LLMResponse(content="general", finish_reason="stop")

    config = Config(
        llm=LLMConfig(api_key="k"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(_Conn(), config, _LLM(), [], "base")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={
                "session_mode": "general",
                "deep_think": True,
                "execution_profile": "fast",
            },
        )
    )
    state = agent._sessions[session.sessionId]
    assert state.thinking_enabled is True
    assert state.agent.thinking_enabled is True
    assert state.execution_profile == "fast"


@pytest.mark.asyncio
async def test_acp_new_session_default_no_deep_think(tmp_path):
    class _Conn:
        async def sessionUpdate(self, payload):
            pass

    class _LLM:
        pass

    config = Config(
        llm=LLMConfig(api_key="k"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(_Conn(), config, _LLM(), [], "base")

    session = await agent.newSession(
        SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "general"})
    )
    state = agent._sessions[session.sessionId]
    assert state.thinking_enabled is False
    assert state.agent.thinking_enabled is False
    assert state.execution_profile == "standard"


@pytest.mark.asyncio
async def test_acp_run_turn_uses_agent_facade_with_deep_think(tmp_path, monkeypatch):
    """ACP keeps deep-think on the Agent and supplies host turn options."""
    from box_agent.events import DoneEvent, StopReason

    class _Conn:
        async def sessionUpdate(self, payload):
            pass

    class _LLM:
        async def generate(self, *args, **kwargs):
            from box_agent.schema import LLMResponse
            return LLMResponse(content="general", finish_reason="stop")

    config = Config(
        llm=LLMConfig(api_key="k"),
        agent=AgentConfig(max_steps=1, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(_Conn(), config, _LLM(), [], "base")

    session = await agent.newSession(
        SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"session_mode": "general", "deep_think": True},
        )
    )
    state = agent._sessions[session.sessionId]

    captured: dict = {}

    async def fake_run_events(*, options=None, **kwargs):
        captured["options"] = options
        captured.update(kwargs)
        yield DoneEvent(stop_reason=StopReason.END_TURN, final_content="ok")

    monkeypatch.setattr(state.agent, "run_events", fake_run_events)

    await agent.prompt(
        SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"type": "text", "text": "hi"}],
        )
    )

    options = captured["options"]
    assert state.agent.thinking_enabled is True
    assert options.cache_fingerprint_context is state.agent.cache_fingerprint_context
    assert options.memory_extractor is state.memory_extractor
    assert options.inject_queue is state.inject_queue
    assert options.artifact_root_dir == state.output_dir
    assert options.artifact_detection_enabled is True
    assert callable(options.cache_fingerprint_sink)
