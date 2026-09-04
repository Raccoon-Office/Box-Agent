"""Tests for provider-level LLM request/debug logging."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from box_agent.acp.debug_logger import ACPDebugLogger
from box_agent.core import run_agent_loop
from box_agent.llm.debug_logging import (
    reset_llm_debug_sink,
    sanitize_for_logging,
    set_llm_debug_sink,
    summarize_request_payload_for_logging,
)
from box_agent.llm.openai_client import OpenAIClient
from box_agent.logger import AgentLogger
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


def test_acp_debug_logger_honors_explicit_log_path(tmp_path, monkeypatch) -> None:
    log_file = tmp_path / "repo/.logs/box-agent.debug.log"
    log_file.parent.mkdir(parents=True)
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    monkeypatch.setenv("BOX_AGENT_LOG_FILE", str(log_file))

    logger = ACPDebugLogger()
    try:
        logger.error("test/log_path", message="written")
    finally:
        logger.close()

    assert "test/log_path" in log_file.read_text(encoding="utf-8")


def test_sanitize_for_logging_redacts_auth_headers() -> None:
    payload = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
        "extra_headers": {
            "Authorization": "Bearer secret",
            "x-api-key": "secret",
            "X-Trace": "keep",
        },
    }

    sanitized = sanitize_for_logging(payload)

    assert sanitized["messages"][0]["content"] == "hello"
    assert sanitized["extra_headers"]["Authorization"] == "<redacted>"
    assert sanitized["extra_headers"]["x-api-key"] == "<redacted>"
    assert sanitized["extra_headers"]["X-Trace"] == "keep"


@pytest.mark.parametrize(
    "block, redacted_field",
    [
        (
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "secret-image-payload",
                },
            },
            ("source", "data"),
        ),
        (
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64,secret-image-payload",
                },
            },
            ("image_url", "url"),
        ),
    ],
)
def test_sanitize_for_logging_always_redacts_inline_image_payloads(
    block,
    redacted_field,
) -> None:
    sanitized = sanitize_for_logging(block)

    assert "secret-image-payload" not in str(sanitized)
    redacted = sanitized[redacted_field[0]][redacted_field[1]]
    assert redacted["redacted"] is True
    assert redacted["characters"] > 0


def test_summarize_request_payload_for_logging_compacts_large_messages_and_tools() -> None:
    long_text = "x" * 2000
    payload = {
        "model": "m",
        "system": long_text,
        "messages": [
            {"role": "system", "content": long_text},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "write_file", "arguments": long_text},
                    }
                ],
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": long_text,
                    "parameters": {
                        "type": "object",
                        "properties": {f"field_{index}": {"type": "string"} for index in range(25)},
                    },
                },
            }
        ],
    }

    summarized = summarize_request_payload_for_logging(payload)

    assert summarized["_payload_logging"]["mode"] == "summary"
    assert summarized["system"]["characters"] == 2000
    assert summarized["messages"][0]["content"]["characters"] == 2000
    assert summarized["messages"][1]["tool_calls"][0]["function"]["arguments"]["characters"] == 2000
    assert summarized["tools"][0]["function"]["description"]["characters"] == 2000
    assert summarized["tools"][0]["function"]["parameters"]["property_count"] == 25
    assert summarized["tools"][0]["function"]["parameters"]["properties_omitted"] == 5
    assert long_text not in str(summarized)


def test_agent_logger_summarizes_large_records_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BOX_AGENT_LOG_FULL_PAYLOAD", raising=False)
    monkeypatch.delenv("BOX_AGENT_LLM_DEBUG_FULL_PAYLOAD", raising=False)
    long_text = "x" * 2000
    agent_logger = AgentLogger()
    agent_logger.log_dir = tmp_path
    agent_logger.start_new_run()

    agent_logger.log_request([Message(role="user", content=long_text)], tools=[])
    agent_logger.log_response(
        long_text,
        tool_calls=[
            ToolCall(
                id="t1",
                type="function",
                function=FunctionCall(name="write_file", arguments={"content": long_text}),
            )
        ],
    )
    agent_logger.log_tool_result(
        "read_file",
        {"path": "deck.html"},
        result_success=True,
        result_content=long_text,
        raw_output={"full": long_text},
        tool_id="mcp:files/read_file",
        server_name="files",
    )

    log_text = agent_logger.get_log_file_path().read_text(encoding="utf-8")
    assert long_text not in log_text
    assert '"characters": 2000' in log_text
    assert '"mode": "summary"' in log_text
    assert '"tool_id": "mcp:files/read_file"' in log_text
    assert '"server_name": "files"' in log_text


def test_agent_logger_records_cache_fingerprint(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("BOX_AGENT_LOG_FULL_PAYLOAD", raising=False)
    monkeypatch.delenv("BOX_AGENT_LLM_DEBUG_FULL_PAYLOAD", raising=False)
    agent_logger = AgentLogger()
    agent_logger.log_dir = tmp_path
    agent_logger.start_new_run()

    agent_logger.log_request(
        [Message(role="system", content="system"), Message(role="user", content="hi")],
        tools=[],
        cache_fingerprint={
            "system_prompt_hash": "sha256:system",
            "tool_schema_hash": "sha256:tools",
            "mcp_tool_schema_hash": "sha256:mcp",
            "filtered_skill_names": ["pptx"],
            "preloaded_skill_names": ["pptx"],
        },
    )

    log_text = agent_logger.get_log_file_path().read_text(encoding="utf-8")
    assert '"cache_fingerprint"' in log_text
    assert '"system_prompt_hash": "sha256:system"' in log_text
    assert '"tool_schema_hash": "sha256:tools"' in log_text
    assert '"mcp_tool_schema_hash": "sha256:mcp"' in log_text
    assert '"pptx"' in log_text


def test_agent_logger_uses_unique_run_files_within_one_second(tmp_path) -> None:
    agent_logger = AgentLogger()
    agent_logger.log_dir = tmp_path

    agent_logger.start_new_run()
    first = agent_logger.get_log_file_path()
    agent_logger.start_new_run()
    second = agent_logger.get_log_file_path()

    assert first != second
    assert first.exists()
    assert second.exists()


@pytest.mark.asyncio
async def test_openai_completion_logs_request_and_request_id(monkeypatch) -> None:
    records: list[dict] = []
    client = OpenAIClient(api_key="k", api_base="https://x.example/v1", model="m")

    parsed = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None, reasoning_details=None))],
        usage=None,
    )

    class FakeRawResponse:
        request_id = "req-openai-123"
        headers = {"x-request-id": "req-openai-123"}

        def parse(self):
            return parsed

    class FakeRawCompletions:
        async def create(self, **_params):
            return FakeRawResponse()

    class FakeCompletions:
        with_raw_response = FakeRawCompletions()

    class FakeChat:
        completions = FakeCompletions()

    client.client.chat = FakeChat()
    monkeypatch.delenv("BOX_AGENT_LLM_DEBUG", raising=False)

    token = set_llm_debug_sink(records.append)
    try:
        await client._make_api_request(
            [{"role": "user", "content": "hi"}],
            thinking_enabled=True,
        )
    finally:
        reset_llm_debug_sink(token)

    request_record = next(record for record in records if record["event"] == "llm/request")
    response_record = next(record for record in records if record["event"] == "llm/response_meta")
    assert request_record["provider"] == "openai"
    assert request_record["mode"] == "completion"
    assert request_record["payload"]["model"] == "m"
    assert request_record["payload"]["max_tokens"] == 64000
    assert request_record["payload"]["messages"][0]["content"] == "hi"
    assert request_record["payload"]["_payload_logging"]["mode"] == "summary"
    assert response_record["request_id"] == "req-openai-123"


@pytest.mark.asyncio
async def test_run_agent_loop_writes_llm_debug_records_to_agent_log(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BOX_AGENT_LLM_DEBUG", "1")

    class DebugLLM:
        async def generate_stream(self, *, messages, tools, thinking_enabled=False, session_id="", **_):
            from box_agent.llm.debug_logging import log_llm_request, log_llm_response_meta

            log_llm_request(
                provider="openai",
                mode="stream",
                api_base="https://x.example/v1",
                params={
                    "model": "m",
                    "messages": [{"role": "user", "content": "hi"}],
                    "extra_headers": {"Authorization": "Bearer secret"},
                },
            )
            log_llm_response_meta(
                provider="openai",
                mode="stream",
                request_id="req-logger-123",
                headers={"x-request-id": "req-logger-123"},
            )
            yield StreamEvent(type="text", delta="ok")
            yield StreamEvent(type="finish", finish_reason="stop", provider_request_id="req-logger-123")

    agent_logger = AgentLogger()
    agent_logger.log_dir = tmp_path

    events = []
    async for event in run_agent_loop(
        llm=DebugLLM(),
        messages=[Message(role="user", content="hi")],
        tools={},
        max_steps=1,
        logger=agent_logger,
    ):
        events.append(event)

    log_text = agent_logger.get_log_file_path().read_text(encoding="utf-8")
    assert "LLM_DEBUG" in log_text
    assert "llm/request" in log_text
    assert "llm/response_meta" in log_text
    assert "req-logger-123" in log_text
    assert "Bearer secret" not in log_text
    assert "<redacted>" in log_text


@pytest.mark.asyncio
async def test_run_agent_loop_logs_cache_fingerprint_context(tmp_path) -> None:
    class OneShotLLM:
        async def generate_stream(self, *, messages, tools, thinking_enabled=False, session_id="", **_):
            yield StreamEvent(type="text", delta="ok")
            yield StreamEvent(type="finish", finish_reason="stop")

    class DemoTool(Tool):
        @property
        def name(self) -> str:
            return "demo"

        @property
        def description(self) -> str:
            return "Demo"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self) -> ToolResult:
            return ToolResult(success=True, content="ok")

    agent_logger = AgentLogger()
    agent_logger.log_dir = tmp_path
    seen_fingerprints: list[dict] = []

    async for _event in run_agent_loop(
        llm=OneShotLLM(),
        messages=[Message(role="system", content="system"), Message(role="user", content="hi")],
        tools={"demo": DemoTool()},
        max_steps=1,
        logger=agent_logger,
        cache_fingerprint_context={
            "filtered_skill_names": ["pptx"],
            "preloaded_skill_names": ["pptx"],
        },
        cache_fingerprint_sink=seen_fingerprints.append,
    ):
        pass

    log_text = agent_logger.get_log_file_path().read_text(encoding="utf-8")
    assert '"cache_fingerprint"' in log_text
    assert '"system_prompt_hash": "sha256:' in log_text
    assert '"tool_schema_hash": "sha256:' in log_text
    assert '"mcp_tool_schema_hash": "sha256:' in log_text
    assert '"filtered_skill_names": [' in log_text
    assert '"preloaded_skill_names": [' in log_text
    assert seen_fingerprints
    assert seen_fingerprints[0]["filtered_skill_names"] == ["pptx"]
