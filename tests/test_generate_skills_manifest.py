import pytest

from box_agent.tools.skill_loader import SkillLoader
from scripts.generate_skills_manifest import (
    BUILTIN_SKILL_NAMES,
    SKILLS_DIR,
    _collect_skills,
)


EXPECTED_BUILTIN_SKILLS = {
    "browser-use": "browser-use/SKILL.md",
    "data-dashboard": "data-dashboard/SKILL.md",
    "docx": "document-skills/docx/SKILL.md",
    "html-templates": "html-templates/SKILL.md",
    "mcp-config": "mcp-config/SKILL.md",
    "memory-guide": "memory-guide/SKILL.md",
    "pdf": "document-skills/pdf/SKILL.md",
    "pptx": "document-skills/pptx/SKILL.md",
    "research-synthesis": "research-synthesis/SKILL.md",
    "roadmap": "roadmap/SKILL.md",
    "scheduled-task": "scheduled-task/SKILL.md",
    "xlsx": "document-skills/xlsx/SKILL.md",
}


def test_builtin_manifest_contains_exactly_the_core_skill_allowlist():
    entries = dict(_collect_skills())

    assert set(BUILTIN_SKILL_NAMES) == set(EXPECTED_BUILTIN_SKILLS)
    assert entries == EXPECTED_BUILTIN_SKILLS

    loader = SkillLoader(SKILLS_DIR)
    loader.discover_skills()
    assert set(loader.list_skills()) == set(EXPECTED_BUILTIN_SKILLS)


@pytest.mark.parametrize(
    ("skill_name", "relative_path"),
    [
        ("artifacts-builder", "artifacts-builder/SKILL.md"),
        ("midu-writing", "midu-writing/SKILL.md"),
        ("dev-code-init", "superpowers/dev-code-init/SKILL.md"),
        ("zhihu", "zhihu/SKILL.md"),
        ("viral-topic", "viral-topic/SKILL.md"),
    ],
)
def test_market_skills_stay_packaged_but_out_of_builtin_manifest(
    skill_name, relative_path
):
    assert (SKILLS_DIR / relative_path).is_file()
    assert skill_name not in dict(_collect_skills())


def test_dev_code_init_remains_loadable_as_a_user_skill_source():
    entries = dict(_collect_skills())
    assert "dev-code-init" not in entries

    builtin_loader = SkillLoader(SKILLS_DIR)
    builtin_loader.discover_skills()
    assert builtin_loader.get_skill("dev-code-init") is None

    loader = SkillLoader(
        sources=[(SKILLS_DIR / "superpowers" / "dev-code-init", "user")]
    )
    loader.discover_skills()

    matches = loader.filter_by_query("/init")
    assert matches[0].name == "dev-code-init"
    skill = loader.get_skill("dev-code-init")
    assert skill is not None
    assert skill.source == "user"
    assert "Create or update" in skill.description
    assert "root `AGENTS.md`" in skill.content


def test_roadmap_is_a_top_level_builtin_skill():
    entries = dict(_collect_skills())

    assert entries["roadmap"] == "roadmap/SKILL.md"
