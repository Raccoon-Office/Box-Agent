"""Tests for best-effort per-session JSONL execution traces."""

from __future__ import annotations

from tests.acp_helpers import acp_params

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

import box_agent.session_trace as session_trace_module
from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.core import run_agent_loop
from box_agent.llm.llm_wrapper import LLMClient
from box_agent.schema import (
    FunctionCall,
    LLMProvider,
    Message,
    StreamEvent,
    TokenUsage,
    ToolCall,
)
from box_agent.session_trace import (
    SessionTraceWriter,
    cleanup_session_traces,
    emit_session_trace,
    reset_session_trace_writer,
    safe_session_trace_name,
    scoped_session_trace,
    set_session_trace_writer,
)
from box_agent.tools.base import Tool, ToolResult


def _records(writer: SessionTraceWriter) -> list[dict]:
    return [json.loads(line) for line in writer.file_path.read_text(encoding="utf-8").splitlines()]


class EchoTool(Tool):
    @property
    def name(self):
        return "echo"

    @property
    def description(self):
        return "Echo text"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}}

    async def execute(self, text: str = ""):
        return ToolResult(success=True, content=f"echo:{text}")


def test_writer_appends_jsonl_and_preserves_ids_with_redaction(tmp_path):
    writer = SessionTraceWriter(
        session_id="office/session 1",
        acp_session_id="sess-0-abcd1234",
        trace_dir=tmp_path,
        enabled=True,
    )

    writer.write(
        "turn.input",
        turn_id="turn-1",
        data={"content": "hello", "authorization": "Bearer secret", "total_tokens": 12},
    )
    writer.write("turn.output", turn_id="turn-1", data={"content": "world"})
    same_session_writer = SessionTraceWriter(
        session_id="office/session 1",
        acp_session_id="sess-1-efgh5678",
        trace_dir=tmp_path,
        enabled=True,
    )
    same_session_writer.write("session.start")

    assert writer.file_path.name == safe_session_trace_name("office/session 1")
    assert same_session_writer.file_path == writer.file_path
    records = _records(writer)
    assert [record["event"] for record in records] == [
        "turn.input",
        "turn.output",
        "session.start",
    ]
    assert all(record["session_id"] == "office/session 1" for record in records)
    assert records[:2][0]["acp_session_id"] == "sess-0-abcd1234"
    assert records[:2][1]["acp_session_id"] == "sess-0-abcd1234"
    assert records[2]["acp_session_id"] == "sess-1-efgh5678"
    assert all(record["turn_id"] == "turn-1" for record in records[:2])
    assert records[0]["data"]["authorization"] == "<redacted>"
    assert records[0]["data"]["total_tokens"] == 12


def test_trace_preserves_surrogate_escaped_file_name_and_surrounding_content(tmp_path):
    writer = SessionTraceWriter(session_id="files", acp_session_id="acp", trace_dir=tmp_path, enabled=True)
    filename = "报告-\udcff.txt"
    writer.write("tool.response", data={"filename": filename, "error": "无法打开文件"})
    record, = _records(writer)
    assert record["data"] == {"filename": filename, "error": "无法打开文件"}


def test_writer_failure_never_escapes_into_agent_flow(tmp_path):
    blocked_trace_dir = tmp_path / "not-a-directory"
    blocked_trace_dir.write_text("occupied", encoding="utf-8")
    writer = SessionTraceWriter(
        session_id="office-session-failure",
        acp_session_id="sess-failure",
        trace_dir=blocked_trace_dir,
        enabled=True,
    )

    writer.write("turn.input", turn_id="turn-failure", data={"content": "still safe"})

    assert blocked_trace_dir.read_text(encoding="utf-8") == "occupied"


@pytest.mark.parametrize("source", ["default_link", "environment", "argument"])
def test_unsafe_trace_directory_disables_diagnostics_without_writing_outside_profile(
    tmp_path, monkeypatch, caplog, source,
):
    profile = tmp_path / "profile"
    (profile / "log").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    options = {}
    if source == "default_link":
        try:
            (profile / "log/sessions").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks unavailable on this platform")
    elif source == "environment":
        monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_DIR", str(outside))
    else:
        options["trace_dir"] = outside

    writer = SessionTraceWriter(session_id="unsafe-path", acp_session_id="acp", enabled=True, **options)
    writer.write("session.start", data={"content": "must stay private"})

    assert writer.enabled is False
    assert list(outside.iterdir()) == []
    assert "session trace disabled" in caplog.text
    if source == "default_link":
        assert (profile / "log/sessions").is_symlink()


