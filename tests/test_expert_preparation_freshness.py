"""Uploaded expert dependencies are discovered before session replacement."""
import asyncio
import json
import threading
from pathlib import Path

import pytest
from acp import RequestError
from acp.schema import SessionNotification

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.expert_dependencies import ExpertDependencyError, validate_expert_dependencies
from box_agent.experts import ExpertSessionContext
from box_agent.schema import StreamEvent
from box_agent.tools.skill_loader import SKILL_SLOT_SENTINEL, SkillLoader, SkillValidation
from box_agent.tools.skill_tool import create_skill_tools


class Connection:
    async def session_update(self, session_id, update, **meta):
        SessionNotification(session_id=session_id, update=update, field_meta=meta or None)


class DoneLLM:
    async def generate_stream(self, *args, **kwargs):
        yield StreamEvent(type="text", delta="done")
        yield StreamEvent(type="finish", finish_reason="stop")


def write_skill(root: Path, name: str, *, required=(), broken=False):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    fields = f"name: {name}\n" + ("" if broken else "description: User expert method\n")
    if required:
        fields += "required_skills: " + json.dumps(list(required)) + "\n"
    (directory / "SKILL.md").write_text(f"---\n{fields}---\n{name} method body\n", encoding="utf-8")
    return directory


def expert(name="review", **fields):
    return {"id": "user-editor", "name": "User editor", "instructions": ["Use the selected method."],
            "defaultSkills": [name], "requiredSkills": [name], **fields}


@pytest.fixture
async def host(tmp_path, monkeypatch):
    monkeypatch.setenv("BOX_AGENT_HOME", str(tmp_path / "profile"))
    root = tmp_path / "user-skills"
    root.mkdir()
    tools, loader = create_skill_tools(sources=[(root, "user")])
    config = Config(
        llm=LLMConfig(api_key="test"),
        agent=AgentConfig(workspace_dir="workspace", max_steps=1,
                          enable_memory=False, enable_memory_extraction=False, goal_autopilot_enabled=False),
        tools=ToolsConfig(enable_file_tools=False, enable_bash=False, enable_sub_agent=False, enable_mcp=False),
    )
    agent = BoxACPAgent(Connection(), config, DoneLLM(), tools,
                        f"system\n{SKILL_SLOT_SENTINEL}", skill_loader=loader)
    try:
        yield agent
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
async def test_same_process_retries_uploaded_skill_for_each_session_entry(host, tmp_path, entry):
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="stable-chat")
    original = host._sessions[first.sessionId]
    call = getattr(host, entry)
    session_id = "stable-chat" if entry == "new_session" else first.sessionId

    async def prepare():
        return await call(cwd=cwd, session_id=session_id, expert=expert())

    with pytest.raises(RequestError) as error:
        await prepare()
    assert error.value.data["code"] == "EXPERT_DEPENDENCIES_NOT_READY"
    assert error.value.data["dependencies"][0]["state"] == "missing"
    assert host._sessions[first.sessionId] is original

    directory = write_skill(tmp_path / "user-skills", "review")
    (directory / "references").mkdir()
    (directory / "references/style.txt").write_text("UPLOAD_REFERENCE_OK", encoding="utf-8")
    assert host._skill_loader.get_skill("review") is None
    response = await prepare()
    current = host._sessions[first.sessionId]
    assert current is not original
    assert current.expert_context.expert.id == "user-editor"
    assert response.field_meta["expert_context"]["expert"]["id"] == "user-editor"
    loaded = current.skill_loader.get_skill("review")
    assert loaded.skill_path == directory / "SKILL.md"
    result = await current.agent.tools["get_skill"].execute("review")
    assert result.success and "review method body" in result.content


