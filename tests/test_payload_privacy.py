"""Session diagnostics preserve useful payloads and filter credential values."""

import io
import json
import logging
import sys

import pytest
import yaml

from box_agent.cli import _config_display_value
from box_agent.payload_privacy import DiagnosticFormatter, redact_diagnostic_text, redact_diagnostic_value
from box_agent.session_trace import SessionTraceWriter, emit_session_trace, reset_session_trace_writer, set_session_trace_writer
from tests.test_desktop_memory_evidence import CREDENTIAL_SAMPLES


def test_bootstrap_path_and_epoch_are_redacted_from_parent_config_display():
    value = {"memory_desktop": {"bootstrap_file": "/private/fixture/bootstrap.json", "identity_epoch": "private-epoch"}}
    displayed = _config_display_value(value, ["agent"])
    assert displayed == {"memory_desktop": {"bootstrap_file": "****", "identity_epoch": "****"}}


def test_failure_details_reach_trace_and_next_request_without_credentials(tmp_path):
    writer = SessionTraceWriter(session_id="session", acp_session_id="acp", trace_dir=tmp_path, enabled=True)
    error = "Command failed with exit code 7\nPermission denied\nAuthorization: Bearer fixture-secret"
    message = {"role": "tool", "content": error, "tool_call_id": "call"}
    fields = {"tool_name": "bash", "success": False, "error": error, "content": error,
              "model_content": error, "raw_output": {"stderr": error}, "duration_ms": 15}
    writer.write("tool.response", data=fields, turn_id="turn", step=2, tool_call_id="call")
    writer.write("llm.request", data={"messages": [message]}, turn_id="turn", step=3, llm_call_id="llm")
    text = writer.file_path.read_text()
    result, request = map(json.loads, text.splitlines())
    assert result["data"]["exit_code"] == 7
    assert result["data"]["duration_ms"] == 15
    assert result["data"]["error"] == result["data"]["raw_output"]["stderr"]
    assert result["data"]["model_content"] == request["data"]["messages"][0]["content"]
    assert result["data"]["error"].startswith("Command failed with exit code 7\nPermission denied")
    assert "fixture-secret" not in text
    assert result["tool_call_id"] == "call" and request["llm_call_id"] == "llm"
    assert result["turn_id"] == request["turn_id"] == "turn"
    assert message["content"] == fields["error"] == error and "exit_code" not in fields


async def test_failed_command_retains_diagnostics_without_changing_result(tmp_path):
    from box_agent.tools.bash_tool import BashTool
    from box_agent.tools.engine.results import ToolResultPipelineInput, process_tool_result
    from box_agent.tool_result_storage import ToolResultStorage

    tool = BashTool(workspace_dir=str(tmp_path))
    writer = SessionTraceWriter(session_id="failed", acp_session_id="acp", enabled=True, trace_dir=tmp_path / "trace")
    token = set_session_trace_writer(writer, turn_id="fixture")
    try:
        result = await tool.execute(command=f'''"{sys.executable}" -c "import sys; sys.stderr.write('missing fixture'); sys.exit(7)"''')
        outcome = process_tool_result(ToolResultPipelineInput(
            messages=[], tool_call_id="failed", tool_name="bash", result=result,
            arguments={"command": "fixture"}, visible_content=result.content,
            visible_error=result.error, result_storage=ToolResultStorage(tmp_path),
        ))
    finally:
        reset_session_trace_writer(token)
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["exit_code"] == 7
    assert "missing fixture" in data["error"]
    assert not outcome.events[0].success
    assert data["error"] == outcome.events[0].error
    assert "missing fixture" in data["model_content"]


@pytest.mark.parametrize("code,error", [
    (9009, "Command failed with exit code 9009\r\n不是内部或外部命令，也不是可运行的程序"),
    (127, "Command failed with exit code 127\n/bin/bash: fixture: command not found"),
])
def test_command_error_preserves_platform_diagnostics(tmp_path, code, error):
    writer = SessionTraceWriter(session_id="platform", acp_session_id="acp", enabled=True, trace_dir=tmp_path)
    writer.write("tool.response", data={"tool_name": "bash", "success": False, "error": error})
    data = json.loads(writer.file_path.read_text())["data"]
    assert data["error"] == error
    assert data["exit_code"] == code