def test_disabled_trace_writer_does_not_resolve_diagnostic_directory(tmp_path, monkeypatch):
    def unexpected():
        pytest.fail("disabled diagnostics must not resolve a directory")

    monkeypatch.setattr(session_trace_module, "default_session_trace_dir", unexpected)
    writer = SessionTraceWriter(session_id="disabled", acp_session_id="acp", enabled=False)
    writer.write("session.start")


@pytest.mark.asyncio
async def test_acp_continues_with_unsafe_default_trace_directory(tmp_path, monkeypatch, caplog):
    from tests.test_acp import DoneLLM, DummyConn

    profile = tmp_path / "profile"
    workspace = profile / "workspace"
    (profile / "log").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (profile / "log/sessions").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable on this platform")
    monkeypatch.setenv("BOX_AGENT_HOME", str(profile))
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_ENABLED", "1")
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(workspace), enable_memory=False, max_steps=1),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False, enable_sub_agent=False),
    )
    adapter = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    try:
        for text in ("first turn", "second turn"):
            session = await adapter.new_session(str(workspace), session_id="unsafe-trace-session")
            state = adapter._sessions[session.session_id]
            assert state.trace_writer.enabled is False
            result = await adapter.prompt(session.session_id, [{"type": "text", "text": text}])
            assert result.stop_reason == "end_turn"
        assert [message.content for message in state.agent.messages
                if message.role == "user" and message.source != "runtime"] == [
            "first turn", "second turn",
        ]
        assert list(outside.iterdir()) == []
        assert "session trace disabled" in caplog.text
    finally:
        await adapter.aclose()


def _trace_file(path, *, size: int, mtime: float):
    path.write_text("x" * size, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_cleanup_deletes_only_expired_inactive_session_files(tmp_path):
    now = time.time()
    current = _trace_file(tmp_path / "current.jsonl", size=10, mtime=now - 10 * 86400)
    newest = _trace_file(tmp_path / "newest.jsonl", size=10, mtime=now - 100)
    second_newest = _trace_file(tmp_path / "second-newest.jsonl", size=10, mtime=now - 200)
    expired = _trace_file(tmp_path / "expired.jsonl", size=10, mtime=now - 10 * 86400)
    unrelated = _trace_file(tmp_path / "unrelated.log", size=10, mtime=now - 10 * 86400)

    deleted = cleanup_session_traces(
        tmp_path,
        current_file=current,
        now=now,
        retention_days=7,
        max_total_bytes=0,
        active_file_grace_seconds=0,
        protected_recent_files=2,
    )

    assert deleted == [expired]
    assert current.exists()
    assert newest.exists()
    assert second_newest.exists()
    assert unrelated.exists()


def test_cleanup_enforces_directory_cap_oldest_first_without_deleting_recent_files(tmp_path):
    now = time.time()
    oldest = _trace_file(tmp_path / "oldest.jsonl", size=60, mtime=now - 400)
    older = _trace_file(tmp_path / "older.jsonl", size=60, mtime=now - 300)
    recent = _trace_file(tmp_path / "recent.jsonl", size=60, mtime=now - 200)
    newest = _trace_file(tmp_path / "newest.jsonl", size=60, mtime=now - 100)

    deleted = cleanup_session_traces(
        tmp_path,
        now=now,
        retention_days=0,
        max_total_bytes=130,
        active_file_grace_seconds=0,
        protected_recent_files=2,
    )

    assert deleted == [oldest, older]
    assert recent.exists()
    assert newest.exists()


def test_cleanup_skips_a_trace_that_changed_after_the_directory_scan(tmp_path, monkeypatch):
    now = time.time()
    trace = _trace_file(tmp_path / "concurrent.jsonl", size=10, mtime=now - 10 * 86400)
    stale_snapshot = session_trace_module._trace_file_snapshots(tmp_path)
    trace.write_text("x" * 10 + "\nconcurrent append", encoding="utf-8")
    monkeypatch.setattr(
        session_trace_module,
        "_trace_file_snapshots",
        lambda trace_dir: stale_snapshot,
    )

    deleted = cleanup_session_traces(
        tmp_path,
        now=now,
        retention_days=7,
        max_total_bytes=0,
        active_file_grace_seconds=0,
        protected_recent_files=0,
    )

    assert deleted == []
    assert trace.read_text(encoding="utf-8").endswith("concurrent append")


def test_writer_runs_retention_without_changing_current_jsonl_contract(tmp_path, monkeypatch):
    now = time.time()
    expired = _trace_file(tmp_path / "expired.jsonl", size=10, mtime=now - 10 * 86400)
    recent = _trace_file(tmp_path / "recent.jsonl", size=10, mtime=now - 100)
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_RETENTION_ENABLED", "1")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_RETENTION_DAYS", "7")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_MAX_TOTAL_BYTES", "0")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_CLEANUP_INTERVAL_SECONDS", "0")
    writer = SessionTraceWriter(
        session_id="office-session-compatible",
        acp_session_id="sess-compatible",
        trace_dir=tmp_path,
        enabled=True,
    )

    writer.write("turn.input", turn_id="turn-1", data={"content": "unchanged"})

    assert not expired.exists()
    assert recent.exists()
    records = _records(writer)
    assert records == [
        {
            "schema_version": "box-agent-session-trace/v1",
            "timestamp": records[0]["timestamp"],
            "event": "turn.input",
            "session_id": "office-session-compatible",
            "acp_session_id": "sess-compatible",
            "turn_id": "turn-1",
            "data": {"content": "unchanged"},
        }
    ]