@pytest.mark.asyncio
async def test_uploading_a_reported_child_dependency_allows_same_process_retry(host, tmp_path):
    write_skill(tmp_path / "user-skills", "review", required=["evidence"])
    params = {"cwd": str(tmp_path / "workspace"), "session_id": "child-upload-chat", "expert": expert()}
    with pytest.raises(RequestError) as error:
        await host.new_session(**params)
    item = error.value.data["dependencies"][0]
    assert item["name"] == "review" and item["dependencyName"] == "evidence"
    assert item["dependencyCode"] == "SKILL_NOT_FOUND" and item["state"] == "missing"
    write_skill(tmp_path / "user-skills", "evidence")
    response = await host.new_session(**params)
    state = host._sessions[response.sessionId]
    assert state.skill_loader.get_skill("evidence") is not None
    result = await state.agent.tools["get_skill"].execute("review")
    assert result.success and "review method body" in result.content


@pytest.mark.asyncio
async def test_initialize_catalog_does_not_hide_later_uploads_from_expert_preparation(host, tmp_path):
    initial = await host.initialize(protocol_version=1)
    assert initial.field_meta["skills"] == []
    write_skill(tmp_path / "user-skills", "review")
    response = await host.new_session(cwd=str(tmp_path / "workspace"), expert=expert())
    assert any(item["name"] == "review" for item in response.field_meta["skills"])


@pytest.mark.asyncio
async def test_initialize_does_not_wait_for_first_skill_discovery(host):
    gate = asyncio.Event()
    task = asyncio.create_task(gate.wait())
    host._skill_task = task
    host._skills_loaded = False
    try:
        response = await asyncio.wait_for(host.initialize(protocol_version=1), timeout=0.25)
        assert "skills" not in response.field_meta
        assert not task.done()
    finally:
        gate.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["new_session", "load_session", "resume_session"])
async def test_refresh_timeout_never_uses_old_catalog_or_retires_live_session(host, tmp_path, monkeypatch, entry):
    write_skill(tmp_path / "user-skills", "review")
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="stable-chat", expert=expert())
    original = host._sessions[first.sessionId]
    loader = host._skill_loader
    calls = []

    async def unfinished(names, *, allow_stale=True):
        calls.append((names, allow_stale))
        return SkillValidation({}, loader._catalog, timed_out=True)

    monkeypatch.setattr(loader, "avalidate_references", unfinished)
    call = getattr(host, entry)
    with pytest.raises(RequestError) as error:
        await call(cwd=cwd, session_id="stable-chat" if entry == "new_session" else first.sessionId,
                   expert=expert())
    assert error.value.data["code"] == "SKILL_VALIDATION_TIMEOUT"
    assert calls == [((), False)]
    assert loader.get_skill("review") is not None  # An old complete snapshot is deliberately present.
    assert host._sessions[first.sessionId] is original
    assert len(host._sessions) == 1


