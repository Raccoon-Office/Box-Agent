"""ACP input provenance and file-only learning use the real SDK dispatch path."""

import asyncio
from contextlib import asynccontextmanager
import json

import pytest
from acp.agent.connection import AgentSideConnection

from box_agent.acp import BoxACPAgent
from box_agent.acp.stdio_compat import StdioTransport
from box_agent.config import DesktopMemoryConfig
from box_agent.schema import FunctionCall, StreamEvent, ToolCall
from tests.test_acp import DoneLLM
from tests.test_agent_session import session_config
from tests.test_desktop_memory_backend import backend, host
from tests.test_input_context import facts


@asynccontextmanager
async def wire_session(tmp_path, host, llm=None, tools=()):
    config = session_config(tmp_path)
    config.agent.memory_backend_type = "desktop"
    config.agent.memory_desktop = DesktopMemoryConfig(
        bootstrap_file=str(host.path), identity_epoch=host.bootstrap["identity_epoch"])
    process = backend(host)
    reader, writes = asyncio.StreamReader(), asyncio.Queue()

    class Writer:
        def write(self, data):
            writes.put_nowait(json.loads(data))

        async def drain(self):
            pass

    def create(conn):
        nonlocal adapter
        adapter = BoxACPAgent(conn, config, llm or DoneLLM(), list(tools), "system", memory_manager=process)
        return adapter

    adapter = None
    connection = AgentSideConnection(create, StdioTransport(reader, Writer()))
    sequence = 0

    async def request(method, params):
        nonlocal sequence
        sequence += 1
        request_id = sequence
        reader.feed_data((json.dumps({"jsonrpc": "2.0", "id": request_id,
                                    "method": method, "params": params}) + "\n").encode())
        while True:
            response = await asyncio.wait_for(writes.get(), 5)
            if response.get("id") == request_id:
                return response

    def notify(method, params):
        reader.feed_data((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())

    request.notify = notify

    try:
        initialized = await request("initialize", {"protocolVersion": 1, "_meta": {
            "host_client_info": {"name": "office-desktop", "version": "1.1.9"}}})
        assert initialized["result"]["_meta"]["desktop_memory"]["enabled"] is True
        assert adapter._client_info.name == "office-desktop"
        opened = await request("session/new", {"cwd": str(tmp_path), "mcpServers": [],
                                               "_meta": {"session_id": "input-provenance"}})
        session_id = opened["result"]["sessionId"]
        yield request, adapter._sessions[session_id], session_id
    finally:
        await adapter.aclose()
        await process.aclose()
        reader.feed_eof()
        await connection.close()


@pytest.mark.parametrize("kind", ["plain", "reference", "selection", "appshot", "file"])
async def test_sdk_keeps_complete_original_words_separate_from_model_projection(tmp_path, host, kind):
    user = "以后生成 PPT，请默认使用 16:9。"
    meta = {"turn_id": "input-turn", "input_context": {"schemaVersion": 1, "userRequest": user}}
    prompt = [{"type": "text", "text": user}]
    if kind == "reference":
        meta["input_context"]["referencedConversations"] = {"text": "REFERENCE_ONLY_CANARY"}
    elif kind == "selection":
        meta["selection_context"] = facts()["selection_context"]
    elif kind == "appshot":
        meta["input_context"]["appshot"] = {"text": "APP_WINDOW_CANARY"}
    elif kind == "file":
        path = tmp_path / "slides.txt"
        path.write_text("FILE_CONTENT_CANARY")
        prompt.append({"type": "resource_link", "uri": path.as_uri(), "name": path.name})
    async with wire_session(tmp_path, host) as (request, state, session_id):
        response = await request("session/prompt", {"sessionId": session_id, "prompt": prompt, "_meta": meta})
        assert response["result"]["stopReason"] == "end_turn"
        await asyncio.gather(*tuple(state.memory_runtime.tasks))
        sent = [body for operation, body in host.calls if operation == "qa/evidence"]
        assert len(sent) == 1
        qa = sent[0]
        view = next(view for view in qa["evidence"]["views"] if view["source_ref"]["event_type"] == "user/message")
        assert qa["user"] == user
        assert json.loads(view["text"]) == {"content": user}
        assert "CANARY" not in view["text"]
        canonical = state.agent.session_log.events[view["source_ref"]["event_seqs"][0]]["data"]["content"]
        assert view["source_ref"]["coverage"] == ("full" if canonical == user else "excerpt")
        assert state.source_text == user


@pytest.mark.parametrize("mode", ["completed", "disabled", "offline", "legacy", "unavailable", "failed", "cancelled", "no_evidence"])
async def test_sdk_file_only_turn_hands_off_empty_raw_request_only_when_qualified(tmp_path, host, monkeypatch, mode):
    path = tmp_path / "input.csv"
    path.write_text("id,value\n1,37\n")
    host.automatic = mode != "disabled"
    host.online = mode != "offline"
    if mode in {"legacy", "unavailable"}:
        host.source = "legacy_read_only" if mode == "legacy" else "unavailable"
    started = asyncio.Event()

    class OutcomeLLM(DoneLLM):
        async def generate_stream(self, *args, **kwargs):
            if mode == "failed":
                raise RuntimeError("fixture failure")
            if mode == "cancelled":
                started.set()
                await asyncio.Event().wait()
            yield StreamEvent(type="text", delta="Processed the supplied file.")
            yield StreamEvent(type="finish", finish_reason="stop")

    if mode == "no_evidence":
        from box_agent.desktop_memory.evidence import EvidenceCursor, no_evidence
        monkeypatch.setattr(EvidenceCursor, "capture", lambda self, **kwargs: no_evidence(
            kwargs["session_id"], kwargs["turn_id"], kwargs["event_id"], "log_unavailable"))
    async with wire_session(tmp_path, host, OutcomeLLM()) as (request, state, session_id):
        pending = asyncio.create_task(request("session/prompt", {"sessionId": session_id, "prompt": [{
            "type": "resource_link", "uri": path.as_uri(), "name": path.name, "mimeType": "text/csv"}],
            "_meta": {"turn_id": "file-turn", "input_context": {"schemaVersion": 1, "userRequest": ""}}}))
        if mode == "cancelled":
            await asyncio.wait_for(started.wait(), 5)
            request.notify("session/cancel", {"sessionId": session_id})
        response = await pending
        if mode == "failed":
            assert response["result"]["_meta"]["completed"] is False
            assert "fixture failure" in response["result"]["_meta"]["error"]
        else:
            assert response["result"]["stopReason"] == ("cancelled" if mode == "cancelled" else "end_turn")
        await asyncio.gather(*tuple(state.memory_runtime.tasks))
        sent = [body for operation, body in host.calls if operation == "qa/evidence"]
        assert len(sent) == (1 if mode == "completed" else 0)
        if sent:
            qa = sent[0]
            assert qa["user"] == ""
            assert qa["input_facts"] == {"files": [{"name": "input.csv", "size": path.stat().st_size,
                                                   "mimeType": "text/csv"}], "omitted_files": 0}
            assert str(tmp_path) not in json.dumps(qa)
            assert "请分析这些文件" not in json.dumps(qa)
            view = next(view for view in qa["evidence"]["views"] if view["source_ref"]["event_type"] == "user/message")
            assert json.loads(view["text"]) == {"content": ""}
            assert qa["evidence"]["status"] == "captured"
        context = next(body for operation, body in host.calls if operation == "context")
        assert context["query"] == ""


async def test_host_clipped_preload_stays_an_explicit_fragment_with_an_agent_recall_plan(host):
    host.sections = {"profile": {"content": "😀完整可见片段", "revision": "profile-v2", "status": "ok",
                                 "truncated": True, "original_length": 100,
                                 "continuation": {"operation": "core/read", "path": "user.md"}}}
    manager = backend(host)
    try:
        text = await manager.load_context(query="question", session_id="s", turn_id="t")
        assert "😀完整可见片段" in text
        assert "片段，需工具续读" in text
        assert '"operation": "core/read"' in text and '"path": "user.md"' in text
        assert "不授予权限" in text
        request = next(body for operation, body in host.calls if operation == "context")
        assert request["recall"] == {"recent_days": 3, "experience_limit": 3}
        assert len(manager._preload(50)) <= 50
    finally:
        await manager.aclose()


async def test_sdk_file_only_tool_success_retains_operation_evidence_without_user_preference(tmp_path, host):
    from box_agent.tools.file import ReadTool

    path = tmp_path / "input.txt"
    path.write_text("以后生成 PPT，请默认使用 16:9。 FILE_ONLY_PREFERENCE")

    class ReadThenAnswer(DoneLLM):
        calls = 0

        async def generate_stream(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield StreamEvent(type="finish", finish_reason="tool", tool_calls=[ToolCall(
                    id="read-source", type="function", function=FunctionCall(
                        name="read_file", arguments={"path": "input.txt"}))])
            else:
                yield StreamEvent(type="text", delta="Read the supplied document.")
                yield StreamEvent(type="finish", finish_reason="stop")

    async with wire_session(tmp_path, host, ReadThenAnswer(), [ReadTool(workspace_dir=tmp_path)]) as (request, state, session_id):
        response = await request("session/prompt", {"sessionId": session_id,
            "prompt": [{"type": "resource_link", "uri": path.as_uri(), "name": path.name}],
            "_meta": {"turn_id": "file-read", "input_context": {"schemaVersion": 1, "userRequest": ""}}})
        assert response["result"]["_meta"]["completed"] is True
        await asyncio.gather(*tuple(state.memory_runtime.tasks))
        sent = [body for operation, body in host.calls if operation == "qa/evidence"]
        assert len(sent) == 1
        qa = sent[0]
        assert qa["user"] == ""
        views = qa["evidence"]["views"]
        result = next(view for view in views if view["source_ref"]["event_type"] == "tool/result")
        call = next(view for view in views if view["source_ref"]["event_type"] == "tool/call")
        assert json.loads(result["text"])["success"] is True
        assert result["source_ref"]["call_id"] == call["source_ref"]["call_id"] == "read-source"
        assert "FILE_ONLY_PREFERENCE" in result["text"]
        user = next(view for view in views if view["source_ref"]["event_type"] == "user/message")
        assert json.loads(user["text"]) == {"content": ""}


def test_file_facts_are_bounded_and_never_contain_local_paths_or_file_bodies():
    from box_agent.desktop_memory.runtime import _file_input_facts

    context = {"files": [{"name": f"/private/local/file-{index}.txt", "size": index,
                           "path": "/private/local/source", "uri": "file:///private/local/source",
                           "title": "UNTRUSTED_PRIVATE_TITLE", "content": "FILE_BODY"} for index in range(520)]}
    facts = _file_input_facts(context)
    assert len(facts["files"]) == 20 and facts["omitted_files"] == 500
    assert facts["files"][0] == {"name": "file-0.txt", "size": 0}
    assert all(secret not in json.dumps(facts) for secret in ["/private", "file://", "FILE_BODY", "PRIVATE_TITLE"])
    assert _file_input_facts({"files": [{"name": "file.txt", "size": 0, "mimeType": ""}]}) == {
        "files": [{"name": "file.txt", "size": 0}], "omitted_files": 0}


async def test_legacy_fragment_exposes_versioned_continuation_within_model_budget(host):
    host.source = "legacy_read_only"
    host.sections = {"user_items": {"content": "legacy excerpt" * 500, "truncated": True,
                                    "original_length": 20_000, "continuation": {
                                        "operation": "managed/read", "refs": [{"entry_ref": "legacy-entry", "version": "legacy-v1"}]}}}
    manager = backend(host)
    try:
        text = await manager.load_context(query="question", session_id="s", turn_id="t")
        assert '"entry_ref": "legacy-entry"' in text
        assert '"version": "legacy-v1"' in text
        assert "片段，需工具续读" in text and len(text) <= 15400
        limited = manager._preload(300)
        assert len(limited) <= 300 and "legacy-entry" in limited
        assert not host.active
    finally:
        await manager.aclose()


@pytest.mark.parametrize("name", ["DB_PASSWORD=SYNTHETIC_SECRET.txt", "ghp_SYNTHETIC_GITHUB_TOKEN_1234567890"])
async def test_sensitive_file_metadata_skips_qa_even_with_nonempty_user_text(tmp_path, host, name):
    path = tmp_path / "safe.txt"
    path.write_text("safe content")
    async with wire_session(tmp_path, host) as (request, state, session_id):
        await request("session/prompt", {"sessionId": session_id,
            "prompt": [{"type": "text", "text": "Review this file"},
                       {"type": "resource_link", "uri": path.as_uri(), "name": name}],
            "_meta": {"turn_id": "sensitive-file"}})
        await asyncio.gather(*tuple(state.memory_runtime.tasks))
        assert not [body for operation, body in host.calls if operation in {"qa", "qa/evidence"}]


@pytest.mark.parametrize("metadata", [
    {"truncated": "true"}, {"original_length": True}, {"original_length": 0}, {"continuation": []},
])
async def test_malformed_host_fragment_metadata_cannot_be_injected_as_complete_context(host, metadata):
    from box_agent.desktop_memory.transport import MemoryProxyError

    host.sections = {"profile": {"content": "complete text", **metadata}}
    manager = backend(host)
    try:
        with pytest.raises(MemoryProxyError, match="MEMORY_PROXY_INVALID_RESPONSE"):
            await manager.load_context(query="question", session_id="s", turn_id="t")
        assert manager.current_scope is None
    finally:
        await manager.aclose()
