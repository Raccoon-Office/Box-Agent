import pytest

from box_agent.tools.skill_loader import SkillLoader
from scripts.generate_skills_manifest import (
    EXCLUDED_SKILL_DIRS,
    SKILLS_DIR,
    _collect_skills,
)


@pytest.mark.parametrize(
    "source_dir",
    [
        "city-travel-skill-developer-1.2.0",
        "city-travel-planner",
        "storymap-generate-person",
    ],
)
def test_recommended_skills_stay_out_of_builtin_manifest(source_dir):
    assert (SKILLS_DIR / source_dir / "SKILL.md").is_file()
    assert source_dir in EXCLUDED_SKILL_DIRS
    assert all(
        not relative_path.startswith(f"{source_dir}/")
        for _, relative_path in _collect_skills()
    )


def test_dev_code_init_is_builtin_and_matches_slash_init():
    entries = dict(_collect_skills())
    assert entries["dev-code-init"] == "superpowers/dev-code-init/SKILL.md"

    loader = SkillLoader(SKILLS_DIR)
    loader.discover_skills()

    matches = loader.filter_by_query("/init")
    assert matches[0].name == "dev-code-init"
    skill = loader.get_skill("dev-code-init")
    assert skill is not None
    assert "Create or update" in skill.description
    assert "root `AGENTS.md`" in skill.content


def test_roadmap_is_a_top_level_builtin_skill():
    entries = dict(_collect_skills())

    assert entries["roadmap"] == "roadmap/SKILL.md"
