"""Desktop source routing, model qualification and final-turn handoff contracts."""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
import secrets
from types import SimpleNamespace
from typing import ClassVar
import uuid

import httpx
import pytest

from box_agent.agent_session import AgentSession
from box_agent.config import AgentConfig, DesktopMemoryConfig
from box_agent.context_input import DefaultContextEngine
from box_agent.desktop_memory.backend import DesktopMemoryBackend, model_json
from box_agent.desktop_memory.transport import MemoryProxyError
from box_agent.memory import (
    _MemoryTurn, create_memory_backend, external_memory_block, is_memory_history_message, memory_user_turn, uses_local_memory,
)
from box_agent.schema import Message, StreamEvent, FunctionCall, ToolCall, TokenUsage
from box_agent.session_context import HostBindings, SessionOptions
from box_agent.session_log import SessionLog, run_session_write
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.memory_tool import create_memory_tools
from tests.test_agent_session import session_config
from tests.test_desktop_memory_evidence import CREDENTIAL_SAMPLES
from tests.test_session_plugins import RecordingLLM
from tests.acp_helpers import acp_params


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
        expected_token = bootstrap["control_token"] if operation.startswith("visibility/") or operation in {"qa/evidence", "experience/job"} else bootstrap["token"]
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
        elif operation == "experience/job":
            data = {"job": None}
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


async def test_acp_utility_generates_text_without_restoring_process_memory(tmp_path, host):
    from box_agent.acp import BoxACPAgent
    from tests.test_acp import DummyConn, DoneLLM

    config = session_config(tmp_path)
    config.agent.memory_promotion_proposal_enabled = True
    process = backend(host)
    adapter = BoxACPAgent(DummyConn(), config, DoneLLM(), [], "BASE", memory_manager=process)
    try:
        result = await adapter.new_session(**acp_params(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"utility": True},
        )))
        state = adapter._sessions[result.sessionId]
        assert state.memory_manager is None
        for request in ("Draft a project", "Revise the draft"):
            response = await adapter.prompt(**acp_params(SimpleNamespace(
                sessionId=result.sessionId, prompt=[{"text": request}], field_meta={},
            )))
            assert response.stopReason == "end_turn"
        assert any(message.role == "assistant" and message.content for message in state.agent.messages)
        assert state.agent.tools == {}
        assert host.calls == []
    finally:
        await adapter.aclose()
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


async def test_desktop_edit_guides_format_correction_and_read_after_write(host):
    manager = backend(host)
    try:
        engine, history = await start_turn(manager)
        tools = {tool.name: tool for tool in create_memory_tools(manager)}
        edit = tools["memory_edit"]
        for name in ("old_text", "new_text"):
            assert "<mem>...</mem>" in edit.parameters["properties"][name]["description"]

        await qualify(manager, engine, history)
        original = host.files["memory.md"]
        malformed = await edit.execute(path="memory.md", old_text="", new_text="<mem>新增")
        assert not malformed.success
        assert "改成 <mem>正文</mem> 后再试" in malformed.error
        assert host.files["memory.md"] == original

        written = await edit.execute(path="memory.md", old_text="", new_text="<mem>新增</mem>")
        assert written.success
        denied = await edit.execute(path="memory.md", old_text="", new_text="<mem>再次写入</mem>")
        assert not denied.success
        assert denied.raw_output == {"code": "CORE_READ_REQUIRED", "outcome": "not_sent"}
        assert "同一 path 调用 memory_read" in denied.error
        assert sum(operation == "core/mutate" for operation, _ in host.calls) == 1

        readback = await tools["memory_read"].execute(path="memory.md")
        assert readback.success
        assert readback.raw_output["revision"] == written.raw_output["revision"]
        assert readback.raw_output["content"].startswith(original)
        assert "新增</mem>" in readback.raw_output["content"]
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


@pytest.mark.parametrize("keep_full_read", [True, False], ids=["full-read", "summary-only"])
async def test_desktop_read_authorization_survives_request_preparation_with_session_log(host, tmp_path, keep_full_read):
    manager = backend(host)
    log = SessionLog.create(tmp_path / "sessions", session_id="session-1", cwd=tmp_path)
    engine = DefaultContextEngine()
    engine.configure_run(session_store=log)
    engine.bind_memory(manager)
    arguments = dict(prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=20000)
    history = [Message(role="user", content="Update my long-term memory")]
    path = "memory.md"
    original = host.files[path]

    try:
        await manager.load_context(query=history[0].content, session_id="session-1", turn_id="turn-1")
        await engine.aprepare_request(history, **arguments)
        read = await manager.read_core_memory(path)
        reply = Message(role="tool", name="memory_read", tool_call_id="read-1", content=model_json(read))
        request = await engine.aprepare_request([*history, reply], **arguments)
        await run_session_write(log, request.on_committed)
        request.on_response()
        await manager.model_input_guard.require(path)
        assert read["read_id"] in host.active

        # 模拟后续请求保留完整结果，或压缩后只保留摘要。
        if keep_full_read:
            next_request = await engine.aprepare_request([*history, reply], **arguments)
        else:
            summary = Message(role="assistant", content="The prior step read the memory file.")
            next_request = await engine.apreview_request([*history, summary], **arguments)
        assert next_request.blocked_reason is None
        await run_session_write(log, next_request.on_committed)
        next_request.on_response()

        if keep_full_read:
            await manager.edit_core_memory(path, old_text="", new_text="<mem>新记忆</mem>")
            assert "新记忆" in host.files[path]
        else:
            with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
                await manager.edit_core_memory(path, old_text="", new_text="<mem>新记忆</mem>")
            assert host.files[path] == original
    finally:
        log.close()
        await manager.aclose()

    operations = [operation for operation, _ in host.calls]
    commit = operations.index("visibility/commit")
    revoke = operations.index("visibility/revoke", commit + 1)
    assert operations.count("core/read") == 1
    if keep_full_read:
        assert operations.index("visibility/commit", revoke + 1) < operations.index("core/mutate")
    else:
        assert "visibility/commit" not in operations[revoke + 1:] and "core/mutate" not in operations
    assert not host.active


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
    # This scripted model uses the legacy direct-tool protocol.
    config.agent.tool_interface_version = 1
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
        snapshots = [message for message in llm.requests[-1]["messages"] if is_memory_history_message(message)]
        text = snapshots[0].content
        assert len(snapshots) == 1 and snapshots[0].role == "user"
        assert "--- MEMORY START ---" not in llm.requests[-1]["messages"][0].content
        assert text.count("--- MEMORY START ---") == 1
        assert "profile-unique-data" in text and "experience-unique-data" in text
        failed = next(message for message in llm.requests[-1]["messages"] if message.role == "tool" and message.name == "memory_edit")
        assert "CORE_READ_REQUIRED" in str(failed.content)
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
        assert len([1 for operation, _ in host.calls if operation == "context"]) == 1
    finally:
        await session.aclose()
        await manager.aclose()


