"""Only dependencies reachable from current desktop expert requests are gated."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from box_agent.expert_dependencies import (
    ExpertDependencyError, expert_dependency_market_bindings, validate_expert_dependencies,
)
from box_agent.experts import ExpertSessionContext
from box_agent.tools.skill_loader import RESERVED_BUILTIN_SKILL_NAMES, SkillLoader


def _write_skill(root: Path, name="review", *, extra="", body="Use the review method."):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Expert dependency test method\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


def _loader(*sources, settings=None):
    loader = SkillLoader(sources=list(sources), skill_settings_path=settings)
    loader.discover_skills()
    return loader


def _expert(names=(), **fields):
    return {"expert": {
        "id": "custom-reviewer", "name": "Custom reviewer", "source": "user",
        "instructions": ["Review the supplied material."],
        "defaultSkills": list(names), "requiredSkills": list(names), **fields,
    }}


def _validate(meta, loader=None, tools=()):
    validate_expert_dependencies(meta, ExpertSessionContext.from_meta(meta), loader, tools)


def _bound(meta, sources=(), settings=None):
    context = ExpertSessionContext.from_meta(meta)
    loader = _loader(*sources, settings=settings).with_expert_skill_sources(
        context.skill_names(), skill_directories=context.skill_directories,
        skill_bindings_by_directory=context.skill_bindings_by_directory,
        skill_bindings_by_path=context.skill_bindings_by_path,
    )
    return context, loader


def _package_expert(tmp_path, name="review"):
    package = tmp_path / "package"
    directory = _write_skill(package / "skills", name)
    binding = {"slug": name, "source": "BUNDLED", "skillId": None, "key": "method"}
    return _expert([name], skillBindings=[binding], packageSnapshot={
        "directory": str(package),
        "skills": [{"key": "method", "slug": name, "directory": str(directory)}],
    })


def test_prompt_only_expert_needs_no_skill_loader_or_tools():
    _validate(_expert())


def test_no_expert_leaves_ordinary_sessions_unchanged():
    _validate({})


def test_selected_skill_missing_is_reported_once_even_when_draft_repeats_optional():
    meta = _expert(["my-local-review"], optionalSkills=["my-local-review"])
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(meta)
    assert raised.value.code == "EXPERT_DEPENDENCIES_NOT_READY"
    assert len(raised.value.dependencies) == 1
    assert raised.value.dependencies[0] == {
        "expertId": "custom-reviewer", "expertName": "Custom reviewer",
        "name": "my-local-review", "source": None, "required": True,
        "state": "missing", "reason": "The selected Skill is not available in the cloud runtime.",
    }


def test_optional_metadata_alone_does_not_create_a_new_required_selection():
    _validate(_expert(optionalSkills=["unused-old-draft-skill"]))


@pytest.mark.parametrize("source", ["builtin", "user"])
def test_selected_available_skill_uses_the_existing_loader(tmp_path, source):
    _write_skill(tmp_path, "review")
    _validate(_expert(["review"]), _loader((tmp_path, source)))


def test_builtin_binding_cannot_be_satisfied_by_same_name_user_skill(tmp_path):
    _write_skill(tmp_path, "review")
    meta = _expert(["review"], skillBindings=[{"slug": "review", "source": "BUILTIN", "skillId": None}])
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(meta, _loader((tmp_path, "user")))
    assert raised.value.dependencies[0]["state"] == "conflict"


def test_builtin_binding_with_real_builtin_is_available(tmp_path):
    _write_skill(tmp_path, "review")
    meta = _expert(["review"], skillBindings=[{"slug": "review", "source": "BUILTIN", "skillId": None}])
    _validate(meta, _loader((tmp_path, "builtin")))


@pytest.mark.parametrize("tools", [[SimpleNamespace(name="generate_image")], {"generate_image": object()}])
def test_inherited_tool_binding_is_not_looked_up_as_a_skill(tools):
    meta = _expert(["generate_image"], skillBindings=[{"slug": "generate_image", "source": "TOOL", "skillId": None}])
    _validate(meta, tools=tools)


def test_same_name_skill_does_not_satisfy_a_missing_tool(tmp_path):
    _write_skill(tmp_path, "generate_image")
    meta = _expert(["generate_image"], skillBindings=[{"slug": "generate_image", "source": "TOOL", "skillId": None}])
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(meta, _loader((tmp_path, "builtin")))
    assert raised.value.dependencies[0]["state"] == "unavailable"
    assert raised.value.dependencies[0]["source"] == "TOOL"


def test_selected_package_skill_has_matching_key_name_and_bound_path(tmp_path):
    meta = _package_expert(tmp_path)
    context, loader = _bound(meta)
    validate_expert_dependencies(meta, context, loader, [])
    assert loader.get_skill("review").source == "expert"


def test_wrong_package_key_is_not_satisfied_by_a_global_same_name_skill(tmp_path):
    meta = _package_expert(tmp_path)
    meta["expert"]["skillBindings"][0]["key"] = "wrong-key"
    _write_skill(tmp_path / "builtin", "review")
    context, loader = _bound(meta, [(tmp_path / "builtin", "builtin")])
    with pytest.raises(ExpertDependencyError) as raised:
        validate_expert_dependencies(meta, context, loader, [])
    assert raised.value.dependencies[0]["state"] == "conflict"


def test_package_skill_disappearing_after_loader_preparation_is_rejected(tmp_path):
    meta = _package_expert(tmp_path)
    context, loader = _bound(meta)
    Path(meta["expert"]["packageSnapshot"]["skills"][0]["directory"], "SKILL.md").unlink()
    with pytest.raises(ExpertDependencyError):
        validate_expert_dependencies(meta, context, loader, [])


def test_reserved_builtin_cannot_be_replaced_via_package_binding(tmp_path):
    name = sorted(RESERVED_BUILTIN_SKILL_NAMES)[0]
    meta = _package_expert(tmp_path, name)
    _write_skill(tmp_path / "builtin", name)
    context, loader = _bound(meta, [(tmp_path / "builtin", "builtin")])
    with pytest.raises(ExpertDependencyError) as raised:
        validate_expert_dependencies(meta, context, loader, [])
    assert raised.value.dependencies[0]["state"] == "conflict"


def test_real_team_shape_allows_declared_same_name_package_copies(tmp_path):
    package = tmp_path / "package"
    first = _write_skill(package / "lead", "writing-workflow")
    second = _write_skill(package / "member", "writing-workflow")
    members = []
    for ident, key in [("lead", "lead-method"), ("member", "member-method")]:
        members.append({"id": ident, "name": ident, "defaultSkills": ["writing-workflow"],
                        "skillBindings": [{"slug": "writing-workflow", "source": "BUNDLED", "key": key, "skillId": None}]})
    meta = {"expert_team": {
        "id": "custom-writing-team", "name": "Custom writing team", "leader": members[0], "members": members[1:],
        "packageSnapshot": {"directory": str(package), "skills": [
            {"key": "lead-method", "slug": "writing-workflow", "directory": str(first)},
            {"key": "member-method", "slug": "writing-workflow", "directory": str(second)},
        ]},
    }}
    context, loader = _bound(meta)
    validate_expert_dependencies(meta, context, loader, [])


def test_missing_team_member_skill_reports_its_member_identity():
    meta = {"expert_team": {"id": "team", "name": "Team", "leader": {"id": "lead", "name": "Lead"},
                             "members": [{"id": "reviewer", "name": "Reviewer", "defaultSkills": ["local-only"]}]}}
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(meta)
    assert raised.value.dependencies[0]["expertId"] == "reviewer"
    assert raised.value.dependencies[0]["name"] == "local-only"


def test_team_alias_selection_uses_the_existing_snake_case_precedence():
    meta = {"expert_team": {"name": "Chosen", "leader": {"name": "Lead"}},
            "expertTeam": {"name": "Ignored", "leader": {"name": "Other", "defaultSkills": ["missing"]}}}
    _validate(meta)


def test_market_binding_is_not_proved_by_same_name_or_authored_metadata(tmp_path):
    _write_skill(tmp_path, "review", extra="metadata:\n  ref: hub:market-id\n")
    meta = _expert(["review"], skillBindings=[{"slug": "review", "source": "MARKET", "skillId": "market-id"}])
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(meta, _loader((tmp_path, "user")))
    item = raised.value.dependencies[0]
    assert item["state"] == "unavailable"
    assert item["skillId"] == "market-id"
    assert "identity" in item["reason"]


def test_conflicting_skill_and_tool_binding_is_rejected(tmp_path):
    _write_skill(tmp_path, "review")
    meta = _expert(["review"], skillBindings=[{"slug": "review", "source": "BUILTIN"}, {"slug": "review", "source": "TOOL"}])
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(meta, _loader((tmp_path, "builtin")), tools=[SimpleNamespace(name="review")])
    assert raised.value.dependencies[0]["state"] == "conflict"


@pytest.mark.parametrize("field,value", [("source", {}), ("skillId", []), ("key", {})])
def test_malformed_binding_identity_returns_a_dependency_error(tmp_path, field, value):
    _write_skill(tmp_path, "review")
    binding = {"slug": "review", "source": "BUILTIN", field: value}
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(_expert(["review"], skillBindings=[binding]), _loader((tmp_path, "builtin")))
    assert raised.value.dependencies[0]["state"] == "conflict"


def test_bound_package_skill_keeps_existing_disabled_name_override(tmp_path):
    meta = _package_expert(tmp_path)
    settings = tmp_path / "skill-settings.json"
    settings.write_text(json.dumps({"disabledSkillNames": ["review"]}))
    context, loader = _bound(meta, settings=settings)
    assert loader.get_skill("review") is not None
    validate_expert_dependencies(meta, context, loader, [])


def test_unavailable_disabled_builtin_is_reported_using_actual_loader_state(tmp_path):
    _write_skill(tmp_path, "review")
    settings = tmp_path / "skill-settings.json"
    settings.write_text(json.dumps({"disabledSkillNames": ["review"]}))
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(_expert(["review"]), _loader((tmp_path, "builtin"), settings=settings))
    assert raised.value.dependencies[0]["state"] == "disabled"


def test_malformed_selected_skill_is_not_considered_available(tmp_path):
    directory = tmp_path / "review"
    directory.mkdir()
    (directory / "SKILL.md").write_text("---\nname: review\n---\nMissing description.")
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(_expert(["review"]), _loader((tmp_path, "user")))
    assert raised.value.dependencies[0]["state"] == "broken"


def test_missing_required_child_skill_reuses_existing_resolution_policy(tmp_path):
    _write_skill(tmp_path, "review", extra="required_skills:\n  - evidence\n")
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(_expert(["review"]), _loader((tmp_path, "user")))
    assert "evidence" in raised.value.dependencies[0]["reason"]


def test_source_changed_since_discovery_fails_preflight(tmp_path):
    directory = _write_skill(tmp_path, "review")
    loader = _loader((tmp_path, "user"))
    (directory / "SKILL.md").write_text("changed")
    with pytest.raises(ExpertDependencyError) as raised:
        _validate(_expert(["review"]), loader)
    item = raised.value.dependencies[0]
    assert item["state"] == "unavailable"
    assert item["dependencyCode"] == "SKILL_SOURCE_CHANGED"
    assert "dependencyName" not in item


def test_preflight_collects_root_and_child_failures_without_leaking_details(tmp_path):
    _write_skill(tmp_path, "review", extra="required_skills: [evidence]\n")
    _write_skill(tmp_path, "blocked-root")
    loader = _loader((tmp_path, "user"))
    meta = _expert(["missing-root", "review", "blocked-root"])
    with pytest.raises(ExpertDependencyError) as raised:
        validate_expert_dependencies(
            meta, ExpertSessionContext.from_meta(meta), loader, [],
            skill_access_filter=lambda skill: skill.name != "blocked-root",
        )
    common = {"expertId": "custom-reviewer", "expertName": "Custom reviewer",
              "source": None, "required": True}
    assert raised.value.dependencies == [
        {**common, "name": "missing-root", "state": "missing",
         "reason": "The selected Skill is not available in the cloud runtime."},
        {**common, "name": "review", "state": "missing",
         "reason": "Required Skill 'evidence' is not found.",
         "dependencyName": "evidence", "dependencyCode": "SKILL_NOT_FOUND"},
        {**common, "name": "blocked-root", "state": "unavailable",
         "reason": "Required Skill 'blocked-root' is not enabled for this conversation.",
         "dependencyName": "blocked-root"},
    ]


@pytest.mark.parametrize("first_failure", ["market", "access", "source"])
def test_child_identity_and_access_errors_precede_source_changes(tmp_path, first_failure):
    _write_skill(tmp_path, "review", extra="required_skills: [evidence]\n")
    child = _write_skill(tmp_path, "evidence")
    (child / ".skill-installation.json").write_text(
        json.dumps({"source": "hub", "skillId": "other-id"}),
    )
    loader = _loader((tmp_path, "user"))
    bindings = ([{"slug": "evidence", "source": "MARKET", "skillId": "expected-id"}]
                if first_failure == "market" else [])
    meta = _expert(["review"], optionalSkills=["evidence"], skillBindings=bindings)
    context = ExpertSessionContext.from_meta(meta)
    loader = loader.with_expert_skill_sources(
        context.skill_names(),
        dependency_market_skill_bindings=expert_dependency_market_bindings(meta, context),
    )
    (child / "SKILL.md").write_text("changed after discovery")
    with pytest.raises(ExpertDependencyError) as raised:
        validate_expert_dependencies(
            meta, context, loader, [],
            skill_access_filter=lambda skill: first_failure == "source" or skill.name != "evidence",
        )
    item = raised.value.dependencies[0]
    assert item["name"] == "review" and item["state"] == "unavailable"
    assert "skillId" not in item
    if first_failure == "source":
        assert item["dependencyCode"] == "SKILL_SOURCE_CHANGED"
        assert "dependencyName" not in item
    else:
        assert item["dependencyName"] == "evidence"
        if first_failure == "market":
            assert item["dependencyCode"] == "SKILL_DEPENDENCY_IDENTITY_MISMATCH"
        else:
            assert "dependencyCode" not in item
        assert ("market installation identity" if first_failure == "market" else
                "not enabled for this conversation") in item["reason"]


@pytest.mark.parametrize("selection", ["expert", "expert_team", "expertTeam"])
def test_installed_market_skill_keeps_complete_host_profile_compatible(tmp_path, selection):
    directory = _write_skill(tmp_path, "review")
    (directory / ".skill-installation.json").write_text(json.dumps({
        "source": "hub", "skillId": "market-review-id", "version": "1.0.0",
    }))
    profile = _expert(["review"], skillBindings=[{
        "slug": "review", "source": "MARKET", "skillId": "market-review-id",
    }])["expert"]
    meta = ({"expert": profile} if selection == "expert" else {selection: {
        "id": "host-team", "name": "Host team", "leader": profile,
        "members": [dict(profile, id="member", name="Member")],
    }})
    _validate(meta, _loader((tmp_path, "user")))


@pytest.mark.parametrize("missing_index", [0, 12])
def test_team_preflight_checks_only_members_kept_by_session_assembly(missing_index):
    members = [{"id": f"member-{index}", "name": f"Member {index}"} for index in range(13)]
    members[missing_index]["defaultSkills"] = ["local-only"]
    meta = {"expert_team": {"id": "team", "name": "Team", "members": members}}
    context = ExpertSessionContext.from_meta(meta)
    assert len(context.team.members) == 12
    if missing_index == 0:
        with pytest.raises(ExpertDependencyError) as raised:
            validate_expert_dependencies(meta, context, None, ())
        assert raised.value.dependencies[0]["expertId"] == "member-0"
    else:
        validate_expert_dependencies(meta, context, None, ())


@pytest.mark.parametrize("selection,field,count", [
    ("expert", "defaultSkills", 12),
    ("expert", "required_skills", 12),
    ("expertTeam", "default_skills", 8),
])
def test_preflight_uses_session_normalized_skill_selection(selection, field, count):
    kept = [f"tool-{index}" for index in range(count)]
    profile = {"id": "custom", "name": "Custom", field: [*kept, "outside-selection"],
               "skillBindings": [{"slug": name, "source": "TOOL"} for name in kept]}
    meta = ({"expert": profile} if selection == "expert" else {
        selection: {"id": "team", "name": "Team", "members": [profile]},
    })
    context = ExpertSessionContext.from_meta(meta)
    assert context.skill_names() == kept
    validate_expert_dependencies(meta, context, None, kept)
    with pytest.raises(ExpertDependencyError) as raised:
        validate_expert_dependencies(meta, context, None, kept[1:])
    assert [item["name"] for item in raised.value.dependencies] == [kept[0]]


@pytest.mark.parametrize("selection", ["expert_team", "expertTeam"])
def test_invalid_team_entries_do_not_shift_local_package_bindings(tmp_path, selection):
    meta = _package_expert(tmp_path)
    member = meta["expert"]
    member["id"] = " local-member "
    member["name"] = "Local  member"
    member["default_skills"] = member.pop("defaultSkills")
    meta = {selection: {
        "id": "local-team", "name": "Local team",
        "members": [None, {}, {"name": "  ", "defaultSkills": ["ignored"]}, member],
    }}
    context, loader = _bound(meta)
    assert context.team.members[0].id == "local-member"
    validate_expert_dependencies(meta, context, loader, ())
    member["skillBindings"][0]["key"] = "wrong-key"
    with pytest.raises(ExpertDependencyError) as raised:
        validate_expert_dependencies(meta, context, loader, ())
    assert raised.value.dependencies[0]["state"] == "conflict"
    assert raised.value.dependencies[0]["expertId"] == "local-member"
    assert raised.value.dependencies[0]["expertName"] == "Local member"
