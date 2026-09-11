"""Local Skill discovery remains complete, scoped, and metadata-only."""

import json
import shutil

import pytest

from box_agent.tools.skill_loader import SkillLoader


def write_skill(root, name, description="shared topic", body="SECRET_BODY_SENTINEL"):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


@pytest.fixture
def catalog(tmp_path):
    for number in range(55):
        write_skill(tmp_path / "skills", f"guide-{number:02d}")
    write_skill(tmp_path / "skills", "memory-guide", "unrelated memory hints")
    loader = SkillLoader(
        sources=[(tmp_path / "skills", "user")],
        skill_settings_path=tmp_path / "settings.json",
    )
    loader.discover_skills()
    return loader


def make_tool(loader, **kwargs):
    from box_agent.tools.skill_catalog_tool import ListSkillsTool

    return ListSkillsTool(loader, **kwargs)


async def payload(tool, **arguments):
    result = await tool.invoke(arguments)
    assert result.success, result.error
    data = json.loads(result.content)
    assert data == result.raw_output
    assert "SECRET_BODY_SENTINEL" not in result.content
    return data


async def test_default_list_and_later_pages_cover_complete_catalog(catalog):
    tool = make_tool(catalog)
    first = await payload(tool)
    second = await payload(tool, offset=first["next_offset"], limit=50)

    assert first["limit"] == 20
    assert first["total"] == 56
    assert len(first["skills"]) == 20
    assert first["next_offset"] == 20
    assert second["next_offset"] is None
    names = [row["name"] for row in first["skills"] + second["skills"]]
    assert names == sorted(catalog.list_skills())
    assert first["revision"] == second["revision"]
    assert all(row["available"] for row in first["skills"])


async def test_query_searches_whole_catalog_before_paging(catalog):
    tool = make_tool(catalog)
    first = await payload(tool, query="shared", limit=50)
    last = await payload(tool, query="shared", offset=50)

    assert first["total"] == 55
    assert len(first["skills"]) == 50
    assert [row["name"] for row in last["skills"]] == [f"guide-{n:02d}" for n in range(50, 55)]
    assert last["next_offset"] is None
    assert first["revision"] == last["revision"]
    assert "memory-guide" not in [row["name"] for row in first["skills"]]


async def test_exact_disabled_and_broken_lookup_returns_only_diagnostic(catalog, tmp_path):
    (tmp_path / "settings.json").write_text('{"disabledSkillNames":["guide-00"]}')
    broken = tmp_path / "skills" / "broken" / "SKILL.md"
    broken.parent.mkdir()
    broken.write_text("---\nname: broken\ndescription: [invalid\n---\nSECRET_BODY_SENTINEL")
    tool = make_tool(catalog, explicitly_allowed_skill_names={"guide-00"})

    listing = await payload(tool, limit=50)
    disabled = await payload(tool, query="guide-00")
    broken_result = await payload(tool, query="broken")

    assert "guide-00" not in [row["name"] for row in listing["skills"]]
    assert "broken" not in [row["name"] for row in listing["skills"]]
    for data, reason in ((disabled, "disabled"), (broken_result, "malformed")):
        assert len(data["skills"]) == 1
        assert data["skills"][0]["available"] is False
        assert reason in data["skills"][0]["unavailable_reason"].lower()