@pytest.mark.parametrize("source,online,expected", [("cloud", True, True), ("cloud", False, False), ("legacy_read_only", True, False)])
async def test_automatic_off_online_cloud_session_can_wake_pending_reconciliation_without_qualifying_new_learning(host, source, online, expected):
    host.automatic, host.source, host.online = False, source, online
    manager = backend(host)
    runtime = manager.create_runtime()
    async def unexpected_model(**kwargs):
        pytest.fail("pending-only wakeup must not generate a new model response")
    agent = SimpleNamespace(system_prompt="BASE", llm=SimpleNamespace(generate=unexpected_model), token_limit=100000)
    agent.set_system_prompt = lambda value, **kwargs: setattr(agent, "system_prompt", value)
    session = SimpleNamespace(agent=agent, cancelled=False)
    try:
        await runtime.refresh(session, _MemoryTurn("question", "session-1", "turn-1", 123))
        await asyncio.gather(*tuple(runtime.tasks))
        assert not runtime._turn_scope.learnable
        actions = [payload["action"] for operation, payload in host.calls if operation == "experience/job"]
        assert actions == (["claim"] if expected else [])
        assert not any(operation in {"qa", "qa/evidence"} for operation, _ in host.calls)
    finally:
        await runtime.aclose()
        await manager.aclose()


async def test_small_model_budget_reduces_preload_without_changing_user_rules_or_full_core(host):
    from box_agent.kernel.context_engine import request_input_tokens

    manager = backend(host)
    try:
        for key in host.sections:
            host.sections[key]["content"] = (key + " ") * 1500
        content = await manager.load_context(query="query", session_id="session-1", turn_id="turn-1")
        manager.memory_history.stage(external_memory_block(content, 15400))
        original = "KEEP-USER-RULES"
        history = [Message(role="system", content=original), Message(role="user", content="current task")]
        engine = DefaultContextEngine()
        engine.bind_memory(manager)
        tools = prepare_tools(create_memory_tools(manager))
        # Leave room for schemas + 1,024 headroom under the same strict budget
        # now used for final requests, while still forcing preload reduction.
        request = engine.prepare_request(history, prepared_tools=tools, token_limit=5000)
        assert request.blocked_reason is None
        assert "KEEP-USER-RULES" in request.messages[0].content
        assert request_input_tokens(request.messages, tools.definitions) + 1024 <= 5000
        assert manager.model_input_guard._input_tokens(request.messages, tools.definitions) + 1024 <= 5000
        snapshot, = request.history_updates
        assert snapshot in request.messages and len(snapshot.content) < len(content)
        assert request.messages[0].content == history[0].content == original
        assert "experience experience" not in snapshot.content
        # 预算恢复后，已发送的裁剪版本按原始召回内容去重。
        repeated = engine.prepare_request([*history, snapshot], prepared_tools=tools, token_limit=40000)
        assert not repeated.history_updates
        assert [m for m in repeated.messages if is_memory_history_message(m)] == [snapshot]
        host.files["memory.md"] = "<mem>" + "core data " * 6000 + "</mem>"
        with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
            await manager.read_core_memory("memory.md")
        assert not any(operation == "core/mutate" for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_desktop_appends_changed_snapshots_and_revalidates_history_on_every_turn(tmp_path, host):
    from box_agent.agent_service import AgentService
    from box_agent.api import RunRequest

    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    manager = backend(host)
    model = RecordingLLM()
    log = SessionLog.create(tmp_path / "logs", session_id="session-1", cwd=tmp_path)
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=model, memory_manager=manager, tools=[], system_prompt="BASE", session_log=log,
    ), options=SessionOptions(workspace_dir=tmp_path, profile="acp"))

    async def turn(number):
        run_id = f"run-{number}"
        handle = await AgentService().start(RunRequest(run_id, "session-1", f"问题 {number}"), session=session)
        assert (await handle.result()).status.value == "completed"
        messages = model.requests[-1]["messages"]
        assert "--- MEMORY START ---" not in messages[0].content
        return messages, [message for message in messages if is_memory_history_message(message)]

    async def timeout(operation, payload):
        if operation == "context":
            raise httpx.ReadTimeout("fixture: context timed out")

    try:
        first, original = await turn(1)
        same, repeated = await turn(2)
        assert len(original) == 1 and repeated == original
        assert same[:len(first)] == first
        host.sections["profile"]["content"] = "更新后的桌面画像"
        changed, updated = await turn(3)
        assert changed[:len(same)] == same
        assert len(updated) == 2 and "更新后的桌面画像" in updated[-1].content
        host.before = timeout
        _, unavailable = await turn(4)
        assert unavailable == []
        host.before = None
        host.sections["profile"]["status"] = "error"
        _, partial = await turn(5)
        assert partial == []
        host.sections = {}
        _, cleared = await turn(6)
        _, still_empty = await turn(7)
        assert cleared[:2] == updated and len(cleared) == 3 and "已失效" in cleared[-1].content
        assert cleared == still_empty
        host.generation += 1
        host.sections = {"profile": {"content": "不同授权的画像"}}
        _, new_scope = await turn(8)
        assert len(new_scope) == 1 and "不同授权的画像" in new_scope[0].content
        assert new_scope[0].name != updated[-1].name
        host.automatic = False
        _, disabled = await turn(9)
        assert disabled == []
        persisted = [message for message in log.replay().messages if is_memory_history_message(message)]
        assert persisted == [*cleared, *new_scope]
    finally:
        await session.aclose()
        await manager.aclose()
        log.close()


