"""Standard ACP clients keep their wire and persistence behavior without device opt-in."""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from tests.acp_host.probe import AcpHostProbe, RpcError, collect_session_updates


_REPOSITORY = Path(__file__).resolve().parents[1]
_CONTEXT = {"schemaVersion": 1, "userRequest": "fixture:input_context",
            "appshot": {"text": "legacy ACP context marker"}}
_ORIGIN = {"parentSessionId": "historical-parent", "operationId": "historical-operation"}


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _start_fixture():
    import box_agent.acp as acp
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from tests.desktop_runtime_fixture import FixtureLLM, install_fixture, record_lifecycle

    class RecordingLLM(FixtureLLM):
        async def generate_stream(self, messages, tools=None, **kwargs):
            record_lifecycle(
                "legacy_model_request",
                tools=[entry.name for entry in tools or []],
                messages=[{"role": message.role, "content": message.content}
                          for message in messages if message.role != "system"],
            )
            async for event in super().generate_stream(messages, tools=tools, **kwargs):
                yield event

    root = Path(os.environ["BOX_AGENT_DESKTOP_TEST_ROOT"])
    install_fixture(acp, root)
    acp.LLMClient = RecordingLLM
    asyncio.run(acp.run_acp_server(Config(
        llm=LLMConfig(api_key="test-key", model="desktop-fixture", context_window=1_000_000),
        agent=AgentConfig(workspace_dir=str(root / "profile" / "workspaces"), max_steps=5,
                          enable_memory=False, enable_memory_extraction=False,
                          memory_maintainer_enabled=False, memory_promotion_proposal_enabled=False),
        tools=ToolsConfig(enable_bash=False, enable_file_tools=False, enable_todo=False,
                          enable_plan=False, enable_sub_agent=False, enable_skills=False, enable_mcp=False),
    )))


def _probe(root):
    (root / "profile" / "log").mkdir(parents=True, exist_ok=True)
    return AcpHostProbe(
        command=[sys.executable, "-m", "tests.test_device_legacy_compatibility"],
        cwd=_REPOSITORY,
        env={"BOX_AGENT_DESKTOP_TEST_ROOT": str(root), "BOX_AGENT_HOME": str(root / "profile"),
             "BOX_AGENT_LOG_FILE": str(root / "profile" / "log" / "box-agent.log")},
        timeout_s=20,
    )


def _ordinary_frames(probe):
    # AcpHostProbe relays JSON-RPC over stdio and answers unsupported reverse
    # requests with -32601. A legacy client should never receive a device frame.
    frames = probe.drain_notifications()
    assert not [frame for frame in frames if frame["method"].startswith("_device/")]
    return frames


def _model_requests(root):
    return [event for event in _records(root / "lifecycle.jsonl")
            if event["event"] == "legacy_model_request"]


def _install_historical_device_records(root):
    """Add the on-disk shape of a runtime turn and an unread device outcome."""
    sessions = root / "profile" / "sessions"
    for path in sessions.glob("*/session.jsonl"):
        rows = _records(path)
        if any(row["type"] == "turn/binding" and row["data"]["turn_id"] == "historical-turn" for row in rows):
            break
    else:
        pytest.fail("Historical turn was not persisted")
    user = next(row["data"] for row in rows if row["type"] == "user/message"
                and row["data"].get("input_facts", {}).get("id") == "historical-turn")
    user["source"] = "runtime"
    user["input_facts"].update(kind="device_delegation", deviceDelegation=_ORIGIN)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")

    from box_agent.session_log import SessionLog

    log = SessionLog.open(sessions, session_id=rows[0]["id"], cwd=root / "workspace")
    try:
        log.append("device/ended", {"turnId": "historical-turn"}, ignorable=True)
        log.append("device/state", {"operationId": _ORIGIN["operationId"], "parentTurnId": "historical-turn",
                                    "deviceId": "old-device", "sessionId": "child-session", "turnId": "child-turn",
                                    "seq": 7, "status": "succeeded", "result": "UNREAD_DEVICE_RESULT"},
                   ignorable=True)
        log.flush()
    finally:
        log.close()
    return path


