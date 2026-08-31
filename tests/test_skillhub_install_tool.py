from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.runtime import invoke_tool_with_permissions
from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skillhub_install_tool import SkillHubInstallTool


CANDIDATE = {
    "id": "6f6f41f0-572b-4e55-9b8b-137d4d9a54a5",
    "slug": "edge-tts",
    "name": "文字转语音",
    "publisherDisplayName": "林",
    "currentVersion": "1.0.0",
}


def _candidate_provider(skill_id: str):
    return CANDIDATE if skill_id == CANDIDATE["id"] else None


@pytest.mark.asyncio
async def test_install_requires_a_candidate_returned_in_this_session() -> None:
    calls = []

    async def installer(payload):
        calls.append(payload)
        return {"status": "installed"}

    tool = SkillHubInstallTool(installer, candidate_provider=_candidate_provider)

    result = await tool.execute("invented-skill-id")

    assert not result.success
    assert result.error.startswith("SEARCH_REQUIRED:")
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_id_reports_exact_known_candidate_instead_of_inviting_guesses() -> None:
    async def installer(_payload):
        raise AssertionError("installer must not run")

    tool = SkillHubInstallTool(
        installer,
        candidate_provider=_candidate_provider,
        candidate_list_provider=lambda: [CANDIDATE],
    )

    result = await tool.execute("文字转语音")

    assert not result.success
    assert f"skill_id='{CANDIDATE['id']}'" in result.error
    assert "slug='edge-tts'" in result.error
    assert "do not guess or bypass the Skill marketplace" in result.error


@pytest.mark.asyncio
async def test_install_requires_one_shot_confirmation_before_host_call() -> None:
    calls = []

    async def installer(payload):
        calls.append(payload)
        return {"status": "installed"}

    tool = SkillHubInstallTool(installer, candidate_provider=_candidate_provider)

    result = await tool.execute(CANDIDATE["id"])

    assert not result.success
    assert result.error.startswith("USER_CONFIRMATION_REQUIRED:")
    assert result.permission_request == {
        "type": "permission_request",
        "scope": "skillhub",
        "requested_scope": f"install:{CANDIDATE['id']}",
        "reason": "Install '文字转语音' from the Skill marketplace, published by 林",
        "temporary_supported": True,
        "persistent_supported": False,
        "skill_id": CANDIDATE["id"],
        "slug": "edge-tts",
        "name": "文字转语音",
        "version": "1.0.0",
    }
    assert calls == []


@pytest.mark.asyncio
async def test_confirmed_install_refreshes_catalog_and_exposes_skill(tmp_path: Path) -> None:
    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    loader = SkillLoader(sources=[(user_skills, "user")])
    loader.discover_skills()
    calls = []

    async def installer(payload):
        calls.append(payload)
        skill_dir = user_skills / "edge-tts"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: edge-tts\ndescription: Generate speech audio.\n---\n\n"
            "Use edge-tts to generate the requested audio.\n",
            encoding="utf-8",
        )
        return {"status": "installed", "skill": {"name": "edge-tts"}}

    tool = SkillHubInstallTool(
        installer,
        candidate_provider=_candidate_provider,
        skill_loader=loader,
    )
    permission = (await tool.execute(CANDIDATE["id"])).permission_request
    assert permission is not None
    tool.approve_permission_request(permission)

    result = await tool.execute(CANDIDATE["id"])

    assert result.success
    assert calls == [
        {
            "skillId": CANDIDATE["id"],
            "slug": "edge-tts",
            "name": "文字转语音",
            "publisherDisplayName": "林",
            "version": "1.0.0",
        }
    ]
    assert result.raw_output["status"] == "installed"
    assert result.raw_output["skillName"] == "edge-tts"
    assert loader.get_skill("edge-tts") is not None
    assert "Call get_skill" in result.content
    assert "do not construct executable paths from environment variables" in result.content


@pytest.mark.asyncio
async def test_exact_manual_market_install_skips_redundant_confirmation(tmp_path: Path) -> None:
    user_skills = tmp_path / "skills"
    skill_dir = user_skills / "edge-tts"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: edge-tts\ndescription: Generate speech audio.\n---\n",
        encoding="utf-8",
    )
    (skill_dir / ".skill-installation.json").write_text(
        '{"source":"hub","skillId":"'
        + CANDIDATE["id"]
        + '"}',
        encoding="utf-8",
    )
    loader = SkillLoader(sources=[(user_skills, "user")])
    loader.discover_skills()
    calls = []

    async def installer(payload):
        calls.append(payload)
        return {"status": "installed"}

    tool = SkillHubInstallTool(
        installer,
        candidate_provider=_candidate_provider,
        skill_loader=loader,
    )

    result = await tool.execute(CANDIDATE["id"])

    assert result.success
    assert result.permission_request is None
    assert result.raw_output["status"] == "already_installed"
    assert result.raw_output["skillName"] == "edge-tts"
    assert calls == []


@pytest.mark.asyncio
async def test_same_slug_without_matching_market_marker_still_requires_confirmation(
    tmp_path: Path,
) -> None:
    user_skills = tmp_path / "skills"
    skill_dir = user_skills / "edge-tts"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: edge-tts\ndescription: Unrelated local skill.\n---\n",
        encoding="utf-8",
    )
    (skill_dir / ".skill-installation.json").write_text(
        '{"source":"hub","skillId":"another-market-id"}',
        encoding="utf-8",
    )
    loader = SkillLoader(sources=[(user_skills, "user")])
    loader.discover_skills()

    async def installer(_payload):
        raise AssertionError("installer must wait for confirmation")

    tool = SkillHubInstallTool(
        installer,
        candidate_provider=_candidate_provider,
        skill_loader=loader,
    )

    result = await tool.execute(CANDIDATE["id"])

    assert not result.success
    assert result.permission_request is not None


@pytest.mark.asyncio
async def test_approval_is_bound_to_the_exact_candidate() -> None:
    calls = []

    async def installer(payload):
        calls.append(payload)
        return {"status": "installed"}

    tool = SkillHubInstallTool(installer, candidate_provider=_candidate_provider)
    tool.approve_permission_request(
        {
            "scope": "skillhub",
            "requested_scope": "install:another-id",
            "skill_id": CANDIDATE["id"],
        }
    )

    result = await tool.execute(CANDIDATE["id"])

    assert result.permission_request is not None
    assert calls == []


@pytest.mark.asyncio
async def test_shared_permission_chain_confirms_then_installs() -> None:
    install_calls = []
    permission_calls = []

    async def installer(payload):
        install_calls.append(payload)
        return {"status": "already_installed", "skill": {"name": "edge-tts"}}

    class Negotiator:
        async def negotiate(self, permission_request):
            permission_calls.append(permission_request)
            return True

    tool = SkillHubInstallTool(installer, candidate_provider=_candidate_provider)

    result, policy_decision = await invoke_tool_with_permissions(
        tool,
        {"skill_id": CANDIDATE["id"]},
        permission_negotiator=Negotiator(),
    )

    assert result.success
    assert result.raw_output["status"] == "installed_not_visible"
    assert len(permission_calls) == 1
    assert len(install_calls) == 1
    assert policy_decision is not None
    assert policy_decision["decision"] == "approved"