async def test_desktop_compaction_restores_latest_snapshot_without_sending_snapshots_to_summarizer(tmp_path, host):
    from box_agent.agent_service import AgentService
    from box_agent.api import RunRequest

    class SummaryLLM(RecordingLLM):
        summary = None

        def estimate_text_tokens(self, text):
            return len(text.encode("utf-8")) // 3

        async def generate_stream(self, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                self.summary = kwargs["messages"]
                yield StreamEvent(type="text", delta="<summary>继续做原任务。</summary>")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            async for event in super().generate_stream(**kwargs):
                yield event

    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    manager = backend(host)
    model = SummaryLLM()
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=model, memory_manager=manager, tools=[], system_prompt="BASE",
    ), options=SessionOptions(workspace_dir=tmp_path, profile="acp"))
    session.agent.token_limit = 104_400

    async def turn(number):
        handle = await AgentService().start(RunRequest(f"run-{number}", "session-1", f"问题 {number}"), session=session)
        assert (await handle.result()).status.value == "completed"
        return [message for message in model.requests[-1]["messages"] if is_memory_history_message(message)]

    try:
        await turn(1)
        host.sections["profile"]["content"] = "最新的桌面画像"
        old, latest = await turn(2)
        session.agent.messages.extend([
            Message(role="user", content="先完成一次运行"),
            Message(role="assistant", content="x" * 200_000,
                    usage=TokenUsage(prompt_tokens=104_400, completion_tokens=100, total_tokens=104_500)),
        ])
        assert await turn(3) == [latest]
        assert model.summary is not None and not any(is_memory_history_message(message) for message in model.summary)
        assert all("profile-unique-data" not in str(message.content) for message in model.summary)
        assert old not in session.agent.messages
        assert await turn(4) == [latest]
    finally:
        await session.aclose()
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
        await asyncio.wait_for(refresh, timeout=5.5)
        elapsed = asyncio.get_running_loop().time() - begin
        assert elapsed < (0.2 if cancel else 5.3)
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


@pytest.mark.parametrize("mode", ["cloud", "disabled", "offline", "unavailable", "legacy_read_only", "retired"])
async def test_memory_turn_state_does_not_impose_desktop_budget_on_ordinary_requests(host, mode):
    host.automatic = mode != "disabled"
    host.online = mode != "offline"
    host.source = mode if mode in {"unavailable", "legacy_read_only"} else "cloud"
    manager = backend(host)
    try:
        engine, _ = await start_turn(manager)
        if mode == "retired":
            manager.end_turn()
        engine.bind_model(SimpleNamespace(estimate_text_tokens=lambda text: len(text) // 2))
        history = [Message(role="system", content="BASE"), Message(role="user", content="x" * 1200000)]
        request = engine.prepare_request(history, prepared_tools=prepare_tools(create_memory_tools(manager)), token_limit=810000)
        assert request.blocked_reason is None
    finally:
        await manager.aclose()


async def test_core_read_budget_error_explains_combined_context_without_exposing_content(host):
    manager = backend(host)
    host.files["memory.md"] = "PRIVATE_CORE " * 5000
    try:
        await start_turn(manager, limit=15000)
        reader = next(tool for tool in create_memory_tools(manager) if tool.name == "memory_read")
        result = await reader.execute(path="memory.md")
        assert not result.success
        assert result.raw_output == {"code": "CORE_CONTEXT_TOO_LARGE", "outcome": "not_sent"}
        assert "不等于记忆文件本身过大" in result.error and "新建短会话" in result.error
        assert "PRIVATE_CORE" not in str(result)
        assert not host.active
    finally:
        await manager.aclose()


@pytest.mark.parametrize(("operation", "compact"), [
    ("edit", False),
    ("write", False),
    ("edit", True),
    ("write", True),
    ("create", True),
])
async def test_desktop_core_read_qualifies_mutation_after_result_delivery(host, operation, compact):
    from box_agent.events import ErrorEvent, SummarizationEvent, ToolCallResult
    from box_agent.runtime import run_agent_loop

    manager = backend(host)
    exists = operation != "create"
    read_attempts = 2 if compact else 1
    if not exists:
        host.files.pop("memory.md")
    else:
        priority = 0 if operation == "edit" else 1
        original_content = f'<mem time="2026-11-17 08:00:00" priority="{priority}">' + "记忆" * 15_000 + "</mem>"
        host.files["memory.md"] = original_content

    class Provider:
        summary_calls = 0
        normal_calls = 0
        read_reply = ""

        def estimate_text_tokens(self, text):
            return len(text.encode("utf-8")) // 2

        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                self.summary_calls += 1
                yield StreamEvent(type="text", delta="<summary>The user requested a new memory entry.</summary>")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            self.normal_calls += 1
            assert self.summary_calls == int(compact and self.normal_calls > 1)
            if self.normal_calls <= read_attempts:
                if self.normal_calls == 2:
                    failed = next(message for message in messages if message.role == "tool" and message.name == "memory_read")
                    assert "CORE_CONTEXT_TOO_LARGE" in failed.content
                    assert not host.active
                name, arguments = "memory_read", {"path": "memory.md"}
            elif self.normal_calls == read_attempts + 1:
                reply = next(message for message in reversed(messages) if message.role == "tool" and message.name == "memory_read")
                self.read_reply = reply.content
                data, _ = json.JSONDecoder().raw_decode(self.read_reply)
                assert data["exists"] is exists
                assert data["content"] == host.files.get("memory.md", "")
                if operation == "edit":
                    name, arguments = "memory_edit", {"path": "memory.md", "old_text": "", "new_text": "<mem>NEW</mem>"}
                else:
                    name, arguments = "memory_write", {"path": "memory.md", "content": "<mem>NEW</mem>"}
            else:
                yield StreamEvent(type="text", delta="saved")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                id=f"call-{self.normal_calls}", type="function", function=FunctionCall(name=name, arguments=arguments))])

    try:
        await start_turn(manager, limit=810000)
        provider = Provider()
        history = [Message(role="system", content="BASE")]
        if compact:
            history.extend([Message(role="user", content="old request"),
                            Message(role="assistant", content="x" * 1_200_000)])
        history.append(Message(role="user", content="remember NEW"))
        events = [event async for event in run_agent_loop(
            llm=provider, messages=history, tools={tool.name: tool for tool in create_memory_tools(manager)},
            memory_manager=manager, max_steps=4, token_limit=810000, logger=None)]
        assert len([event for event in events if isinstance(event, SummarizationEvent)]) == int(compact)
        assert not [event.message for event in events if isinstance(event, ErrorEvent)]
        failures = [event for event in events if isinstance(event, ToolCallResult) and not event.success]
        assert len(failures) == int(compact)
        if compact:
            assert failures[0].tool_name == "memory_read"
            assert "CORE_CONTEXT_TOO_LARGE" in failures[0].error
        assert provider.normal_calls == read_attempts + 2
        assert host.files["memory.md"].endswith(">NEW</mem>")
        if operation == "edit":
            assert host.files["memory.md"].startswith(original_content + "\n")
        else:
            assert host.files["memory.md"].count("<mem ") == 1

        if exists:
            read_event = next(event for event in events if isinstance(event, ToolCallResult)
                              and event.tool_name == "memory_read" and event.success)
            assert "Structured output externalized." in read_event.content
            saved = Path(read_event.raw_output["_persisted_output"]["path"]).read_text(encoding="utf-8")
            assert provider.read_reply == model_json(json.loads(saved))

        operations = [operation for operation, _ in host.calls]
        assert operations.count("core/read") == read_attempts and operations.count("core/mutate") == 1
        assert operations.index("visibility/commit") < operations.index("core/mutate")
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


