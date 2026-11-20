"""Downloaded/full expert profiles share the existing ACP lifecycle."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from acp import RequestError
from acp.schema import SessionNotification

import box_agent.acp as acp_module
import box_agent.expert_packages as packages
from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import StreamEvent
from box_agent.session_log import SessionLog
from box_agent.tools.file_tools import ReadTool
from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL
from box_agent.tools.skill_tool import create_skill_tools
from tests.acp_helpers import acp_params
from tests.expert_package_helpers import archive_bytes, reference


class Connection:
    async def session_update(self, session_id, update, **meta):
        SessionNotification(session_id=session_id, update=update, field_meta=meta or None)


class DoneLLM:
    async def generate_stream(self, *args, **kwargs):
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    bodies = {"expert": archive_bytes(), "team": archive_bytes("team")}

    def serve(request):
        return httpx.Response(200, content=bodies["team" if "/team.zip" in str(request.url) else "expert"])

    transport = httpx.MockTransport(serve)
    prepare = packages.prepare_expert_meta

    async def prepare_meta(meta):
        return await prepare(meta, transport=transport)

    monkeypatch.setattr(acp_module, "prepare_expert_meta", prepare_meta)
    tools, loader = create_skill_tools(sources=[])
    agent = BoxACPAgent(Connection(), Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir="workspace", max_steps=1,
                          enable_memory=False, enable_memory_extraction=False, goal_autopilot_enabled=False),
        tools=ToolsConfig(enable_file_tools=False, enable_bash=False, enable_sub_agent=False, enable_mcp=False),
    ), DoneLLM(), tools, f"system\n{SKILL_SLOT_SENTINEL}", skill_loader=loader)
    try:
        yield agent
    finally:
        await agent.aclose()


def new_params(tmp_path, kind="expert", **meta):
    key = "expert" if kind == "expert" else "expert_team"
    selected = reference(archive_bytes(kind), f"client-{kind}-id")
    if kind == "team":
        selected["packageDownload"]["downloadUrl"] = selected["packageDownload"]["downloadUrl"].replace("package.zip", "team.zip")
    return acp_params(SimpleNamespace(cwd=str(tmp_path / "workspace"), field_meta={key: selected, **meta}))


@pytest.mark.asyncio
async def test_initialize_advertises_download_support_instead_of_runtime_catalog(host):
    response = await host.initialize(protocol_version=1)
    assert response.field_meta["expert_package_download"] == 1
    assert "builtin_experts" not in response.field_meta


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_download_assembles_identity_rules_and_reads_skill_reference(host, tmp_path, kind):
    params = new_params(tmp_path, kind)
    original = copy.deepcopy(params)
    response = await host.new_session(**params)
    state = host._sessions[response.sessionId]
    key = "expert" if kind == "expert" else "expert_team"
    assert response.field_meta["expert_context"][key]["id"] == f"client-{kind}-id"
    assert f"Follow {kind} expert rules." in state.agent.system_prompt
    result = await state.agent.tools["get_skill"].execute(f"method-{kind}")
    assert result.success and f"{kind} method body" in result.content
    skill = state.skill_loader.get_skill(f"method-{kind}")
    reference_path = skill.skill_path.parent / "references/style.txt"
    read = ReadTool(str(tmp_path / "workspace"), permission_engine=state.permission_engine)
    read_result = await read.execute(str(reference_path))
    assert read_result.success and "ORIGINAL_REFERENCE_OK" in read_result.content
    assert not reference_path.is_relative_to(tmp_path / "workspace")
    assert params == original
    assert "PRIVATE_TICKET" not in json.dumps(response.model_dump(mode="json"))
    if kind == "team":
        assert [member.id for member in state.expert_context.team.members] == ["member"]


@pytest.mark.asyncio
async def test_two_chats_share_package_but_not_workspace_or_expert_context(host, tmp_path):
    first = await host.new_session(**new_params(tmp_path, session_id="chat-one"))
    params = new_params(tmp_path, session_id="chat-two")
    params["cwd"] = str(tmp_path / "another-workspace")
    second = await host.new_session(**params)
    one, two = host._sessions[first.sessionId], host._sessions[second.sessionId]
    assert one is not two and one.agent.workspace_dir != two.agent.workspace_dir
    assert one.expert_context.skill_directories == two.expert_context.skill_directories


@pytest.mark.asyncio
async def test_prompt_keeps_existing_selection_and_does_not_prepare_again(host, tmp_path, monkeypatch):
    response = await host.new_session(**new_params(tmp_path))
    state = host._sessions[response.sessionId]

    async def unexpected(_):
        raise AssertionError("Prompt must not prepare expert resources")

    monkeypatch.setattr(acp_module, "prepare_expert_meta", unexpected)
    result = await host.prompt(**acp_params(SimpleNamespace(
        sessionId=response.sessionId, prompt=[{"type": "text", "text": "short task"}],
        field_meta={"expert_team": {"id": "another-id"}},
    )))
    assert result.stopReason == "end_turn"
    assert host._sessions[response.sessionId] is state
    assert state.expert_context.expert.id == "client-expert-id"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["expert", "expert_team", "expertTeam"])
async def test_complete_host_profiles_keep_original_rules_without_download(host, tmp_path, key):
    profile = {"id": "custom", "name": "Custom", "instructions": ["Host rules"], "workflow": ["Host workflow"]}
    response = await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path / "workspace"), field_meta={key: profile})))
    state = host._sessions[response.sessionId]
    context = state.expert_context
    assert (context.expert or context.team).id == "custom"
    assert not (tmp_path / "profile/experts/packages").exists()


@pytest.mark.asyncio
async def test_missing_user_selected_skill_blocks_without_changing_live_session(host, tmp_path):
    first = await host.new_session(**new_params(tmp_path, session_id="stable-chat"))
    original = host._sessions[first.sessionId]
    meta = {"session_id": "stable-chat", "expert": {"id": "custom", "name": "Custom",
            "instructions": ["User rules"], "defaultSkills": ["only-on-local-machine"],
            "requiredSkills": ["only-on-local-machine"], "optionalSkills": ["only-on-local-machine"]}}
    with pytest.raises(RequestError) as error:
        await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path / "workspace"), field_meta=meta)))
    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert error.value.data["dependencies"][0]["name"] == "only-on-local-machine"
    assert host._sessions[first.sessionId] is original
    result = await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "continue"}])
    assert result.stopReason == "end_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["hash", "tool"])
async def test_package_or_inherited_tool_failure_keeps_previous_session(host, tmp_path, failure):
    first = await host.new_session(**new_params(tmp_path, session_id="stable-chat"))
    original = host._sessions[first.sessionId]
    if failure == "hash":
        params = new_params(tmp_path, session_id="stable-chat")
        params["expert"]["packageDownload"]["digest"] = "0" * 64
    else:
        params = {"cwd": str(tmp_path / "workspace"), "expert": {"id": "custom", "name": "Custom",
            "defaultSkills": ["generate_image"], "skillBindings": [{"slug": "generate_image", "source": "TOOL", "skillId": None}]},
            "session_id": "stable-chat"}
        host._config.image_generation.endpoint = ""
        host._config.officev3.use_default_image_generation_preset = False
    with pytest.raises(RequestError):
        await host.new_session(**params)
    assert host._sessions[first.sessionId] is original


@pytest.mark.asyncio
async def test_busy_chat_does_not_start_download(host, tmp_path, monkeypatch):
    first = await host.new_session(**new_params(tmp_path, session_id="busy-chat"))
    state = host._sessions[first.sessionId]
    state._prompt_in_progress = True

    async def unexpected(_):
        raise AssertionError("Busy chat must fail before resource preparation")

    monkeypatch.setattr(acp_module, "prepare_expert_meta", unexpected)
    try:
        with pytest.raises(RequestError) as error:
            await host.new_session(**new_params(tmp_path, session_id="busy-chat"))
        assert error.value.data["code"] == "SESSION_BUSY"
    finally:
        state._prompt_in_progress = False


@pytest.mark.asyncio
async def test_resume_and_load_reassemble_full_selection_through_the_same_preparation(host, tmp_path):
    first = await host.new_session(**new_params(tmp_path, session_id="resume-chat"))
    original = host._sessions[first.sessionId]
    params = new_params(tmp_path)
    resumed = await host.resume_session(cwd=params["cwd"], session_id=first.sessionId, expert=params["expert"])
    current = host._sessions[first.sessionId]
    assert current is not original
    assert resumed.field_meta["expert_context"]["expert"]["id"] == "client-expert-id"
    loaded = await host.load_session(cwd=params["cwd"], session_id=first.sessionId)
    assert "expert_context" not in loaded.field_meta
    assert host._sessions[first.sessionId].expert_context is None


@pytest.mark.asyncio
async def test_unsupported_id_only_input_is_not_silently_treated_as_ordinary_chat(host, tmp_path):
    with pytest.raises(RequestError) as error:
        await host.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path / "workspace"), field_meta={"expert": {"id": "old-id"}})))
    assert error.value.data["code"] == "EXPERT_PACKAGE_REFERENCE_REQUIRED"
    assert not host._sessions


def _connector_expert_profile(kind, skill_name="pkulaw"):
    member = {"id": "legal-reviewer", "name": "Legal reviewer",
              "instructions": ["Use the selected legal method."],
              "defaultSkills": [skill_name], "requiredSkills": [skill_name]}
    if kind == "expert":
        return {"expert": member}
    return {"expert_team": {"id": "legal-team", "name": "Legal team",
                            "leader": member, "members": [],
                            "workflow": ["Review the supplied case."]}}


def _add_connector_skills(host, tmp_path, *, require_connector=False):
    connector_root = tmp_path / "connectors/skills"
    directory = connector_root / "connector-pkulaw"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: pkulaw\ndescription: Legal connector method\n---\n"
        "Read the current legal sources before answering.\n", encoding="utf-8",
    )
    sources = [(connector_root, "connector")]
    if require_connector:
        user_root = tmp_path / "user-skills"
        directory = user_root / "legal-review"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            "---\nname: legal-review\ndescription: Review with legal sources\n"
            "required_skills: [pkulaw]\n---\nUse current legal sources.\n", encoding="utf-8",
        )
        sources.insert(0, (user_root, "user"))
    tools, loader = create_skill_tools(sources=sources)
    host._base_tools[:] = tools
    host._skill_loader = loader
    assert loader.get_skill("pkulaw").source == "connector"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_ungranted_connector_dependency_rejects_rebind_and_keeps_live_expert(host, tmp_path, kind):
    _add_connector_skills(host, tmp_path)
    first = await host.new_session(**new_params(tmp_path, session_id="legal-chat"))
    original = host._sessions[first.sessionId]

    with pytest.raises(RequestError) as error:
        await host.new_session(cwd=str(tmp_path / "workspace"), session_id="legal-chat",
                               **_connector_expert_profile(kind))

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert error.value.data["dependencies"][0]["name"] == "pkulaw"
    assert host._sessions[first.sessionId] is original
    result = await original.agent.tools["get_skill"].execute("method-expert")
    assert result.success


def _ready_cli_connector_meta():
    return {"host_capabilities": {"connector_discovery": 1}, "selected_connector_ids": ["pkulaw"],
            "connector_discovery": {
                "schema_version": 1, "catalog_revision": "legal-catalog", "state_revision": 1,
                "available": True, "entries": [
                    {"id": "pkulaw", "name": "Legal connector", "description": "Retrieve current legal sources.",
                     "kind": "cli", "auth_mode": "oauth", "skills": ["pkulaw"],
                     "configured": True, "enabled": True, "status": "connected", "execution_ready": True},
                ],
            }}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_selected_ready_cli_connector_dependency_stays_readable_after_restore(host, tmp_path, kind):
    _add_connector_skills(host, tmp_path)
    meta = {**_ready_cli_connector_meta(), **_connector_expert_profile(kind)}
    response = await host.new_session(cwd=str(tmp_path / "workspace"), **meta)
    for restore in (None, host.resume_session, host.load_session):
        if restore is not None:
            await restore(cwd=str(tmp_path / "workspace"), session_id=response.sessionId, **meta)
        state = host._sessions[response.sessionId]
        result = await state.agent.tools["get_skill"].execute("pkulaw")
        assert result.success
        assert "Read the current legal sources" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_transitive_ungranted_connector_dependency_blocks_expert_session(host, tmp_path, kind):
    _add_connector_skills(host, tmp_path, require_connector=True)
    assert host._skill_loader.get_skill("legal-review").required_skills == ["pkulaw"]

    with pytest.raises(RequestError) as error:
        await host.new_session(cwd=str(tmp_path / "workspace"),
                               **_connector_expert_profile(kind, "legal-review"))

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert error.value.data["dependencies"][0]["name"] == "legal-review"
    assert not host._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["new_session", "resume_session", "load_session"])
async def test_legacy_session_cannot_enable_cli_connector_by_changing_rebind_metadata(host, tmp_path, method):
    _add_connector_skills(host, tmp_path)
    first = await host.new_session(cwd=str(tmp_path / "workspace"), session_id="legacy-legal-chat")
    original = host._sessions[first.sessionId]
    requested = {**_ready_cli_connector_meta(), **_connector_expert_profile("expert")}
    session_id = "legacy-legal-chat" if method == "new_session" else first.sessionId

    with pytest.raises(RequestError) as error:
        await getattr(host, method)(cwd=str(tmp_path / "workspace"), session_id=session_id, **requested)

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert host._sessions[first.sessionId] is original
    assert original.agent.connector_discovery is None


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["expert", "expert_team", "expertTeam"])
@pytest.mark.parametrize("source", ["builtin", "user"])
async def test_prepared_local_market_profiles_keep_full_object_and_restore_compatibility(
    host, tmp_path, selection, source,
):
    directory = tmp_path / "user-skills/market-review"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: market-review\ndescription: Installed market method\n---\n"
        "Use the installed market review method.\n", encoding="utf-8",
    )
    (directory / ".skill-installation.json").write_text(json.dumps({
        "source": "hub", "skillId": "market-review-id", "version": "1.0.0",
    }), encoding="utf-8")
    tools, loader = create_skill_tools(sources=[(directory.parent, "user")])
    host._base_tools[:] = tools
    host._skill_loader = loader
    profile = {"id": "host-market-reviewer", "name": "Host market reviewer", "source": source,
               "instructions": ["Preserve the host supplied rules."],
               "defaultSkills": ["market-review"], "requiredSkills": ["market-review"],
               "skillBindings": [{"slug": "market-review", "source": "MARKET", "skillId": "market-review-id"}]}
    meta = ({"expert": profile} if selection == "expert" else {selection: {
        "id": "host-market-team", "name": "Host market team", "source": source,
        "leader": profile, "members": [dict(profile, id="member", name="Member")],
        "workflow": ["Use the host supplied workflow."],
    }})
    original = copy.deepcopy(meta)
    response = await host.new_session(cwd=str(tmp_path / "workspace"), **meta)
    for restore in (None, host.resume_session, host.load_session):
        if restore is not None:
            await restore(cwd=str(tmp_path / "workspace"), session_id=response.sessionId, **meta)
        state = host._sessions[response.sessionId]
        assert "Preserve the host supplied rules." in state.agent.system_prompt
        result = await state.agent.tools["get_skill"].execute("market-review")
        assert result.success
        assert "Use the installed market review method." in result.content
    assert meta == original
    assert not (tmp_path / "profile/experts/packages").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("method", ["resume_session", "load_session"])
async def test_ready_cli_expert_reads_connector_skill_after_cold_restore(host, tmp_path, kind, method):
    _add_connector_skills(host, tmp_path)
    meta = {**_ready_cli_connector_meta(), **_connector_expert_profile(kind)}
    first = await host.new_session(cwd=str(tmp_path / "workspace"), session_id="cold-cli-chat", **meta)
    original = host._sessions[first.sessionId]
    session_log = original.agent.session_log
    await original.aclose()
    session_log.close()
    host._sessions.pop(first.sessionId)

    await getattr(host, method)(cwd=str(tmp_path / "workspace"), session_id=first.sessionId, **meta)

    restored = host._sessions[first.sessionId]
    assert restored is not original
    assert restored.agent.connector_discovery is not None
    result = await restored.agent.tools["get_skill"].execute("pkulaw")
    assert result.success
    assert "Read the current legal sources" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["resume_session", "load_session"])
async def test_cold_legacy_restore_cannot_enable_cli_discovery_from_new_metadata(host, tmp_path, method):
    _add_connector_skills(host, tmp_path)
    first = await host.new_session(cwd=str(tmp_path / "workspace"), session_id="cold-legacy-chat")
    original = host._sessions[first.sessionId]
    session_log = original.agent.session_log
    await original.aclose()
    session_log.close()
    host._sessions.pop(first.sessionId)
    requested = {**_ready_cli_connector_meta(), **_connector_expert_profile("expert")}

    with pytest.raises(RequestError) as error:
        await getattr(host, method)(cwd=str(tmp_path / "workspace"), session_id=first.sessionId, **requested)

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert not host._sessions
    retained, _ = SessionLog.open_with_projection(
        tmp_path / "profile/sessions", session_id="cold-legacy-chat", cwd=tmp_path / "workspace",
    )
    try:
        assert not list(retained.events_of_type("connectors/discovery-contract"))
    finally:
        retained.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("team_key", ["expert_team", "expertTeam"])
async def test_missing_required_team_member_is_rejected_without_replacing_live_session(
    host, tmp_path, entry, team_key,
):
    team = {
        "id": "custom-review-team", "name": "Custom review team", "executionMode": "orchestrated",
        "leader": {"id": "lead", "name": "Lead"},
        "members": [{"id": "writer", "name": "Writer"}, {"id": "reviewer", "name": "Reviewer"}],
        "orchestration": {"workstreams": [
            {"memberId": "lead", "title": "Framing", "required": True},
            {"memberId": "writer", "title": "Writing", "required": True},
            {"memberId": "reviewer", "title": "Review", "required": True},
        ]},
    }
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="stable-team", **{team_key: team})
    original = host._sessions[first.sessionId]
    incomplete = copy.deepcopy(team)
    incomplete["members"].pop()
    call = getattr(host, entry)
    params = {"cwd": cwd, "session_id": "stable-team" if entry == "new_session" else first.sessionId}
    with pytest.raises(RequestError) as error:
        await call(**params, **{team_key: incomplete})
    assert error.value.code == -32602
    assert error.value.data["code"] == "EXPERT_TEAM_INVALID_MEMBER_REFERENCE"
    assert error.value.data["teamId"] == "custom-review-team"
    assert error.value.data["missingMemberIds"] == ["reviewer"]
    assert host._sessions[first.sessionId] is original
    assert len(host._sessions) == 1
    continued = await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "continue"}])
    assert continued.stopReason == "end_turn"

    await call(**params, **{team_key: team})
    current = host._sessions[first.sessionId]
    assert current is not original
    assert [member.id for member in current.expert_context.team.members] == ["writer", "reviewer"]

@pytest.mark.asyncio
async def test_incomplete_team_cannot_create_a_new_session(host, tmp_path):
    team = {
        "id": "incomplete-team", "name": "Incomplete team",
        "members": [{"id": "writer", "name": "Writer"}],
        "orchestration": {"workstreams": [
            {"memberId": "reviewer", "title": "Required review", "required": True},
        ]},
    }
    with pytest.raises(RequestError) as error:
        await host.new_session(cwd=str(tmp_path / "workspace"), expert_team=team)
    assert error.value.code == -32602
    assert error.value.data["code"] == "EXPERT_TEAM_INVALID_MEMBER_REFERENCE"
    assert error.value.data["missingMemberIds"] == ["reviewer"]
    assert host._sessions == {}
    assert not list((tmp_path / "profile" / "sessions").rglob("session.jsonl"))