def test_exit_code_fallback_preserves_reported_code_and_unparseable_error(tmp_path):
    writer = SessionTraceWriter(session_id="legacy", acp_session_id="acp", enabled=True, trace_dir=tmp_path)
    fields = {"tool_name": "bash", "success": False, "error": "Command failed with exit code 7"}
    writer.write("tool.response", data={**fields, "exit_code": 42})
    malformed = {**fields, "error": "Command failed with exit code " + "9" * 5000}
    writer.write("tool.response", data=malformed)
    reported, unparseable = map(json.loads, writer.file_path.read_text().splitlines())
    assert reported["data"] == {**fields, "exit_code": 42}
    assert unparseable["data"] == malformed


@pytest.mark.parametrize("credentials", [
    "api_key='fixture-key'", "Authorization: Basic fixture-basic",
    "Bearer fixturetoken",
    "cookie: a=fixture-cookie; b=other", "secret=fixture-secret",
    "https://user:fixture-password@host", "-----BEGIN KEY-----fixture-key-----END KEY-----",
])
def test_credentials_are_removed_from_command_errors(credentials):
    assert "fixture" not in redact_diagnostic_text("Permission denied: " + credentials)


@pytest.mark.parametrize("quote", ['"', "'"])
def test_incomplete_quoted_credential_preserves_error_prefix(quote):
    output = redact_diagnostic_text("Provider stopped: password=" + quote + "fixture secret")
    assert output.startswith("Provider stopped: password=")
    assert "<redacted>" in output and "fixture" not in output


@pytest.mark.parametrize("sample", CREDENTIAL_SAMPLES)
def test_trace_masks_existing_credential_types_without_losing_adjacent_fields(tmp_path, sample):
    writer = SessionTraceWriter(session_id="credentials", acp_session_id="acp", enabled=True, trace_dir=tmp_path)
    writer.write("tool.response", data={"content": "ordinary output\n" + sample, "error": "useful error"})
    text = writer.file_path.read_text()
    fields = json.loads(text)["data"]
    assert "SYNTHETIC" not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    assert fields["content"].startswith("ordinary output\n") and "<redacted>" in fields["content"]
    assert fields["error"] == "useful error"


@pytest.mark.parametrize("args, expected", [
    (["--password", "fixture", "--verbose"], ["--password", "<redacted>", "--verbose"]),
    (["curl", "-u", "user:fixture", "url"], ["curl", "-u", "<redacted>", "url"]),
    (["mysql", "-pfixture", "db"], ["mysql", "-p<redacted>", "db"]),
    (["python", "-u", "file.py"], ["python", "-u", "file.py"]),
])
def test_credential_options_in_structured_and_serialized_argv(args, expected):
    text = json.dumps(args, separators=(",  ", ": "))
    assert redact_diagnostic_value(args) == expected
    assert redact_diagnostic_text("argv = " + text) == "argv = " + json.dumps(expected, separators=(",  ", ": "))


@pytest.mark.parametrize("program,options,expected", [
    ("curl", ["-ufixtureuser:fixturepass"], ["-u<redacted>"]),
    ("curl", ["-u", "fixtureuser:fixturepass"], ["-u", "<redacted>"]),
    ("curl", ["--user=fixtureuser:fixturepass"], ["--user=<redacted>"]),
    ("curl", ["--user", "fixtureuser:fixturepass"], ["--user", "<redacted>"]),
    ("utility", ["--password=fixturepass", "--token", "fixturetoken"], ["--password=<redacted>", "--token", "<redacted>"]),
    ("python", ["-u", "main.py"], ["-u", "main.py"]),
    ("utility", ["-update", "--user=account"], ["-update", "--user=account"]),
])
def test_session_trace_command_options_match_shell_and_argv_copies(tmp_path, program, options, expected):
    argv = [program, *options, "target"]
    safe = [program, *expected, "target"]
    args = {"command": " ".join(argv), "argv": argv, "serialized": json.dumps(argv)}
    original = json.dumps(args)
    writer = SessionTraceWriter(session_id="options", acp_session_id="", trace_dir=tmp_path, enabled=True)
    writer.write("tool.request", data={"arguments": args})
    logged = json.loads(writer.file_path.read_text())["data"]["arguments"]
    assert logged == {"command": " ".join(safe), "argv": safe, "serialized": json.dumps(safe)}
    assert json.dumps(args) == original


