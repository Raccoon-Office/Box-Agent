from pathlib import Path

import pytest

from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.tools.file_tools import (
    AppendTool,
    EditTool,
    MAX_FILE_TOOL_CONTENT_CHARS,
    WriteTool,
)
from box_agent.tools.setup import SANDBOX_INFO_PROMPT, add_workspace_tools


def test_write_file_schema_exposes_content_size_limit():
    content_schema = WriteTool().parameters["properties"]["content"]

    assert content_schema["maxLength"] == MAX_FILE_TOOL_CONTENT_CHARS
    assert f"{MAX_FILE_TOOL_CONTENT_CHARS:,} characters" in content_schema["description"]
    assert "large generated artifacts" in content_schema["description"]
    assert "staged_file_write" in content_schema["description"]
    assert "commit" in content_schema["description"]


def test_append_file_schema_exposes_content_size_limit():
    content_schema = AppendTool().parameters["properties"]["content"]

    assert content_schema["maxLength"] == MAX_FILE_TOOL_CONTENT_CHARS
    assert f"{MAX_FILE_TOOL_CONTENT_CHARS:,} characters" in content_schema["description"]
    assert "staged_file_write" in content_schema["description"]
    assert "commit" in content_schema["description"]


@pytest.mark.asyncio
async def test_write_file_rejects_oversized_content_before_writing(tmp_path):
    tool = WriteTool(workspace_dir=str(tmp_path))
    target = tmp_path / "output" / "large.html"
    content = "<!doctype html>\n" + ("x" * MAX_FILE_TOOL_CONTENT_CHARS)

    result = await tool.execute(path="output/large.html", content=content)

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("FILE_TOOL_ARGUMENT_TOO_LARGE")
    assert "use staged_file_write" in result.error
    assert "commit" in result.error
    assert not target.exists()


@pytest.mark.asyncio
async def test_append_file_appends_chunks_and_rejects_oversized_content(tmp_path):
    tool = AppendTool(workspace_dir=str(tmp_path))
    target = tmp_path / "output" / "large.html"

    first = await tool.execute(path="output/large.html", content="<html>")
    second = await tool.execute(path="output/large.html", content="<body>ok</body></html>")
    oversized = await tool.execute(
        path="output/large.html",
        content="x" * (MAX_FILE_TOOL_CONTENT_CHARS + 1),
    )

    assert first.success is True
    assert second.success is True
    assert target.read_text(encoding="utf-8") == "<html><body>ok</body></html>"
    assert oversized.success is False
    assert oversized.error is not None
    assert oversized.error.startswith("FILE_TOOL_ARGUMENT_TOO_LARGE")
    assert target.read_text(encoding="utf-8") == "<html><body>ok</body></html>"


def test_workspace_file_tools_include_append_file(tmp_path):
    tools = []
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(),
        tools=ToolsConfig(enable_bash=False, enable_todo=False, enable_plan=False),
    )

    add_workspace_tools(tools, config, tmp_path)

    names = {tool.name for tool in tools}
    assert "append_file" in names
    assert "staged_file_write" in names


def test_sandbox_prompt_limits_single_write_file_content_argument():
    assert f"预计超过 {MAX_FILE_TOOL_CONTENT_CHARS} 字符" in SANDBOX_INFO_PROMPT
    assert "`staged_file_write`" in SANDBOX_INFO_PROMPT
    assert "禁止把文件正文、heredoc 或 base64 载荷塞进 `bash`" in SANDBOX_INFO_PROMPT


def test_system_prompt_warns_against_single_write_file_large_artifacts():
    prompt = Path("box_agent/config/system_prompt.md").read_text(encoding="utf-8")

    assert "预计超过 8,000 字符" in prompt
    assert "`staged_file_write`" in prompt
    assert "禁止把文件正文、heredoc 或 base64 载荷塞进 `bash`" in prompt


@pytest.mark.asyncio
async def test_edit_file_rejects_oversized_replacement(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("old", encoding="utf-8")
    tool = EditTool(workspace_dir=str(tmp_path))

    result = await tool.execute(
        path="sample.txt",
        old_str="old",
        new_str="x" * (MAX_FILE_TOOL_CONTENT_CHARS + 1),
    )

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("FILE_TOOL_ARGUMENT_TOO_LARGE")
    assert target.read_text(encoding="utf-8") == "old"
