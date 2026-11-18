"""Diagnostic copies retain payloads while removing credentials."""
import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from box_agent.acp.debug_logger import ACPDebugLogger


@pytest.mark.parametrize("level,log_format", [(None, "json"), ("debug", "json"), ("debug", "text")])
async def test_acp_stream_logs_and_session_trace_with_split_credentials(tmp_path, monkeypatch, capsys, level, log_format):
    from box_agent.acp import BoxACPAgent
    from box_agent.llm import LLMClient
    from box_agent.schema import LLMProvider, LLMResponse, StreamEvent
    from tests.test_acp import DummyConn, acp_params
    from tests.test_agent_session import session_config

    # A value without its label must never be sent to a diagnostic sink by itself.
    parts = ["ordinary opening: pass", "word", "=", "fixture-part-A", "fixture-part-B", " ordinary ending."]

    class Provider:
        async def generate_stream(self, *args, **kwargs):
            for part in parts:
                yield StreamEvent(type="text", delta=part)
            yield StreamEvent(type="finish", finish_reason="stop")

        async def generate(self, *args, **kwargs):
            return LLMResponse(content='{"continue":false}', finish_reason="stop")

    if level is None:
        monkeypatch.delenv("BOX_AGENT_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("BOX_AGENT_LOG_LEVEL", level)
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    trace_dir = tmp_path / "traces"
    logfile = tmp_path / "debug.log"
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_DIR", str(trace_dir))
    monkeypatch.setenv("BOX_AGENT_LOG_FILE", str(logfile))
    monkeypatch.setenv("BOX_AGENT_LOG_FORMAT", log_format)
    logger = ACPDebugLogger()
    monkeypatch.setattr("box_agent.acp.log", logger)
    conn = DummyConn()
    model = LLMClient(api_key="test", provider=LLMProvider.OPENAI, model="fixture")
    model._client = Provider()
    adapter = BoxACPAgent(conn, session_config(tmp_path), model, [], "system")
    try:
        session = await adapter.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={"session_mode": "general"})))
        response = await adapter.prompt(session_id=session.sessionId, prompt=[{"type": "text", "text": "inspect streaming"}])
        assert response.stopReason == "end_turn"
        delivered = [u.update.content.text for u in conn.updates if u.update.sessionUpdate == "agent_message_chunk"]
        assert "".join(delivered) == "".join(parts)
    finally:
        await adapter.aclose()
        logger.close()

    trace_file, = trace_dir.glob("*.jsonl")
    rows = [json.loads(line) for line in trace_file.read_text().splitlines()]
    expected = "ordinary opening: password=<redacted> ordinary ending."
    assert any(r["event"] == "llm.response" and r["data"]["content"] == expected for r in rows)
    for output in (capsys.readouterr().err, logfile.read_text()):
        assert "fixture-part-A" not in output and "fixture-part-B" not in output
        if level == "debug":
            assert "content/stream" in output and expected in output
        else:
            assert "content/stream" not in output


@pytest.mark.parametrize("log_format", ["json", "text"])
def test_command_failure_keeps_arguments_error_and_exit_code(tmp_path, monkeypatch, capsys, log_format):
    monkeypatch.setenv("BOX_AGENT_LOG_LEVEL", "info")
    monkeypatch.setenv("BOX_AGENT_LOG_FORMAT", log_format)
    logfile = tmp_path / "agent.log"
    monkeypatch.setenv("BOX_AGENT_LOG_FILE", str(logfile))
    logger = ACPDebugLogger()
    try:
        logger.warn("tool/fail", tool_name="bash", tool_call_id="tc-failure",
                    error="Command failed with exit code 4\nPermission denied\napi_key=fixture-command-secret\n" + "x" * 500,
                    arguments={"command": "printf permission"})
        output = logfile.read_text()
        assert "Permission denied" in output and "printf permission" in output
        assert "exit_code" in output and "tc-failure" in output
        assert "fixture-command-secret" not in output
        assert "x" * 500 in output
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == output
    finally:
        logger.close()