@pytest.mark.parametrize("operation", ["qa", "qa/evidence", "turn/close", "visibility/revoke"])
@pytest.mark.parametrize("code", ["TURN_REVOKED", "AUTH_REQUIRED", "MEMORY_SESSION_RETIRED"])
async def test_old_background_rejection_only_revokes_the_matching_turn(host, operation, code):
    manager = backend(host)
    try:
        await start_turn(manager, "old")
        old = manager.current_scope
        await start_turn(manager, "current")
        current = manager.current_scope

        async def reject(op, payload):
            if op == operation:
                return httpx.Response(403, json={"protocol": 2, "identity_epoch": host.bootstrap["identity_epoch"],
                                                "ok": False, "error": {"code": code}})

        host.before = reject
        with pytest.raises(MemoryProxyError, match=code):
            await manager.transport.request(operation, {"binding": dict(old.binding)})
        assert (manager.current_scope is current) == (code == "TURN_REVOKED")
        if code == "TURN_REVOKED":
            with pytest.raises(MemoryProxyError, match=code):
                await manager.transport.request(operation, {"binding": dict(current.binding)})
            assert manager.current_scope is None
    finally:
        await manager.aclose()


@pytest.mark.parametrize("user", [
    'The key: persistence matters', 'cache_key = "query-cache"',
    'primary_key=True', 'total_token: 1200', '使用 Bearer token 认证',
])
async def test_ordinary_key_text_reaches_host_qa_without_being_redacted(host, user):
    manager = backend(host)
    try:
        await start_turn(manager)
        assistant = 'Example: {"key": "value"}'
        await manager.handoff(manager.current_scope, user, assistant, 123)
        payload = next(payload for operation, payload in host.calls if operation == "qa")
        assert payload["user"] == user
        assert payload["assistant"] == assistant
    finally:
        await manager.aclose()


@pytest.mark.parametrize("side", ["user", "assistant"])
async def test_redacted_qa_is_skipped_even_without_an_evidence_bundle(host, side):
    manager = backend(host)
    try:
        await start_turn(manager)
        qa = {"user": "question", "assistant": "answer"}
        qa[side] = "密码：SYNTHETIC_SECRET"
        receipt = await manager.handoff(manager.current_scope, **qa, timestamp=123)
        assert receipt == {"event_id": manager.current_scope.qa_event_id, "state": "skipped_redacted"}
        assert not any(operation in {"qa", "qa/evidence"} for operation, _ in host.calls)
    finally:
        await manager.aclose()


async def test_failed_visibility_revoke_has_payload_free_diagnostics(host, caplog):
    manager = backend(host)
    try:
        engine, history = await start_turn(manager)
        await qualify(manager, engine, history)

        async def reject(operation, payload):
            if operation == "visibility/revoke":
                raise httpx.ReadError("SYNTHETIC_PRIVATE_ERROR")

        host.before = reject
        manager.model_input_guard.forget("memory.md")
        await asyncio.gather(*tuple(manager._control_tasks), return_exceptions=True)
        await asyncio.sleep(0)
        assert "operation=visibility_revoke" in caplog.text and "reason=unconfirmed" in caplog.text
        assert "SYNTHETIC_PRIVATE_ERROR" not in caplog.text
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await manager.model_input_guard.require("memory.md")
    finally:
        await manager.aclose()


async def test_full_handoff_queue_still_closes_skipped_turn_without_blocking(host, caplog):
    manager = backend(host)
    manager.config.save_queue_limit = 1
    runtime = manager.create_runtime()
    agent = SimpleNamespace(system_prompt="BASE", set_system_prompt=lambda value, **kwargs: None)
    turn = _MemoryTurn("question", "session-1", "turn-1", 123)
    stalled = asyncio.create_task(asyncio.Event().wait())
    runtime.tasks.add(stalled)
    try:
        await runtime.refresh(SimpleNamespace(agent=agent), turn)
        runtime.schedule(turn)
        runtime.end_turn(turn)
        await asyncio.gather(*tuple(runtime._close_tasks))
        assert len([1 for operation, _ in host.calls if operation == "turn/close"]) == 1
        assert not any(operation in {"qa", "qa/evidence"} for operation, _ in host.calls)
        assert "operation=handoff" in caplog.text and "reason=queue_full" in caplog.text
        assert not stalled.done()
    finally:
        stalled.cancel()
        await asyncio.gather(stalled, return_exceptions=True)
        runtime.tasks.discard(stalled)
        await runtime.aclose()