def test_writer_retention_can_be_disabled_for_external_log_owners(tmp_path, monkeypatch):
    now = time.time()
    expired = _trace_file(tmp_path / "expired.jsonl", size=10, mtime=now - 10 * 86400)
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_RETENTION_ENABLED", "0")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_CLEANUP_INTERVAL_SECONDS", "0")
    writer = SessionTraceWriter(
        session_id="office-session-retention-disabled",
        acp_session_id="sess-retention-disabled",
        trace_dir=tmp_path,
        enabled=True,
    )

    writer.write("turn.input", data={"content": "keep external ownership"})

    assert expired.exists()
    assert _records(writer)[0]["data"]["content"] == "keep external ownership"


def test_retention_failure_does_not_interrupt_trace_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_RETENTION_ENABLED", "1")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_CLEANUP_INTERVAL_SECONDS", "0")

    def fail_cleanup(*args, **kwargs):
        raise OSError("cleanup unavailable")

    monkeypatch.setattr(session_trace_module, "cleanup_session_traces", fail_cleanup)
    writer = SessionTraceWriter(
        session_id="office-session-cleanup-failure",
        acp_session_id="sess-cleanup-failure",
        trace_dir=tmp_path,
        enabled=True,
    )

    writer.write("turn.input", data={"content": "still recorded"})

    assert _records(writer)[0]["data"]["content"] == "still recorded"


@pytest.mark.asyncio
async def test_scoped_trace_can_be_closed_from_another_task_context(tmp_path):
    writer = SessionTraceWriter(
        session_id="office-session-close",
        acp_session_id="sess-close",
        trace_dir=tmp_path,
        enabled=True,
    )

    async def source():
        try:
            emit_session_trace("source.next")
            yield "event"
        finally:
            emit_session_trace("source.close")

    stream = scoped_session_trace(source(), writer=writer, turn_id="turn-close")
    assert await anext(stream) == "event"
    await asyncio.create_task(stream.aclose())

    assert [record["event"] for record in _records(writer)] == [
        "source.next",
        "source.close",
    ]


@pytest.mark.parametrize("connection_error", [False, True])
async def test_incomplete_stream_keeps_filtered_partial_output_at_default_log_level(tmp_path, monkeypatch, connection_error):
    monkeypatch.delenv("BOX_AGENT_LOG_LEVEL", raising=False)
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)

    class PartialProvider:
        async def generate_stream(self, *args, **kwargs):
            for part in ("ordinary partial reply: pass", "word=", "fixture-partial", " follow-up"):
                yield StreamEvent(type="text", delta=part)
            if connection_error:
                raise ConnectionError("fixture disconnected")

    client = LLMClient(api_key="test", provider=LLMProvider.OPENAI, model="test-model")
    client._client = PartialProvider()
    writer = SessionTraceWriter(session_id="partial", acp_session_id="acp-partial", trace_dir=tmp_path)
    token = set_session_trace_writer(writer, turn_id="partial-turn")
    try:
        stream = client.generate_stream([Message(role="user", content="request")])
        if connection_error:
            with pytest.raises(ConnectionError, match="fixture disconnected"):
                _ = [event async for event in stream]
        else:
            _ = [event async for event in stream]
    finally:
        reset_session_trace_writer(token)
    request, error = _records(writer)
    assert error["event"] == "llm.error" and error["llm_call_id"] == request["llm_call_id"]
    assert error["data"]["partial_content"] == "ordinary partial reply: password=<redacted> follow-up"
    assert error["data"]["error_type"] == ("ConnectionError" if connection_error else "IncompleteStream")


