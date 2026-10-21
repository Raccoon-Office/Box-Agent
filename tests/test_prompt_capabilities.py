"""Restricted sessions must receive instructions for their actual tools."""
from pathlib import Path

import pytest

from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.prompt_capabilities import render_code_search_tools, resolve_capability_placeholders
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.tools.request_user_input_tool import RequestUserInputTool
from box_agent.tools.setup import build_image_generation_prompt


@pytest.mark.parametrize("tools", [frozenset(), frozenset({"read_file"}),
                                   frozenset({"search_files"}), frozenset({"glob", "grep"})])
def test_search_guidance_names_only_available_file_tools(tools):
    prompt = render_code_search_tools(tools)
    for name in {"read_file", "search_files", "glob", "grep"} - tools:
        assert f"`{name}`" not in prompt


def test_email_guidance_is_only_present_when_the_decoder_is_available():
    without = resolve_capability_placeholders('{FILE_OPERATIONS}', {'read_file'})
    with_email = resolve_capability_placeholders('{FILE_OPERATIONS}', {'read_file', 'read_email'})
    assert '`read_email`' not in without
    assert '`read_email`' in with_email
    assert 'attachment_index' not in with_email
    assert '`extract_email_attachment`' not in with_email
    with_extraction = resolve_capability_placeholders(
        '{FILE_OPERATIONS}', {'read_email', 'extract_email_attachment'},
    )
    assert '`extract_email_attachment`' in with_extraction
    assert 'attachment_index' in with_extraction


@pytest.mark.parametrize("source", ["config", "env"])
def test_missing_image_tool_does_not_misdiagnose_configured_service(monkeypatch, source):
    config = Config(llm=LLMConfig(api_key="test"), agent=AgentConfig(), tools=ToolsConfig())
    env = {}
    if source == "config":
        config.image_generation.endpoint = "https://example.invalid/images"
    else:
        env["BOX_AGENT_IMAGE_GENERATION_ENDPOINT"] = "https://example.invalid/images"
    prompt = build_image_generation_prompt(config, env, tools=frozenset())
    assert "未配置" not in prompt
    assert "当前会话没有生图工具" in prompt


@pytest.mark.parametrize("mode,file_tools,bash,sandbox", [
    ("code_agent", False, True, False),
    ("code_agent", True, False, False),
    ("data_analysis", True, False, False),
    ("general", False, False, True),
])
async def test_real_session_prompt_respects_disabled_capabilities(tmp_path, monkeypatch, mode, file_tools, bash, sandbox):
    # No turn is run: this verifies the real registration -> shared assembly path.
    config_dir = Path(__file__).resolve().parents[1] / "box_agent/config"
    config = Config(llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(enable_memory=False, workspace_dir=str(tmp_path),
                                      system_prompt_path=str(config_dir / "system_prompt.md"),
                                      code_prompt_path=str(config_dir / "code_prompt.md"),
                                      analysis_prompt_path=str(config_dir / "analysis_prompt.md")),
                    tools=ToolsConfig(enable_mcp=False, enable_skills=False,
                                      enable_file_tools=file_tools, enable_bash=bash))
    session = await AgentSession.open(config=config,
        options=SessionOptions(workspace_dir=tmp_path, session_mode=mode, sandbox_mode=sandbox),
        host=HostBindings(llm_client=object()))
    try:
        prompt = session.agent.system_prompt
        if not file_tools:
            assert "`read_email`" not in prompt
            assert "`extract_email_attachment`" not in prompt
            for instruction in ("用 `search_files` 查看", "直接调用 `search_files`", "；调用 `search_files`",
                                "文本正文用 `read_file`", "用 `write_file(path, content)`", "大文件使用 `write_file`"):
                assert instruction not in prompt
        if not bash:
            assert "使用 Bash 执行" not in prompt
            assert "**用 bash**" not in prompt
        if not sandbox:
            assert "always prefer `execute_code`" not in prompt
            assert "## Python Sandbox (execute_code)" not in prompt
    finally:
        await session.aclose()


@pytest.mark.parametrize("profile", ["acp", "cli", "python"])
@pytest.mark.parametrize("sandbox", [False, True])
async def test_utility_prompt_does_not_instruct_unregistered_tools(tmp_path, profile, sandbox):
    config_dir = Path(__file__).resolve().parents[1] / "box_agent/config"
    template_path = config_dir / "system_prompt.md"
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(enable_memory=False, workspace_dir=str(tmp_path),
                          system_prompt_path=str(template_path)),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False),
    )
    template = template_path.read_text(encoding="utf-8").replace("{SKILLS_METADATA}", "")
    session = await AgentSession.open(
        config=config,
        options=SessionOptions(profile=profile, utility=True, sandbox_mode=sandbox,
                               workspace_dir=tmp_path),
        host=HostBindings(llm_client=object(), system_prompt=template if profile == "acp" else None),
    )
    try:
        assert not session.agent.tools
        prompt = session.agent.system_prompt
        assert "`read_email`" not in prompt
        assert "`extract_email_attachment`" not in prompt
        assert "文本正文用 `read_file`" not in prompt
        assert "大文件使用 `write_file`" not in prompt
        assert "## Python Sandbox (execute_code)" not in prompt
        assert "使用 Bash 执行" not in prompt
        assert "`request_user_decision`" not in prompt
        assert "`sub_agent`" not in prompt
    finally:
        await session.aclose()


async def test_restricted_host_prompt_does_not_claim_configured_image_service_is_missing(tmp_path):
    config = Config(llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(enable_memory=False, workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(enable_mcp=False, enable_skills=False))
    config.image_generation.endpoint = "https://example.invalid/images"
    session = await AgentSession.open(config=config,
        options=SessionOptions(workspace_dir=tmp_path),
        host=HostBindings(llm_client=object(), tools=[RequestUserInputTool()]))
    try:
        prompt = session.agent.system_prompt
        assert "当前会话没有生图工具" in prompt
        assert "未配置 `image_generation.endpoint`" not in prompt
        assert "`request_user_decision`" not in prompt
        assert "调用 `publish_artifact`" not in prompt
    finally:
        await session.aclose()
