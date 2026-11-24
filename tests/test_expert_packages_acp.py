"""Downloaded/full expert profiles share the existing ACP lifecycle."""
import asyncio
import copy
import hashlib
import io
import json
import socket
import zipfile
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
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("required,include_lock,installed", [
    (False, False, False), (False, True, False), (False, True, True),
    (True, True, False), (True, True, True),
])
async def test_market_binding_requirement_survives_download_and_restore(
    host, tmp_path, monkeypatch, kind, required, include_lock, installed,
):
    with zipfile.ZipFile(io.BytesIO(archive_bytes(kind))) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files[".box-agent-plugin/plugin.json"])
    manifest["skills"].append({"key": "market", "source": "HUB", "skillId": "market-id"})
    for agent in manifest["agents"]:
        agent["skillBindings"].append({"key": "market", "required": required})
    files[".box-agent-plugin/plugin.json"] = json.dumps(manifest).encode()
    if include_lock:
        files["dependencies.lock.json"] = json.dumps({"skills": [
            {"key": "market", "skillId": "market-id", "slug": "market-method"},
        ]}).encode()
    files["files.json"] = json.dumps([
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items() if name != "files.json"
    ]).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    body = stream.getvalue()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))

    async def prepare(meta):
        return await packages.prepare_expert_meta(meta, transport=transport)

    monkeypatch.setattr(acp_module, "prepare_expert_meta", prepare)
    if installed:
        root = tmp_path / "installed"
        directory = root / "market-method"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text("---\nname: market-method\ndescription: Market method\n---\nMARKET_OK")
        (directory / ".skill-installation.json").write_text(json.dumps({"source": "hub", "skillId": "market-id"}))
        host._base_tools[:], host._skill_loader = create_skill_tools(sources=[(root, "user")])
    key = "expert" if kind == "expert" else "expert_team"
    params = {"cwd": str(tmp_path / "workspace"), key: reference(body)}
    if required and not installed:
        with pytest.raises(RequestError) as error:
            await host.new_session(**params)
        assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
        assert error.value.data["dependencies"][0]["name"] == "market-method"
        assert not host._sessions
        return
    response = await host.new_session(**params)
    for restore in (None, host.load_session, host.resume_session):
        if restore is not None:
            await restore(session_id=response.sessionId, **params)
        state = host._sessions[response.sessionId]
        profiles = ([state.expert_context.expert] if kind == "expert" else
                    [state.expert_context.team.leader, *state.expert_context.team.members])
        for profile in profiles:
            assert ("market-method" in profile.default_skills) is required
            assert ("market-method" in profile.optional_skills) is (include_lock and not required)
        if installed:
            assert (await state.agent.tools["get_skill"].execute("market-method")).success


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
@pytest.mark.parametrize("failure", ["missing-member", "missing-title", "missing-member-id"])
async def test_invalid_required_team_workstream_is_rejected_without_replacing_live_session(
    host, tmp_path, entry, team_key, failure,
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
    before = original.agent.session_log.path.read_bytes()
    incomplete = copy.deepcopy(team)
    if failure == "missing-member":
        incomplete["members"].pop()
    else:
        field = "title" if failure == "missing-title" else "memberId"
        incomplete["orchestration"]["workstreams"][-1].pop(field)
    call = getattr(host, entry)
    params = {"cwd": cwd, "session_id": "stable-team" if entry == "new_session" else first.sessionId}
    with pytest.raises(RequestError) as error:
        await call(**params, **{team_key: incomplete})
    assert error.value.code == -32602
    expected_code = ("EXPERT_TEAM_INVALID_MEMBER_REFERENCE" if failure == "missing-member"
                     else "EXPERT_TEAM_INVALID_WORKSTREAM")
    assert error.value.data["code"] == expected_code
    assert error.value.data["teamId"] == "custom-review-team"
    assert error.value.data["missingMemberIds"] == (["reviewer"] if failure == "missing-member" else [])
    assert host._sessions[first.sessionId] is original
    assert original.agent.session_log.path.read_bytes() == before
    assert len(host._sessions) == 1
    continued = await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "continue"}])
    assert continued.stopReason == "end_turn"

    await call(**params, **{team_key: team})
    current = host._sessions[first.sessionId]
    assert current is not original
    assert [member.id for member in current.expert_context.team.members] == ["writer", "reviewer"]


