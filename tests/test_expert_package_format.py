"""Prepared packages retain the installed client's full-profile conventions."""
import json

import pytest

from box_agent.expert_package_format import ExpertPackageError, load_expert_profile
from box_agent.experts import ExpertSessionContext


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "downloaded"
    skill = root / "skills/skill-0"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: actual-skill\ndescription: Bound method\n---\nUse this method.\n")
    (skill / "references/example.txt").write_text("reference body")
    (root / "agents").mkdir()
    (root / "agents/lead.md").write_text("\ufeff---\nname: lead\n---\n\nOriginal expert instructions.\n")
    manifest = {
        "schemaVersion": "1.0", "packageId": "package-id-not-selection-id", "expertType": "agent", "entryAgent": "lead",
        "agents": [{"id": "lead", "file": "agents/lead.md", "skillBindings": [{"key": "method", "required": True}], "toolRequests": ["generate_image"]}],
        "skills": [{"key": "method", "source": "BUNDLED", "path": "skills/skill-0"}],
    }
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    write_json(root / "files.json", [{"path": "agents/lead.md"}, {"path": "skills/skill-0/SKILL.md"}, {"path": "skills/skill-0/references/example.txt"}])
    return root, manifest


def load(root, kind="expert"):
    return load_expert_profile(root, kind, {"id": "catalog-id", "name": "Selected expert", "description": "Selected role"}, "a" * 64)


def test_full_profile_uses_selection_identity_and_actual_skill_name(package):
    root, _ = package
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    profile = load(root)
    assert profile["id"] == "catalog-id"
    assert profile["name"] == "Selected expert"
    assert profile["role"] == "Selected role"
    assert profile["instructions"] == profile["visibleRules"] == ["Original expert instructions."]
    assert profile["defaultSkills"] == ["actual-skill", "generate_image"]
    assert profile["skillBindings"] == [
        {"key": "method", "slug": "actual-skill", "source": "BUNDLED", "skillId": None},
        {"slug": "generate_image", "source": "TOOL", "skillId": None},
    ]
    assert profile["revision"] == "package:" + "a" * 64
    assert profile["packageSnapshot"]["directory"] == str(root)
    assert profile["packageSnapshot"]["skills"] == [{"key": "method", "slug": "actual-skill", "directory": str(root / "skills/skill-0")}]
    context = ExpertSessionContext.from_meta({"expert": profile})
    assert context.skill_bindings_by_path[root / "skills/skill-0/SKILL.md"] == "actual-skill"
    assert before == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_team_preserves_member_bindings_and_workflow(package):
    root, manifest = package
    (root / "agents/member.md").write_text("Member rules.")
    (root / "workflows").mkdir()
    (root / "workflows/team.md").write_text("---\nname: flow\n---\nReview and synthesize.\n")
    manifest.update(expertType="team", team={"leadAgent": "lead", "memberAgents": ["lead", "member"], "workflow": "workflows/team.md", "executionMode": "orchestrated"})
    manifest["agents"].append({"id": "member", "file": "agents/member.md", "skillBindings": [{"key": "method"}]})
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    profile = load(root, "team")
    assert profile["id"] == "catalog-id"
    assert profile["leader"]["id"] == "lead"
    assert [member["id"] for member in profile["members"]] == ["member"]
    assert profile["members"][0]["instructions"] == ["Member rules."]
    assert profile["members"][0]["defaultSkills"] == ["actual-skill"]
    assert profile["workflow"] == ["Review and synthesize."]
    assert profile["executionMode"] == "orchestrated"


def test_market_bindings_keep_real_identity_for_later_installed_skill_check(package):
    root, manifest = package
    skill_id = "a0b4e989-6c12-4d78-893c-cb0f6ef9f542"
    manifest["skills"].append({"key": "market", "source": "HUB", "skillId": skill_id})
    manifest["agents"][0]["skillBindings"].append({"key": "market", "required": True})
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    write_json(root / "dependencies.lock.json", {"skills": [{"key": "market", "slug": "real-market-skill", "skillId": skill_id}]})
    profile = load(root)
    assert profile["defaultSkills"] == ["actual-skill", "real-market-skill", "generate_image"]
    assert {"key": "market", "slug": "real-market-skill", "source": "MARKET", "skillId": skill_id} in profile["skillBindings"]
    assert len(profile["packageSnapshot"]["skills"]) == 1