@pytest.mark.asyncio
async def test_llm_wrapper_records_request_response_tokens_and_ttfb(tmp_path):
    class FakeUnderlying:
        retry_callback = None

        async def generate_stream(self, messages, tools=None, **kwargs):
            yield StreamEvent(type="text", delta="hello")
            yield StreamEvent(
                type="finish",
                finish_reason="stop",
                usage=TokenUsage(
                    prompt_tokens=7, completion_tokens=2, total_tokens=9,
                    input_tokens=4, cache_read_input_tokens=3,
                    cache_read_input_tokens_reported=True,
                ),
                provider_response_id="chatcmpl-provider-response-1",
                provider_request_id="provider-request-1",
            )

    client = LLMClient(api_key="test", provider=LLMProvider.ANTHROPIC, model="test-model")
    client._client = FakeUnderlying()
    writer = SessionTraceWriter(
        session_id="office-session-1",
        acp_session_id="sess-1",
        trace_dir=tmp_path,
        enabled=True,
    )
    token = set_session_trace_writer(writer, turn_id="turn-1")
    try:
        events = [
            event
            async for event in client.generate_stream(
                [Message(role="user", content="full input")],
                [EchoTool()],
                session_id="office-session-1",
                turn_id="turn-1",
            )
        ]
    finally:
        reset_session_trace_writer(token)

    assert [event.type for event in events] == ["text", "finish"]
    records = _records(writer)
    request, response = records
    assert request["event"] == "llm.request"
    assert request["data"]["messages"][0]["content"] == "full input"
    assert request["data"]["tools"][0]["name"] == "echo"
    assert response["event"] == "llm.response"
    assert response["llm_call_id"] == request["llm_call_id"]
    assert response["data"]["content"] == "hello"
    assert response["data"]["provider_response_id"] == "chatcmpl-provider-response-1"
    assert response["data"]["provider_request_id"] == "provider-request-1"
    assert response["data"]["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "total_tokens": 9,
        "cached_tokens": 3,
    }
    assert isinstance(response["data"]["timing"]["ttfb_ms"], int)
    assert response["data"]["timing"]["ttfb_ms"] <= response["data"]["timing"]["duration_ms"]


@pytest.mark.asyncio
async def test_llm_wrapper_redacts_request_only_image_payload_from_trace(tmp_path):
    class FakeUnderlying:
        retry_callback = None

        async def generate_stream(self, messages, tools=None, **kwargs):
            yield StreamEvent(type="finish", finish_reason="stop")

    client = LLMClient(api_key="test", provider=LLMProvider.ANTHROPIC, model="test-model")
    client._client = FakeUnderlying()
    writer = SessionTraceWriter(
        session_id="office-session-image",
        acp_session_id="sess-image",
        trace_dir=tmp_path,
        enabled=True,
    )
    token = set_session_trace_writer(writer, turn_id="turn-image")
    try:
        events = [
            event
            async for event in client.generate_stream(
                [
                    Message(
                        role="user",
                        content=[
                            {"type": "text", "text": "Inspect it."},
                            {
                                "type": "input_image",
                                "media_type": "image/png",
                                "data": "secret-base64-payload",
                                "width": 10,
                                "height": 20,
                                "source_bytes": 123,
                                "sha256": "digest",
                            },
                        ],
                        trace_redact_content=True,
                    )
                ],
                session_id="office-session-image",
                turn_id="turn-image",
            )
        ]
    finally:
        reset_session_trace_writer(token)

    assert [event.type for event in events] == ["finish"]
    request = _records(writer)[0]
    serialized = json.dumps(request, ensure_ascii=False)
    assert "secret-base64-payload" not in serialized
    image_block = request["data"]["messages"][0]["content"][1]
    assert image_block == {
        "type": "input_image",
        "media_type": "image/png",
        "width": 10,
        "height": 20,
        "source_bytes": 123,
        "sha256": "digest",
        "redacted": True,
    }