@pytest.mark.asyncio
async def test_invalid_required_workstream_keeps_acp_connection_usable(host, tmp_path):
    from acp.agent.connection import AgentSideConnection
    from acp.client.connection import ClientSideConnection
    from box_agent.acp.stdio_compat import StdioTransport

    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
    client = ClientSideConnection(Connection(), StdioTransport(client_reader, client_writer))
    try:
        await asyncio.wait_for(client.initialize(protocol_version=1), 10)
        cwd = str(tmp_path / "workspace")
        first = await client.new_session(cwd=cwd, session_id="wire-team")
        original = host._sessions[first.sessionId]
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(client.new_session(cwd=cwd, session_id="wire-team", expert_team={
                "id": "team", "name": "Team", "leader": {"id": "lead", "name": "Lead"},
                "orchestration": {"workstreams": [{"memberId": "lead", "required": True}]},
            }), 10)
        assert error.value.code == -32602
        assert error.value.data["code"] == "EXPERT_TEAM_INVALID_WORKSTREAM"
        assert host._sessions[first.sessionId] is original
        second = await asyncio.wait_for(client.new_session(cwd=cwd, session_id="other-team"), 10)
        for session_id in (first.sessionId, second.sessionId):
            response = await asyncio.wait_for(client.prompt(
                session_id=session_id, prompt=[{"type": "text", "text": "continue"}],
            ), 10)
            assert response.stopReason == "end_turn"
    finally:
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())


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


def _workspace_tool_package(monkeypatch, kind, tool_name):
    body = archive_bytes(kind, tool_requests=[tool_name])
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))

    async def prepare_meta(meta):
        return await packages.prepare_expert_meta(meta, transport=transport)

    monkeypatch.setattr(acp_module, "prepare_expert_meta", prepare_meta)
    key = "expert" if kind == "expert" else "expert_team"
    return {key: reference(body, f"client-{kind}-with-{tool_name}")}


def _set_workspace_tool(host, tool_name, enabled):
    setting = "enable_bash" if tool_name == "bash" else "enable_file_tools"
    setattr(host._config.tools, setting, enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("tool_name", ["bash", "read_file"])
async def test_downloaded_expert_can_require_enabled_workspace_tool(
    host, tmp_path, monkeypatch, entry, kind, tool_name,
):
    _set_workspace_tool(host, tool_name, True)
    cwd = str(tmp_path / "workspace")
    # The normal session proves native registration; do not add the tool to _base_tools.
    first = await host.new_session(cwd=cwd, session_id="workspace-tool-chat")
    assert tool_name in host._sessions[first.sessionId].agent.tools
    assert tool_name not in {tool.name for tool in host._base_tools}
    selected = _workspace_tool_package(monkeypatch, kind, tool_name)
    if entry == "new_session":
        response = await host.new_session(cwd=cwd, session_id="expert-workspace-tool-chat", **selected)
        target_session_id = response.sessionId
    else:
        # Exercise cold restore as well as the live rebind path covered below.
        original = host._sessions.pop(first.sessionId)
        session_log = original.agent.session_log
        await original.aclose()
        session_log.close()
        await getattr(host, entry)(cwd=cwd, session_id=first.sessionId, **selected)
        target_session_id = first.sessionId

    state = host._sessions[target_session_id]
    assert state.expert_context is not None
    tool = state.agent.tools[tool_name]
    if tool_name == "read_file":
        path = Path(cwd) / "expert-tool-check.txt"
        path.write_text("EXPERT_WORKSPACE_TOOL_OK", encoding="utf-8")
        result = await tool.execute(str(path))
    else:
        result = await tool.execute("echo EXPERT_WORKSPACE_TOOL_OK", timeout=10)
    assert result.success, result.error
    assert "EXPERT_WORKSPACE_TOOL_OK" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("tool_name", ["bash", "read_file"])
async def test_disabled_workspace_dependency_preserves_live_session_and_can_retry(
    host, tmp_path, monkeypatch, entry, kind, tool_name,
):
    _set_workspace_tool(host, tool_name, False)
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="disabled-tool-chat")
    original = host._sessions[first.sessionId]
    assert tool_name not in original.agent.tools
    selected = _workspace_tool_package(monkeypatch, kind, tool_name)
    session_id = "disabled-tool-chat" if entry == "new_session" else first.sessionId

    with pytest.raises(RequestError) as error:
        await getattr(host, entry)(cwd=cwd, session_id=session_id, **selected)

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    dependency = next(item for item in error.value.data["dependencies"] if item["name"] == tool_name)
    assert dependency["source"] == "TOOL"
    assert dependency["state"] == "unavailable"
    assert host._sessions[first.sessionId] is original
    continued = await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "continue"}])
    assert continued.stopReason == "end_turn"

    _set_workspace_tool(host, tool_name, True)
    await getattr(host, entry)(cwd=cwd, session_id=session_id, **selected)
    current = host._sessions[first.sessionId]
    assert current is not original
    assert tool_name in current.agent.tools


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("tool_name", [
    "bash", "read_file", "get_skill", "goal_read", "goal_write", "call_tool",
])
async def test_utility_expert_cannot_require_registered_tool(
    host, tmp_path, monkeypatch, kind, tool_name,
):
    host._config.tools.enable_bash = True
    host._config.tools.enable_file_tools = True
    cwd = str(tmp_path / "workspace")
    ordinary = await host.new_session(cwd=cwd, utility=True)
    assert not host._sessions[ordinary.sessionId].agent.tools
    selected = _workspace_tool_package(monkeypatch, kind, tool_name)

    with pytest.raises(RequestError) as error:
        await host.new_session(cwd=cwd, utility=True, **selected)

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert any(item["name"] == tool_name and item["source"] == "TOOL"
               for item in error.value.data["dependencies"])
    assert ordinary.sessionId in host._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", ["bash", "read_file", "research-synthesis", "pkulaw"])