@pytest.mark.parametrize("log_format", ["json", "text"])
@pytest.mark.parametrize("tool_name", ["memory_read", "memory_managed_read", "memory_write", "memory_edit", "memory_save", "echo"])
def test_tool_diagnostics_preserve_bodies_and_redact_only_credentials(
    tmp_path, monkeypatch, capsys, log_format, tool_name,
):
    body = "ordinary memory and tool body"
    logfile = tmp_path / "agent.log"
    monkeypatch.setenv("BOX_AGENT_LOG_LEVEL", "debug")
    monkeypatch.setenv("BOX_AGENT_LOG_FORMAT", log_format)
    monkeypatch.setenv("BOX_AGENT_LOG_FILE", str(logfile))
    monkeypatch.setenv("BOX_AGENT_LOG_FULL_PAYLOAD", "1")
    logger = ACPDebugLogger()
    arguments = {"content": body, "api_key": "fixture-tool-secret"}
    original = deepcopy(arguments)
    try:
        for method, event in [(logger.debug, "tool/start"), (logger.info, "tool/end"),
                              (logger.warn, "tool/fail"), (logger.error, "tool/cancel")]:
            method(event, tool_name=tool_name, tool_call_id="tc-1", arguments=arguments,
                   result=body, error=body + " Authorization: Bearer fixture-error-secret",
                   content=body, raw_output=arguments, source=body, user_visible=True, duration_ms=25)
        try:
            raise ValueError(body + " api_key=fixture-exception-secret")
        except ValueError as exc:
            logger.exception("tool/fail", exc, tool_name=tool_name, tool_call_id="tc-1")
        output = logfile.read_text()
        assert body in output and "tc-1" in output and tool_name in output and "duration_ms" in output
        assert "fixture-tool-secret" not in output
        assert "fixture-error-secret" not in output
        assert "fixture-exception-secret" not in output
        assert "payload_redacted" not in output
        assert "<redacted>" in output
        assert arguments == original
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == output
    finally:
        logger.close()


