"""
Test Skill Tool

Tests for skill tools after Progressive Disclosure optimization:
- Only GetSkillTool remains (ListSkillsTool and UseSkillTool removed)
- Tests verify the single-tool approach
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
async def test_get_skill_tool_returns_short_context_when_skill_is_preloaded(skill_loader):
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
    assert "already preloaded" in result.content
    assert result.model_context == result.content
    assert "Test skill 0 content" not in result.content


@pytest.mark.asyncio
async def test_get_skill_tool_returns_full_content_when_preloaded_skill_changed(skill_loader):
    tool = GetSkillTool(
        skill_loader,
        preloaded_skill_hashes={"test-skill-0": "outdated"},
    )

    result = await tool.execute(skill_name="test-skill-0")

    assert result.success
    assert result.model_context is None
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
    assert preloaded_result.success
    assert "already preloaded" in preloaded_result.content


@pytest.mark.asyncio
async def test_get_skill_tool_rejects_competing_skill_during_locked_workflow(
    skill_loader,
):
    locked = {"test-skill-0"}
    explicitly_allowed: set[str] = set()
    tool = GetSkillTool(
        skill_loader,
        workflow_locked_skill_names=locked,
        explicitly_allowed_skill_names=explicitly_allowed,
    )

    blocked = await tool.execute(skill_name="test-skill-1")
    assert blocked.success is False
    assert "cannot replace the active workflow" in (blocked.error or "")
    assert "do not retry" in (blocked.error or "")

    explicitly_allowed.add("test-skill-1")
    allowed = await tool.execute(skill_name="test-skill-1")
    assert allowed.success is True


@pytest.mark.asyncio
async def test_get_skill_tool_allows_declared_workflow_dependency(skill_loader):
    primary = skill_loader.get_skill("test-skill-0")
    assert primary is not None
    primary.related_skills = ["test-skill-1"]
    tool = GetSkillTool(
        skill_loader,
        workflow_locked_skill_names={"test-skill-0"},
    )

    result = await tool.execute(skill_name="test-skill-1")

    assert result.success is True
    assert "Test skill 1 content" in result.content


@pytest.mark.asyncio
async def test_get_skill_tool_nonexistent(skill_loader):
    """Test getting non-existent skill"""
    tool = GetSkillTool(skill_loader)

    result = await tool.execute(skill_name="nonexistent-skill")

    assert not result.success
    assert "不存在" in result.error or "not exist" in result.error.lower()


def test_create_skill_tools_returns_single_tool(skill_loader):
    """Test that create_skill_tools only returns GetSkillTool after optimization"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()
        create_test_skill(
            skill_dir, "test-skill", "Test skill", "Test content"
        )

        tools, loader = create_skill_tools(tmpdir)

        # Should only have one tool now (GetSkillTool)
        assert len(tools) == 1
        assert isinstance(tools[0], GetSkillTool)
        assert loader is not None


def test_tool_count_optimization():
    """Verify Progressive Disclosure optimization: 3 tools -> 1 tool"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a simple test skill
        skill_dir = Path(tmpdir) / "simple-skill"
        skill_dir.mkdir()
        create_test_skill(
            skill_dir, "simple-skill", "Simple test", "Content"
        )

        tools, _ = create_skill_tools(tmpdir)

        # After optimization, should only have 1 tool (GetSkillTool)
        # Before optimization, we had 3 tools (ListSkillsTool, GetSkillTool, UseSkillTool)
        assert len(tools) == 1

        # Verify it's GetSkillTool
        tool = tools[0]
        assert tool.name == "get_skill"
        assert "get complete content" in tool.description.lower() or "获取" in tool.description