@pytest.mark.parametrize("flag", ["--client-secret", "--access-token", "--refresh-token", "--apiKey"])
@pytest.mark.parametrize("separate", [True, False], ids=["separate", "equals"])
def test_default_trace_masks_credential_cli_values_and_keeps_ordinary_flags(tmp_path, monkeypatch, flag, separate):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    value = "SYNTHETIC-CLI-CANARY"
    options = [flag, value] if separate else [f"{flag}={value}"]
    argv = ["oauth2l", "fetch", *options, "--scope", "email", "--no-password", "--max-tokens", "128"]
    command = " ".join(argv)
    arguments = {"command": command, "argv": argv, "serialized": json.dumps(argv)}
    history = json.dumps({"arguments": arguments, "status": "retry"})
    writer = SessionTraceWriter(session_id="cli-flags", acp_session_id="", trace_dir=tmp_path)
    writer.write("tool.request", data={"arguments": arguments})
    writer.write("llm.request", data={"messages": [{"role": "tool", "content": history}]})

    output = writer.file_path.read_text()
    assert value not in output
    tool, model = (json.loads(line)["data"] for line in output.splitlines())
    safe = [word.replace(value, "<redacted>") for word in argv]
    expected = {"command": " ".join(safe), "argv": safe, "serialized": json.dumps(safe)}
    assert tool["arguments"] == expected
    assert json.loads(model["messages"][0]["content"]) == {"arguments": expected, "status": "retry"}
    assert command == " ".join(argv) and value in command and value in history


@pytest.mark.parametrize("command,expected", [
    ('curl -u user:"fixture secret" host', 'curl -u <redacted> host'),
    ('curl -u "user":"fixture" host', 'curl -u <redacted> host'),
    (r'DB_PASSWORD=fixture\ secret utility', 'DB_PASSWORD=<redacted> utility'),
    ("curl 'https://example.test/?page=1&limit=5' -u user:fixture",
     "curl 'https://example.test/?page=1&limit=5' -u <redacted>"),
    ("curl https://example.test/mysql -u user:fixture", "curl https://example.test/mysql -u <redacted>"),
    ("curl -u user:fixture; python -u main.py", "curl -u <redacted>; python -u main.py"),
    ("error: curl -u acct:fixture host", "error: curl -u <redacted> host"),
    ("env TEST_ENV=neutral curl -u acct:fixture host", "env TEST_ENV=neutral curl -u <redacted> host"),
    ("sudo curl -u acct:fixture host", "sudo curl -u <redacted> host"),
    ("Please run curl -u acct:fixture host", "Please run curl -u <redacted> host"),
    ("Couldn't run curl -u acct:fixture host", "Couldn't run curl -u <redacted> host"),
    ("curl -u acct'first last' host", "curl -u <redacted> host"),
    ("utility --password acct'first last' host", "utility --password <redacted> host"),
    ('bash -lc "curl -u acct:fixture host"', 'bash -lc "curl -u <redacted> host"'),
])
def test_trace_masks_complete_shell_words_and_preserves_neighboring_arguments(tmp_path, command, expected):
    writer = SessionTraceWriter(session_id="shell", acp_session_id="", trace_dir=tmp_path, enabled=True)
    writer.write("tool.request", data={"arguments": {"command": command}})
    assert json.loads(writer.file_path.read_text())["data"]["arguments"]["command"] == expected


def test_standalone_basic_auth_preserves_ordinary_documentation():
    text = "Choose Basic Installation. Support Visual Basic 6.0. Basic read/write. Basic Zm9vOmJhcg== here."
    assert redact_diagnostic_text(text) == text.replace("Zm9vOmJhcg==", "<redacted>")


def test_ordinary_provider_identifiers_and_escaped_json_keys():
    text = "npm_config_registry=https://registry.npmjs.org/ npm ci; from huggingface_hub import hf_hub_download"
    assert redact_diagnostic_text(text) == text
    encoded = json.dumps({"密码": "private fixture", "body": text})
    assert json.loads(redact_diagnostic_text(encoded)) == {"密码": "<redacted>", "body": text}


def test_successful_tool_output_and_long_errors_remain_readable(tmp_path):
    writer = SessionTraceWriter(session_id="body", acp_session_id="acp", enabled=True, trace_dir=tmp_path)
    token = set_session_trace_writer(writer, turn_id="turn")
    content = "a" * 20_000 + " distinct result"
    error = "Command failed with exit code 1\n" + content
    try:
        emit_session_trace("tool.response", data={"tool_name": "bash", "success": True, "content": content})
        emit_session_trace("tool.response", data={"tool_name": "bash", "success": False, "error": error})
    finally:
        reset_session_trace_writer(token)
    success, failure = map(json.loads, writer.file_path.read_text().splitlines())
    assert success["data"]["content"] == content
    assert failure["data"]["error"] == error


