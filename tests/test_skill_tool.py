"""
Test Skill Tool

Tests for local directory discovery and on-demand reference reading.
"""

import tempfile
from hashlib import sha256
from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_tool import GetSkillTool, create_skill_tools


def create_test_skill(skill_dir: Path, name: str, description: str, content: str):
    """Create a test skill"""
    skill_file = skill_dir / "SKILL.md"
    skill_content = f"""---
name: {name}
description: {description}
---

{content}
"""
    skill_file.write_text(skill_content, encoding="utf-8")


@pytest.fixture
def skill_loader():
    """Create a loader with test skills"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test skills
        for i in range(2):
            skill_dir = Path(tmpdir) / f"test-skill-{i}"
            skill_dir.mkdir()
            create_test_skill(
                skill_dir,
                f"test-skill-{i}",
                f"Test skill {i} description",
                f"Test skill {i} content and instructions.",
            )

        loader = SkillLoader(tmpdir)
        loader.discover_skills()
        yield loader


@pytest.mark.asyncio
async def test_get_skill_tool(skill_loader):
    """Test GetSkillTool"""
    tool = GetSkillTool(skill_loader)

    result = await tool.execute(skill_name="test-skill-0")

    assert result.success
    assert "test-skill-0" in result.content
    assert "Test skill 0 description" in result.content
    assert "Test skill 0 content" in result.content


@pytest.mark.asyncio
async def test_get_skill_tool_does_not_trust_legacy_preload_hash_as_visible_body(skill_loader):
    skill = skill_loader.get_skill("test-skill-0")
    assert skill is not None
    skill_prompt = skill.to_prompt()
    tool = GetSkillTool(
        skill_loader,
        preloaded_skill_hashes={
            skill.name: sha256(skill_prompt.encode("utf-8")).hexdigest()
        },
    )

    result = await tool.execute(skill_name=skill.name)

    assert result.success
    assert "Test skill 0 content" in result.content
    assert result.model_context == result.content
    assert "already preloaded" not in result.content


@pytest.mark.asyncio
async def test_get_skill_tool_returns_full_content_when_preloaded_skill_changed(skill_loader):
    tool = GetSkillTool(
        skill_loader,
        preloaded_skill_hashes={"test-skill-0": "outdated"},
    )

    result = await tool.execute(skill_name="test-skill-0")

    assert result.success
    assert result.model_context == result.content
    assert "Test skill 0 content" in result.content


@pytest.mark.asyncio
async def test_get_skill_tool_honors_profile_block_until_user_explicitly_allows_skill(
    skill_loader,
):
    explicitly_allowed: set[str] = set()
    tool = GetSkillTool(
        skill_loader,
        blocked_skill_names={"test-skill-0"},
        explicitly_allowed_skill_names=explicitly_allowed,
    )

    blocked = await tool.execute(skill_name="test-skill-0")
    assert not blocked.success
    assert "execution profile" in blocked.error
    assert "do not retry" in blocked.error

    explicitly_allowed.add("test-skill-0")
    allowed = await tool.execute(skill_name="test-skill-0")
    assert allowed.success
    assert "Test skill 0 content" in allowed.content

    skill = skill_loader.get_skill("test-skill-0")
    assert skill is not None
    preloaded = GetSkillTool(
        skill_loader,
        blocked_skill_names={"test-skill-0"},
        preloaded_skill_hashes={
            skill.name: sha256(skill.to_prompt().encode("utf-8")).hexdigest()
        },
    )
    preloaded_result = await preloaded.execute(skill_name="test-skill-0")
    assert not preloaded_result.success
    assert "execution profile" in preloaded_result.error


@pytest.mark.asyncio
async def test_get_skill_tool_nonexistent(skill_loader):
    """Test getting non-existent skill"""
    tool = GetSkillTool(skill_loader)

    result = await tool.execute(skill_name="nonexistent-skill")

    assert not result.success
    assert "不存在" in result.error or "not found" in result.error.lower()


def test_create_skill_tools_returns_local_discovery_and_reader(skill_loader):
    """Both lightweight discovery and reading are offered without an execution tool."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()
        create_test_skill(
            skill_dir, "test-skill", "Test skill", "Test content"
        )

        tools, loader = create_skill_tools(tmpdir)

        # Discovery and reading share the same source catalog.
        assert {tool.name for tool in tools} == {"get_skill", "list_skills"}
        assert isinstance(tools[0], GetSkillTool)
        assert loader is not None


def test_skill_tools_keep_get_skill_compatibility():
    """The existing read name remains alongside local discovery."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a simple test skill
        skill_dir = Path(tmpdir) / "simple-skill"
        skill_dir.mkdir()
        create_test_skill(
            skill_dir, "simple-skill", "Simple test", "Content"
        )

        tools, _ = create_skill_tools(tmpdir)

        assert {tool.name for tool in tools} == {"get_skill", "list_skills"}

        # Verify it's GetSkillTool
        tool = tools[0]
        assert tool.name == "get_skill"
    assert "next_offset" in tool.description and "required_skills" in tool.description