@pytest.mark.asyncio
async def test_core_records_tool_request_and_response_without_changing_events(tmp_path):
    class ToolThenDoneLLM:
        def __init__(self):
            self.calls = 0

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(
                    type="finish",
                    finish_reason="tool",
                    tool_calls=[
                        ToolCall(
                            id="tool-1",
                            type="function",
                            function=FunctionCall(name="echo", arguments={"text": "ping"}),
                        )
                    ],
                )
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    writer = SessionTraceWriter(
        session_id="office-session-2",
        acp_session_id="sess-2",
        trace_dir=tmp_path,
        enabled=True,
    )
    messages = [Message(role="system", content="sys"), Message(role="user", content="go")]
    original_events = run_agent_loop(
        llm=ToolThenDoneLLM(),
        messages=messages,
        tools={"echo": EchoTool()},
        max_steps=3,
        session_id="office-session-2",
        turn_id="turn-2",
    )
    events = [
        event
        async for event in scoped_session_trace(
            original_events,
            writer=writer,
            turn_id="turn-2",
        )
    ]

    records = _records(writer)
    request = next(record for record in records if record["event"] == "tool.request")
    response = next(record for record in records if record["event"] == "tool.response")
    assert request["tool_call_id"] == "tool-1"
    assert request["data"]["arguments"] == {"text": "ping"}
    assert response["tool_call_id"] == "tool-1"
    assert response["data"]["success"] is True
    assert response["data"]["content"] == "echo:ping"
    assert any(type(event).__name__ == "ToolCallStart" for event in events)
    assert any(type(event).__name__ == "ToolCallResult" for event in events)


@pytest.mark.asyncio
async def test_acp_uses_upstream_session_id_with_stable_acp_identity(
    tmp_path,
    monkeypatch,
):
    class DummyConn:
        async def session_update(self, session_id, update, **meta):
            return None

    class DoneLLM:
        async def generate_stream(self, messages, tools=None, **kwargs):
            from box_agent.llm.token_meter import record_usage

            emit_session_trace("test.producer_started")
            yield StreamEvent(type="text", delta="answer")
            usage = TokenUsage(
                prompt_tokens=4, completion_tokens=1, total_tokens=5,
                input_tokens=2, cache_read_input_tokens=2,
                cache_read_input_tokens_reported=True,
            )
            record_usage(usage)
            yield StreamEvent(
                type="finish",
                finish_reason="stop",
                usage=usage,
            )

    isolated_home = str(tmp_path / "home")
    monkeypatch.setenv("HOME", isolated_home)
    monkeypatch.setenv("USERPROFILE", isolated_home)
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_ENABLED", "1")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_DIR", str(tmp_path / "traces"))
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(max_steps=2, workspace_dir=str(tmp_path)),
        tools=ToolsConfig(enable_sub_agent=False),
    )
    agent = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "system")
    session = await agent.new_session(
        **acp_params(SimpleNamespace(
            cwd=str(tmp_path),
            field_meta={"session_id": "office-session-current", "session_mode": "general"},
        ))
    )

    assert session.sessionId.startswith("acp-v1-")
    await agent.prompt(
        **acp_params(SimpleNamespace(
            sessionId=session.sessionId,
            prompt=[{"text": "user input"}],
            field_meta={"turn_id": "turn-current"},
        ))
    )

    trace_path = tmp_path / "traces" / "office-session-current.jsonl"
    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert all(record["session_id"] == "office-session-current" for record in records)
    assert all(record["acp_session_id"] == session.sessionId for record in records)
    assert {record["event"] for record in records} >= {
        "session.start",
        "turn.input",
        "turn.output",
        "turn.end",
        "test.producer_started",
    }
    turn_records = [record for record in records if record["event"].startswith("turn.")]
    assert all(record["turn_id"] == "turn-current" for record in turn_records)
    turn_end = next(record for record in turn_records if record["event"] == "turn.end")
    assert turn_end["data"]["usage"]["cached_tokens"] == 2
    producer = next(record for record in records if record["event"] == "test.producer_started")
    assert producer["turn_id"] == "turn-current"
    emit_session_trace("test.after_prompt")
    assert "test.after_prompt" not in {record["event"] for record in _records(agent._sessions[session.sessionId].trace_writer)}
