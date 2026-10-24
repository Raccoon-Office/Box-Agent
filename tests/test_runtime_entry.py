"""Tests for the frozen runtime's multi-service entry point."""

from __future__ import annotations

import io
import json
import sys

import pytest

from box_agent.acp import runtime_entry
from box_agent.mcp_servers import web_extract_server
from box_agent.tools import mcp_bootstrap


def test_runtime_entry_bootstraps_managed_mcp_config(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["box-agent-acp", "--bootstrap-mcp-config"])
    monkeypatch.setattr(mcp_bootstrap.shutil, "which", lambda _command: None)

    runtime_entry.main()

    response = json.loads(capsys.readouterr().out)
    config_path = tmp_path / ".box-agent" / "config" / "mcp.json"
    assert response == {
        "managed_mcp_config_version": 1,
        "path": str(config_path),
        "changed": True,
        "warning": None,
    }
    assert config_path.is_file()


@pytest.mark.parametrize('service', ['web_extract', 'cua'])
def test_runtime_entry_dispatches_mcp_without_closing_protocol_stdout(monkeypatch, tmp_path, service) -> None:
    from box_agent.mcp_servers import cua_server

    module = cua_server if service == 'cua' else web_extract_server
    argument = '--cua-mcp' if service == 'cua' else '--web-extract-mcp'
    called: list[bool] = []
    diagnostic_stderr = io.StringIO()
    with monkeypatch.context() as context:
        stdout_path = tmp_path / "protocol.stdout"
        with stdout_path.open("w+", encoding="utf-8") as protocol_stdout:
            context.setattr(sys, "argv", ["box-agent-acp", argument])
            context.setattr(sys, "stdout", protocol_stdout)
            context.setattr(sys, "stderr", diagnostic_stderr)

            def close_owned_stdout() -> None:
                called.append(True)
                sys.stdout.buffer.close()

            context.setattr(module, "main", close_owned_stdout)

            runtime_entry.main()

            assert called == [True]
            assert sys.stdout is protocol_stdout
            assert protocol_stdout.closed is False