async def test_dependency_preflight_error_keeps_acp_connection_and_sessions_usable(
    host, tmp_path, monkeypatch, dependency,
):
    from acp.agent.connection import AgentSideConnection
    from acp.client.connection import ClientSideConnection
    from box_agent.acp.stdio_compat import StdioTransport

    if dependency == "research-synthesis":
        _add_research_skills(host, tmp_path)
        selected = {"execution_profile": "fast", **_connector_expert_profile("expert", dependency)}
    elif dependency == "pkulaw":
        _add_connector_skills(host, tmp_path)
        monkeypatch.setattr(acp_module, "_connected_connector_ids", lambda ids: frozenset(ids))
        selected = _connector_expert_profile("expert", dependency)
    else:
        selected = _workspace_tool_package(monkeypatch, "expert", dependency)

    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
    client = ClientSideConnection(Connection(), StdioTransport(client_reader, client_writer))
    try:
        await asyncio.wait_for(client.initialize(protocol_version=1), 10)
        cwd = str(tmp_path / "workspace")
        first = await asyncio.wait_for(client.new_session(cwd=cwd, session_id="wire-chat"), 10)
        original = host._sessions[first.sessionId]
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(client.new_session(cwd=cwd, session_id="wire-chat", **selected), 10)
        assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
        assert host._sessions[first.sessionId] is original

        second = await asyncio.wait_for(client.new_session(cwd=cwd), 10)
        for session_id in (first.sessionId, second.sessionId):
            response = await asyncio.wait_for(client.prompt(
                session_id=session_id, prompt=[{"type": "text", "text": "continue"}],
            ), 10)
            assert response.stopReason == "end_turn"

        if dependency == "research-synthesis":
            selected["execution_profile"] = "standard"
        elif dependency == "pkulaw":
            selected["selected_connector_ids"] = ["pkulaw"]
        else:
            _set_workspace_tool(host, dependency, True)
        retried = await asyncio.wait_for(client.new_session(cwd=cwd, session_id="wire-chat", **selected), 10)
        assert retried.sessionId == first.sessionId
        current = host._sessions[retried.sessionId]
        if dependency in {"bash", "read_file"}:
            assert dependency in current.agent.tools
        else:
            response = await asyncio.wait_for(client.prompt(
                session_id=retried.sessionId, prompt=[{"type": "text", "text": "请核对材料。"}],
            ), 10)
            assert response.stopReason == "end_turn"
            assert (await _read_session_skill(current, dependency)).success
    finally:
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("version,tool_name", [
    (1, "goal_read"), (1, "goal_write"),
    (2, "goal_read"), (2, "goal_write"), (2, "call_tool"),
])
async def test_downloaded_expert_can_require_agent_native_tool(
    host, tmp_path, monkeypatch, kind, version, tool_name,
):
    host._config.agent.tool_interface_version = version
    cwd = str(tmp_path / "workspace")
    ordinary = await host.new_session(cwd=cwd)
    assert tool_name in host._sessions[ordinary.sessionId].agent.tools
    assert tool_name not in {tool.name for tool in host._base_tools}
    selected = _workspace_tool_package(monkeypatch, kind, tool_name)

    response = await host.new_session(cwd=cwd, **selected)

    state = host._sessions[response.sessionId]
    assert state.expert_context is not None
    assert state.agent.tool_interface_version == version
    tool = state.agent.tools[tool_name]
    if tool_name == "goal_read":
        result = await tool.execute()
        assert result.success and "No current goal" in result.content
    elif tool_name == "goal_write":
        result = await tool.execute(action="set", objective="Verify the native expert tool")
        assert result.success, result.error
        assert state.agent.goal.objective == "Verify the native expert tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
