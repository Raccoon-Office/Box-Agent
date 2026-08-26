"""
Test Skill Loader
"""

import json
import tempfile
from pathlib import Path

import pytest

from box_agent.tools.skill_loader import Skill, SkillLoader


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


def test_load_valid_skill():
    """Test loading a valid skill"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()

        create_test_skill(
            skill_dir,
            "test-skill",
            "A test skill",
            "This is a test skill content.",
        )

        loader = SkillLoader(tmpdir)
        skill = loader.load_skill(skill_dir / "SKILL.md")

        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.description == "A test skill"
        assert "This is a test skill content" in skill.content


def test_load_skill_with_metadata():
    """Test loading a skill with metadata"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()

        skill_file = skill_dir / "SKILL.md"
        skill_content = """---
name: test-skill
description: A test skill
license: MIT
allowed-tools:
  - read_file
  - write_file
metadata:
  author: Test Author
  version: "1.0"
capabilities: [presentation.authoring]
workflow: controlled_presentation
required_skills: [html-templates]
related-skills: "research-synthesis, research-to-deck-outline"
---

Skill content here.
"""
        skill_file.write_text(skill_content, encoding="utf-8")

        loader = SkillLoader(tmpdir)
        skill = loader.load_skill(skill_file)

        assert skill is not None
        assert skill.name == "test-skill"
        assert skill.license == "MIT"
        assert skill.allowed_tools == ["read_file", "write_file"]
        assert skill.metadata["author"] == "Test Author"
        assert skill.metadata["version"] == "1.0"
        assert skill.required_skills == ["html-templates"]
        assert skill.related_skills == [
            "research-synthesis",
            "research-to-deck-outline",
        ]
        assert skill.capabilities == ["presentation.authoring"]
        assert skill.workflow == "controlled_presentation"
        metadata = skill.to_metadata_dict()
        assert metadata["allowed_tools"] == ["read_file", "write_file"]
        assert metadata["capabilities"] == ["presentation.authoring"]
        assert metadata["workflow"] == "controlled_presentation"


def test_allowed_tools_are_normalized_and_rendered_as_routing_metadata(tmp_path):
    skill_dir = tmp_path / "tool-routing"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: tool-routing
description: Route a small task
allowed_tools: "write_file, read_file read_file"
---

Use the selected tools.
""",
        encoding="utf-8",
    )
    loader = SkillLoader(tmp_path)
    loader.discover_skills()

    skill = loader.get_skill("tool-routing")
    assert skill.allowed_tools == ["read_file", "write_file"]
    prompt = loader.get_skills_metadata_prompt()
    assert "allowed tools: read_file, write_file" in prompt


def test_load_invalid_skill():
    """A malformed SKILL.md returns a Hermes-style directory-name placeholder
    (broken=True) instead of dropping silently — otherwise the skill author
    can't tell why their skill vanished from ``## Available Skills``."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "invalid-skill"
        skill_dir.mkdir()

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("No frontmatter here!", encoding="utf-8")

        loader = SkillLoader(tmpdir)
        skill = loader.load_skill(skill_file)

        assert skill is not None
        assert skill.name == "invalid-skill"
        assert skill.broken is True
        assert skill.content == ""


def test_discover_skills():
    """Test discovering multiple skills"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create multiple skills
        for i in range(3):
            skill_dir = Path(tmpdir) / f"skill-{i}"
            skill_dir.mkdir()
            create_test_skill(
                skill_dir, f"skill-{i}", f"Test skill {i}", f"Content {i}"
            )

        loader = SkillLoader(tmpdir)
        skills = loader.discover_skills()

        assert len(skills) == 3
        assert len(loader.list_skills()) == 3


def test_get_skill():
    """Test getting a loaded skill"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()
        create_test_skill(skill_dir, "test-skill", "Test", "Content")

        loader = SkillLoader(tmpdir)
        loader.discover_skills()

        skill = loader.get_skill("test-skill")
        assert skill is not None
        assert skill.name == "test-skill"

        # Test non-existent skill
        assert loader.get_skill("nonexistent") is None



