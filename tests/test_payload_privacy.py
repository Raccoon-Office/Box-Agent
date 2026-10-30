"""Private diagnostic scopes never change model data or leak into other tasks."""

import asyncio
import json
import logging

import pytest

from box_agent.cli import _config_display_value
from box_agent.llm.debug_logging import log_llm_request, reset_llm_debug_sink, set_llm_debug_sink
from box_agent.logger import AgentLogger
from box_agent.payload_privacy import private_payload_scope, private_payloads
from box_agent.schema import Message
from box_agent.session_trace import SessionTraceWriter, emit_session_trace, reset_session_trace_writer, set_session_trace_writer


def test_private_scope_omits_diagnostics_even_with_full_payload_environment(tmp_path, monkeypatch, caplog):
    secret = "fixture-private-memory-body"
    monkeypatch.setenv("BOX_AGENT_LOG_FULL_PAYLOAD", "1")
    monkeypatch.setenv("BOX_AGENT_LLM_DEBUG", "1")
    monkeypatch.setattr("box_agent.logger.state_path", lambda _: tmp_path / "agent-log")
    logger = AgentLogger()
    logger.start_new_run()
    writer = SessionTraceWriter(session_id="session-1", acp_session_id="acp-1", trace_dir=tmp_path / "trace", enabled=True)
    records = []
    sink = set_llm_debug_sink(records.append)
    trace = set_session_trace_writer(writer, turn_id="turn-1")
    messages = [Message(role="system", content=secret)]
    caplog.set_level(logging.DEBUG)
    try:
        with private_payload_scope():
            assert private_payloads()
            logger.log_request(messages)
            logger.log_response(secret, thinking=secret)
            logger.log_tool_result("memory_read", {"content": secret}, True, result_content=secret, raw_output={"content": secret})
            log_llm_request(provider="openai", mode="stream", api_base="https://model.invalid", params={"messages": [{"content": secret}]})
            emit_session_trace("llm.request", data={"messages": [{"content": secret}]})
            logging.getLogger("openai._base_client").debug("SDK raw payload %s", secret)
            logging.getLogger("httpcore.http11").debug("HTTP raw payload %s", secret)
        assert not private_payloads()
    finally:
        reset_llm_debug_sink(sink)
        reset_session_trace_writer(trace)
    assert messages[0].content == secret
    assert secret not in logger.log_file.read_text() + writer.file_path.read_text() + json.dumps(records) + caplog.text
    assert "payload_redacted" in writer.file_path.read_text() + json.dumps(records)
    # Ordinary diagnostics retain the previous configured behavior.
    logger.log_request(messages)
    assert secret in logger.log_file.read_text()
    logging.getLogger("openai._base_client").debug("ordinary-sdk-debug")
    assert "ordinary-sdk-debug" in caplog.text


async def test_private_scope_isolated_between_concurrent_tasks_and_resets_on_exception():
    release = asyncio.Event()

    async def private():
        with pytest.raises(RuntimeError):
            with private_payload_scope():
                release.set()
                await asyncio.sleep(0)
                assert private_payloads()
                raise RuntimeError("synthetic error")
        assert not private_payloads()

    async def ordinary():
        await release.wait()
        assert not private_payloads()

    await asyncio.gather(private(), ordinary())


def test_bootstrap_path_and_epoch_are_redacted_from_parent_config_display():
    value = {"memory_desktop": {"bootstrap_file": "/private/fixture/bootstrap.json", "identity_epoch": "private-epoch"}}
    displayed = _config_display_value(value, ["agent"])
    assert displayed == {"memory_desktop": {"bootstrap_file": "****", "identity_epoch": "****"}}


def test_direct_trace_write_omits_private_data_and_preserves_event_ids(tmp_path):
    writer = SessionTraceWriter(session_id="private", acp_session_id="acp",
                                trace_dir=tmp_path, enabled=True)
    payload = {"content": "PRIVATE-TRACE-CANARY"}
    with private_payload_scope():
        writer.write("turn.error", turn_id="turn", step=2, llm_call_id="llm",
                     tool_call_id="tool", data=payload)
    writer.write("ordinary", data=payload)
    private, ordinary = [json.loads(line) for line in writer.file_path.read_text().splitlines()]
    assert private["data"] == {"payload_redacted": True}
    assert private["event"] == "turn.error" and private["turn_id"] == "turn"
    assert private["step"] == 2 and private["llm_call_id"] == "llm" and private["tool_call_id"] == "tool"
    assert ordinary["data"] == payload
    assert payload == {"content": "PRIVATE-TRACE-CANARY"}