async def test_v1_expert_cannot_require_call_tool_and_keeps_original_session(
    host, tmp_path, monkeypatch, kind,
):
    host._config.agent.tool_interface_version = 1
    cwd = str(tmp_path / "workspace")
    ordinary = await host.new_session(cwd=cwd, session_id="native-v1-chat")
    original = host._sessions[ordinary.sessionId]
    assert "call_tool" not in original.agent.tools
    selected = _workspace_tool_package(monkeypatch, kind, "call_tool")

    with pytest.raises(RequestError) as error:
        await host.new_session(cwd=cwd, session_id="native-v1-chat", **selected)

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert any(item["name"] == "call_tool" and item["state"] == "unavailable"
               for item in error.value.data["dependencies"])
    assert host._sessions[ordinary.sessionId] is original
    continued = await host.prompt(
        session_id=ordinary.sessionId, prompt=[{"type": "text", "text": "continue"}],
    )
    assert continued.stopReason == "end_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("cold", [False, True], ids=["live-rebind", "cold-restore"])
@pytest.mark.parametrize("saved_version", [1, 2], ids=["saved-v1-config-v2", "saved-v2-config-v1"])
async def test_expert_call_tool_preflight_uses_saved_interface_version(
    host, tmp_path, monkeypatch, entry, cold, saved_version,
):
    host._config.agent.tool_interface_version = saved_version
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="native-interface-chat")
    original = host._sessions[first.sessionId]
    assert original.agent.tool_interface_version == saved_version
    host._config.agent.tool_interface_version = 3 - saved_version
    selected = _workspace_tool_package(monkeypatch, "team", "call_tool")
    log_path = original.agent.session_log.path
    log_before = log_path.read_bytes()
    if cold:
        host._sessions.pop(first.sessionId)
        log = original.agent.session_log
        await original.aclose()
        log.close()
    session_id = "native-interface-chat" if entry == "new_session" else first.sessionId
    call = getattr(host, entry)

    if saved_version == 1:
        with pytest.raises(RequestError) as error:
            await call(cwd=cwd, session_id=session_id, **selected)
        assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
        log_after = log_path.read_bytes()
        if cold:
            # Cold restore can append its existing checkpoint before preflight;
            # resolving the saved interface must not publish a new contract.
            contracts_before = [event for event in map(json.loads, log_before.splitlines())
                                if event.get("type") == "tools/contract"]
            contracts_after = [event for event in map(json.loads, log_after.splitlines())
                               if event.get("type") == "tools/contract"]
            assert contracts_after == contracts_before
        else:
            assert log_after == log_before
        assert any(item["name"] == "call_tool" and item["state"] == "unavailable"
                   for item in error.value.data["dependencies"])
        if cold:
            assert first.sessionId not in host._sessions
            await call(cwd=cwd, session_id=session_id)
        else:
            assert host._sessions[first.sessionId] is original
        state = host._sessions[first.sessionId]
        assert state.agent.tool_interface_version == 1
        assert "call_tool" not in state.agent.tools
    else:
        await call(cwd=cwd, session_id=session_id, **selected)
        state = host._sessions[first.sessionId]
        assert state is not original
        assert state.agent.tool_interface_version == 2
        assert "call_tool" in state.agent.tools
        assert state.expert_context.team is not None

    continued = await host.prompt(
        session_id=first.sessionId, prompt=[{"type": "text", "text": "continue"}],
    )
    assert continued.stopReason == "end_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_expert_preflight_matches_final_agent_tool_registry(
    host, tmp_path, monkeypatch, version,
):
    host._config.agent.tool_interface_version = version
    validate = acp_module.validate_expert_dependencies
    observed = []

    def observe(raw_meta, context, loader, tools, **kwargs):
        observed.append(frozenset(tools))
        return validate(raw_meta, context, loader, tools, **kwargs)

    monkeypatch.setattr(acp_module, "validate_expert_dependencies", observe)
    response = await host.new_session(**new_params(tmp_path))
    state = host._sessions[response.sessionId]

    assert observed == [frozenset(state.agent.tools)]
    assert {"tool_search", "goal_read", "goal_write"} <= observed[0]
    assert ("call_tool" in observed[0]) is (version == 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("owner", ["workspace", "agent"])
async def test_added_tool_definition_reaches_expert_preflight_and_registration(
    host, tmp_path, monkeypatch, entry, owner,
):
    import box_agent.agent as agent_module
    from box_agent.tools import setup as tool_setup
    from box_agent.tools.base import Tool, ToolResult

    tool_name = f"registered_{owner}_probe"
    constructed = []

    class ProbeTool(Tool):
        name = tool_name
        description = "Verify a tool added through its owning registration definition."
        parameters = {"type": "object", "properties": {}}

        def __init__(self):
            constructed.append(self)

        async def execute(self):
            return ToolResult(success=True, content="EXPERT_REGISTERED_TOOL_OK")

    # Add only the owning declaration; neither ACP nor availability code is patched.
    if owner == "workspace":
        registrations = tool_setup._workspace_tool_registrations

        def extended(*args, **kwargs):
            yield tool_name, ProbeTool
            yield from registrations(*args, **kwargs)

        monkeypatch.setattr(tool_setup, "_workspace_tool_registrations", extended)
    else:
        registrations = agent_module._agent_tool_registrations

        def extended(**kwargs):
            discovery, state = registrations(**kwargs)
            if kwargs.get("enable_builtin_tools", True):
                discovery[tool_name] = lambda *_: ProbeTool()
            return discovery, state

        monkeypatch.setattr(agent_module, "_agent_tool_registrations", extended)

    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="registered-tool-chat")
    original = host._sessions[first.sessionId]
    assert tool_name in original.agent.tools
    assert len(constructed) == 1
    session_id = "registered-tool-chat" if entry == "new_session" else first.sessionId
    call = getattr(host, entry)
    before = original.agent.session_log.path.read_bytes()
    missing = _workspace_tool_package(monkeypatch, "expert", "missing_registered_tool")

    with pytest.raises(RequestError) as error:
        await call(cwd=cwd, session_id=session_id, **missing)

    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert host._sessions[first.sessionId] is original
    assert original.agent.session_log.path.read_bytes() == before
    assert len(constructed) == 1  # Rejected preflight did not invoke lazy factories.
    continued = await host.prompt(
        session_id=first.sessionId, prompt=[{"type": "text", "text": "continue"}],
    )
    assert continued.stopReason == "end_turn"

    selected = _workspace_tool_package(monkeypatch, "expert", tool_name)
    await call(cwd=cwd, session_id=session_id, **selected)
    current = host._sessions[first.sessionId]
    assert current is not original
    assert len(constructed) == 2
    result = await current.agent.tools[tool_name].execute()
    assert result.success and result.content == "EXPERT_REGISTERED_TOOL_OK"