@pytest.mark.parametrize("sample", CREDENTIAL_SAMPLES)
@pytest.mark.parametrize("finish_sentence", [True, False])
@pytest.mark.parametrize("qa_secret", ["user", "assistant", None])
async def test_real_tool_result_and_qa_credentials_never_reach_host_handoff(tmp_path, host, sample, finish_sentence, qa_secret, monkeypatch):
    from box_agent.agent_service import AgentService
    from box_agent.api import RunRequest
    from box_agent.session_log import SessionLog
    from box_agent.tools.base import Tool, ToolResult

    class FixtureTool(Tool):
        name = "credential_fixture"
        description = "Return synthetic fixture data."
        parameters: ClassVar[dict] = {"type": "object", "properties": {"env": {"type": "object"}}}

        async def execute(self, **kwargs):
            return ToolResult(success=True, content=sample)

    class FixtureLLM(RecordingLLM):
        async def generate_stream(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="fixture", type="function", function=FunctionCall(name="credential_fixture", arguments={
                        "env": {"DB_PASSWORD": "SYNTHETIC_NESTED_PASSWORD", "MY_API_KEY": "SYNTHETIC_NESTED_KEY"}}))])
            else:
                answer = sample if qa_secret == "assistant" else "Safe fixture answer"
                yield StreamEvent(type="text", delta=answer + ("\nFixture response complete." if finish_sentence else ""))
                yield StreamEvent(type="finish", finish_reason="stop")

    log = SessionLog.create(tmp_path / "private-canonical", session_id="fixture", cwd=tmp_path)
    manager = backend(host)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.retry_on_suspected_truncation = False
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=FixtureLLM(), memory_manager=manager, tools=[FixtureTool()],
        system_prompt="BASE", session_log=log), options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    experience_schedules = []
    monkeypatch.setattr(session.memory_runtime._experience, "schedule", lambda *args: experience_schedules.append(args))
    try:
        await session.agent.tools["tool_search"].execute(tool_names=["credential_fixture"])
        user = sample if qa_secret == "user" else "Run the fixture"
        handle = await AgentService().start(RunRequest(run_id="fixture-turn", session_id="session-1", user_message=user), session=session)
        assert (await handle.result()).status.value == "completed"
        await asyncio.gather(*tuple(session.memory_runtime.tasks))
        payloads = [payload for operation, payload in host.calls if operation in {"qa", "qa/evidence"}]
        assert len(payloads) == (0 if qa_secret else 1)
        assert len(experience_schedules) == (1 if qa_secret else 2)
        assert len([1 for operation, _ in host.calls if operation == "turn/close"]) == 1
        if payloads:
            payload = payloads[0]
            assert payload["user"] == user
            assert payload["assistant"].startswith("Safe fixture answer")
            assert "SYNTHETIC" not in json.dumps(payload) and "AKIAABCDEFGHIJKLMNOP" not in json.dumps(payload)
            views = payload["evidence"]["views"]
            assert payload["evidence"]["status"] == "captured"
            assert {view["source_ref"]["event_type"] for view in views} >= {"tool/call", "tool/result"}
            for view in views:
                assert view["source_ref"]["visible_sha256"] == hashlib.sha256(view["text"].encode()).hexdigest()
        assert any(event["type"] == "tool/result" and event["data"]["result"]["content"] == sample for event in log.events)
        assert any(event["type"] == "user/message" and event["data"]["content"] == user for event in log.events)
    finally:
        await session.aclose()
        await manager.aclose()
        log.close()


async def test_desktop_hook_errors_preserve_existing_model_and_tool_diagnostics(tmp_path, host, caplog, monkeypatch):
    from box_agent.agent_service import AgentService
    from box_agent.api import RunRequest
    from box_agent.hooks import HookManager
    from box_agent.session_log import SessionLog
    from box_agent.tools.base import Tool, ToolResult

    class FixtureTool(Tool):
        name = "privacy_fixture"
        description = "Return synthetic fixture data."
        parameters: ClassVar[dict] = {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ToolResult(success=True, content="PRIVATE_TOOL_RESULT")

    class FixtureLLM(RecordingLLM):
        async def generate_stream(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                yield StreamEvent(type="text", delta="PRIVATE_MODEL_RESPONSE")
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="fixture", type="function", function=FunctionCall(name="privacy_fixture", arguments={}))])
            else:
                yield StreamEvent(type="text", delta="PRIVATE_FINAL_ANSWER")
                yield StreamEvent(type="finish", finish_reason="stop")

    seen = []

    class FailingHook:
        async def on_llm_response(self, *, response):
            seen.append(("model", response.content))
            raise ValueError(response.content)

        async def on_tool_result(self, *, tool_call_id, tool_name, success, content, error):
            seen.append(("tool", content))
            raise ValueError(content)

        async def on_done(self, *, stop_reason, final_content):
            seen.append(("done", final_content))
            raise ValueError(final_content)

    caplog.set_level(logging.WARNING, logger="box_agent.hooks")
    log = SessionLog.create(tmp_path / "canonical", session_id="fixture", cwd=tmp_path)
    manager = backend(host)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.retry_on_suspected_truncation = False
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=FixtureLLM(), memory_manager=manager, tools=[FixtureTool()], hooks=[FailingHook()],
        system_prompt="BASE", session_log=log), options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    monkeypatch.setattr(session.memory_runtime._experience, "schedule", lambda *args: None)
    try:
        await session.agent.tools["tool_search"].execute(tool_names=["privacy_fixture"])
        handle = await AgentService().start(RunRequest(
            run_id="privacy-turn", session_id="session-1", user_message="Run the fixture"), session=session)
        result = await handle.result()
        assert result.status.value == "completed"
        assert result.final_content == "PRIVATE_FINAL_ANSWER"
        await asyncio.gather(*tuple(session.memory_runtime.tasks))
        assert ("model", "PRIVATE_MODEL_RESPONSE") in seen
        assert ("tool", "PRIVATE_TOOL_RESULT") in seen
        assert ("done", "PRIVATE_FINAL_ANSWER") in seen
        assert all(value in caplog.text for value in (
            "PRIVATE_MODEL_RESPONSE", "PRIVATE_TOOL_RESULT", "PRIVATE_FINAL_ANSWER"))

        # Ordinary Hook diagnostics keep the same behavior as desktop runs.
        await HookManager([FailingHook()]).fire_done(stop_reason="completed", final_content="ORDINARY_HOOK_ERROR")
        assert "ORDINARY_HOOK_ERROR" in caplog.text
    finally:
        await session.aclose()
        await manager.aclose()
        log.close()


@pytest.mark.parametrize("code", ["AUTH_REQUIRED", "MEMORY_SESSION_RETIRED"])
async def test_authentication_revocation_clears_preload_and_core_qualification(host, code):
    manager = backend(host)

    async def denied(operation, payload):
        if operation == "search":
            return httpx.Response(403, json={"protocol": 2, "identity_epoch": host.bootstrap["identity_epoch"], "ok": False,
                                            "error": {"code": code}})

    try:
        engine, history = await start_turn(manager)
        await qualify(manager, engine, history)
        host.before = denied
        with pytest.raises(MemoryProxyError, match=code):
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