def test_formatter_preserves_messages_and_filters_credentials_in_traceback():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(DiagnosticFormatter("%(message)s"))
    try:
        raise ValueError("ordinary failure: access_token=fixture-exception")
    except ValueError:
        record = logging.LogRecord("fixture", logging.ERROR, "", 0, "ordinary message %s",
                                   ("API_KEY=fixture-message",), sys.exc_info())
        handler.handle(record)
    output = stream.getvalue()
    assert "ordinary message" in output and "ValueError: ordinary failure" in output
    assert "fixture-exception" not in output and "fixture-message" not in output
    assert record.getMessage() == "ordinary message API_KEY=fixture-message"


def test_filter_keeps_noncredential_diagnostics_and_serialized_structure():
    diagnostic = 'total_tokens=100, total_token=20. Basic Installation ideas for a Bearer token. https://docs.invalid/path/@handle'
    assert redact_diagnostic_text(diagnostic) == diagnostic
    text = '{"password": "escape \\"and space", "next": "same", "apikey":"abc"}'
    assert json.loads(redact_diagnostic_text(text)) == {"password": "<redacted>", "next": "same", "apikey": "<redacted>"}


def test_nested_json_argument_text_keeps_shape_and_filters_inner_credentials():
    inner = {"refresh_token": 'nested"credential', "details": "normal text"}
    outer = {"tool": "fixture", "arguments": json.dumps(inner)}
    filtered = json.loads(redact_diagnostic_text(json.dumps(outer)))
    assert filtered["tool"] == "fixture"
    assert json.loads(filtered["arguments"]) == {"refresh_token": "<redacted>", "details": "normal text"}


def test_camel_case_credentials_and_token_counters():
    names = ("accessToken", "dbPassword", "clientSecret", "apiKey")
    payload = {name: "fixture-value" for name in names}
    payload.update(totalToken=20, input_token=9, completionTokens=11)
    filtered = redact_diagnostic_value(payload)
    for name in names:
        assert filtered[name] == "<redacted>"
        assert redact_diagnostic_text(f'{name}="fixture-value"') == f'{name}="<redacted>"'
    for key in ("totalToken", "input_token", "completionTokens"):
        assert filtered[key] == payload[key]
        text = f"{key}={payload[key]}"
        assert redact_diagnostic_text(text) == text

@pytest.mark.parametrize("json_argument", [False, True])
def test_sdk_options_formatter_keeps_nested_json_spacing_and_original_record(json_argument):
    # Match the OpenAI SDK's single mapping argument. Some loggers stringify
    # the same mapping before passing it to the formatter.
    arguments = '{  "path" : "config.json",   "content" : ' + json.dumps('{"api_key":"fixture-nested","body":"keep  space"}') + '}'
    options = {"messages": [{"tool_calls": [{"function": {"arguments": arguments}}]}]}
    arg = json.dumps(options) if json_argument else options
    record = logging.LogRecord("fixture", logging.DEBUG, "", 0, "Request options: %s", (arg,), None)
    original = record.getMessage()
    output = DiagnosticFormatter("%(asctime)s %(name)s: %(message)s").format(record)
    assert "fixture-nested" not in output and "<redacted>" in output and "keep  space" in output
    assert "   " in output and record.getMessage() == original and "fixture-nested" in original
    assert "0007" == DiagnosticFormatter("%(message)s").format(
        logging.LogRecord("fixture", logging.INFO, "", 0, "%04d", (7,), None))


@pytest.mark.parametrize("header", [
    'Cookie: id="fixture-cookie"; other="fixture-other"',
    'Cookie: id=fixture-cookie; second="fixture-other"',
    'Set-Cookie: id="fixture-cookie"; HttpOnly; Secure',
])
def test_cookie_header_masks_multiple_values_without_overwriting_following_content(header):
    error = header + "\nPermission denied"
    assert redact_diagnostic_text(error).splitlines() == [header.split(":")[0] + ": <redacted>", "Permission denied"]
    text = json.dumps({"content": error, "body": "unchanged"})
    filtered = json.loads(redact_diagnostic_text(text))
    assert filtered["body"] == "unchanged" and filtered["content"].endswith("Permission denied")
    assert "fixture-" not in filtered["content"]


