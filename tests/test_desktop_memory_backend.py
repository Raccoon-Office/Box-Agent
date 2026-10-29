"""Desktop source routing, model qualification and final-turn handoff contracts."""

import asyncio
import hashlib
import json
from pathlib import Path
import secrets
from types import SimpleNamespace
import uuid

import httpx
import pytest

from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, DesktopMemoryConfig
from box_agent.context_input import DefaultContextEngine
from box_agent.desktop_memory.backend import DesktopMemoryBackend, model_json
from box_agent.desktop_memory.transport import MemoryProxyError
from box_agent.memory import _MemoryTurn, create_memory_backend, memory_user_turn, uses_local_memory
from box_agent.schema import Message, StreamEvent, FunctionCall, ToolCall
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.memory_tool import create_memory_tools
from tests.test_agent_session import session_config
from tests.test_session_plugins import RecordingLLM


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "bootstrap.json"
    bootstrap = {"protocol": 2, "base_url": "http://127.0.0.1:49125/memory/v2", "token": secrets.token_urlsafe(32),
                 "control_token": secrets.token_urlsafe(32), "identity_epoch": str(uuid.uuid4()), "profile_id": str(uuid.uuid4())}
    path.write_text(json.dumps(bootstrap))
    path.chmod(0o600)
    state = SimpleNamespace(bootstrap=bootstrap, path=path, calls=[], reads={}, active=set(), source="cloud", generation=1,
                            online=True, automatic=True, before=None, qa_release=None,
                            sections={key: {"content": key + "-unique-data", "revision": key + "-v1", "status": "ok"}
                                      for key in ("user_items", "profile", "automatic", "dates", "experience")},
                            files={"user.md": '<mem time="2026-10-28 08:00:00" priority="1">中文</mem>',
                                   "memory.md": '<mem time="2026-10-28 08:00:00" priority="0">保护项</mem>\n<mem time="2026-10-28 08:00:00" priority="1">可编辑</mem>'})

    async def respond(request):
        operation = request.url.path.removeprefix("/memory/v2/")
        payload = json.loads(request.content)
        state.calls.append((operation, payload))
        expected_token = bootstrap["control_token"] if operation.startswith("visibility/") or operation == "qa/evidence" else bootstrap["token"]
        assert request.headers["authorization"] == "Bearer " + expected_token
        if state.before is not None:
            override = await state.before(operation, payload)
            if override is not None:
                return override
        if operation == "context":
            binding = {"accountKey": "account-1", "officeIdentity": "personal", "identityEpoch": bootstrap["identity_epoch"],
                       "subject": "subject-1", "sessionId": payload["session_id"], "turnId": payload["turn_id"],
                       "source": state.source, "memoryGeneration": state.generation if state.source == "cloud" else 0,
                       "automaticAtStart": state.automatic, "onlineAtStart": state.online, "automaticRevision": 0}
            data = {"binding": binding, "sections": state.sections,
                    "qa_event_id": str(uuid.uuid5(uuid.NAMESPACE_URL, payload["session_id"] + "/" + payload["turn_id"]))}
        elif operation == "core/read":
            content = state.files.get(payload["path"])
            data = core_record(payload["path"], content)
            data["read_id"] = str(uuid.uuid4())
            state.reads[data["read_id"]] = (payload["binding"], data)
        elif operation.startswith("visibility/"):
            read_binding, read = state.reads[payload["read_id"]]
            assert read_binding == payload["binding"]
            assert payload["content_sha256"] == hashlib.sha256(read["content"].encode()).hexdigest()
            if operation == "visibility/commit":
                state.active.add(payload["read_id"])
            else:
                state.active.discard(payload["read_id"])
            data = {"state": "active" if operation.endswith("commit") else "revoked", "read_id": payload["read_id"]}
        elif operation == "core/mutate":
            assert payload["read_id"] in state.active
            state.active.remove(payload["read_id"])
            assert core_record(payload["path"], state.files.get(payload["path"]))["revision"] == payload["base_revision"]
            state.files[payload["path"]] = payload["content"]
            data = core_record(payload["path"], payload["content"])
            data["content_sha256"] = hashlib.sha256(data.pop("content").encode()).hexdigest()
        elif operation in {"qa", "qa/evidence"}:
            if state.qa_release is not None:
                await state.qa_release.wait()
            data = {"event_id": payload["event_id"], "state": "accepted", "source_id": "source-1"}
        elif operation == "turn/close":
            state.active.difference_update(key for key, (binding, _) in state.reads.items() if binding == payload["binding"])
            data = {"closed": True}
        elif operation == "search":
            data = {"results": [{"excerpt": "hit", "entry_ref": "ref-1", "version": "file-v1"}]}
        elif operation == "managed/read":
            data = {"content": "page", "version": payload["version"], "next_offset": 4}
        elif operation == "experience/search":
            data = {"observations": [{"observation_id": "obs-1", "content": "verified-experience"}]}
        else:
            pytest.fail("Unexpected operation: " + operation)
        return httpx.Response(200, json={"protocol": 2, "identity_epoch": bootstrap["identity_epoch"], "ok": True, "data": data})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    return state