def test_get_skills_metadata_prompt():
    """Test generating metadata-only prompt (Progressive Disclosure Level 1)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test skills with different names to test categorization
        # Use longer content to simulate real skills
        long_content = """
# Detailed Skill Content

This is a comprehensive skill guide with lots of detailed instructions.

## Section 1
Here are detailed instructions for using this skill.

## Section 2
More detailed content and examples.

## Section 3
Even more content to make this realistic.
""" * 3  # Make it substantial

        skills_data = [
            ("pdf", "PDF manipulation toolkit", long_content),
            ("docx", "Document creation tool", long_content),
            ("canvas-design", "Canvas design tool", long_content),
        ]

        for name, desc, content in skills_data:
            skill_dir = Path(tmpdir) / name
            skill_dir.mkdir()
            create_test_skill(skill_dir, name, desc, content)

        loader = SkillLoader(tmpdir)
        loader.discover_skills()

        # Test metadata prompt generation
        metadata_prompt = loader.get_skills_metadata_prompt()

        # Should contain skill names and descriptions
        assert "pdf" in metadata_prompt
        assert "docx" in metadata_prompt
        assert "canvas-design" in metadata_prompt
        assert "PDF manipulation toolkit" in metadata_prompt
        assert "Document creation tool" in metadata_prompt

        # Should contain Progressive Disclosure explanation
        assert "Available Skills" in metadata_prompt

        # Should NOT contain full content (only metadata)
        assert "Detailed Skill Content" not in metadata_prompt
        assert "Section 1" not in metadata_prompt
        assert "Section 2" not in metadata_prompt


def test_nested_document_path_processing():
    """Test processing of nested document references (Level 3+)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()

        # Create nested documents
        (skill_dir / "reference.md").write_text("Reference content", encoding="utf-8")
        (skill_dir / "forms.md").write_text("Forms content", encoding="utf-8")

        # Create SKILL.md with nested references
        skill_content = """---
name: test-skill
description: Test skill with nested docs
---

For advanced features, see reference.md.
If you need forms, read forms.md and follow instructions.
"""
        (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        loader = SkillLoader(tmpdir)
        skill = loader.load_skill(skill_dir / "SKILL.md")

        assert skill is not None

        # Check that paths are converted to absolute and include instructions
        assert str(skill_dir / "reference.md") in skill.content
        assert str(skill_dir / "forms.md") in skill.content
        assert "use read_file" in skill.content.lower()


def test_script_path_processing():
    """Test processing of script paths in skills"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()

        # Create scripts directory
        scripts_dir = skill_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "test_script.py").write_text("# Python script", encoding="utf-8")

        # Create SKILL.md with script reference
        skill_content = """---
name: test-skill
description: Test skill with scripts
---

Run the script: python scripts/test_script.py
"""
        (skill_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        loader = SkillLoader(tmpdir)
        skill = loader.load_skill(skill_dir / "SKILL.md")

        assert skill is not None

        # Check that script path is converted to absolute
        assert str(skill_dir / "scripts" / "test_script.py") in skill.content


def test_skill_dir_placeholder_resolves_to_loaded_skill_root(tmp_path: Path):
    """Installed Skill shell snippets should not retain a guessed root."""
    skill_dir = tmp_path / "research-synthesis"
    skill_dir.mkdir()
    create_test_skill(
        skill_dir,
        "research-synthesis",
        "Research workflow",
        'SKILL_ROOT="{skill_dir}"\npython "$SKILL_ROOT/scripts/check.py"',
    )

    skill = SkillLoader(tmp_path).load_skill(skill_dir / "SKILL.md")

    assert skill is not None
    assert "{skill_dir}" not in skill.content
    assert f'SKILL_ROOT="{skill_dir.resolve()}"' in skill.content


def test_builtin_research_skill_does_not_fall_back_to_other_skill_set():
    """The bundled validator must stay within research-synthesis roots."""
    skills_dir = Path(__file__).parents[1] / "box_agent" / "skills"
    skill = SkillLoader(skills_dir).load_skill(
        skills_dir / "research-synthesis" / "SKILL.md"
    )

    assert skill is not None
    assert "{skill_dir}" not in skill.content
    assert "deep-research-swarm-officev3" not in skill.content
    assert str((skills_dir / "research-synthesis").resolve()) in skill.content


def test_skill_to_prompt_includes_root_directory():
    """Test that to_prompt includes skill root directory path"""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "test-skill"
        skill_dir.mkdir()

        skill_file = skill_dir / "SKILL.md"
        skill_content = """---
name: test-skill
description: A test skill
---

Skill content here.
"""
        skill_file.write_text(skill_content, encoding="utf-8")

        loader = SkillLoader(tmpdir)
        skill = loader.load_skill(skill_file)

        assert skill is not None

        # Test to_prompt includes root directory
        prompt = skill.to_prompt()
        assert "Skill Root Directory" in prompt
        assert str(skill_dir) in prompt
        assert "All files and references in this skill are relative to this directory" in prompt


def _write_manifest(builtin_dir: Path, names: list[str]) -> None:
    import json

    payload = {
        "schema_version": 1,
        "skills": [{"name": name, "path": f"{name}/SKILL.md"} for name in names],
    }
    (builtin_dir / "_manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_builtin_manifest_filters_orphan_skills():
    """Orphan SKILL.md (left on disk by a non-deleting installer) is ignored."""
    with tempfile.TemporaryDirectory() as tmpdir:
        builtin_dir = Path(tmpdir) / "builtin"
        builtin_dir.mkdir()

        # Two skills exist on disk; manifest only lists one.
        for name in ("kept", "orphan"):
            sd = builtin_dir / name
            sd.mkdir()
            create_test_skill(sd, name, f"{name} desc", f"{name} content")
        _write_manifest(builtin_dir, ["kept"])

        loader = SkillLoader(sources=[(builtin_dir, "builtin")])
        loader.discover_skills()

        assert loader.get_skill("kept") is not None
        assert loader.get_skill("orphan") is None
        assert set(loader.list_skills()) == {"kept"}


def test_builtin_manifest_availability_requires_platform_and_env_path(
    tmp_path, monkeypatch
):
    builtin_dir = tmp_path / "builtin"
    skill_dir = builtin_dir / "hosted"
    skill_dir.mkdir(parents=True)
    create_test_skill(skill_dir, "hosted", "hosted desc", "hosted content")
    manifest = {
        "schema_version": 1,
        "skills": [
            {
                "name": "hosted",
                "path": "hosted/SKILL.md",
                "availability": {
                    "platforms": ["darwin", "win32"],
                    "required_env_paths": ["HOSTED_CLI_HOME"],
                },
            }
        ],
    }
    (builtin_dir / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    cli_home = tmp_path / "cli-home"
    cli_home.mkdir()
    monkeypatch.setenv("HOSTED_CLI_HOME", str(cli_home))
    monkeypatch.setattr("box_agent.tools.skill_loader.sys.platform", "linux")
    loader = SkillLoader(sources=[(builtin_dir, "builtin")])
    assert loader.discover_skills() == []

    monkeypatch.setattr("box_agent.tools.skill_loader.sys.platform", "win32")
    monkeypatch.setenv("HOSTED_CLI_HOME", str(tmp_path / "missing"))
    loader = SkillLoader(sources=[(builtin_dir, "builtin")])
    assert loader.discover_skills() == []

    monkeypatch.setenv("HOSTED_CLI_HOME", str(cli_home))
    loader = SkillLoader(sources=[(builtin_dir, "builtin")])
    assert [skill.name for skill in loader.discover_skills()] == ["hosted"]


def test_builtin_manifest_reload_signature_ignores_resource_files():
    """Builtin reload checks should not stat every bundled resource file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        builtin_dir = Path(tmpdir) / "builtin"
        builtin_dir.mkdir()

        sd = builtin_dir / "kept"
        sd.mkdir()
        create_test_skill(sd, "kept", "kept desc", "kept content")
        asset = sd / "scripts" / "large-bundle.js"
        asset.parent.mkdir()
        asset.write_text("bundle v1", encoding="utf-8")
        _write_manifest(builtin_dir, ["kept"])

        loader = SkillLoader(sources=[(builtin_dir, "builtin")])
        loader.discover_skills()

        asset.write_text("bundle v2 with unrelated resource changes", encoding="utf-8")

        assert loader.maybe_reload() is False
        assert loader.get_skill("kept") is not None


def test_builtin_manifest_reload_detects_listed_skill_change():
    """Changing a manifest-listed SKILL.md should still trigger reload."""
    with tempfile.TemporaryDirectory() as tmpdir:
        builtin_dir = Path(tmpdir) / "builtin"
        builtin_dir.mkdir()

        sd = builtin_dir / "kept"
        sd.mkdir()
        create_test_skill(sd, "kept", "old desc", "old content")
        _write_manifest(builtin_dir, ["kept"])

        loader = SkillLoader(sources=[(builtin_dir, "builtin")])
        loader.discover_skills()

        create_test_skill(sd, "kept", "new desc", "new content that changes size")

        assert loader.maybe_reload() is True
        assert loader.get_skill("kept").description == "new desc"


def test_user_source_reload_detects_new_skill_without_manifest():
    """User skills remain dynamically discoverable without a manifest."""
    with tempfile.TemporaryDirectory() as tmpdir:
        user_dir = Path(tmpdir) / "user"
        user_dir.mkdir()

        sd = user_dir / "first"
        sd.mkdir()
        create_test_skill(sd, "first", "first desc", "first content")

        loader = SkillLoader(sources=[(user_dir, "user")])
        loader.discover_skills()

        sd = user_dir / "second"
        sd.mkdir()
        create_test_skill(sd, "second", "second desc", "second content")

        assert loader.maybe_reload() is True
        assert set(loader.list_skills()) == {"first", "second"}


def test_user_source_not_filtered_by_manifest():
    """Manifest filtering must only apply to builtin sources, never user."""
    with tempfile.TemporaryDirectory() as tmpdir:
        builtin_dir = Path(tmpdir) / "builtin"
        user_dir = Path(tmpdir) / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()

        # Builtin manifest is empty → all builtin skills are orphans.
        sd = builtin_dir / "builtin-orphan"
        sd.mkdir()
        create_test_skill(sd, "builtin-orphan", "x", "x")
        _write_manifest(builtin_dir, [])

        # User skill must still load even with no manifest there.
        sd = user_dir / "user-skill"
        sd.mkdir()
        create_test_skill(sd, "user-skill", "y", "y")

        loader = SkillLoader(sources=[(user_dir, "user"), (builtin_dir, "builtin")])
        loader.discover_skills()

        assert loader.get_skill("user-skill") is not None
        assert loader.get_skill("builtin-orphan") is None


def test_user_skill_cannot_override_reserved_roadmap_runtime():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        builtin_dir = root / "builtin"
        user_dir = root / "user"
        builtin_dir.mkdir()
        user_dir.mkdir()

        builtin_skill = builtin_dir / "roadmap"
        builtin_skill.mkdir()
        create_test_skill(
            builtin_skill, "roadmap", "builtin roadmap", "trusted builtin content"
        )
        _write_manifest(builtin_dir, ["roadmap"])

        user_skill = user_dir / "roadmap"
        user_skill.mkdir()
        create_test_skill(user_skill, "roadmap", "user roadmap", "override content")

        loader = SkillLoader(sources=[(user_dir, "user"), (builtin_dir, "builtin")])
        loader.discover_skills()

        roadmap = loader.get_skill("roadmap")
        assert roadmap is not None
        assert roadmap.source == "builtin"
        assert roadmap.content == "trusted builtin content"


def test_skill_settings_disable_filters_loaded_skills():
    """officev3 disabled skills must not be listed or loadable via get_skill."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skills_dir = root / "skills"
        skills_dir.mkdir()

        for name in ("enabled-skill", "disabled-skill"):
            sd = skills_dir / name
            sd.mkdir()
            create_test_skill(sd, name, f"{name} desc", f"{name} content")

        settings_path = root / "skill-settings.json"
        settings_path.write_text(
            '{"disabledSkillNames":["disabled-skill"]}', encoding="utf-8"
        )

        loader = SkillLoader(skills_dir, skill_settings_path=settings_path)
        loader.discover_skills()

        assert loader.get_skill("enabled-skill") is not None
        assert loader.get_skill("disabled-skill") is None
        assert set(loader.list_skills()) == {"enabled-skill"}


def test_skill_settings_can_include_disabled_for_expert_sessions():
    """Expert sessions can opt into disabled skills without changing default filtering."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skills_dir = root / "skills"
        skills_dir.mkdir()

        for name in ("enabled-skill", "disabled-skill"):
            sd = skills_dir / name
            sd.mkdir()
            create_test_skill(sd, name, f"{name} desc", f"{name} content")

        settings_path = root / "skill-settings.json"
        settings_path.write_text(
            '{"disabledSkillNames":["disabled-skill"]}', encoding="utf-8"
        )

        loader = SkillLoader(skills_dir, skill_settings_path=settings_path)
        loader.discover_skills()

        assert loader.get_skill("disabled-skill") is None
        assert loader.get_skill("disabled-skill", include_disabled=True) is not None
        assert set(loader.list_skills()) == {"enabled-skill"}
        assert set(loader.list_skills(include_disabled=True)) == {
            "enabled-skill",
            "disabled-skill",
        }
        assert [s.name for s in loader.filter_by_query("disabled")] == []
        assert [
            s.name
            for s in loader.filter_by_query("disabled", include_disabled=True)
        ] == ["disabled-skill"]


def test_skill_settings_change_triggers_reload():
    """Changing officev3 skill settings should affect the next get_skill path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        skills_dir = root / "skills"
        skills_dir.mkdir()

        sd = skills_dir / "toggle-skill"
        sd.mkdir()
        create_test_skill(sd, "toggle-skill", "toggle desc", "toggle content")

        settings_path = root / "skill-settings.json"
        settings_path.write_text('{"disabledSkillNames":[]}', encoding="utf-8")

        loader = SkillLoader(skills_dir, skill_settings_path=settings_path)
        loader.discover_skills()

        assert loader.get_skill("toggle-skill") is not None

        settings_path.write_text(
            '{"disabledSkillNames":["toggle-skill"]}', encoding="utf-8"
        )

        assert loader.maybe_reload() is True
        assert loader.get_skill("toggle-skill") is None


def test_missing_manifest_falls_back_to_unfiltered():
    """No manifest in builtin dir → behave like before (load everything)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        builtin_dir = Path(tmpdir) / "builtin"
        builtin_dir.mkdir()

        sd = builtin_dir / "any-skill"
        sd.mkdir()
        create_test_skill(sd, "any-skill", "x", "x")
        # no _manifest.json written

        loader = SkillLoader(sources=[(builtin_dir, "builtin")])
        loader.discover_skills()

        assert loader.get_skill("any-skill") is not None


def test_malformed_manifest_falls_back_to_unfiltered():
    """Malformed manifest → warn + load everything (don't break startup)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        builtin_dir = Path(tmpdir) / "builtin"
        builtin_dir.mkdir()

        sd = builtin_dir / "any-skill"
        sd.mkdir()
        create_test_skill(sd, "any-skill", "x", "x")
        (builtin_dir / "_manifest.json").write_text("not json {", encoding="utf-8")

        loader = SkillLoader(sources=[(builtin_dir, "builtin")])
        loader.discover_skills()

        assert loader.get_skill("any-skill") is not None
