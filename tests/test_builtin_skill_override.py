"""A bundled method can opt out of accidental replacement by an old install."""

import pytest

from box_agent.tools.skill_loader import SkillLoader


def write_skill(root, *, policy=None, visible=True):
    root.mkdir(parents=True)
    metadata = f"metadata:\n  user_visible: {str(visible).lower()}\n"
    if policy is not None:
        metadata += f"  allow_override: {policy}\n"
    (root / "SKILL.md").write_text(
        "---\nname: method\ndescription: PPT method\n" + metadata + "---\n"
        + str(root), encoding="utf-8",
    )


@pytest.mark.parametrize("source", ["user", "connector"])
def test_canonical_builtin_remains_readable_and_hidden_despite_same_name_install(tmp_path, source):
    builtin, overlay = tmp_path / "builtin", tmp_path / "overlay"
    write_skill(builtin, policy="false", visible=False)
    write_skill(overlay)
    loader = SkillLoader(sources=[(overlay, source), (builtin, "builtin")])
    loader.discover_skills()
    skill = loader.get_skill("method")
    assert skill.source == "builtin"
    assert str(builtin) in skill.content
    assert skill.to_metadata_dict()["allow_override"] is False
    assert loader.filter_by_query("PPT") == []
    assert (overlay / "SKILL.md").is_file()


@pytest.mark.parametrize("policy", [None, "true", '"false"', "0"])
def test_user_override_stays_supported_unless_builtin_explicitly_opts_out(tmp_path, policy):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    write_skill(builtin, policy=policy)
    write_skill(user)
    loader = SkillLoader(sources=[(user, "user"), (builtin, "builtin")])
    loader.discover_skills()
    assert loader.get_skill("method").source == "user"


def test_canonical_builtin_does_not_bypass_user_disabled_setting(tmp_path):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    write_skill(builtin, policy="false", visible=False)
    write_skill(user)
    settings = tmp_path / "settings.json"
    settings.write_text('{"disabledSkillNames":["method"]}')
    loader = SkillLoader(sources=[(user, "user"), (builtin, "builtin")], skill_settings_path=settings)
    loader.discover_skills()
    assert loader.get_skill("method") is None
    assert loader.get_skill("method", include_disabled=True).source == "builtin"