def core_record(path, content):
    return {"path": "memory://" + path, "content": content or "", "exists": content is not None,
            "revision": "sha256:" + hashlib.sha256(content.encode()).hexdigest() if content is not None else "absent"}


def backend(host):
    return DesktopMemoryBackend(DesktopMemoryConfig(bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"]))


async def start_turn(manager, turn="turn-1", *, limit=20000):
    await manager.load_context(query="question", session_id="session-1", turn_id=turn)
    engine = DefaultContextEngine()
    engine.configure_run()
    engine.bind_memory(manager)
    history = [Message(role="user", content="question")]
    engine.prepare_request(history, prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=limit)
    return engine, history


async def qualify(manager, engine, history, path="memory.md", *, limit=20000):
    model = await manager.read_core_memory(path)
    history.append(Message(role="tool", name="memory_read", tool_call_id="read-1", content=model_json(model)))
    request = engine.prepare_request(history, prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=limit)
    request.on_committed()
    request.on_response()
    await manager.model_input_guard.require(path)
    return model


async def test_desktop_factory_never_constructs_local_memory_or_corrections(tmp_path, host):
    def forbidden(**kwargs):
        pytest.fail("A desktop backend must never construct MemoryManager")

    settings = AgentConfig(memory_backend_type="desktop", memory_dir=str(tmp_path / "must-not-exist"),
                           memory_desktop=DesktopMemoryConfig(bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"]))
    manager = create_memory_backend(settings, manager_factory=forbidden)
    assert not uses_local_memory(manager)
    assert manager.corrections is manager.correction_curator is None
    assert manager.recall() == "" and manager.recall_corrections() == []
    assert manager.auto_match_context("anything") == []
    assert {tool.name for tool in create_memory_tools(manager)} == {
        "memory_read", "memory_write", "memory_edit", "memory_search", "memory_managed_read", "memory_experience_search"}
    assert not (tmp_path / "must-not-exist").exists()
    assert host.calls == []
    await manager.aclose()


@pytest.mark.parametrize("profile", ["cli", "acp"])
async def test_session_assembly_disables_local_writers_and_clones_desktop_state(tmp_path, host, profile):
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.memory_dir = str(tmp_path / "no-local-store")
    config.agent.memory_desktop = DesktopMemoryConfig(bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"])
    config.agent.enable_memory_extraction = True
    config.agent.memory_maintainer_enabled = True
    process = backend(host)
    sessions = []
    try:
        for _ in range(2):
            sessions.append(await AgentSession.open(config=config, host=HostBindings(
                llm_client=RecordingLLM(), memory_manager=process, tools=create_memory_tools(process), system_prompt="BASE"),
                options=SessionOptions(profile=profile, workspace_dir=tmp_path)))
        assert sessions[0].memory_manager is not sessions[1].memory_manager
        assert sessions[0].memory_manager.model_input_guard is not sessions[1].memory_manager.model_input_guard
        assert sessions[0].memory_extractor is None
        assert sessions[0].memory_runtime.backend is sessions[0].memory_manager
        assert not any("correction" in name for name in sessions[0].agent.tools)
        assert not (tmp_path / "no-local-store").exists()
        assert host.calls == []
    finally:
        for session in sessions:
            await session.aclose()
        await process.aclose()


@pytest.mark.parametrize("mode", ["utility", "disabled"])
async def test_utility_or_disabled_session_removes_borrowed_memory_tools(tmp_path, host, mode):
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.enable_memory = mode != "disabled"
    process = backend(host)
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=RecordingLLM(), memory_manager=process, tools=create_memory_tools(process), system_prompt="BASE"),
        options=SessionOptions(profile="acp", utility=mode == "utility", workspace_dir=tmp_path))
    try:
        assert session.memory_manager is None and session.memory_runtime is None
        assert not any(name.startswith("memory_") for name in session.agent.tools)
        assert host.calls == []
    finally:
        await session.aclose()
        await process.aclose()


async def test_all_prompt_sections_are_bounded_and_legacy_excludes_cloud_experience(host):
    manager = backend(host)
    try:
        text = await manager.load_context(query="question", session_id="session-1", turn_id="turn-1")
        assert all(key + "-unique-data" in text for key in host.sections)
        for section in host.sections.values():
            section["content"] *= 1000
        text = await manager.load_context(query="question", session_id="session-1", turn_id="turn-2")
        assert len(text) <= 15400
        host.source = "legacy_read_only"
        text = await manager.load_context(query="question", session_id="session-1", turn_id="turn-3")
        assert "experience-unique-data" not in text
        assert "user_items-unique-data" in text
        with pytest.raises(MemoryProxyError, match="MEMORY_READ_ONLY"):
            await manager.read_core_memory("memory.md")
        assert await manager.search_memory("query")
        assert not any(operation == "core/read" for operation, _ in host.calls)
    finally:
        await manager.aclose()


@pytest.mark.parametrize("mode,code", [("unavailable", "MEMORY_NOT_READY"), ("disabled", "MEMORY_DISABLED"), ("offline", "MEMORY_OFFLINE")])
async def test_invalid_turn_qualification_does_not_reach_cloud_core(host, mode, code):
    host.source = "unavailable" if mode == "unavailable" else "cloud"
    host.automatic = mode != "disabled"
    host.online = mode != "offline"
    manager = backend(host)
    try:
        text = await manager.load_context(query="question", session_id="session-1", turn_id="turn-1")
        if mode != "offline":
            assert text == ""
        else:
            assert "experience-unique-data" not in text
        with pytest.raises(MemoryProxyError, match=code):
            await manager.read_core_memory("memory.md")
        assert not any(operation == "core/read" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_model_read_qualification_controls_remote_write_and_is_consumed(host):
    manager = backend(host)
    try:
        engine, history = await start_turn(manager)
        model = await manager.read_core_memory("memory.md")
        assert 'priority="0"' in model["content"] and model["memory_edit_write_rules"]
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.edit_core_memory("memory.md", old_text="", new_text="<mem>新增</mem>")
        history.append(Message(role="tool", name="memory_read", tool_call_id="read-1", content=model_json(model)))
        request = engine.prepare_request(history, prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=20000)
        request.on_committed()
        request.on_response()
        written = await manager.edit_core_memory("memory.md", old_text="", new_text="<mem>新增</mem>")
        assert written["revision"].startswith("sha256:")
        assert '<mem time="2026-10-28 08:00:00" priority="0">保护项</mem>' in host.files["memory.md"]
        assert "新增" in host.files["memory.md"]
        assert len([1 for operation, _ in host.calls if operation == "core/mutate"]) == 1
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.edit_core_memory("memory.md", old_text="", new_text="<mem>不能重复写</mem>")
    finally:
        await manager.aclose()


async def test_protected_entries_cannot_be_edited_or_rewritten_even_with_forged_old_attributes(host):
    manager = backend(host)
    try:
        engine, history = await start_turn(manager)
        await qualify(manager, engine, history)
        tools = {tool.name: tool for tool in create_memory_tools(manager)}
        failed = await tools["memory_edit"].execute(path="memory.md", old_text='<mem priority="1">保护项</mem>', new_text="<mem>changed</mem>")
        assert not failed.success and "保护" in failed.error
        failed = await tools["memory_write"].execute(path="memory.md", content="<mem>changed</mem>")
        assert not failed.success and "保护" in failed.error
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_absent_core_uses_absent_revision_and_does_not_unlock_second_write(host):
    host.files.pop("user.md")
    manager = backend(host)
    try:
        engine, history = await start_turn(manager)
        model = await qualify(manager, engine, history, "user.md")
        assert model["exists"] is False and model["revision"] == "absent"
        await manager.write_core_memory("user.md", "<mem>用户确认的偏好</mem>")
        mutation = next(payload for operation, payload in host.calls if operation == "core/mutate")
        assert mutation["base_revision"] == "absent"
        assert "用户确认的偏好" in host.files["user.md"]
    finally:
        await manager.aclose()


async def test_read_capacity_failure_and_model_compaction_revoke_host_grant(host):
    manager = backend(host)
    try:
        engine, history = await start_turn(manager)
        await qualify(manager, engine, history)
        assert host.active
        engine.prepare_request([history[0]], prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=20000)
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.edit_core_memory("memory.md", old_text="", new_text="<mem>changed</mem>")
        if manager._control_tail:
            await manager._control_tail
        assert not host.active
        host.files["memory.md"] = "<mem>" + "中" * 20000 + "</mem>"
        with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
            await manager.read_core_memory("memory.md")
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_repeated_core_reads_keep_only_current_path_records(host):
    manager = backend(host)
    try:
        await start_turn(manager)
        for _ in range(8):
            await manager.read_core_memory("memory.md")
        assert len(manager._records) == 1
    finally:
        await manager.aclose()


async def test_unknown_mutation_consumes_local_qualification_without_retry(host):
    manager = backend(host)

    async def fail(operation, payload):
        if operation == "core/mutate":
            raise httpx.ReadError("private wire detail")

    try:
        engine, history = await start_turn(manager)
        await qualify(manager, engine, history)
        host.before = fail
        with pytest.raises(MemoryProxyError) as failure:
            await manager.edit_core_memory("memory.md", old_text="", new_text="<mem>new</mem>")
        assert failure.value.outcome == "unknown"
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.edit_core_memory("memory.md", old_text="", new_text="<mem>new</mem>")
        assert len([1 for operation, _ in host.calls if operation == "core/mutate"]) == 1
    finally:
        await manager.aclose()


@pytest.mark.parametrize("first_source", ["legacy_read_only", "cloud"])
async def test_background_qa_uses_original_turn_scope_after_next_turn_started(host, first_source):
    host.source = first_source
    manager = backend(host)
    runtime = manager.create_runtime()
    host.qa_release = asyncio.Event()
    agent = SimpleNamespace(system_prompt="BASE", set_system_prompt=lambda value, **kwargs: setattr(agent, "system_prompt", value))
    session = SimpleNamespace(agent=agent, cancelled=False)

    async def result(**kwargs):
        return SimpleNamespace(status=SimpleNamespace(value="completed"), final_content="final answer")

    first = _MemoryTurn("first question", "session-1", "turn-1", 123, SimpleNamespace(result=result))
    try:
        await runtime.refresh(session, first)
        scope = runtime._turn_scope
        runtime.schedule(first)
        runtime.end_turn(first)
        host.source, host.generation = "cloud", 2
        await runtime.refresh(session, _MemoryTurn("next question", "session-1", "turn-2", 124))
        host.qa_release.set()
        await asyncio.gather(*tuple(runtime.tasks))
        qas = [payload for operation, payload in host.calls if operation in {"qa", "qa/evidence"}]
        if first_source == "legacy_read_only":
            assert qas == []
        else:
            assert len(qas) == 1 and qas[0]["binding"] == dict(scope.binding)
            assert qas[0]["binding"]["turnId"] == "turn-1"
            assert qas[0]["binding"]["memoryGeneration"] == 1
            assert qas[0]["timestamp"] == 123
        assert len([1 for operation, _ in host.calls if operation == "turn/close"]) == 1
    finally:
        await runtime.aclose()


async def test_real_session_keeps_single_frozen_block_and_same_batch_mutation_is_denied(tmp_path, host):
    class ReadWriteLLM(RecordingLLM):
        async def generate_stream(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                    ToolCall(id="read", type="function", function=FunctionCall(name="memory_read", arguments={"path": "memory.md"})),
                    ToolCall(id="edit", type="function", function=FunctionCall(name="memory_edit", arguments={"path": "memory.md", "old_text": "", "new_text": "<mem>not yet</mem>"}))])
            else:
                yield StreamEvent(type="text", delta="done")
                yield StreamEvent(type="finish", finish_reason="stop")

    manager = backend(host)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    llm = ReadWriteLLM()
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=llm, memory_manager=manager, tools=create_memory_tools(manager), system_prompt="BASE"),
        options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    try:
        activation = await session.agent.tools["tool_search"].execute(tool_names=["memory_read", "memory_edit"])
        assert activation.success
        async with memory_user_turn(session, user_text="read then write", session_id="session-1", turn_id="turn-1"):
            session.agent.add_user_message("read then write")
            _ = [event async for event in session.run_events(options=session.build_run_options(logger=None))]
        assert len(llm.requests) == 2
        text = llm.requests[-1]["messages"][0].content
        assert text.count("--- MEMORY START ---") == 1
        assert "profile-unique-data" in text and "experience-unique-data" in text
        failed = next(message for message in llm.requests[-1]["messages"] if message.role == "tool" and message.name == "memory_edit")
        assert "CORE_READ_REQUIRED" in str(failed.content)
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
        assert len([1 for operation, _ in host.calls if operation == "context"]) == 1
    finally:
        await session.aclose()
        await manager.aclose()


async def test_small_model_budget_reduces_preload_without_changing_user_rules_or_full_core(host):
    from box_agent.kernel.context_engine import request_input_tokens
    from box_agent.memory import place_memory_block

    manager = backend(host)
    try:
        for key in host.sections:
            host.sections[key]["content"] = (key + " ") * 1500
        content = await manager.load_context(query="query", session_id="session-1", turn_id="turn-1")
        original = "KEEP-USER-RULES\n" + place_memory_block(
            "", "--- MEMORY START ---\n" + content + "\n--- MEMORY END ---", reserve=True)
        history = [Message(role="system", content=original), Message(role="user", content="current task")]
        engine = DefaultContextEngine()
        engine.bind_memory(manager)
        tools = prepare_tools(create_memory_tools(manager))
        request = engine.prepare_request(history, prepared_tools=tools, token_limit=3000)
        assert request.blocked_reason is None
        assert "KEEP-USER-RULES" in request.messages[0].content
        assert request_input_tokens(request.messages, tools.definitions) + 1024 <= 3000
        assert len(request.messages[0].content) < len(original)
        assert history[0].content == original
        assert "experience experience" not in request.messages[0].content
        host.files["memory.md"] = "<mem>" + "core data " * 6000 + "</mem>"
        with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
            await manager.read_core_memory("memory.md")
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_missing_bootstrap_never_blocks_session_start_or_falls_back_to_local(tmp_path, host):
    host.path.unlink()
    manager = backend(host)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.memory_dir = str(tmp_path / "forbidden-local")
    llm = RecordingLLM("main conversation continues")
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=llm, memory_manager=manager, tools=create_memory_tools(manager), system_prompt="BASE"),
        options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    try:
        async with memory_user_turn(session, user_text="question", session_id="session-1", turn_id="turn-1"):
            session.agent.add_user_message("question")
            _ = [event async for event in session.run_events(options=session.build_run_options(logger=None))]
        assert len(llm.requests) == 1
        assert not (tmp_path / "forbidden-local").exists()
        assert host.calls == []
    finally:
        await session.aclose()
        await manager.aclose()


@pytest.mark.parametrize("cancel", [False, True])
async def test_hung_preload_is_bounded_and_cancellation_does_not_cancel_main_task(host, cancel):
    started = asyncio.Event()

    async def hang(operation, payload):
        if operation == "context":
            started.set()
            await asyncio.Event().wait()

    host.before = hang
    manager = backend(host)
    runtime = manager.create_runtime()
    agent = SimpleNamespace(system_prompt="BASE", set_system_prompt=lambda value, **kwargs: setattr(agent, "system_prompt", value))
    session = SimpleNamespace(agent=agent, cancelled=False)
    try:
        begin = asyncio.get_running_loop().time()
        refresh = asyncio.create_task(runtime.refresh(session, _MemoryTurn("question", "session-1", "turn-1", 123)))
        await started.wait()
        if cancel:
            runtime.request_cancel()
        await asyncio.wait_for(refresh, timeout=1.1)
        elapsed = asyncio.get_running_loop().time() - begin
        assert elapsed < (0.2 if cancel else 1.05)
        assert runtime._turn_scope is None
        assert not session.cancelled
        assert not any(operation in {"qa", "qa/evidence"} for operation, _ in host.calls)
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("operation", ["read", "write", "edit", "search", "managed_read", "experience_search"])
def test_desktop_child_delegation_keeps_parent_core_qualification_private(host, operation):
    from dataclasses import replace
    from box_agent.tools.sub_agent_capabilities import CapabilityResolver, CapabilityFailure, ResolvedCapabilityBundle
    from tests.test_sub_agent_capabilities import _parse

    manager = backend(host)
    tools = {tool.name: tool for tool in create_memory_tools(manager)}
    name = "memory_" + operation
    spec = _parse(required_tools=[name])
    spec = replace(spec, constraints=replace(spec.constraints, network=True))
    resolved = CapabilityResolver().resolve(spec, parent_tools=tools)
    if operation in {"read", "write", "edit"}:
        assert isinstance(resolved, CapabilityFailure)
    else:
        assert isinstance(resolved, ResolvedCapabilityBundle)
        assert resolved.tools[name] is tools[name]


async def test_emoji_full_read_cannot_use_character_division_as_token_capacity(host):
    manager = backend(host)
    host.files["memory.md"] = '<mem time="2026-10-28 08:00:00" priority="1">' + "🙂" * 5000 + "</mem>"
    try:
        await start_turn(manager, limit=15000)
        with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
            await manager.read_core_memory("memory.md")
        assert not host.active
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_incorrect_visibility_acknowledgement_never_grants_write(host):
    manager = backend(host)

    async def wrong(operation, payload):
        if operation == "visibility/commit":
            return httpx.Response(200, json={"protocol": 2, "identity_epoch": host.bootstrap["identity_epoch"], "ok": True,
                                            "data": {"read_id": str(uuid.uuid4()), "state": "active"}})

    try:
        engine, history = await start_turn(manager)
        model = await manager.read_core_memory("memory.md")
        history.append(Message(role="tool", name="memory_read", tool_call_id="read", content=model_json(model)))
        host.before = wrong
        request = engine.prepare_request(history, prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=20000)
        request.on_committed()
        request.on_response()
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.edit_core_memory("memory.md", "", "<mem>new</mem>")
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_durable_unknown_qa_receipt_is_preserved_without_agent_retry(host):
    manager = backend(host)

    async def unknown(operation, payload):
        if operation == "qa":
            return httpx.Response(200, json={"protocol": 2, "identity_epoch": host.bootstrap["identity_epoch"], "ok": True,
                                            "data": {"event_id": payload["event_id"], "state": "durable_unknown"}})

    try:
        await manager.load_context(query="question", session_id="session-1", turn_id="turn-1")
        scope = manager.current_scope
        host.before = unknown
        receipt = await manager.handoff(scope, "question", "answer", 123)
        assert receipt["state"] == "durable_unknown"
        assert len([1 for operation, _ in host.calls if operation == "qa"]) == 1
    finally:
        await manager.aclose()


async def test_authentication_revocation_clears_preload_and_core_qualification(host):
    manager = backend(host)

    async def denied(operation, payload):
        if operation == "search":
            return httpx.Response(403, json={"protocol": 2, "identity_epoch": host.bootstrap["identity_epoch"], "ok": False,
                                            "error": {"code": "AUTH_REQUIRED"}})

    try:
        engine, history = await start_turn(manager)
        await qualify(manager, engine, history)
        host.before = denied
        with pytest.raises(MemoryProxyError, match="AUTH_REQUIRED"):
            await manager.search_memory("query")
        assert manager.current_scope is None and manager._preload(16000) == ""
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.model_input_guard.require("memory.md")
    finally:
        await manager.aclose()


@pytest.mark.parametrize("evidence_read_fails", [False, True])
@pytest.mark.parametrize("active_goal", [False, True])
async def test_real_managed_agent_run_hands_off_actual_session_log_with_control_capability(tmp_path, host, monkeypatch, evidence_read_fails, active_goal):
    from box_agent.agent_service import AgentService
    from box_agent.api import RunRequest
    from box_agent.session_log import SessionLog

    log = SessionLog.create(tmp_path / "canonical", session_id="actual-log", cwd=tmp_path)
    if evidence_read_fails:
        def unavailable(*args, **kwargs):
            raise OSError("synthetic evidence read failure")
        monkeypatch.setattr(log, "event_page", unavailable)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.memory_desktop = DesktopMemoryConfig(bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"])
    process = backend(host)
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=RecordingLLM("actual final answer"), memory_manager=process, tools=[],
        system_prompt="BASE", session_log=log), options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    try:
        if active_goal:
            session.agent.set_goal("Synthetic goal")
        handle = await AgentService().start(RunRequest(run_id="actual-turn", session_id="session-1", user_message="question"), session=session)
        result = await handle.result()
        assert result.status.value == "completed"
        await asyncio.gather(*tuple(session.memory_runtime.tasks))
        operations = [(operation, payload) for operation, payload in host.calls if operation == "qa/evidence"]
        assert len(operations) == 1
        evidence = operations[0][1]["evidence"]
        if evidence_read_fails:
            assert evidence["status"] == "no_evidence" and evidence["reason"] == "log_unavailable"
            return
        assert evidence["status"] == "captured"
        assert evidence["session_id"] == "session-1" and evidence["turn_id"] == "actual-turn"
        actual = {event["seq"]: event for event in log.events}
        for view in evidence["views"]:
            ref = view["source_ref"]
            assert ref["visible_sha256"] == hashlib.sha256(view["text"].encode()).hexdigest()
            assert all(actual[seq]["type"] == ref["event_type"] for seq in ref["event_seqs"])
        assert {view["source_ref"]["event_type"] for view in evidence["views"]} == {"user/message", "assistant/message", "turn/end"}
    finally:
        await session.aclose()
        await process.aclose()
        log.close()
