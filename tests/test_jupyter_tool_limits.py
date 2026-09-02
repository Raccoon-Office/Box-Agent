"""Regression tests for generic execute_code input limits."""

from __future__ import annotations

import pytest

from box_agent.tools.file_tools import MAX_FILE_TOOL_CONTENT_CHARS
from box_agent.tools.jupyter_tool import MAX_EXECUTE_CODE_CHARS, JupyterSandboxTool


def test_execute_code_schema_exposes_code_size_limit():
    code_schema = JupyterSandboxTool().parameters["properties"]["code"]

    assert code_schema["maxLength"] == MAX_EXECUTE_CODE_CHARS
    assert f"{MAX_EXECUTE_CODE_CHARS:,} characters" in code_schema["description"]
    assert "do not inline the body in execute_code" in code_schema["description"]
    assert "ordered write_file chunks" in code_schema["description"]
    assert "chunk_index/final" in code_schema["description"]
    assert "JSON manifests" in code_schema["description"]


def test_execute_code_limit_is_not_looser_than_file_chunk_limit():
    assert MAX_EXECUTE_CODE_CHARS <= MAX_FILE_TOOL_CONTENT_CHARS


@pytest.mark.asyncio
async def test_execute_code_rejects_oversized_code_before_kernel_start(monkeypatch):
    def fail_if_sandbox_requested(self):
        raise AssertionError("oversized code should be rejected before sandbox startup")

    monkeypatch.setattr(JupyterSandboxTool, "_get_sandbox_env", fail_if_sandbox_requested)
    tool = JupyterSandboxTool()
    code = "x = 1\n" + ("print(x)\n" * MAX_EXECUTE_CODE_CHARS)

    result = await tool.execute(code=code)

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("EXECUTE_CODE_TOO_LARGE")
    assert "Split the work into multiple execute_code calls" in result.error
    assert "ordered write_file chunks" in result.error
    assert "chunk_index/final" in result.error
