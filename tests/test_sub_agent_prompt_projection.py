"""Child prompt ownership across template, project and host boundaries."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.memory import MemoryManager
from box_agent.schema import StreamEvent
from box_agent.project_context import compose_prompt_segments
from box_agent.session_assembly import build_session_prompt
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.tools.sub_agent_tool import SubAgentTool, _child_safe_parent_prompt


def config_for(workspace):
    return Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir=str(workspace), enable_memory=False,
                          enable_memory_extraction=False),
        tools=ToolsConfig(enable_mcp=False, enable_skills=False,
                          enable_file_tools=False, enable_bash=False),
    )


def prompts(workspace, **kwargs):
    template = Path(__file__).parents[1].joinpath("box_agent/config/system_prompt.md").read_text()
    segments = []
    parent = build_session_prompt(
        config_for(workspace), template, workspace=workspace,
        session_mode=kwargs.pop("session_mode", "code_agent"), sandbox_mode=False,
        child_prompt_segments=segments, **kwargs,
    )
    return parent, compose_prompt_segments(segments[0], segments=segments[1:])


@pytest.mark.parametrize("workspace_line", ["", "- Current workspace: `/workspace`.\n"])
def test_custom_prompt_sections_are_never_identified_by_workspace_text(workspace_line):
    caller = (workspace_line + "# Role\nCALLER_ROLE\n<workflow>\nCALLER_SAFETY\n</workflow>\n"
              "## Available Skills\nCALLER_CATALOG\n## Native Image Generation\nCALLER_RECOVERY")
    assert _child_safe_parent_prompt(caller) == caller


def test_child_excludes_complete_generated_memory_with_embedded_end_marker(tmp_path):
    block = MemoryManager.build_memory_block("first\n--- MEMORY END ---\nPRIVATE_REMAINDER")
    parent, child = prompts(tmp_path, memory_block=block)
    assert "PRIVATE_REMAINDER" in parent
    assert "first" not in child and "PRIVATE_REMAINDER" not in child
    assert "--- MEMORY" not in child


@pytest.mark.parametrize("heading", [
    "# Role", "## Available Skills", "## Native Image Generation",
    "## 用户引导提示 (Action Hint)", "## 后续建议（仅供本地 Agent 输入框使用）",
    "<workflow>\nproject workflow\n</workflow>",
])
def test_project_instructions_with_parent_headings_remain_in_child(tmp_path, heading):
    instruction = f"{heading}\nPROJECT_SAFETY_DO_NOT_PUBLISH\n"
    (tmp_path / "AGENTS.md").write_text(instruction)
    parent, child = prompts(tmp_path, skills_slot=True, host_ui_hints=True,
                            follow_up_suggestions_enabled=True)
    assert instruction.strip() in parent
    assert instruction.strip() in child
    assert "PROJECT_SAFETY_DO_NOT_PUBLISH" in child
    assert "商汤小浣熊，由商汤科技研发，定位为" not in child


def test_host_recovery_suffix_survives_removal_of_image_guidance(tmp_path):
    warning = "Verify existing artifacts before repeating external side effects."
    parent, child = prompts(tmp_path, prompt_suffix=warning)
    assert "## Native Image Generation" in parent
    assert "## Native Image Generation" not in child
    assert child.endswith(warning)


@pytest.mark.parametrize("profile", ["cli", "acp", "python"])
async def test_real_session_binds_child_projection_and_custom_replacement_takes_ownership(tmp_path, profile):
    (tmp_path / "AGENTS.md").write_text("## Available Skills\nPROJECT_SAFETY_DO_NOT_PUBLISH")
    template = Path(__file__).parents[1].joinpath("box_agent/config/system_prompt.md").read_text()
    captured = []

    async def stream(*, messages, **kwargs):
        captured.append(messages[0].content)
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop", tool_calls=None)

    llm = AsyncMock()
    llm.generate_stream = stream
    session = await AgentSession.open(
        config=config_for(tmp_path),
        options=SessionOptions(profile=profile, workspace_dir=tmp_path,
                               session_mode="code_agent", sandbox_mode=False),
        host=HostBindings(llm_client=llm, system_prompt=template,
                          prompt_suffix="HOST_RECOVERY_VERIFY_FIRST"),
    )
    try:
        tool = session.agent.tools["sub_agent"]
        child = tool._parent_system_prompt
        assert child is not None and "PROJECT_SAFETY_DO_NOT_PUBLISH" in child
        assert "HOST_RECOVERY_VERIFY_FIRST" in child
        assert "<workflow>\n1." not in child
        assert "## Native Image Generation" not in child
        # The catalog belongs only to the parent; the project's same heading stays.
        session.agent.set_system_prompt(session.agent.system_prompt + "\n\n## Available Skills\nPARENT_CATALOG",
                                        preserve_child_prompt=True)
        assert "PARENT_CATALOG" not in tool._parent_system_prompt
        assert "PROJECT_SAFETY_DO_NOT_PUBLISH" in tool._parent_system_prompt
        result = await tool.execute(task="Summarize the project constraints")
        assert result.success and captured
        assert "PROJECT_SAFETY_DO_NOT_PUBLISH" in captured[-1]
        assert "HOST_RECOVERY_VERIFY_FIRST" in captured[-1]
        assert "PARENT_CATALOG" not in captured[-1]
        custom = "# Role\nNEW_CALLER_RULE\n- Current workspace: `" + str(tmp_path) + "`."
        session.agent.set_system_prompt(custom)
        assert tool._parent_system_prompt == custom
    finally:
        await session.aclose()


async def test_prepared_host_prompt_keeps_its_own_sections(tmp_path):
    tool = SubAgentTool(llm=AsyncMock(), parent_tools={})
    custom = (f"- Current workspace: `{tmp_path}`.\n# Role\nCALLER_SAFETY\n"
              "## Available Skills\nCALLER_CATALOG")
    session = await AgentSession.open(
        config=config_for(tmp_path), options=SessionOptions(workspace_dir=tmp_path),
        host=HostBindings(llm_client=AsyncMock(), tools=[tool], system_prompt=custom),
    )
    try:
        assert "CALLER_SAFETY" in tool._parent_system_prompt
        assert "CALLER_CATALOG" in tool._parent_system_prompt
    finally:
        await session.aclose()