@pytest.mark.parametrize("active_goal", [False, True])
@pytest.mark.parametrize("prelogged", [False, True])
@pytest.mark.parametrize("wrong_host_text", [False, True])
async def test_legacy_acp_wrapped_user_event_is_exactly_verified_but_only_original_words_are_handed_off(
        tmp_path, host, active_goal, prelogged, wrong_host_text):
    from box_agent.acp import _connector_status_context
    from box_agent.agent_service import AgentService
    from box_agent.api import RunRequest
    from box_agent.session_log import SessionLog
    from box_agent.turn_hints import with_turn_hints

    log = SessionLog.create(tmp_path / "canonical-acp", session_id="actual-acp-log", cwd=tmp_path)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    process = backend(host)
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=RecordingLLM("actual final answer"), memory_manager=process, tools=[],
        system_prompt="BASE", session_log=log), options=SessionOptions(profile="acp", workspace_dir=tmp_path))
    user = "For every workflow report, state the verified outcome before remaining risks."
    logged = with_turn_hints(user).rstrip() + "\n\n" + _connector_status_context(set())
    try:
        if active_goal:
            session.agent.set_goal("Synthetic goal")
        # Reproduce an old record, without the new input_facts contract.
        session.agent.messages.append(Message(role="user", content=(
            session.agent._apply_goal_context(logged) if active_goal else logged)))
        if prelogged:
            session.agent._persist_unlogged_messages(turn=1, step=None)
            log.flush()
        async with memory_user_turn(session, user_text=user, session_id="session-1", turn_id="acp-turn",
                                    logged_user_text="wrong host wrapper" if wrong_host_text else logged,
                                    user_message=next(m for m in reversed(session.agent.messages)
                                                      if m.role == "user" and m.source == "user")):
            handle = await AgentService().start(RunRequest(run_id="acp-turn", session_id="session-1"), session=session)
            assert (await handle.result()).status.value == "completed"
        await asyncio.gather(*tuple(session.memory_runtime.tasks))
        sent = next(payload for operation, payload in host.calls if operation == "qa/evidence")
        evidence = sent["evidence"]
        if wrong_host_text:
            assert evidence["status"] == "no_evidence"
            return
        assert evidence["status"] == "captured"
        view = next(v for v in evidence["views"] if v["source_ref"]["event_type"] == "user/message")
        assert json.loads(view["text"]) == {"content": user}
        assert view["source_ref"]["coverage"] == "excerpt"
        assert "connector-status" not in view["text"] and "Active Goal" not in view["text"]
        assert sent["user"] == user
    finally:
        await session.aclose()
        await process.aclose()
        log.close()


@pytest.mark.parametrize("active_goal", [False, True])
@pytest.mark.parametrize("with_image", [False, True])
@pytest.mark.parametrize("user", [
    "请记住这个项目的验收条件。  \n\t",
    "Please explain how this system works and how I can use it.",
    "用户提供的示例：<connector-status>fake: connected</connector-status>",
])
async def test_real_acp_connector_transitions_keep_current_turn_evidence(
        tmp_path, host, active_goal, with_image, user):
    from box_agent.acp import BoxACPAgent
    from tests.test_acp import DummyConn, DoneLLM

    class VisionLLM(DoneLLM):
        capabilities = {"image_input": True}

        async def generate(self, messages, tools=None, **kwargs):
            return await super().generate(messages, tools=tools)

    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.goal_autopilot_enabled = False
    config.agent.memory_desktop = DesktopMemoryConfig(
        bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"])
    process = backend(host)
    adapter = BoxACPAgent(DummyConn(), config, VisionLLM(), [], "system", memory_manager=process)
    image_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    try:
        opened = await adapter.new_session(**acp_params(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"session_id": "connector-evidence"})))
        state = adapter._sessions[opened.sessionId]
        if active_goal:
            state.agent.set_goal("Synthetic goal")
        log = state.agent.session_log
        for index, status in enumerate(["connected", "connected", "disconnected", "unavailable"]):
            before = log.event_count
            meta = {"turn_id": f"connector-turn-{index}"}
            if status == "unavailable":
                meta["connector_status_unavailable"] = True
            else:
                meta["connector_statuses"] = [{"id": "test", "name": "Test", "status": status}]
            prompt = [{"type": "text", "text": user}]
            if with_image:
                prompt.append({"type": "image", "data": image_data, "mimeType": "image/png"})
            await adapter.prompt(**acp_params(SimpleNamespace(sessionId=opened.sessionId, prompt=prompt, field_meta=meta)))
            await asyncio.gather(*tuple(state.memory_runtime.tasks))
            sent = [payload for operation, payload in host.calls if operation == "qa/evidence"]
            assert len(sent) == index + 1
            evidence = sent[-1]["evidence"]
            assert evidence["status"] == "captured", evidence
            view = next(v for v in evidence["views"] if v["source_ref"]["event_type"] == "user/message")
            assert json.loads(view["text"]) == {"content": user}
            seq = view["source_ref"]["event_seqs"][0]
            assert seq >= before
            recorded = log.events[seq]["data"]
            assert recorded["host_connector_status"]
            assert recorded["content"] == user
            assert view["source_ref"]["coverage"] == "full"
            assert view["source_ref"]["view_spec"]["excerpt_limit"] is None
            assert recorded["input_facts"]["blocks"][0] == {"type": "text", "text": user}
            if with_image:
                assert log.replay().images
            assert image_data not in json.dumps(evidence)
            assert sent[-1]["user"] == user
    finally:
        await adapter.aclose()
        await process.aclose()