async def _read_session_skill(state, name):
    from box_agent.tools.base import ToolInvocationContext

    return await state.agent.tools["get_skill"].invoke(
        {"skill_name": name}, context=ToolInvocationContext(skill_reader=state.agent.skill_runtime.read),
    )


def _selected_mcp_meta(discovery):
    if not discovery:
        return {"selected_connector_ids": ["pkulaw"]}
    meta = _ready_cli_connector_meta()
    meta["connector_discovery"]["entries"][0]["kind"] = "mcp"
    return meta


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("discovery", [False, True])
async def test_connected_mcp_expert_prepares_without_granting_reads_before_the_task(
    host, tmp_path, monkeypatch, kind, discovery,
):
    _add_connector_skills(host, tmp_path)
    monkeypatch.setattr(acp_module, "_connected_connector_ids", lambda ids: frozenset(ids))
    response = await host.new_session(cwd=str(tmp_path / "workspace"),
                                     **_selected_mcp_meta(discovery), **_connector_expert_profile(kind))
    state = host._sessions[response.sessionId]
    assert not state.connector_skill_grants
    assert not (await state.agent.tools["get_skill"].execute("pkulaw")).success
    await host.prompt(session_id=response.sessionId, prompt=[{"type": "text", "text": "请核对这份材料。"}],
                      **_selected_mcp_meta(discovery))
    result = await state.agent.tools["get_skill"].execute("pkulaw")
    assert result.success, result.error
    assert "Read the current legal sources" in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("cold", [False, True])