@pytest.mark.parametrize("lock", [None, {"skills": []}, {"skills": [{"key": "market", "slug": "market-skill", "skillId": "wrong"}]}])
def test_required_market_definition_cannot_lose_its_lock_identity(package, lock):
    root, manifest = package
    manifest["skills"].append({"key": "market", "source": "HUB", "skillId": "expected"})
    manifest["agents"][0]["skillBindings"].append({"key": "market"})
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    if lock is not None:
        write_json(root / "dependencies.lock.json", lock)
    with pytest.raises(ExpertPackageError, match="[Mm]arket"):
        load(root)


def test_unbound_market_definition_does_not_require_installation_or_lock(package):
    root, manifest = package
    manifest["skills"].append({"key": "unused", "source": "HUB", "skillId": "not-selected"})
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    assert load(root)["defaultSkills"] == ["actual-skill", "generate_image"]


@pytest.mark.parametrize("resource", ["../outside", "/absolute", "C:/host", "skills\\method"])
def test_resource_paths_cannot_escape_or_use_host_paths(package, resource):
    root, manifest = package
    manifest["agents"][0]["file"] = resource
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    with pytest.raises(ExpertPackageError):
        load(root)


@pytest.mark.parametrize("resource", [".box-agent-plugin/plugin.json", "files.json", "agents/lead.md", "skills/skill-0/SKILL.md", "skills/skill-0/references/example.txt"])
def test_symlinked_resources_cannot_escape_package(package, tmp_path, resource):
    root, _ = package
    path = root / resource
    outside = tmp_path / "outside"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(ExpertPackageError, match="outside"):
        load(root)


def test_missing_required_skill_subfile_blocks_profile(package):
    root, _ = package
    (root / "skills/skill-0/references/example.txt").unlink()
    with pytest.raises(ExpertPackageError, match="required expert Skill resources"):
        load(root)


def test_existing_manifest_optional_missing_resource_semantics_are_retained(package):
    root, manifest = package
    manifest["agents"][0]["skillBindings"][0]["required"] = False
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    (root / "skills/skill-0/references/example.txt").unlink()
    profile = load(root)
    assert profile["packageSnapshot"]["skills"] == []
    assert profile["defaultSkills"] == ["generate_image"]
    assert "optionalSkills" not in profile


def test_missing_persona_or_other_required_file_is_not_an_optional_skill(package):
    root, _ = package
    write_json(root / "files.json", [{"path": "other/missing.txt"}])
    with pytest.raises(ExpertPackageError, match="missing a required resource"):
        load(root)


@pytest.mark.parametrize("change", [
    {"entryAgent": []}, {"agents": []}, {"agents": [{"id": "", "file": "agents/lead.md"}]},
    {"skills": [{"key": "method", "source": "BUNDLED", "path": "skills/skill-0"}] * 2},
    {"skills": [{"key": "method", "source": "UNKNOWN"}]},
    {"expertType": "team"},
])
def test_invalid_manifest_returns_typed_package_error(package, change):
    root, manifest = package
    manifest.update(change)
    write_json(root / ".box-agent-plugin/plugin.json", manifest)
    with pytest.raises(ExpertPackageError) as error:
        load(root)
    assert error.value.code == "EXPERT_PACKAGE_INVALID"


@pytest.mark.parametrize("kind,metadata,digest", [
    ("unknown", {"id": "id", "name": "Name"}, "a" * 64),
    ("expert", {"id": "../id", "name": "Name"}, "a" * 64),
    ("expert", {"id": "id", "name": " "}, "a" * 64),
    ("expert", {"id": "id", "name": "Name"}, "g" * 64),
])
def test_invalid_selection_metadata_or_digest_is_rejected(package, kind, metadata, digest):
    with pytest.raises(ExpertPackageError, match="selection identity"):
        load_expert_profile(package[0], kind, metadata, digest)