@pytest.mark.asyncio
async def test_slow_scan_times_out_without_blocking_initialize_or_replacing_session(host, tmp_path, monkeypatch):
    write_skill(tmp_path / "user-skills", "review")
    cwd = str(tmp_path / "workspace")
    first = await host.new_session(cwd=cwd, session_id="slow-refresh-chat")
    original = host._sessions[first.sessionId]
    loader = host._skill_loader
    loader.validation_timeout_seconds = 0.03
    signature = loader._source_signature
    entered, release = threading.Event(), threading.Event()

    def blocked_signature(source):
        entered.set()
        if not release.wait(3):
            raise AssertionError("Test did not release the background scan")
        return signature(source)

    monkeypatch.setattr(loader, "_source_signature", blocked_signature)
    preparation = asyncio.create_task(host.new_session(cwd=cwd, session_id="slow-refresh-chat", expert=expert()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        initialized = await asyncio.wait_for(host.initialize(protocol_version=1), timeout=0.25)
        assert initialized.field_meta["expert_package_download"] == 1
        with pytest.raises(RequestError) as error:
            await asyncio.wait_for(asyncio.shield(preparation), timeout=1)
        assert error.value.data["code"] == "SKILL_VALIDATION_TIMEOUT"
        assert host._sessions[first.sessionId] is original
    finally:
        release.set()
        if not preparation.done():
            preparation.cancel()
        await asyncio.gather(preparation, return_exceptions=True)
        if loader._validation_task is not None:
            await asyncio.wait_for(asyncio.shield(loader._validation_task), timeout=2)


@pytest.mark.asyncio
async def test_ordinary_empty_sessions_do_not_force_expert_refresh(host, tmp_path, monkeypatch):
    validate = host._skill_loader.avalidate_references
    calls = []

    async def record(names, **kwargs):
        calls.append((names, kwargs.get("allow_stale")))
        return await validate(names, **kwargs)

    monkeypatch.setattr(host._skill_loader, "avalidate_references", record)
    response = await host.new_session(cwd=str(tmp_path / "workspace"))
    assert host._sessions[response.sessionId].expert_context is None
    # Main skips source validation when there are no restored Skill references.
    # Ordinary sessions must not gain the strict expert-preparation refresh.
    assert calls == []


@pytest.mark.parametrize("child_state", ["missing", "disabled", "broken", "cycle"])
def test_transitive_dependency_identity_and_failure_category_are_structured(tmp_path, child_state):
    write_skill(tmp_path, "review", required=["evidence"])
    settings = tmp_path / "skill-settings.json"
    if child_state != "missing":
        write_skill(tmp_path, "evidence", broken=child_state == "broken",
                    required=["review"] if child_state == "cycle" else [])
    if child_state == "disabled":
        settings.write_text(json.dumps({"disabledSkillNames": ["evidence"]}))
    loader = SkillLoader(sources=[(tmp_path, "user")], skill_settings_path=settings)
    loader.discover_skills()
    meta = {"expert": expert()}
    with pytest.raises(ExpertDependencyError) as error:
        validate_expert_dependencies(meta, ExpertSessionContext.from_meta(meta), loader, [])
    item = error.value.dependencies[0]
    assert item["name"] == "review"
    code = {"missing": "SKILL_NOT_FOUND", "disabled": "SKILL_DISABLED",
            "broken": "SKILL_BROKEN", "cycle": "SKILL_DEPENDENCY_CYCLE"}[child_state]
    assert item["dependencyCode"] == code
    assert item["state"] == (child_state if child_state != "cycle" else "unavailable")
    if child_state != "cycle":
        assert item["dependencyName"] == "evidence"


def test_missing_child_does_not_inherit_parent_market_installation_identity(tmp_path):
    parent = write_skill(tmp_path, "review", required=["evidence"])
    (parent / ".skill-installation.json").write_text(json.dumps({"source": "hub", "skillId": "market-parent"}))
    loader = SkillLoader(sources=[(tmp_path, "user")])
    loader.discover_skills()
    meta = {"expert": expert(skillBindings=[{"slug": "review", "source": "MARKET", "skillId": "market-parent"}])}
    with pytest.raises(ExpertDependencyError) as error:
        validate_expert_dependencies(meta, ExpertSessionContext.from_meta(meta), loader, [])
    item = error.value.dependencies[0]
    assert item["source"] == "MARKET" and item["skillId"] == "market-parent"
    assert item["dependencyName"] == "evidence" and item["dependencyCode"] == "SKILL_NOT_FOUND"


def test_ungranted_child_remains_unavailable_instead_of_uploadable_missing(tmp_path):
    write_skill(tmp_path, "review", required=["evidence"])
    write_skill(tmp_path, "evidence")
    loader = SkillLoader(sources=[(tmp_path, "user")])
    loader.discover_skills()
    meta = {"expert": expert()}
    with pytest.raises(ExpertDependencyError) as error:
        validate_expert_dependencies(meta, ExpertSessionContext.from_meta(meta), loader, [],
                                     skill_access_filter=lambda skill: skill.name != "evidence")
    item = error.value.dependencies[0]
    assert item["state"] == "unavailable" and item["dependencyName"] == "evidence"