def test_nonstring_credential_in_serialized_tool_arguments():
    text = '{ "token":123,  "authorization": {"id":42}, "state": true }'
    filtered = redact_diagnostic_text(text)
    assert filtered == '{ "token":"<redacted>",  "authorization": "<redacted>", "state": true }'


@pytest.mark.parametrize("prefix,secret,suffix", [
    ("Config read failed\n", "password: |\n  YAML-CANARY-A\n\n  YAML-CANARY-B\n", "status: retry\n"),
    ("Config read failed\n", "apiKey: >- # folded key\n  YAML-CANARY-A\n  YAML-CANARY-B\n", "status: retry\n"),
    ("service:\n  status: waiting\n", '  "密码": |2+\n    YAML-CANARY-A\n    YAML-CANARY-B\n',
     "  attempts: 2\nnext: normal\n"),
    ("Config read failed\n", "token:\n  YAML-CANARY-A\n  YAML-CANARY-B\n", "status: retry\n"),
    ("Config read failed\r\n", "api_key: |-\r\n  YAML-CANARY-A\r\n  YAML-CANARY-B\r\n", "status: retry\r\n"),
    ("Config read failed\n", "password: 'YAML-CANARY-A''s YAML-CANARY-B'\n", "status: retry\n"),
    ("service:\n", "  password: YAML-CANARY-A\n    YAML-CANARY-B\n", "  attempts: 2\n"),
    ("service:\n", "  credentials: # supplied by client\n    account: YAML-CANARY-A\n"
     "    devices:\n      - value: YAML-CANARY-B\n", "  attempts: 2\n"),
    ("service:\n", "  credentials:\n  - YAML-CANARY-A\n  - YAML-CANARY-B\n", "  attempts: 2\n"),
    ("jobs:\n", "  - credentials:\n      - name: YAML-CANARY-A\n        value: YAML-CANARY-B\n",
     "    status: retry\n  - name: ordinary job\n"),
], ids=["literal", "folded", "explicit-indent", "next-line-scalar", "windows-lines", "single-quote-escape", "plain-continuation",
        "nested-mapping", "indentless-sequence", "sequence-item"])
def test_default_trace_masks_yaml_credentials_and_keeps_following_diagnostics(
    tmp_path, monkeypatch, prefix, secret, suffix,
):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    text = prefix + secret + suffix
    content = {"error": text, "model_content": json.dumps({"stderr": text, "exit_code": 7})}
    writer = SessionTraceWriter(session_id="yaml", acp_session_id="", trace_dir=tmp_path)
    writer.write("tool.response", data=content)
    writer.write("llm.request", data={"messages": [{"role": "tool", "content": text}]})

    output = writer.file_path.read_text()
    assert "YAML-CANARY" not in output
    result, request = (json.loads(line)["data"] for line in output.splitlines())
    cleaned = result["error"]
    assert cleaned.startswith(prefix) and cleaned.endswith(suffix) and "<redacted>" in cleaned
    assert json.loads(result["model_content"]) == {"stderr": cleaned, "exit_code": 7}
    assert request["messages"][0]["content"] == cleaned
    assert content["error"] == text and json.loads(content["model_content"])["stderr"] == text


def test_formatter_with_timestamp_masks_http_fields_before_prefixing():
    record = logging.LogRecord("wire", logging.INFO, "", 0, "Authorization: %s", ("Token fixture-secret",), None)
    rendered = DiagnosticFormatter("%(asctime)s %(levelname)s %(message)s").format(record)
    assert rendered.endswith("INFO Authorization: <redacted>")
    assert record.getMessage() == "Authorization: Token fixture-secret"