@pytest.mark.parametrize("discovery", [False, True])
async def test_connected_mcp_expert_restore_preserves_previously_read_skill(
    host, tmp_path, monkeypatch, entry, cold, discovery,
):
    _add_connector_skills(host, tmp_path)
    monkeypatch.setattr(acp_module, "_connected_connector_ids", lambda ids: frozenset(ids))
    cwd = str(tmp_path / "workspace")
    meta = _selected_mcp_meta(discovery)
    first = await host.new_session(cwd=cwd, session_id="connected-mcp", **meta)
    original = host._sessions[first.sessionId]
    await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "使用 pkulaw 核对法律材料。"}],
                      **meta)
    assert (await _read_session_skill(original, "pkulaw")).success
    if cold:
        await original.aclose()
        original.agent.session_log.close()
        host._sessions.pop(first.sessionId)
    session_id = "connected-mcp" if entry == "new_session" else first.sessionId
    await getattr(host, entry)(cwd=cwd, session_id=session_id, **meta, **_connector_expert_profile("team"))
    restored = host._sessions[first.sessionId]
    assert restored is not original
    assert "pkulaw" in restored.connector_skill_grants
    assert (await restored.agent.tools["get_skill"].execute("pkulaw")).success


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("failure", ["unselected", "disconnected", "discovery-unavailable"])
async def test_unavailable_mcp_expert_keeps_live_session_and_can_retry(
    host, tmp_path, monkeypatch, entry, failure,
):
    _add_connector_skills(host, tmp_path)
    connected = {"pkulaw"}
    monkeypatch.setattr(acp_module, "_connected_connector_ids", lambda ids: frozenset(ids & connected))
    cwd = str(tmp_path / "workspace")
    ready = _selected_mcp_meta(True)
    first = await host.new_session(cwd=cwd, session_id="mcp-retry", **ready)
    original = host._sessions[first.sessionId]
    await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "使用 pkulaw 核对法律材料。"}],
                      **ready)
    assert (await _read_session_skill(original, "pkulaw")).success
    before = original.agent.session_log.path.read_bytes()
    requested = copy.deepcopy(ready)
    if failure == "unselected":
        requested["selected_connector_ids"] = []
    elif failure == "disconnected":
        connected.clear()
    else:
        requested["connector_discovery"]["available"] = False
    session_id = "mcp-retry" if entry == "new_session" else first.sessionId
    with pytest.raises(RequestError) as error:
        await getattr(host, entry)(cwd=cwd, session_id=session_id, **requested, **_connector_expert_profile("expert"))
    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert error.value.data["dependencies"][0]["state"] == "unavailable"
    assert host._sessions[first.sessionId] is original
    assert original.agent.session_log.path.read_bytes() == before
    continued = await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "继续回答。"}],
                                  **ready)
    assert continued.stopReason == "end_turn"
    connected.add("pkulaw")
    await getattr(host, entry)(cwd=cwd, session_id=session_id, **ready, **_connector_expert_profile("expert"))
    assert (await host._sessions[first.sessionId].agent.tools["get_skill"].execute("pkulaw")).success