@pytest.mark.parametrize("field", ["name", "description"])
@pytest.mark.parametrize("yaml_value", [
    "2026", "true", "null", "[entry]", "{key: entry}", "''", "'   '", "2026-09-09",
])
async def test_malformed_required_metadata_cannot_disable_catalog(tmp_path, field, yaml_value):
    from box_agent.tools.skill_tool import GetSkillTool

    root = tmp_path / "skills"
    write_skill(root, "valid")
    bad_path = root / "broken-meta" / "SKILL.md"
    bad_path.parent.mkdir()
    name = yaml_value if field == "name" else "broken-meta"
    description = yaml_value if field == "description" else "Some method"
    bad_body = "MALFORMED_BODY_MUST_NOT_RUN"
    bad_path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{bad_body}\n")
    loader = SkillLoader(sources=[(root, "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    tool = make_tool(loader)

    for query in ("", "valid"):
        result = await payload(tool, query=query)
        assert [row["name"] for row in result["skills"]] == ["valid"]
        assert result["skills"][0]["available"]

    diagnostic = await payload(tool, query="broken-meta")
    row = diagnostic["skills"][0]
    assert row["name"] == "broken-meta"
    assert row["broken"] and not row["available"]
    assert field in row["unavailable_reason"]
    assert "non-empty string" in row["unavailable_reason"]
    assert loader.get_skill("broken-meta").content == ""
    invalid_read = await GetSkillTool(loader).invoke({"skill_name": "broken-meta"})
    assert not invalid_read.success
    assert bad_body not in str(invalid_read)
    valid_read = await GetSkillTool(loader).invoke({"skill_name": "valid"})
    assert valid_read.success


async def test_quoted_numeric_required_metadata_remains_valid(tmp_path):
    root = tmp_path / "skills"
    path = root / "numeric-text" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: '2026'\ndescription: '123'\n---\nA valid method\n")
    loader = SkillLoader(sources=[(root, "user")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()

    result = await payload(make_tool(loader), query="2026")

    assert result["skills"][0]["name"] == "2026"
    assert result["skills"][0]["description"] == "123"
    assert result["skills"][0]["available"]


async def test_include_disabled_only_adds_diagnostics_and_never_unlocks_reading(catalog, tmp_path):
    from box_agent.tools.skill_tool import GetSkillTool

    (tmp_path / "settings.json").write_text('{"disabledSkillNames":["guide-00"]}')
    normal = await payload(make_tool(catalog), limit=50)
    diagnostic_tool = make_tool(catalog, include_disabled=True,
                                explicitly_allowed_skill_names={"guide-00"})

    included = await payload(diagnostic_tool, limit=50)
    exact = await payload(diagnostic_tool, query="guide-00")

    row = next(row for row in included["skills"] if row["name"] == "guide-00")
    assert row["available"] is False
    assert "disabled" in row["unavailable_reason"].lower()
    assert exact["skills"] == [row]
    assert included["total"] == normal["total"] + 1
    assert included["revision"] != normal["revision"]
    read = await GetSkillTool(catalog, include_disabled=True,
                              explicitly_allowed_skill_names={"guide-00"}).invoke({"skill_name": "guide-00"})
    assert not read.success and "disabled" in read.error.lower()
    assert "SECRET_BODY_SENTINEL" not in (read.model_context or "")


async def test_include_disabled_diagnostics_still_obey_child_scope(catalog, tmp_path):
    (tmp_path / "settings.json").write_text('{"disabledSkillNames":["guide-00"]}')
    tool = make_tool(catalog, include_disabled=True, allowed_skill_names={"guide-01"},
                     explicitly_allowed_skill_names={"guide-00"})

    listed = await payload(tool)
    exact = await payload(tool, query="guide-00")

    assert [row["name"] for row in listed["skills"]] == ["guide-01"]
    assert all(row["name"] == "guide-01" for row in exact["skills"])


async def test_child_scope_precedes_explicit_profile_exception(catalog):
    allowed = {"guide-01"}
    explicit = {"guide-00", "guide-01"}
    tool = make_tool(catalog, allowed_skill_names=allowed,
                     blocked_skill_names={"guide-00", "guide-01"},
                     explicitly_allowed_skill_names=explicit)
    all_rows = await payload(tool)
    outside = await payload(tool, query="guide-00")

    assert [row["name"] for row in all_rows["skills"]] == ["guide-01"]
    assert all(row["name"] in allowed for row in outside["skills"])
    explicit.remove("guide-01")
    blocked = await payload(tool, query="guide-01")
    assert blocked["skills"][0]["available"] is False
    assert "profile" in blocked["skills"][0]["unavailable_reason"].lower()


async def test_revision_changes_for_metadata_but_not_body(catalog, tmp_path):
    tool = make_tool(catalog)
    first = await payload(tool)
    write_skill(tmp_path / "skills", "guide-00", "shared UPDATED description")
    changed = await payload(tool)
    write_skill(tmp_path / "skills", "guide-00", "shared UPDATED description", "different body")
    body_only = await payload(tool)

    assert changed["revision"] != first["revision"]
    assert changed["revision"] == body_only["revision"]
    assert "UPDATED" in changed["skills"][0]["description"]


async def test_builtin_platform_unavailability_stays_hidden_with_named_diagnostic(tmp_path):
    root = tmp_path / "builtin"
    write_skill(root, "hosted")
    (root / "_manifest.json").write_text(json.dumps({"skills": [{
        "name": "hosted", "path": "hosted/SKILL.md",
        "availability": {"platforms": ["fictional-os"]},
    }]}))
    loader = SkillLoader(sources=[(root, "builtin")], skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    tool = make_tool(loader)

    assert (await payload(tool))["skills"] == []
    diagnostic = await payload(tool, query="hosted")
    assert diagnostic["skills"][0]["available"] is False
    assert "platform" in diagnostic["skills"][0]["unavailable_reason"].lower()

    shutil.rmtree(root)
    removed = await payload(tool, query="hosted")
    assert removed["skills"] == []
    assert removed["revision"] != diagnostic["revision"]


async def test_same_name_source_fallback_refreshes_catalog_revision(tmp_path):
    user = tmp_path / "user"
    builtin = tmp_path / "builtin"
    user_path = write_skill(user, "demo", "same metadata")
    write_skill(builtin, "demo", "same metadata")
    loader = SkillLoader(sources=[(user, "user"), (builtin, "builtin")],
                         skill_settings_path=tmp_path / "settings.json")
    loader.discover_skills()
    tool = make_tool(loader)
    before = await payload(tool)
    user_path.unlink()

    after = await payload(tool)

    assert before["skills"][0]["source"] == "user"
    assert after["skills"][0]["source"] == "builtin"
    assert after["revision"] != before["revision"]


async def test_empty_search_and_offset_past_end_have_no_next_page(catalog):
    tool = make_tool(catalog)
    empty = await payload(tool, query="unfindableword")
    past_end = await payload(tool, offset=100)
    assert empty["skills"] == []
    assert empty["total"] == 0
    assert empty["next_offset"] is None
    assert past_end["skills"] == []
    assert past_end["next_offset"] is None


async def test_disabled_config_change_invalidates_catalog_revision(catalog, tmp_path):
    tool = make_tool(catalog)
    before = await payload(tool)
    (tmp_path / "settings.json").write_text('{"disabledSkillNames":["guide-00"]}')
    after = await payload(tool)
    assert after["revision"] != before["revision"]
    assert after["total"] == before["total"] - 1


@pytest.mark.parametrize("arguments", [{"limit": 51}, {"limit": 0}, {"offset": -1}, {"limit": True}, {"include_disabled": True}])
async def test_invalid_pagination_or_visibility_arguments_fail(catalog, arguments):
    result = await make_tool(catalog).invoke(arguments)
    assert not result.success
    assert result.raw_output["code"] == "INVALID_TOOL_ARGUMENTS"