async def _exercise_legacy_host(root):
    trace = {}
    cwd = root / "workspace"
    cwd.mkdir()
    probe = _probe(root)
    await probe.start()
    try:
        initialized = await probe.initialize()
        assert initialized["agentCapabilities"]["loadSession"] is True
        assert initialized["agentCapabilities"]["sessionCapabilities"]["resume"] == {}
        trace["initialize"] = initialized

        for index, device in enumerate((None, {"version": 2}, {"version": "1"},
                                        {"version": True}, {"version": 1.0}, "1")):
            new_params = {"cwd": str(cwd), "mcpServers": []}
            if device is not None:
                new_params["_meta"] = {"device": device}
            new_session = await probe.request("session/new", new_params)
            session_id = new_session["sessionId"]
            assert "device" not in new_session.get("_meta", {})
            meta = {"turnId": f"case-{index}", "input_context": _CONTEXT}
            assert (await probe.session_prompt(session_id, "fixture:input_context", meta=meta))["stopReason"] == "end_turn"
            updates = collect_session_updates(_ordinary_frames(probe))
            assert any(update["sessionUpdate"] == "tool_call" and update.get("rawInput") == {"n": 1}
                       for update in updates)
            assert any(update["sessionUpdate"] == "tool_call_update" and update.get("status") == "completed"
                       for update in updates)
            requests = _model_requests(root)[-2:]
            assert "fixture_count" in requests[0]["tools"]
            assert all(request["tools"] == requests[0]["tools"] for request in requests)
            assert all(not name.startswith("device") for name in requests[0]["tools"])
            assert set(requests[0]["tools"]).isdisjoint({
                "session_list", "session_read", "session_start", "session_send",
                "session_get_turn", "session_wait", "session_cancel",
            })
            assert any("legacy ACP context marker" in str(message["content"]) for message in requests[0]["messages"])
            trace.setdefault("new_sessions", []).append({
                "result": {key: value for key, value in new_session.items() if key != "sessionId"},
                "model": requests,
                "updates": updates,
            })
            if index == 0:
                retained_id = session_id
                for duplicate in ("fixture:input_context", "different input"):
                    with pytest.raises(RpcError) as rejected:
                        await probe.session_prompt(session_id, duplicate, meta=meta)
                    assert rejected.value.code == -32603
                assert _model_requests(root) == requests
                _ordinary_frames(probe)

        permission_id = await probe.session_new(cwd=str(cwd))
        assert (await probe.session_prompt(permission_id, "fixture:permission"))["stopReason"] == "end_turn"
        reverse = [frame for frame in _ordinary_frames(probe) if "id" in frame]
        assert [frame["method"] for frame in reverse] == ["session/request_permission"]
        trace["permission"] = {key: value for key, value in reverse[0]["params"].items() if key != "sessionId"}

        assert (await probe.session_prompt(retained_id, "historical delegated input",
                                           meta={"turn_id": "historical-turn"}))["stopReason"] == "end_turn"
        _ordinary_frames(probe)
        prompt = asyncio.create_task(probe.session_prompt(retained_id, "fixture:stream_wait",
                                                          meta={"turn_id": "cancelled-turn"}))
        try:
            trace["cancel"] = await _cancel_legacy_prompt(probe, retained_id, prompt)
        finally:
            if not prompt.done():
                prompt.cancel()
                await asyncio.gather(prompt, return_exceptions=True)
        _ordinary_frames(probe)
    finally:
        await probe.stop()

    path = _install_historical_device_records(root)
    saved = _records(path)
    before = _model_requests(root)
    probe = _probe(root)
    await probe.start()
    try:
        assert await probe.initialize() == initialized
        params = {"sessionId": retained_id, "cwd": str(cwd), "mcpServers": []}
        assert (await probe.request("session/resume", params))["_meta"]["history_revision_id"]
        assert collect_session_updates(_ordinary_frames(probe)) == []
        full = await probe.request("session/load", {**params, "_meta": {"device": {"version": 2}}})
        assert full["_meta"]["replayed"] is True
        load = collect_session_updates(_ordinary_frames(probe))
        assert {update["_meta"]["turnId"] for update in load} == {"case-0", "historical-turn", "cancelled-turn"}
        user_chunks = [update["content"]["text"] for update in load if update["sessionUpdate"] == "user_message_chunk"]
        assert user_chunks == ["fixture:input_context", "fixture:stream_wait"]
        assert any(update["sessionUpdate"] == "agent_message_chunk" and update["_meta"]["turnId"] == "historical-turn"
                   for update in load)
        assert all("deviceDelegation" not in update["_meta"] for update in load)
        assert any(update.get("status") == "completed" for update in load)

        cursor = {**params, "_meta": {"replayFromTurnId": "case-0"}}
        assert (await probe.request("session/resume", cursor))["_meta"]["replayFromTurnId"] == "case-0"
        assert collect_session_updates(_ordinary_frames(probe)) == load
        with pytest.raises(RpcError, match="REPLAY_CURSOR_NOT_FOUND"):
            await probe.request("session/resume", {**params, "_meta": {"replayFromTurnId": "historical-turn"}})
        assert collect_session_updates(_ordinary_frames(probe)) == []
        assert _model_requests(root) == before

        await probe.request("session/resume", params)
        assert (await probe.session_prompt(retained_id, "continue normally", meta={"turnId": "continued-turn"}))["stopReason"] == "end_turn"
        _ordinary_frames(probe)
        continuation = _model_requests(root)[-1]
        assert continuation["tools"] == trace["new_sessions"][0]["model"][0]["tools"]
        restored = str(continuation["messages"])
        assert "legacy ACP context marker" in restored
        assert "counted 1" in restored
        assert "UNREAD_DEVICE_RESULT" not in restored
        assert [row for row in _records(path) if row["type"].startswith("device/")] == [
            row for row in saved if row["type"].startswith("device/")
        ]
        trace["replay"] = load
        trace["continuation"] = continuation
        trace["journal"] = [{key: value for key, value in row.items() if key not in {"time", "createdAt", "cwd", "id"}}
                            for row in _records(path)]
    finally:
        await probe.stop()
    return trace


async def _cancel_legacy_prompt(probe, session_id, prompt):
    async def wait_for_stream():
        while not prompt.done():
            for update in collect_session_updates(_ordinary_frames(probe)):
                if update.get("content", {}).get("text") == "STREAM_WAIT_READY":
                    return
            await asyncio.sleep(0.01)
        pytest.fail(f"Prompt completed before its cancellation: {prompt.result()!r}")

    await asyncio.wait_for(wait_for_stream(), timeout=10)
    await probe.notify("session/cancel", {"sessionId": session_id, "_meta": {"turnId": "unrecognized-legacy-meta"}})
    response = await asyncio.wait_for(asyncio.shield(prompt), timeout=5)
    assert response["stopReason"] == "cancelled"
    return response["stopReason"]


@pytest.mark.asyncio
async def test_legacy_acp_without_supported_device_version(tmp_path):
    await _exercise_legacy_host(tmp_path)


if __name__ == "__main__":
    _start_fixture()