def _add_research_skills(host, tmp_path):
    root = tmp_path / "user-skills"
    for name, extra in (("research-synthesis", ""), ("review-method", "required_skills: [research-synthesis]\n")):
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Review the material\n{extra}---\nUse the research evidence.\n",
            encoding="utf-8",
        )
    tools, loader = create_skill_tools(sources=[(root, "user")])
    host._base_tools[:] = tools
    host._skill_loader = loader


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
@pytest.mark.parametrize("kind", ["expert", "team"])
@pytest.mark.parametrize("restriction", ["fast", "reader-blocked", "reader-scope"])
async def test_expert_reader_restrictions_reject_before_retiring_live_session(
    host, tmp_path, entry, kind, restriction,
):
    from box_agent.tools.skill_tool import GetSkillTool

    _add_research_skills(host, tmp_path)
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="reader-retry")
    original = host._sessions[first.sessionId]
    before = original.agent.session_log.path.read_bytes()
    reader = next(tool for tool in host._base_tools if isinstance(tool, GetSkillTool))
    requested = {"execution_profile": "fast" if restriction == "fast" else "standard"}
    if restriction == "reader-blocked":
        reader.blocked_skill_names = {"research-synthesis"}
    elif restriction == "reader-scope":
        reader.allowed_skill_names = frozenset({"review-method"})
    session_id = "reader-retry" if entry == "new_session" else first.sessionId
    # The restricted Skill is a transitive dependency, not just the selected root.
    selected = _connector_expert_profile(kind, "review-method")
    with pytest.raises(RequestError) as error:
        await getattr(host, entry)(cwd=cwd, session_id=session_id, **requested, **selected)
    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    dependency = error.value.data["dependencies"][0]
    assert dependency["name"] == "review-method"
    assert dependency["dependencyName"] == "research-synthesis"
    assert dependency["state"] == "unavailable"
    assert host._sessions[first.sessionId] is original
    assert original.agent.session_log.path.read_bytes() == before
    assert (await original.agent.tools["get_skill"].execute("research-synthesis")).success
    assert (await host.prompt(session_id=first.sessionId, prompt=[{"type": "text", "text": "继续。"}])).stopReason == "end_turn"
    reader.blocked_skill_names = frozenset()
    reader.allowed_skill_names = None
    await getattr(host, entry)(cwd=cwd, session_id=session_id, execution_profile="standard", **selected)
    current = host._sessions[first.sessionId]
    assert current is not original
    for name in ("review-method", "research-synthesis"):
        assert (await current.agent.tools["get_skill"].execute(name)).success


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["fast", "standard", "deep"])
async def test_expert_prepare_uses_the_same_execution_policy_as_skill_reads(host, tmp_path, profile):
    _add_research_skills(host, tmp_path)
    params = dict(cwd=str(tmp_path / "workspace"), execution_profile=profile,
                  **_connector_expert_profile("expert", "research-synthesis"))
    if profile == "fast":
        with pytest.raises(RequestError) as error:
            await host.new_session(**params)
        assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
        assert not host._sessions
    else:
        response = await host.new_session(**params)
        result = await host._sessions[response.sessionId].agent.tools["get_skill"].execute("research-synthesis")
        assert result.success


@pytest.mark.asyncio
async def test_slow_expert_archive_write_does_not_block_other_acp_session(host, tmp_path, monkeypatch):
    import threading
    from acp.agent.connection import AgentSideConnection
    from acp.client.connection import ClientSideConnection
    from box_agent.acp.stdio_compat import StdioTransport

    entered, release = threading.Event(), threading.Event()
    original_open = Path.open

    class SlowWriter:
        def __init__(self, stream):
            self.stream = stream
        def write(self, chunk):
            entered.set()
            assert release.wait(10), "ZIP writing blocked the other ACP session"
            return self.stream.write(chunk)
        def close(self):
            return self.stream.close()

    def open_archive(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return SlowWriter(stream) if path.name == "package.zip" and mode == "xb" else stream

    monkeypatch.setattr(Path, "open", open_archive)
    local, remote = socket.socketpair()
    client_reader, client_writer = await asyncio.open_connection(sock=local)
    agent_reader, agent_writer = await asyncio.open_connection(sock=remote)
    server = AgentSideConnection(host, StdioTransport(agent_reader, agent_writer))
    client = ClientSideConnection(Connection(), StdioTransport(client_reader, client_writer))
    preparation = None
    try:
        await asyncio.wait_for(client.initialize(protocol_version=1), 10)
        first = await client.new_session(cwd=str(tmp_path / "workspace"), session_id="still-responsive")
        preparation = asyncio.create_task(client.new_session(**new_params(tmp_path, session_id="slow-archive")))
        assert await asyncio.to_thread(entered.wait, 5)
        response = await asyncio.wait_for(client.prompt(
            session_id=first.sessionId, prompt=[{"type": "text", "text": "回答一句即可。"}],
        ), 5)
        assert response.stopReason == "end_turn"
        assert not preparation.done()
        release.set()
        prepared = await asyncio.wait_for(preparation, 10)
        assert host._sessions[prepared.sessionId].expert_context.expert.id == "client-expert-id"
    finally:
        release.set()
        if preparation is not None:
            await asyncio.gather(preparation, return_exceptions=True)
        await client.close()
        await server.close()
        client_writer.close()
        agent_writer.close()
        await asyncio.gather(client_writer.wait_closed(), agent_writer.wait_closed())
