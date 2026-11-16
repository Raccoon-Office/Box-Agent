"""Private diagnostic scopes never change model data or leak into other tasks."""

import asyncio
import json
import logging
import os

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
            logging.getLogger("box_agent.core").warning("suspected_truncation tail=%s", secret)
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
    logging.getLogger("box_agent.core").warning("ordinary-core-diagnostic")
    assert "ordinary-core-diagnostic" in caplog.text


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


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell command")
async def test_private_bash_truncation_does_not_log_command(tmp_path, caplog):
    from box_agent.tools.bash_tool import BashTool

    caplog.set_level(logging.WARNING, logger="box_agent.tools.bash_tool")
    tool = BashTool(workspace_dir=str(tmp_path))
    command = "printf 'SYNTHETIC_PRIVATE_COMMAND'; printf 'x%.0s' {1..60000}"
    with private_payload_scope():
        result = await tool.execute(command=command)
    assert result.success
    assert "SYNTHETIC_PRIVATE_COMMAND" not in caplog.text

    result = await tool.execute(command=command)
    assert result.success
    assert "SYNTHETIC_PRIVATE_COMMAND" in caplog.text


def test_private_bash_spawn_diagnostics_are_suppressed(caplog):
    logger = logging.getLogger("box_agent.tools.bash_tool")
    caplog.set_level(logging.INFO, logger=logger.name)
    with private_payload_scope():
        logger.info("bash/spawn shell=powershell cmd=%r", "SYNTHETIC_PRIVATE_COMMAND")
    assert "SYNTHETIC_PRIVATE_COMMAND" not in caplog.text
    logger.info("bash/spawn shell=powershell cmd=%r", "SYNTHETIC_ORDINARY_COMMAND")
    assert "SYNTHETIC_ORDINARY_COMMAND" in caplog.text


def test_private_mcp_adapter_exception_does_not_log_tool_result(caplog):
    from box_agent.tools.mcp_result_hooks import adapt_mcp_result_context, bind_mcp_result_adapter

    class FailingAdapter:
        def result_context(self, *, server_name, remote_name, arguments, structured_content):
            raise ValueError(structured_content["result"])

    caplog.set_level(logging.DEBUG, logger="box_agent.tools.mcp_result_hooks")

    def invoke(value):
        with bind_mcp_result_adapter(FailingAdapter()):
            assert adapt_mcp_result_context(server_name="fixture", remote_name="fixture",
                                            arguments={}, structured_content={"result": value}) is None

    with private_payload_scope():
        invoke("SYNTHETIC_PRIVATE_RESULT")
    assert "SYNTHETIC_PRIVATE_RESULT" not in caplog.text
    invoke("SYNTHETIC_ORDINARY_RESULT")
    assert "SYNTHETIC_ORDINARY_RESULT" in caplog.text


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


async def test_failed_command_retains_diagnostics_without_changing_result(tmp_path):
    from box_agent.tools.bash_tool import BashTool
    from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result
    from box_agent.tool_result_storage import ToolResultStorage

    tool = BashTool(workspace_dir=str(tmp_path))
    writer = SessionTraceWriter(session_id="failed", acp_session_id="acp", enabled=True,
                                trace_dir=tmp_path / "trace")
    token = set_session_trace_writer(writer, turn_id="fixture")
    try:
        with private_payload_scope():
            result = await tool.execute(command="python -c \"import sys; sys.stderr.write('missing fixture'); sys.exit(7)\"")
            outcome = process_tool_result(ToolResultPipelineInput(
                messages=[], tool_call_id="failed", tool_name="bash", result=result,
                arguments={"command": "private"}, visible_content=result.content,
                visible_error=result.error, result_storage=ToolResultStorage(tmp_path),
            ))
    finally:
        reset_session_trace_writer(token)
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["exit_code"] == 7
    assert "missing fixture" in data["error"]
    assert not outcome.events[0].success
    assert data["error"] == outcome.events[0].error
    assert "arguments" not in data
    assert "content" not in data


def test_command_trace_retains_safe_metadata_and_redacts_credentials(tmp_path):
    writer = SessionTraceWriter(session_id="failed", acp_session_id="acp", enabled=True,
                                trace_dir=tmp_path / "trace")
    with private_payload_scope():
        writer.write("tool.response", data={
            "tool_name": "bash", "success": False, "duration_ms": 10,
            "exit_code": 2, "error": "Command failed with exit code 2\nAuthorization: Bearer fake-token",
        })
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["success"] is False
    assert data["exit_code"] == 2
    assert data["duration_ms"] == 10
    assert "fake-token" not in data["error"]


@pytest.mark.parametrize("error", [
    "Command failed with exit code 9009\r\n不是内部或外部命令，也不是可运行的程序",
    "Command failed with exit code 127\n/bin/bash: fixture: command not found",
])
def test_command_error_preserves_platform_diagnostics(tmp_path, error):
    writer = SessionTraceWriter(session_id="platform", acp_session_id="acp", enabled=True,
                                trace_dir=tmp_path)
    with private_payload_scope():
        writer.write("tool.response", data={"tool_name": "bash", "success": False, "error": error})
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["error"] == error
    assert data["exit_code"] in (9009, 127)


@pytest.mark.parametrize("credentials", [
    "api_key='fixture-key'", "Authorization: Basic fixture-basic",
    "cookie: a=fixture-cookie; b=other", "secret=fixture-secret",
    "https://user:fixture-password@host", "-----BEGIN KEY-----fixture-key-----END KEY-----",
])
def test_credentials_are_removed_from_command_errors(tmp_path, credentials):
    writer = SessionTraceWriter(session_id="credentials", acp_session_id="acp", enabled=True,
                                trace_dir=tmp_path)
    with private_payload_scope():
        writer.write("tool.response", data={"tool_name": "bash", "success": False,
                                           "error": "Permission denied: " + credentials})
    data = json.loads(writer.file_path.read_text())["data"]
    assert "Permission denied" in data["error"]
    assert "fixture-" not in data["error"]


def test_successful_tool_output_stays_private(tmp_path):
    writer = SessionTraceWriter(session_id="success", acp_session_id="acp", enabled=True,
                                trace_dir=tmp_path)
    with private_payload_scope():
        writer.write("tool.response", data={"tool_name": "bash", "success": True,
                                           "content": "secret"})
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["success"] is True
    assert "content" not in data


def test_large_error_keeps_the_end(tmp_path):
    writer = SessionTraceWriter(session_id="large", acp_session_id="acp", enabled=True,
                                trace_dir=tmp_path)
    with private_payload_scope():
        writer.write("tool.response", data={"tool_name": "bash", "success": False,
            "error": "Command failed with exit code 1\n" + "x" * 20000 + "\nPermission denied"})
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["error"].endswith("Permission denied")
    assert len(data["error"]) < 9000