def test_default_trace_preserves_ordinary_yaml_multiline_text(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    text = "report: |\n  Build failed.\n\n  Retry on Friday.\nchecks:\n  - outcome: pending\n  - outcome: done\n"
    writer = SessionTraceWriter(session_id="yaml-body", acp_session_id="", trace_dir=tmp_path)
    writer.write("turn.input", data={"content": text})
    assert json.loads(writer.file_path.read_text())["data"]["content"] == text


@pytest.mark.parametrize("value", ["first CANARY-FLOW", "'first''CANARY-FLOW'"])
def test_trace_masks_entire_yaml_flow_value_and_preserves_adjacent_fields(tmp_path, value):
    text = f"settings: {{password: {value}, mode: retry}}\nstatus: waiting\n"
    message = {"role": "tool", "content": text}
    writer = SessionTraceWriter(session_id="flow", acp_session_id="", trace_dir=tmp_path, enabled=True)
    writer.write("llm.request", data={"messages": [message]})
    recorded = json.loads(writer.file_path.read_text())["data"]["messages"][0]["content"]
    assert yaml.safe_load(recorded) == {"settings": {"password": "<redacted>", "mode": "retry"}, "status": "waiting"}
    assert "CANARY-FLOW" not in recorded and message["content"] == text


@pytest.mark.parametrize("as_list", [False, True], ids=["mapping", "sequence"])
def test_trace_preserves_flow_yaml_around_a_plain_credential_with_an_apostrophe(tmp_path, as_list):
    service = {"service": {"credentials": "quote'd and CANARY-END", "ordinary": "keep # part", "attempts": 3}}
    original = [service] if as_list else service
    text = yaml.safe_dump(original, default_flow_style=True, sort_keys=False)
    writer = SessionTraceWriter(session_id="flow-apostrophe", acp_session_id="", trace_dir=tmp_path, enabled=True)
    writer.write("tool.response", data={"content": text})
    recorded = json.loads(writer.file_path.read_text())["data"]["content"]
    safe = {"service": {**service["service"], "credentials": "<redacted>"}}
    assert yaml.safe_load(recorded) == ([safe] if as_list else safe)
    assert "CANARY-END" not in recorded and "CANARY-END" in text


@pytest.mark.parametrize("prefix,value", [
    ("settings: {mode: keep, password: ", "split CANARY-FLOW"),
    ("settings: {password: ", "first CANARY-FLOW"),
    ("status before password: ", "#CANARY-PROSE"),
])
def test_default_trace_masks_partial_flow_and_plain_prose_credentials(tmp_path, monkeypatch, prefix, value):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    suffix = "\nstatus: retry\n"
    text = prefix + value + suffix
    writer = SessionTraceWriter(session_id="partial-prose", acp_session_id="", trace_dir=tmp_path)
    writer.write("tool.response", data={"error": text, "model_content": json.dumps({"stderr": text})})
    recorded = json.loads(writer.file_path.read_text())["data"]
    expected = prefix + "<redacted>" + suffix
    assert recorded["error"] == expected
    assert json.loads(recorded["model_content"]) == {"stderr": expected}


def test_trace_preserves_yaml_tool_schema_constraints_while_masking_credential_default(tmp_path):
    definition = {"type": "string", "description": "Client authentication", "minLength": 8, "default": "CANARY-SCHEMA"}
    schema = {"name": "lookup", "inputSchema": {
        "type": "object", "properties": {"password": definition}, "required": ["password"],
    }}
    text = yaml.safe_dump(schema, sort_keys=False)
    writer = SessionTraceWriter(session_id="schema", acp_session_id="", trace_dir=tmp_path, enabled=True)
    writer.write("tool.response", data={"content": text})
    recorded = json.loads(writer.file_path.read_text())["data"]["content"]
    expected = yaml.safe_load(text)
    expected["inputSchema"]["properties"]["password"]["default"] = "<redacted>"
    assert yaml.safe_load(recorded) == expected
    assert "CANARY-SCHEMA" not in recorded and "CANARY-SCHEMA" in text


def test_trace_keeps_indented_traceback_after_an_http_authorization_header(tmp_path):
    header_value = 'Digest username="demo", response="CANARY-HTTP"'
    text = f'HTTP parse failed\nAuthorization: {header_value}\n  File "client.py", line 5\nHTTP 401'
    writer = SessionTraceWriter(session_id="http", acp_session_id="", trace_dir=tmp_path, enabled=True)
    writer.write("tool.response", data={"error": text})
    recorded = json.loads(writer.file_path.read_text())["data"]["error"]
    assert recorded == text.replace(header_value, "<redacted>")
    assert redact_diagnostic_text(recorded) == recorded


@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_default_trace_masks_toml_multiline_credentials_only(tmp_path, monkeypatch, quote):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    prefix = "[service]\nname = 'build'\n"
    suffix = "\n[retries]\nmax_attempts = 2\n"
    text = prefix + "password = " + quote + "\nTOML-CANARY-A\nTOML-CANARY-B" + quote + suffix
    writer = SessionTraceWriter(session_id="toml", acp_session_id="", trace_dir=tmp_path)
    writer.write("tool.response", data={"content": text})
    logged = json.loads(writer.file_path.read_text())["data"]["content"]
    assert "TOML-CANARY" not in logged and "<redacted>" in logged
    assert logged.startswith(prefix) and logged.endswith(suffix)


def test_default_trace_masks_curl_user_after_shell_line_continuation(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    prefix = "Previous response: https://first.example.test/status\ncurl --user "
    suffix = " https://second.example.test/status\nConnection closed normally.\n"
    command = prefix + "\\\n  fixture-account:SHELL-CANARY" + suffix
    writer = SessionTraceWriter(session_id="shell-continuation", acp_session_id="", trace_dir=tmp_path)
    writer.write("tool.request", data={"arguments": {"command": command}})
    logged = json.loads(writer.file_path.read_text())["data"]["arguments"]["command"]
    assert "fixture-account" not in logged and "SHELL-CANARY" not in logged
    assert "<redacted>" in logged and logged.startswith(prefix) and logged.endswith(suffix)


@pytest.mark.parametrize("field", ['"credentials"', '"密码"', r'"\u5bc6\u7801"', r'"creden\u0074ials"'])
def test_default_trace_masks_composite_credentials_in_embedded_json(tmp_path, monkeypatch, field):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    name = json.loads(field)
    embedded = '{' + field + ':{"username":"alice","value":"SYNTHETIC-TEST"},"status":401}'
    error = "Request failed: " + embedded + " (retry pending)"
    fields = {"error": error, "raw_output": json.loads(embedded),
              "model_content": json.dumps({"stderr": error}, ensure_ascii=False)}
    original = json.dumps(fields)
    writer = SessionTraceWriter(session_id="embedded", acp_session_id="", trace_dir=tmp_path)
    writer.write("tool.response", data=fields)

    output = writer.file_path.read_text()
    assert "SYNTHETIC-TEST" not in output and "alice" not in output
    logged = json.loads(output)["data"]
    assert logged["raw_output"] == {name: "<redacted>", "status": 401}
    assert json.loads(logged["model_content"]) == {"stderr": logged["error"]}
    text = logged["error"]
    assert text.startswith("Request failed: ") and text.endswith(" (retry pending)")
    assert json.loads(text.removeprefix("Request failed: ").removesuffix(" (retry pending)")) == logged["raw_output"]
    assert json.dumps(fields) == original


def test_default_trace_masks_multiple_json_fragments_and_nested_argument_text(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    args = {"credentials": [{"value": 'SYNTHETIC-ONE"}]'}], "operation": "upload"}
    first = json.dumps({"arguments": json.dumps(args), "attempt": 1})
    second = json.dumps({"密码": ["SYNTHETIC-TWO"], "attempt": 2}, ensure_ascii=False)
    content = f"Start {first} then {second} done"
    ordinary = 'Keep {target} and [pending]. Response: {"value":"ordinary","note":"} and ]"} intact.'
    writer = SessionTraceWriter(session_id="several", acp_session_id="", trace_dir=tmp_path)
    writer.write("llm.request", data={"messages": [{"role": "tool", "content": content},
                                                  {"role": "user", "content": ordinary}]})

    output = writer.file_path.read_text()
    assert "SYNTHETIC" not in output
    tool, user = json.loads(output)["data"]["messages"]
    assert user["content"] == ordinary
    text = tool["content"]
    assert text.startswith("Start ") and text.endswith(" done")
    left, right = map(json.loads, text.removeprefix("Start ").removesuffix(" done").split(" then "))
    assert left["attempt"] == 1
    assert json.loads(left["arguments"]) == {"credentials": "<redacted>", "operation": "upload"}
    assert right == {"密码": "<redacted>", "attempt": 2}


@pytest.mark.parametrize("fragment", [
    '{"credentials":{"username":"alice","value":"SYNTHETIC-TRUNCATED',
    r'{"\u5bc6\u7801":[{"value":"SYNTHETIC-TRUNCATED"},',
    '{"credentials":{"username":"alice",oops,"value":"SYNTHETIC-MALFORMED"}}',
    "{'credentials': {'username': 'alice', 'value': 'SYNTHETIC-REPR'}}",
])
def test_default_trace_masks_incomplete_or_malformed_composite_credentials(tmp_path, monkeypatch, fragment):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    writer = SessionTraceWriter(session_id="partial", acp_session_id="", trace_dir=tmp_path)
    writer.write("llm.error", data={"partial_content": "Partial reply: " + fragment, "attempt": 3})
    output = writer.file_path.read_text()
    assert "SYNTHETIC" not in output and "alice" not in output
    logged = json.loads(output)["data"]
    assert logged["attempt"] == 3 and logged["partial_content"].startswith("Partial reply: ")
    assert "<redacted>" in logged["partial_content"]


def test_redacting_json_preserves_escaped_filesystem_byte_in_utf8_logs():
    text = json.dumps({"stderr": "password=fixture-value\nmissing path-\udcff"})
    sanitized = redact_diagnostic_text(text)
    assert json.loads(sanitized.encode("utf-8")) == {"stderr": "password=<redacted>\nmissing path-\udcff"}


@pytest.mark.parametrize("text", [
    r'Python example: labels = {"\U0001F600": "happy"}',
    r'Unstructured label "\U0001F600": "happy"',
    "Debug: {'values': {1, 2}, 'point': (3, 4)}",
])
def test_trace_keeps_ordinary_python_fragments(tmp_path, monkeypatch, text):
    monkeypatch.delenv("BOX_AGENT_SESSION_TRACE_ENABLED", raising=False)
    writer = SessionTraceWriter(session_id="python-text", acp_session_id="", trace_dir=tmp_path)
    writer.write("turn.input", data={"content": text})
    assert json.loads(writer.file_path.read_text())["data"]["content"] == text


@pytest.mark.parametrize("verdict", [True, False])
def test_tool_qa_verdict_keeps_boolean_in_trace_and_model_message(tmp_path, verdict):
    writer = SessionTraceWriter(session_id="qa", acp_session_id="", trace_dir=tmp_path, enabled=True)
    report = {"pass": verdict, "checks": 3}
    serialized = json.dumps(report, indent=2)
    writer.write("tool.response", data={"raw_output": report, "content": serialized})
    writer.write("llm.request", data={"messages": [{"role": "tool", "content": serialized}]})
    result, request = (json.loads(line)["data"] for line in writer.file_path.read_text().splitlines())
    assert result == {"raw_output": report, "content": serialized}
    assert request["messages"][0]["content"] == serialized
    assert redact_diagnostic_value({"pass": "fixture-value"}) == {"pass": "<redacted>"}


@pytest.mark.parametrize("schema_key", ["parameters", "input_schema", "inputSchema"])
def test_tool_schema_preserves_credential_declarations_and_filters_actual_values(schema_key):
    definition = {
        "type": "string", "description": "Credential supplied by the client.", "minLength": 12,
        "default": "fixture-default", "example": "fixture-example",
        "examples": ["fixture-first", "fixture-second"], "enum": ["fixture-choice"],
        "anyOf": [{"const": "fixture-constant"}, {"pattern": "^[a-z]+$"}],
    }
    tool = {"name": "fixture", schema_key: {"type": "object", "properties": {
        "accessToken": definition, "path": {"type": "string", "default": "readme.md"},
    }, "required": ["accessToken"]}}
    payload = {"tools": [tool], "arguments": {"accessToken": "fixture-argument"}}
    actual = redact_diagnostic_value(payload)
    encoded = json.dumps(payload, indent=2)
    assert json.loads(redact_diagnostic_text(encoded)) == actual
    cleaned = actual["tools"][0][schema_key]
    assert cleaned["required"] == ["accessToken"] and cleaned["type"] == "object"
    assert cleaned["properties"]["path"] == {"type": "string", "default": "readme.md"}
    assert cleaned["properties"]["accessToken"] == {
        **definition, "default": "<redacted>", "example": "<redacted>",
        "examples": ["<redacted>", "<redacted>"], "enum": ["<redacted>"],
        "anyOf": [{"const": "<redacted>"}, {"pattern": "^[a-z]+$"}],
    }
    assert actual["arguments"] == {"accessToken": "<redacted>"}
    assert definition["default"] == "fixture-default"


@pytest.mark.parametrize("field", ["tools", "parameters", "input_schema"])
def test_ordinary_tool_data_named_like_a_schema_still_redacts_credentials(field):
    record = {field: {"properties": {"token": {"value": "private-fixture", "source": "tool"}}}}
    expected = {field: {"properties": {"token": "<redacted>"}}}
    assert redact_diagnostic_value(record) == expected
    assert json.loads(redact_diagnostic_text(json.dumps(record))) == expected
