from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader, SkillSelector
from box_agent.tools.skill_tool import GetSkillTool
from box_agent.tools.sub_agent_capabilities import (
    CapabilityResolver,
    DelegationSpec,
    parse_delegation_spec,
)


def _write_connector_skill(root: Path, *, disabled: bool = False) -> Path:
    skill_dir = root / "connector-pkulaw"
    skill_dir.mkdir(parents=True)
    disable_line = "disable: true\n" if disabled else ""
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: pkulaw\n"
        "description: PKULaw connector guidance\n"
        f"{disable_line}"
        "---\n\n"
        "Always retrieve before answering.\n",
        encoding="utf-8",
    )
    return skill_path


def test_connector_source_exposes_owner_metadata(tmp_path: Path) -> None:
    connector_root = tmp_path / "connectors" / "skills"
    skill_path = _write_connector_skill(connector_root)
    loader = SkillLoader(sources=[(connector_root, "connector")])

    loader.discover_skills()

    skill = loader.get_skill("pkulaw")
    assert skill is not None
    assert skill.source == "connector"
    assert skill.owner_id == "pkulaw"
    assert skill.skill_path == skill_path
    assert skill.to_metadata_dict()["ownerId"] == "pkulaw"
    assert loader.list_skills_metadata(include_connector=False) == []


def test_disabled_connector_skill_stays_out_of_model_catalog(tmp_path: Path) -> None:
    connector_root = tmp_path / "connectors" / "skills"
    _write_connector_skill(connector_root, disabled=True)
    loader = SkillLoader(sources=[(connector_root, "connector")])

    loader.discover_skills()

    assert loader.get_skill("pkulaw") is None
    disabled = loader.get_skill("pkulaw", include_disabled=True)
    assert disabled is not None
    assert disabled.disabled is True


def test_enabled_connector_skill_stays_out_of_automatic_recommendations(
    tmp_path: Path,
) -> None:
    connector_root = tmp_path / "connectors" / "skills"
    _write_connector_skill(connector_root)
    loader = SkillLoader(sources=[(connector_root, "connector")])
    loader.discover_skills()
    selector = SkillSelector(loader)
    selector.bind(f"prefix\n\n{SKILL_SLOT_SENTINEL}\n\nsuffix")

    assert "pkulaw" not in (selector.update("PKULaw legal search") or "")
    selector.update("continue")
    assert selector.matched_skill_names == ()


@pytest.mark.asyncio
async def test_connector_skill_loads_without_grant_and_obeys_disable_and_removal(tmp_path: Path) -> None:
    connector_root = tmp_path / "connectors" / "skills"
    skill_path = _write_connector_skill(connector_root)
    loader = SkillLoader(sources=[(connector_root, "connector")])
    loader.discover_skills()
    tool = GetSkillTool(loader)
    allowed = await tool.execute(skill_name="pkulaw")
    assert allowed.success is True
    assert "Always retrieve before answering." in allowed.content

    enabled_content = skill_path.read_text(encoding="utf-8")
    skill_path.write_text(enabled_content.replace("---\n", "---\ndisable: true\n", 1), encoding="utf-8")
    await loader.areload()
    assert (await tool.execute(skill_name="pkulaw")).success is False
    assert loader.filter_by_query("PKULaw legal search") == []

    skill_path.write_text(enabled_content, encoding="utf-8")
    await loader.areload()
    assert (await tool.execute(skill_name="pkulaw")).success is True
    skill_path.unlink()
    await loader.areload()
    assert (await tool.execute(skill_name="pkulaw")).success is False


def test_enabled_connector_skill_can_be_delegated_without_conversation_grant(tmp_path: Path) -> None:
    connector_root = tmp_path / "connectors" / "skills"
    _write_connector_skill(connector_root)
    loader = SkillLoader(sources=[(connector_root, "connector")])
    loader.discover_skills()
    spec = parse_delegation_spec(task="Search the law", skills=["pkulaw"], required_tools=[])
    assert isinstance(spec, DelegationSpec)

    result = CapabilityResolver().resolve(
        spec,
        parent_tools={},
        skill_loader=loader,
    )

    assert [skill.name for skill in result.skills] == ["pkulaw"]


def test_connector_disable_flag_does_not_change_existing_user_skill_behavior(
    tmp_path: Path,
) -> None:
    user_root = tmp_path / "skills"
    skill_dir = user_root / "ordinary-user-skill"
    skill_dir.mkdir(parents=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(
        "---\n"
        "name: ordinary-user-skill\n"
        "description: Existing user skill\n"
        "disable: true\n"
        "---\n\n"
        "Existing behavior remains unchanged.\n",
        encoding="utf-8",
    )
    loader = SkillLoader(sources=[(user_root, "user")])

    loader.discover_skills()

    skill = loader.get_skill("ordinary-user-skill")
    assert skill is not None
    assert skill.disabled is False
