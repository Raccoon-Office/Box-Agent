"""Tests for the frozen runtime's multi-service entry point."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import textwrap

import pytest

from box_agent.acp import runtime_entry
from box_agent.mcp_servers import web_extract_server
from box_agent.tools import mcp_bootstrap


@pytest.mark.parametrize(
    ("platform", "frozen", "skip_docs"),
    [("win32", True, True), ("win32", False, False), ("darwin", True, False)],
)
def test_runtime_entry_cold_acp_import_preserves_protocol_validation(
    platform, frozen, skip_docs,
) -> None:
    # A fresh interpreter is essential: this test module has already imported ACP.
    script = textwrap.dedent("""
        import runpy
        import sys
        from types import SimpleNamespace
        from pydantic import ValidationError
        from pydantic._internal import _fields

        entry = runpy.run_path(sys.argv[1])
        preload = entry['_preload_frozen_acp_schema']
        preload.__globals__['sys'] = SimpleNamespace(
            platform=sys.argv[2], frozen=sys.argv[3] == 'True',
        )
        original = _fields.extract_docstrings_from_cls
        acp_calls = []
        def record(cls, *args, **kwargs):
            if cls.__module__ == 'acp.schema':
                acp_calls.append(cls.__name__)
            return original(cls, *args, **kwargs)
        _fields.extract_docstrings_from_cls = record
        preload()
        assert _fields.extract_docstrings_from_cls is record

        from acp.schema import ReadTextFileRequest
        assert bool(acp_calls) is (sys.argv[4] != 'True'), acp_calls
        request = ReadTextFileRequest.model_validate({
            'sessionId': 'session-1', 'path': '/tmp/example.txt', 'line': 1,
        })
        assert request.session_id == 'session-1'
        assert request.model_dump(by_alias=True)['sessionId'] == 'session-1'
        assert ReadTextFileRequest.model_validate({
            'session_id': 'session-1', 'path': '/tmp/example.txt',
        }).session_id == 'session-1'
        # The SDK intentionally salvages invalid optional fields to defaults.
        assert ReadTextFileRequest.model_validate({
            'sessionId': 'session-1', 'path': '/tmp/example.txt', 'line': -1,
        }).line is None
        for invalid in ({'path': '/tmp/example.txt'}, {
            'sessionId': 123, 'path': '/tmp/example.txt',
        }):
            try:
                ReadTextFileRequest.model_validate(invalid)
            except ValidationError:
                pass
            else:
                raise AssertionError('ACP validation was bypassed')
    """)
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script, runtime_entry.__file__,
         platform, str(frozen), str(skip_docs)],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_entry_restores_docstring_extraction_on_import_failure(monkeypatch) -> None:
    import importlib
    from types import SimpleNamespace
    from pydantic._internal import _fields

    monkeypatch.setattr(runtime_entry, "sys", SimpleNamespace(platform="win32", frozen=True))
    calls = []

    def extract(cls, **kwargs):
        calls.append((cls, kwargs))
        return {"value": "Other model documentation"}

    monkeypatch.setattr(_fields, "extract_docstrings_from_cls", extract)

    def fail_import(name):
        assert name == "acp.schema"
        other_model = type("OtherModel", (), {"__module__": "other.schema"})
        assert _fields.extract_docstrings_from_cls(other_model, use_inspect=True) == {
            "value": "Other model documentation",
        }
        assert calls == [(other_model, {"use_inspect": True})]
        raise ImportError("test import failure")

    monkeypatch.setattr(importlib, "import_module", fail_import)
    with pytest.raises(ImportError, match="test import failure"):
        runtime_entry._preload_frozen_acp_schema()
    assert _fields.extract_docstrings_from_cls is extract


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
