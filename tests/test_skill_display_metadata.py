"""Display labels survive plugin naming without changing runtime identities."""

from pathlib import Path

import pytest
import yaml

from box_agent.acp import BoxACPAgent
from box_agent.plugins.skill_sources import build_skill_plan
from box_agent.tools.skill_loader import SkillLoader


def write_skill(root: Path, name: str, **extra) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / "SKILL.md"
    frontmatter = {"name": name, "description": "Author workflow", **extra}
    file.write_text("---\n" + yaml.safe_dump(frontmatter, allow_unicode=False)
                    + "---\nUse this workflow.\n", encoding="utf-8")
    return file


@pytest.mark.parametrize("label", [
    None, "", "  ", 42, {"en": "Title"}, "x" * 257, "bad\nlabel",
    "bad\x85label", "bad\u2028label", "bad\u2029label",
])
def test_invalid_optional_label_preserves_the_authored_name(tmp_path, label):
    file = write_skill(tmp_path, "guide", displayName=label)
    skill = SkillLoader(sources=[]).load_skill(file, source="user")
    assert skill is not None and not skill.broken
    assert skill.name == "guide"
    assert skill.to_metadata_dict()["displayName"] == "guide"


@pytest.mark.parametrize("label", [None, "  团队简报  "])
def test_plugin_labels_survive_namespace_alias_and_acp_catalog(tmp_path, label):
    extra = {} if label is None else {"displayName": label}
    write_skill(tmp_path / "package/skills", "guide", **extra)
    item = {
        "manifest": {"id": "org.example.guide", "version": "1.0.0",
                     "contributes": {"skills": [{"id": "guide", "path": "skills/guide"}]}},
        "package_root": str(tmp_path / "package"), "enabled": True,
        "package_digest": "a" * 64, "config_revision": 1,
    }
    plan = build_skill_plan({"generation": "display-test", "instances": {
        "sales": {**item, "aliases": {}},
        "research": {**item, "aliases": {"research_brief": "guide"}},
    }})
    loader = SkillLoader(sources=[], skill_settings_path=tmp_path / "settings.json",
                         **plan.loader_arguments())
    loader.discover_skills()
    server = object.__new__(BoxACPAgent)
    server._skill_loader = loader
    server._skills_loaded = True
    rows = server._skills_meta(include_disabled=True)
    assert len(rows) == 2
    assert {row["displayName"] for row in rows} == {label.strip() if label else "guide"}
    assert {row["plugin"]["instance_id"] for row in rows} == {"sales", "research"}
    names = {row["name"] for row in rows}
    assert "research_brief" in names
    assert any(name.startswith("p__sales__guide__") for name in names)
    for row in rows:
        assert loader.get_skill(row["name"]).to_prompt().startswith("\n# Skill: " + row["name"])


def test_acp_catalog_discovers_new_user_skill_without_reviving_disabled_alias(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "legacy_alias", displayName="Old method")
    loader = SkillLoader(sources=[(root, "user")], replaced_skill_names={"legacy_alias"},
                         skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    server = object.__new__(BoxACPAgent)
    server._skill_loader = loader
    server._skills_loaded = True
    assert server._skills_meta(include_disabled=True) == []

    write_skill(root, "new_method", displayName="New method")
    rows = server._skills_meta(include_disabled=True)
    assert [(row["name"], row["displayName"]) for row in rows] == [("new_method", "New method")]