@pytest.mark.parametrize("failed", [False, True])
async def test_desktop_acp_trace_preserves_input_output_and_errors(tmp_path, monkeypatch, host, failed):
    from box_agent.acp import BoxACPAgent
    from tests.test_acp import DummyConn, DoneLLM

    monkeypatch.setenv('BOX_AGENT_LOG_FULL_PAYLOAD', '1')
    monkeypatch.setenv('BOX_AGENT_SESSION_TRACE_ENABLED', '1')
    monkeypatch.setenv('BOX_AGENT_SESSION_TRACE_DIR', str(tmp_path / 'diagnostics'))
    config = session_config(tmp_path)
    config.agent.memory_backend_type = 'desktop'
    config.agent.memory_desktop = DesktopMemoryConfig(
        bootstrap_file=str(host.path), identity_epoch=host.bootstrap['identity_epoch'])
    process = backend(host)
    adapter = BoxACPAgent(DummyConn(), config, DoneLLM(), [], 'system', memory_manager=process)
    canary = 'REVIEW-PRIVATE-QA-CANARY-20261030'
    try:
        opened = await adapter.new_session(**acp_params(SimpleNamespace(cwd=str(tmp_path), field_meta={'session_id': 'review-trace'})))
        state = adapter._sessions[opened.sessionId]
        request = SimpleNamespace(sessionId=opened.sessionId,
            prompt=[{'type': 'text', 'text': canary}], field_meta={'turn_id': 'trace-turn'})
        if failed:
            async def fail_turn(*args, **kwargs):
                raise RuntimeError(canary)
            monkeypatch.setattr(adapter, '_run_turn', fail_turn)
            with pytest.raises(RuntimeError, match=canary):
                await adapter.prompt(**acp_params(request))
        else:
            await adapter.prompt(**acp_params(request))
            assert canary in state.agent.session_log.path.read_text()
        records = [json.loads(line) for line in state.trace_writer.file_path.read_text().splitlines()]
        input_record = next(row for row in records if row['event'] == 'turn.input')
        assert input_record['data']['content'] == canary
        assert input_record['turn_id'] == 'trace-turn'
        assert input_record['data']['prompt'] == [{'type': 'text', 'text': canary}]
        if failed:
            error_record = next(row for row in records if row['event'] == 'turn.error')
            assert error_record['turn_id'] == 'trace-turn'
            assert error_record['data']['message'] == canary
        else:
            output_record = next(row for row in records if row['event'] == 'turn.output')
            assert output_record['turn_id'] == 'trace-turn'
            assert output_record['data']['content'] == 'done'
    finally:
        await adapter.aclose()
        await process.aclose()