def test_acp_log_levels_and_payload_previews_are_preserved(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOX_AGENT_LOG_LEVEL", "info")
    monkeypatch.setenv("BOX_AGENT_LOG_FORMAT", "json")
    monkeypatch.delenv("BOX_AGENT_LOG_FILE", raising=False)
    logger = ACPDebugLogger()
    try:
        logger.debug("hidden", content="debug payload")
        logger.info("session/injected", text="instruction preview", content="z" * 500)
        records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
        assert len(records) == 1 and records[0]["event"] == "session/injected"
        assert records[0]["text"] == "instruction preview"
        assert records[0]["content"] == "z" * 200 + "...(500 chars)"
    finally:
        logger.close()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("backend", ["desktop", "local"])
async def test_prompt_failure_keeps_sanitized_diagnostics_and_releases_reservation(
    tmp_path, monkeypatch, capsys, cancelled, backend,
):
    from box_agent.acp import BoxACPAgent, SessionState
    from tests.test_agent_session import session_config

    monkeypatch.setenv("BOX_AGENT_LOG_LEVEL", "debug")
    monkeypatch.delenv("BOX_AGENT_LOG_FILE", raising=False)
    logger = ACPDebugLogger()
    config = session_config(tmp_path)
    config.agent.memory_backend_type = backend
    state = SessionState(agent=SimpleNamespace(session_log=None, workspace_dir=tmp_path), config=config)

    async def fail_prompt(*_):
        try:
            message = "fixture prompt failure api_key=fixture-prompt-secret"
            raise asyncio.CancelledError(message) if cancelled else RuntimeError(message)
        except BaseException as exc:
            logger.exception("session/prompt", exc, session_id="s-1")
            raise

    stub = SimpleNamespace(_config=config, _require_accepting_requests=lambda: None,
                           _session_bindings_in_progress={}, _sessions={"s-1": state},
                           _prompt_for_session=fail_prompt)
    try:
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await BoxACPAgent.prompt(stub, session_id="s-1", prompt=[{"type": "text", "text": "original request"}])
        assert not state._prompt_in_progress
        assert state._prompt_finished.done()
        output = capsys.readouterr().err
        assert "fixture prompt failure" in output
        assert "fixture-prompt-secret" not in output
        assert "payload_redacted" not in output
    finally:
        logger.close()


@pytest.mark.parametrize("full_payload", [False, True])
def test_run_and_provider_logs_keep_payload_policy_and_scrub_credentials(
    tmp_path, monkeypatch, capsys, full_payload,
):
    from box_agent.llm.debug_logging import log_llm_request, reset_llm_debug_sink, set_llm_debug_sink
    from box_agent.logger import AgentLogger
    from box_agent.schema import Message

    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("BOX_AGENT_LLM_DEBUG", "1")
    monkeypatch.setenv("BOX_AGENT_LOG_FULL_PAYLOAD", "1" if full_payload else "0")
    provider_file = tmp_path / "provider.log"
    monkeypatch.setenv("BOX_AGENT_LOG_FILE", str(provider_file))
    text = "ordinary request body " + "x" * 800
    messages = [Message(role="user", content=text + " api_key=fixture-request-secret")]
    original = messages[0].content
    arguments = {"content": "ordinary tool body", "password": "fixture-tool-secret"}
    run_logger = AgentLogger()
    run_logger.start_new_run()
    run_logger.log_request(messages)
    run_logger.log_response("ordinary answer api_key=fixture-response-secret")
    run_logger.log_tool_result("memory_read", arguments, False,
                               result_error="Command failed with exit code 9\nordinary failure api_key=fixture-result-secret")
    records = []
    token = set_llm_debug_sink(records.append)
    params = {"messages": [message.model_dump() for message in messages],
              "headers": {"Authorization": "Bearer fixture-header-secret"}}
    before = deepcopy(params)
    try:
        log_llm_request(provider="openai", mode="utility", api_base="https://model.invalid/v1", params=params)
    finally:
        reset_llm_debug_sink(token)
    run_output = run_logger.get_log_file_path().read_text()
    provider_output = provider_file.read_text()
    for output in [run_output, provider_output, json.dumps(records)]:
        assert "ordinary request body" in output
        assert "fixture-request-secret" not in output
        assert "fixture-header-secret" not in output
        assert "payload_redacted" not in output and "private diagnostic payload omitted" not in output
        assert ("_payload_logging" not in output) is full_payload
        assert ("x" * 800 in output) is full_payload
    assert "ordinary answer" in run_output and "ordinary tool body" in run_output and "ordinary failure" in run_output
    assert all(secret not in run_output for secret in ["fixture-response-secret", "fixture-tool-secret", "fixture-result-secret"])
    assert messages[0].content == original and params == before
    assert arguments["password"] == "fixture-tool-secret"
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("full_payload", [False, True])
def test_log_credentials_are_cleaned_before_preview_and_json_serialization(
    tmp_path, monkeypatch, full_payload,
):
    from box_agent.llm.debug_logging import log_llm_request, reset_llm_debug_sink, set_llm_debug_sink
    from box_agent.logger import AgentLogger
    from box_agent.schema import Message

    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("BOX_AGENT_LLM_DEBUG", "1")
    monkeypatch.setenv("BOX_AGENT_LOG_FULL_PAYLOAD", "1" if full_payload else "0")
    monkeypatch.setenv("BOX_AGENT_LOG_FILE", str(tmp_path / "provider.log"))
    secret = "fixture-boundary-secret-" + "s" * 500
    content = 'ordinary request {"api_key": "' + secret + '", "note": "keep"}'
    message = Message(role="user", content=content)
    run_logger = AgentLogger()
    run_logger.start_new_run()
    run_logger.log_request([message])
    logged_request = json.loads(run_logger.get_log_file_path().read_text().split("LLM Request:\n\n", 1)[1])
    records = []
    token = set_llm_debug_sink(records.append)
    try:
        log_llm_request(provider="openai", mode="stream", api_base="https://model.invalid", params={"messages": [message.model_dump()]})
    finally:
        reset_llm_debug_sink(token)
    for payload in [logged_request, records[0]["payload"]]:
        serialized = json.dumps(payload)
        assert "ordinary request" in serialized
        assert "fixture-boundary-secret" not in serialized
        assert "<redacted>" in serialized
        assert "keep" in serialized
    assert message.content == content