@pytest.mark.parametrize("job_available", ["refresh", "handoff"])
async def test_desktop_acp_trace_correlates_recall_tools_model_rounds_and_qa(tmp_path, monkeypatch, host, job_available):
    from copy import deepcopy

    from box_agent.acp import BoxACPAgent
    from box_agent.llm.llm_wrapper import LLMClient
    from box_agent.schema import LLMProvider, LLMResponse
    from box_agent.session_trace import (
        SessionTraceWriter, emit_session_trace, reset_session_trace_writer, set_session_trace_writer,
    )
    from box_agent.tools.base import Tool, ToolResult
    from tests.test_acp import DummyConn
    from tests.test_desktop_memory_experience import Transport

    job = Transport()
    release_model = asyncio.Event()
    proposed = asyncio.Event()

    async def experience_job(operation, payload):
        if operation != "experience/job":
            return None
        if job_available == "handoff" and not any(op == "qa/evidence" for op, _ in host.calls):
            return None  # The initial refresh finds no pending job.
        data = await job.request(operation, payload)
        if payload["action"] == "propose":
            proposed.set()
        return httpx.Response(200, json={
            "protocol": 2, "identity_epoch": host.bootstrap["identity_epoch"], "ok": True, "data": data,
        })

    host.before = experience_job

    class TraceTool(Tool):
        name = "trace_fixture"
        description = "Return the synthetic trace fixture."
        parameters: ClassVar[dict] = {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        }

        async def execute(self, text):
            assert text == "trace-tool-argument"
            return ToolResult(success=True, content="trace-tool-result")

    class Provider:
        retry_callback = None

        def __init__(self):
            self.requests = []

        async def generate_stream(self, messages, tools=None, **kwargs):
            self.requests.append(deepcopy(messages))
            if len(self.requests) == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="trace-call", type="function", function=FunctionCall(
                        name="trace_fixture", arguments={"text": "trace-tool-argument"}))])
            else:
                yield StreamEvent(type="text", delta="Trace flow complete.")
                yield StreamEvent(type="finish", finish_reason="stop")

        async def generate(self, messages, tools=None, **kwargs):
            if kwargs["call_kind"] == "memory_experience":
                assert messages[1].content == job.prompt
                await release_model.wait()
                return LLMResponse(content='{"facts":[],"observations":[]}', finish_reason="stop")
            assert kwargs["call_kind"] == "turn_continuation_judge"
            return LLMResponse(content='{"continue":false}', finish_reason="stop")

    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_ENABLED", "1")
    monkeypatch.setenv("BOX_AGENT_SESSION_TRACE_DIR", str(tmp_path / "diagnostics"))
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.memory_desktop = DesktopMemoryConfig(
        bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"])
    config.agent.tool_interface_version = 1
    config.agent.retry_on_suspected_truncation = False
    provider = Provider()
    model = LLMClient(api_key="test-only", provider=LLMProvider.OPENAI, model="fixture-model")
    model._client = provider
    process = backend(host)
    adapter = BoxACPAgent(DummyConn(), config, model, [TraceTool()], "BASE", memory_manager=process)
    try:
        opened = await adapter.new_session(**acp_params(SimpleNamespace(
            cwd=str(tmp_path), field_meta={"session_id": "trace-session"})))
        state = adapter._sessions[opened.sessionId]
        await state.agent.tools["tool_search"].execute(tool_names=["trace_fixture"])
        other = SessionTraceWriter(session_id="other-session", acp_session_id="other-acp",
                                   trace_dir=tmp_path / "diagnostics", enabled=True)
        # Cover both an unbound caller and one already tracing another session.
        token = set_session_trace_writer(other, turn_id="other-turn") if job_available == "handoff" else None
        try:
            await adapter.prompt(**acp_params(SimpleNamespace(
                sessionId=opened.sessionId, prompt=[{"type": "text", "text": "Run the trace fixture."}],
                field_meta={"turn_id": "trace-turn"})))
            emit_session_trace("caller.after_prompt")
        finally:
            if token is not None:
                reset_session_trace_writer(token)
        # Complete the background model after the foreground prompt has returned.
        release_model.set()
        await asyncio.wait_for(proposed.wait(), timeout=5)
        await asyncio.gather(*tuple(state.memory_runtime.tasks))
        assert job.proposal == {"facts": [], "observations": []}
        if token is not None:
            other_rows = other.file_path.read_text(encoding="utf-8").splitlines()
            assert [json.loads(line)["event"] for line in other_rows] == ["caller.after_prompt"]

        records = [json.loads(line) for line in state.trace_writer.file_path.read_text(encoding="utf-8").splitlines()]
        requests = [row for row in records if row["event"] == "llm.request" and row["data"]["streaming"]]
        by_call = {row["llm_call_id"]: row for row in records if row["event"] == "llm.response"}
        responses = [by_call[row["llm_call_id"]] for row in requests]
        assert len(requests) == len(responses) == len(provider.requests) == 2
        judge = next(row for row in records if row["event"] == "llm.request"
                     and row["data"]["call_kind"] == "turn_continuation_judge")
        assert by_call[judge["llm_call_id"]]["data"]["content"] == '{"continue":false}'
        experience, = [row for row in records if row["event"] == "llm.request"
                       and row["data"]["call_kind"] == "memory_experience"]
        experience_reply = by_call[experience["llm_call_id"]]
        assert experience["session_id"] == experience_reply["session_id"] == "trace-session"
        assert experience["turn_id"] == experience_reply["turn_id"] == "event"
        assert experience["data"]["messages"][1]["content"] == job.prompt
        assert json.loads(experience_reply["data"]["content"]) == job.proposal
        assert all(row["event"] != "caller.after_prompt" for row in records)
        assert next(row for row in records if row["event"] == "turn.input")["data"]["content"] == "Run the trace fixture."
        for request, response, actual in zip(requests, responses, provider.requests):
            assert request["session_id"] == response["session_id"] == "trace-session"
            assert request["turn_id"] == response["turn_id"] == "trace-turn"
            assert request["llm_call_id"] == response["llm_call_id"]
            expected = []
            for message in actual:
                recorded = message.model_dump(exclude_none=True, exclude={"host_connector_status"})
                if message.role == "user":
                    recorded["source"] = message.source
                expected.append(recorded)
            assert request["data"]["messages"] == expected
            assert "user_items-unique-data" not in request["data"]["messages"][0]["content"]
            assert any(m["role"] == "user" and "user_items-unique-data" in m["content"]
                       for m in request["data"]["messages"] if isinstance(m.get("content"), str))
        assert requests[0]["llm_call_id"] != requests[1]["llm_call_id"]
        tool_request = next(row for row in records if row["event"] == "tool.request" and row["tool_call_id"] == "trace-call")
        tool_response = next(row for row in records if row["event"] == "tool.response" and row["tool_call_id"] == "trace-call")
        assert tool_request["data"]["arguments"] == {"text": "trace-tool-argument"}
        assert tool_request["turn_id"] == tool_response["turn_id"] == "trace-turn"
        assert tool_response["data"]["content"] == tool_response["data"]["model_content"] == "trace-tool-result"
        assert tool_response["data"]["success"] is True
        assert next(message for message in requests[1]["data"]["messages"]
                    if message["role"] == "tool" and message["tool_call_id"] == "trace-call")["content"] == "trace-tool-result"
        assert responses[0]["data"]["tool_calls"][0]["id"] == "trace-call"
        assert responses[1]["data"]["content"] == "Trace flow complete."
        assert next(row for row in records if row["event"] == "turn.output")["data"]["content"] == "Trace flow complete."
        assert not any(row.get("data", {}).get("payload_redacted") for row in records)

        qa = next(payload for operation, payload in host.calls if operation == "qa/evidence")
        assert qa["user"] == "Run the trace fixture." and qa["assistant"] == "Trace flow complete."
        assert qa["evidence"]["status"] == "captured"
        assert {view["source_ref"]["event_type"] for view in qa["evidence"]["views"]} >= {"user/message", "tool/call", "tool/result"}
        assert "user_items-unique-data" not in json.dumps(qa)
        assert any(operation == "turn/close" for operation, _ in host.calls)
    finally:
        await adapter.aclose()
        await process.aclose()


@pytest.mark.parametrize("code", ["AUTH_REQUIRED", "IDENTITY_CHANGED", "MEMORY_DISABLED", "MEMORY_SESSION_RETIRED"])
async def test_revocation_removes_preload_from_next_model_request(tmp_path, host, code):
    from copy import deepcopy

    class SearchThenAnswer(RecordingLLM):
        async def generate_stream(self, **kwargs):
            self.requests.append({'messages': deepcopy(kwargs['messages'])})
            if len(self.requests) == 1:
                yield StreamEvent(type='finish', finish_reason='tool_use', tool_calls=[
                    ToolCall(id='search', type='function', function=FunctionCall(name='memory_search', arguments={'query': 'query'}))])
            else:
                yield StreamEvent(type='text', delta='done')
                yield StreamEvent(type='finish', finish_reason='stop')

    async def denied(operation, payload):
        if operation == 'search':
            return httpx.Response(403, json={'protocol': 2, 'identity_epoch': host.bootstrap['identity_epoch'],
                                            'ok': False, 'error': {'code': code}})
    host.before = denied
    process = backend(host)
    config = session_config(tmp_path)
    config.agent.memory_backend_type = 'desktop'
    # This scripted model uses the legacy direct-tool protocol.
    config.agent.tool_interface_version = 1
    model = SearchThenAnswer()
    session = await AgentSession.open(config=config, host=HostBindings(
        llm_client=model, memory_manager=process, tools=create_memory_tools(process), system_prompt='BASE'),
        options=SessionOptions(profile='acp', workspace_dir=tmp_path))
    try:
        assert (await session.agent.tools['tool_search'].execute(tool_names=['memory_search'])).success
        async with memory_user_turn(session, user_text='question', session_id='session-1', turn_id='turn-1'):
            session.agent.add_user_message('question')
            _ = [event async for event in session.run_events(options=session.build_run_options(logger=None))]
            assert session.memory_manager.current_scope is None
        assert len(model.requests) == 2
        assert 'user_items-unique-data' in str(model.requests[0]['messages'])
        failure = next(m for m in model.requests[1]['messages'] if m.role == 'tool' and m.name == 'memory_search')
        assert code in str(failure.content)
        assert 'user_items-unique-data' not in str(model.requests[1]['messages'])
    finally:
        await session.aclose()
        await process.aclose()
