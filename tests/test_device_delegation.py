import asyncio
from copy import deepcopy

import pytest
from acp import RequestError
from box_agent.auth import scoped_request_org
from box_agent.device_delegation import DeviceDelegations, DeviceTool


def target(device="d", session="s", turn="t"):
    return {"deviceId": device, "sessionId": session, "turnId": turn}


def bind_wait_targets(broker, *targets):
    for index, identity in enumerate(targets, 1):
        broker.save({**identity, "operationId": f"wait-fixture-{index}", "parentTurnId": "prior-turn",
                     "status": "accepted", "seq": 0})


def approval():
    return {"approvalRequestId": "approval-1", "title": "Write a file",
            "expiresAt": "2031-01-01T00:00:00Z", "decisions": ["allow_once", "deny"]}


class Log:
    def __init__(self):
        self.events = []
    def flush(self):
        pass
    def append(self, kind, data, **kwargs):
        self.events.append({"type": kind, "data": deepcopy(data)})


@pytest.mark.asyncio
async def test_pending_wake_uses_parent_turn_or_resume_identity(tmp_path):
    from box_agent.session_log import SessionLog

    async def request(*args):
        return {}

    log = SessionLog.create(tmp_path / "logs", session_id="parent", cwd=tmp_path)
    broker = DeviceDelegations("cloud", request, lambda: "parent", log)
    try:
        broker.save({**target(), "operationId": "op", "parentTurnId": "parent",
                     "status": "accepted", "seq": 0})
        broker.end("parent")
        broker.bind_turn_org("parent", "origin-team")
        def event(seq, status):
            return {"version": 1, "sessionId": "cloud", "event": {
                **target(), "operationId": "op", "seq": seq, "status": status}}
        broker.event(event(2, "succeeded"))
        broker.event(event(2, "succeeded"))
        reopened_gap = DeviceDelegations("cloud", request, lambda: "next", log)
        reopened_gap.bind_pending_org("resume-team", replace=True)
        reopened_gap.event(event(1, "running"))
        assert reopened_gap.next_wake() is not None
        assert reopened_gap.wake_org(reopened_gap.next_wake()) == "resume-team"
        broker.event(event(1, "running"))
        broker.bind_pending_org("finishing-team")
        turn = broker.next_wake()
        assert turn is not None
        assert broker.wake_org(turn) == "origin-team"
        # Org is not a durable session default; reopening must select it anew.
        assert all("origin-team" not in str(e["data"]) for e in log.events)
        restored = DeviceDelegations("cloud", request, lambda: "next", log)
        restored.bind_pending_org("", replace=True)
        assert restored.next_wake() is not None
        assert restored.wake_org(restored.next_wake()) == ""
        broker.confirm_receipts({"device": {broker._cursor_key(target()): 2}})
        assert broker.next_wake() is None
    finally:
        await broker.aclose()
        log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,session_id", [
    ("session.start", None), ("session.send", "local-9"),
])
async def test_device_turn_keeps_parent_org_and_restores_without_reexecution(operation, session_id):
    calls = []
    async def request(method, params):
        calls.append((method, params))
        return {"deviceId": "FixtureDesktopA", "sessionId": "local-9", "turnId": "t1", "status": "accepted"}
    log = Log()
    broker = DeviceDelegations("cloud-3", request, lambda: "parent-1", log)
    with scoped_request_org("origin-team"):
        args = {"deviceId": "FixtureDesktopA", "instruction": "write"}
        if session_id is not None:
            args["sessionId"] = session_id
        result = await DeviceTool(broker, operation).invoke(args)
    assert result.success
    op = calls[0][1]["operationId"]
    assert calls[0][0] == "device/request"
    assert calls[0][1]["operation"] == operation
    event = {"version": 1, "sessionId": "cloud-3", "event": {
        "operationId": op, "deviceId": "FixtureDesktopA", "sessionId": "local-9", "turnId": "t1", "seq": 1, "status": "succeeded", "result": {"text": "ok"}}}
    assert broker.event(event) == {"recorded": True}
    broker.end("parent-1")
    turn = broker.next_wake()
    assert turn is not None and broker.wake_org(turn) == "origin-team"
    count = len(log.events)
    broker.event(event)
    assert len(log.events) == count
    restored = DeviceDelegations("cloud-3", request, lambda: "parent-2", log)
    assert restored.operations[op]["status"] == "succeeded"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_before_start_receipt_cancels_only_original_child_turn():
    gate = asyncio.Event()
    calls = []
    turn = "parent-1"
    async def request(method, params):
        calls.append(params)
        if params["operation"] == "session.start":
            await gate.wait()
        return {"deviceId": "FixtureDesktopA", "sessionId": "local-9", "turnId": "t1", "status": "accepted"}
    broker = DeviceDelegations("cloud-3", request, lambda: turn)
    pending = asyncio.create_task(broker.start("FixtureDesktopA", "wait"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    broker.stop("parent-1")
    turn = "parent-2"
    gate.set()
    result = await pending
    await asyncio.wait_for(asyncio.gather(*broker.pending), 5)
    assert calls[-1]["operation"] == "session.cancel"
    assert calls[-1]["operationId"] != result["operationId"]
    assert calls[-1]["args"] == {"sessionId": "local-9", "turnId": "t1"}
    assert broker.operations[result["operationId"]]["status"] == "accepted"
    assert broker.operations[result["operationId"]]["parentTurnId"] == "parent-1"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_and_wrong_event_identity_are_rejected():
    async def request(*args):
        return {"deviceId": "d", "sessionId": "s", "turnId": "t", "status": "accepted"}
    broker = DeviceDelegations("p", request, lambda: "turn")
    assert not (await DeviceTool(broker, "session.start").invoke({"deviceId": "d"})).success
    result = await broker.start("d", "work")
    with pytest.raises(ValueError, match="identity mismatch"):
        broker.event({"version": 1, "sessionId": "p", "event": {"operationId": result["operationId"], "deviceId": "other"}})

@pytest.mark.asyncio
async def test_acp_device_tools_are_session_opt_in_and_events_use_request_receipts(tmp_path):
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, ToolsConfig, LLMConfig, AgentConfig
    from examples.device_parent_demo import DeterministicLLM

    class Conn:
        async def ext_method(self, method, params):
            return {"deviceId": params["deviceId"], "sessionId": "local", "turnId": "child", "status": "accepted"}
        async def session_update(self, *args, **kwargs):
            pass
    adapter = BoxACPAgent(Conn(), Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path))), DeterministicLLM(), [], "test")
    try:
        plain = await adapter.new_session(str(tmp_path), [])
        enabled = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        assert "session_start" not in adapter._sessions[plain.session_id].agent.tools
        state = adapter._sessions[enabled.session_id]
        assert "session_start" in state.agent.tools
        assert "session_cancel" in state.agent.tools
        expected = {"device_list", "session_list", "session_read", "session_start",
                    "session_send", "session_get_turn", "session_wait", "session_cancel"}
        assert expected <= state.agent.tools.keys()
        assert not expected.intersection(adapter._sessions[plain.session_id].agent.tools)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["encode", "fsync"])
async def test_acp_device_event_waits_for_worker_checkpoint_and_acknowledges_durable_order(
    tmp_path, monkeypatch, barrier,
):
    import threading

    import box_agent.session_log as session_log
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Connection:
        async def ext_method(self, *args):
            pytest.fail("recording progress should not issue another device request")

        async def session_update(self, *args, **kwargs):
            pass

    loop = asyncio.get_running_loop()
    worker_blocked = asyncio.Event()
    release = threading.Event()
    original_encode = session_log._encode_record
    original_fsync = session_log._timed_fsync
    loop_thread = threading.get_ident()

    def pause_worker():
        loop.call_soon_threadsafe(worker_blocked.set)
        assert release.wait(10)

    def encode(record):
        if record.get("type") == "assistant/chunk":
            assert threading.get_ident() != loop_thread
            if barrier == "encode":
                pause_worker()
        if record.get("type") == "device/state":
            assert asyncio.get_running_loop() is loop
        return original_encode(record)

    def fsync(fd):
        if barrier == "fsync" and threading.get_ident() != loop_thread:
            pause_worker()
        original_fsync(fd)

    config = Config(agent=AgentConfig(workspace_dir=str(tmp_path)), tools=ToolsConfig(),
                    llm=LLMConfig(api_key="test"))
    adapter = BoxACPAgent(Connection(), config, DeterministicLLM(), [], "test")
    tasks = []
    try:
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        broker = adapter._sessions[session.session_id].device_delegations
        log = broker.log
        bind_wait_targets(broker, target())
        operation_id = next(iter(broker.operations))
        next_seq = log.event_count
        monkeypatch.setattr(session_log, "_encode_record", encode)
        monkeypatch.setattr(session_log, "_timed_fsync", fsync)

        def checkpoint():
            log.append("assistant/chunk", {"turn": 1, "step": 1, "kind": "text", "content": "checkpoint"})
            log.flush()

        worker = asyncio.create_task(session_log.run_session_write(log, checkpoint))
        tasks.append(worker)
        await asyncio.wait_for(worker_blocked.wait(), 5)
        device_event = asyncio.create_task(adapter.ext_method("device/event", {
            "version": 1, "sessionId": session.session_id, "event": {
                **target(), "operationId": operation_id, "seq": 1, "status": "running",
            },
        }))
        tasks.append(device_event)
        await asyncio.sleep(0)
        assert not device_event.done()
        assert broker.operations[operation_id]["status"] == "accepted"

        release.set()
        assert await asyncio.wait_for(device_event, 5) == {"recorded": True}
        await asyncio.wait_for(worker, 5)
        assert log._durable_event_count == log.event_count
        path = log.path
        log_id = log.header["id"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.aclose()

    reopened = session_log.SessionLog.open(path.parent.parent, session_id=log_id, cwd=tmp_path)
    try:
        assert [(event["seq"], event["type"]) for event in reopened.events[-2:]] == [
            (next_seq, "assistant/chunk"), (next_seq + 1, "device/state"),
        ]
        assert reopened.events[-1]["data"]["status"] == "running"
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["unique", "named", "ambiguous", "missing"])
async def test_real_parent_discovers_and_targets_registered_device_name(tmp_path, selection):
    import json
    from uuid import uuid4
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, ToolsConfig, LLMConfig, AgentConfig
    from examples.device_parent_demo import DeterministicLLM
    calls = []
    device_id = "FixtureDesktop-" + uuid4().hex[:8]
    eligible = {"online": True, "capabilities": ["session.execute"], "authorization": {"execute": True}}
    earlier_devices = [
        {**eligible, "deviceId": "OfflineFixture", "online": False},
        {**eligible, "deviceId": "ReadOnlyFixture", "authorization": {"execute": False}},
        {**eligible, "deviceId": "HistoryFixture", "capabilities": ["session.history"]},
    ]
    if selection in {"named", "ambiguous"}:
        earlier_devices.append({**eligible, "deviceId": "AnotherFixture"})

    class Conn:
        async def ext_method(self, method, params):
            calls.append((method, params))
            if params["operation"] == "list":
                if not params["args"]:
                    return {"devices": earlier_devices, "nextCursor": "page-2"}
                assert params["args"] == {"cursor": "page-2"}
                return {"devices": [{**eligible, "deviceId": device_id}]}
            assert params["deviceId"] == device_id
            return {**target(device_id, "local", "child"), "status": "accepted"}
        async def session_update(self, *args, **kwargs):
            pass
    adapter = BoxACPAgent(Conn(), Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path))), DeterministicLLM(), [], "test")
    try:
        response = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        sid = response.session_id
        prompt = {"instruction": "write"}
        if selection == "named":
            prompt["deviceName"] = device_id
        elif selection == "missing":
            prompt["deviceName"] = device_id.lower()
        await asyncio.wait_for(adapter.prompt(sid, [text_block(json.dumps(prompt))]), 20)
        operations = [params["operation"] for _, params in calls]
        if selection in {"ambiguous", "missing"}:
            assert operations == ["list", "list"]
            return
        assert operations == ["list", "list", "session.start"]
        assert calls[-1][1]["args"] == {"instruction": "write"}
        op = calls[-1][1]["operationId"]
        assert await adapter.ext_method("device/event", {"version": 1, "sessionId": sid, "event": {
            **target(device_id, "local", "child"), "operationId": op, "seq": 1,
            "status": "succeeded", "result": {"text": "ok"}}}) == {"recorded": True}
    finally:
        await adapter.aclose()

@pytest.mark.asyncio
async def test_cancelled_parent_tool_still_processes_late_start_receipt():
    gate = asyncio.Event()
    started = asyncio.Event()
    calls = []
    async def request(method, params):
        calls.append(params)
        if params["operation"] == "session.start":
            started.set()
            await gate.wait()
        return {"deviceId": "d", "sessionId": "s", "turnId": "old", "status": "accepted"}
    broker = DeviceDelegations("p", request, lambda: "old-parent")
    task = asyncio.create_task(broker.start("d", "wait"))
    await started.wait()
    broker.stop("old-parent")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    await asyncio.gather(*broker.pending)
    assert calls[-1]["operation"] == "session.cancel"
    assert calls[-1]["args"]["turnId"] == "old"

@pytest.mark.asyncio
async def test_stdio_parent_emits_id_bearing_device_request_and_acknowledges_event(tmp_path):
    import json
    import sys
    from uuid import uuid4
    device_id = "StdioDesktop-" + uuid4().hex[:8]
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "examples.device_parent_demo", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    async def send(payload):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", **payload}) + "\n").encode())
        await proc.stdin.drain()
    async def until(predicate):
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 20)
            if not line:
                raise AssertionError((await proc.stderr.read()).decode(errors="replace"))
            msg = json.loads(line)
            if predicate(msg):
                return msg
    try:
        await send({"id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        assert "result" in await until(lambda m: m.get("id") == 1)
        await send({"id": 2, "method": "session/new", "params": {"cwd": str(tmp_path), "mcpServers": [], "_meta": {"device": {"version": 1}}}})
        response = await until(lambda m: m.get("id") == 2)
        sid = response["result"]["sessionId"]
        await send({"id": 3, "method": "session/prompt", "params": {"sessionId": sid, "prompt": [{"type": "text", "text": '{"instruction":"write"}'}]}})
        discovery = await until(lambda m: m.get("method") == "_device/request")
        assert discovery["params"]["operation"] == "list"
        assert "deviceId" not in discovery["params"]
        await send({"id": discovery["id"], "result": {"devices": [{
            "deviceId": device_id, "online": True, "authorization": {"execute": True},
            "capabilities": ["session.execute"],
        }]}})
        request = await until(lambda m: m.get("method") == "_device/request")
        assert "id" in request
        assert request["params"]["operation"] == "session.start"
        assert request["params"]["deviceId"] == device_id
        await send({"id": request["id"], "result": {**target(device_id, "local", "child"), "status": "accepted"}})
        await until(lambda m: m.get("id") == 3)
        await send({"id": 4, "method": "_device/event", "params": {"version": 1, "sessionId": sid, "event": {
            **target(device_id, "local", "child"), "operationId": request["params"]["operationId"],
            "seq": 1, "status": "succeeded", "result": {"text": "ok"},
        }}})
        assert (await until(lambda m: m.get("id") == 4))["result"] == {"recorded": True}
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

@pytest.mark.asyncio
async def test_real_parent_stop_targets_direct_child_and_waits_for_cancelled_event(tmp_path):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, ToolsConfig, LLMConfig, AgentConfig
    from examples.device_parent_demo import DeterministicLLM
    started = asyncio.Event()
    cancel_dispatched = asyncio.Event()
    remote_release = asyncio.Event()
    calls = []
    class Conn:
        async def ext_method(self, method, params):
            calls.append(params)
            if params["operation"] == "session.cancel":
                cancel_dispatched.set()
                await remote_release.wait()
            else:
                started.set()
            return {"deviceId": "FixtureDesktopA", "sessionId": "local", "turnId": "child", "status": "accepted"}
        async def session_update(self, *args, **kwargs):
            pass
    adapter = BoxACPAgent(Conn(), Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path))), DeterministicLLM(), [], "test")
    try:
        response = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        sid = response.session_id
        task = asyncio.create_task(adapter.prompt(sid, [text_block('{"deviceId":"FixtureDesktopA","instruction":"wait","hold":true}')]))
        await asyncio.wait_for(started.wait(), 20)
        turn_id = calls[0]["parentTurnId"]
        accepted = await asyncio.wait_for(
            adapter.ext_method("device/cancel_turn", {"sessionId": sid, "turnId": turn_id}), 1,
        )
        assert accepted == {"accepted": True}
        assert (await asyncio.wait_for(task, 20)).stop_reason == "cancelled"
        await asyncio.wait_for(cancel_dispatched.wait(), 5)
        assert not remote_release.is_set()
        assert [c["operation"] for c in calls] == ["session.start", "session.cancel"]
        broker = adapter._sessions[sid].device_delegations
        op = calls[0]["operationId"]
        assert broker.operations[op]["status"] == "accepted"
        broker.event({"version": 1, "sessionId": sid, "event": {"operationId": op, "deviceId": "FixtureDesktopA", "sessionId": "local", "turnId": "child", "seq": 1, "status": "cancelled"}})
        assert broker.operations[op]["status"] == "cancelled"
        assert broker.operations[op]["parentTurnId"] in broker.stopped_turns
        assert broker.next_wake() is None
        assert broker.request_injections([]) == ()
    finally:
        remote_release.set()
        await adapter.aclose()


@pytest.mark.asyncio
async def test_deterministic_child_uses_real_file_tool_in_session_workspace(tmp_path):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, ToolsConfig, LLMConfig, AgentConfig
    from examples.device_parent_demo import DeterministicLLM
    class Conn:
        async def session_update(self, *args, **kwargs):
            pass
    adapter = BoxACPAgent(Conn(), Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path))), DeterministicLLM(), [], "test")
    try:
        response = await adapter.new_session(str(tmp_path), [])
        await asyncio.wait_for(adapter.prompt(response.session_id, [text_block('write result.txt')]), 20)
        assert (tmp_path / "result.txt").read_text() == "ok"
    finally:
        await adapter.aclose()

@pytest.mark.asyncio
async def test_real_session_log_restores_device_state_and_stop_intent(tmp_path):
    from box_agent.session_log import SessionLog
    async def request(method, params):
        return {"deviceId": "d", "sessionId": "s", "turnId": "child", "status": "accepted"}
    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="parent", cwd=tmp_path)
    broker = DeviceDelegations("parent-acp", request, lambda: "turn-1", log)
    result = await broker.start("d", "wait")
    broker.stop("turn-1")
    await asyncio.wait_for(asyncio.gather(*broker.pending), 5)
    log.close()
    reopened = SessionLog.open(root, session_id="parent", cwd=tmp_path)
    try:
        restored = DeviceDelegations("parent-acp", request, lambda: "turn-2", reopened)
        record = restored.operations[result["operationId"]]
        assert record["turnId"] == "child"
        assert record["cancelRequested"] is True
        assert "turn-1" in restored.stopped_turns
        assert record["status"] == "accepted"
    finally:
        reopened.close()

@pytest.mark.asyncio
async def test_events_before_acceptance_preserve_terminal_state_and_reject_late_progress():
    broker = None
    async def request(method, params):
        broker.event({"version": 1, "sessionId": "p", "event": {
            "operationId": params["operationId"], "deviceId": "d", "sessionId": "s", "turnId": "t", "seq": 2, "status": "succeeded", "result": {"text": "ok"}}})
        return {"deviceId": "d", "sessionId": "s", "turnId": "t", "status": "accepted"}
    broker = DeviceDelegations("p", request, lambda: "parent")
    result = await broker.start("d", "work")
    op = result["operationId"]
    assert broker.operations[op]["status"] == "accepted"
    for seq in (1, 2, 3):
        broker.event({"version": 1, "sessionId": "p", "event": {
            "operationId": op, "deviceId": "d", "sessionId": "s", "turnId": "t", "seq": seq, "status": "running", "parentTurnId": "forged"}})
    assert broker.operations[op]["status"] == "succeeded"
    assert broker.operations[op]["parentTurnId"] == "parent"
    assert broker.operations[op]["seq"] == 2

@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments,response", [
    ("list", {"cursor": "page-1", "limit": 2}, {"devices": [{"deviceId": "view-only", "online": False, "capabilities": ["session.history"], "authorization": {"view": True, "execute": False}}], "nextCursor": "page-2"}),
    ("session.list", {"deviceId": "d", "cursor": "p1", "limit": 1}, {"sessions": [{"sessionId": "s", "title": "authorized", "workspaceDir": "/original", "status": "idle"}], "nextCursor": "p2"}),
    ("session.read", {"deviceId": "d", "sessionId": "s", "limit": 1}, {"sessionId": "s", "messages": [{"role": "user", "content": "hello", "turnId": "t"}], "nextCursor": "p2"}),
    ("session.get_turn", target(), {**target(), "status": "running", "deviceOnline": True, "source": "device", "observedAt": "2026-11-18T10:00:00Z", "seq": 1}),
    ("session.wait", {"targets": [target()], "timeoutMs": 0}, {"results": [], "timedOut": True, "states": [{**target(), "status": "running", "source": "device"}]}),
])
async def test_query_tools_route_valid_arguments_and_deliver_full_response(operation, arguments, response):
    calls = []
    async def request(method, params):
        calls.append((method, params))
        return response
    broker = DeviceDelegations("parent", request, lambda: "turn")
    if operation == "session.wait":
        bind_wait_targets(broker, *arguments["targets"])
    tool = DeviceTool(broker, operation)
    result = await tool.invoke(arguments)
    assert result.success
    assert result.raw_output == response
    import json
    assert json.loads(result.content) == response
    assert calls[0][0] == "device/request"
    payload = calls[0][1]
    assert payload["operation"] == operation
    assert payload["sessionId"] == "parent"
    assert payload["operationId"]
    assert payload["args"] == {k: v for k, v in arguments.items() if k != "deviceId"}
    assert broker.consumed == {}
    assert len(calls) == 1  # wait timeout never sends cancellation


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [
    ("list", {"limit": 0}), ("session.list", {}),
    ("session.read", {"deviceId": "d"}),
    ("session.start", {"deviceId": "d", "instruction": ""}),
    ("session.start", {"deviceId": "d", "instruction": "  "}),
    ("session.start", {"deviceId": "d", "instruction": "x" * 4097}),
    ("session.send", {"deviceId": "d", "instruction": "hi"}),
    ("session.get_turn", {"operationId": 1}),
    ("session.wait", {"targets": [{**target(), "afterSeq": 1}]}),
    ("session.cancel", {"operationId": "x", "turnId": "untrusted"}),
])
async def test_each_device_tool_validates_before_rpc(operation, arguments):
    async def request(*args):
        pytest.fail("invalid arguments reached transport")
    result = await DeviceTool(DeviceDelegations("p", request, lambda: "t"), operation).invoke(arguments)
    assert not result.success
    assert result.raw_output["code"] == "INVALID_TOOL_ARGUMENTS"


@pytest.mark.asyncio
async def test_send_tracks_new_child_turn_and_get_carries_exact_local_identity():
    calls = []
    async def request(method, params):
        calls.append(params)
        if params["operation"] == "session.send":
            return {"deviceId": "d", "sessionId": "original", "turnId": "new-turn", "status": "accepted"}
        return {"deviceId": "d", "sessionId": "original", "turnId": "new-turn", "status": "running", "seq": 1, "deviceOnline": True, "source": "device", "observedAt": "now"}
    broker = DeviceDelegations("p", request, lambda: "parent-turn")
    sent = await DeviceTool(broker, "session.send").invoke({"deviceId": "d", "sessionId": "original", "instruction": "continue"})
    assert sent.success
    assert "operationId" not in sent.raw_output
    op = calls[0]["operationId"]
    assert calls[0]["args"] == {"sessionId": "original", "instruction": "continue"}
    got = await DeviceTool(broker, "session.get_turn").invoke(target("d", "original", "new-turn"))
    assert got.raw_output["source"] == "device"
    assert calls[1]["deviceId"] == "d"
    assert calls[1]["args"] == {"sessionId": "original", "turnId": "new-turn"}
    assert broker.operations[op]["parentTurnId"] == "parent-turn"


@pytest.mark.asyncio
async def test_discovery_ignores_unknown_capability_groups():
    async def request(*args):
        return {"devices": [{"deviceId": "d", "capabilities": [
            "session.history", "session.execute", "future.capability", "artifact.upload",
        ]}]}

    broker = DeviceDelegations("p", request, lambda: "t")
    result = await DeviceTool(broker, "list").invoke({})
    assert result.success
    assert result.raw_output["devices"][0]["capabilities"] == [
        "session.history", "session.execute", "artifact.upload",
    ]


@pytest.mark.asyncio
async def test_send_busy_and_unauthorized_history_remain_tool_failures():
    class BusyError(Exception):
        code = -32001
    async def request(method, params):
        if params["operation"] == "session.send":
            raise BusyError("SESSION_BUSY")
        return {"error": {"code": "FORBIDDEN", "message": "unauthorized history"}}
    broker = DeviceDelegations("p", request, lambda: "t")
    busy = await DeviceTool(broker, "session.send").invoke({"deviceId": "d", "sessionId": "s", "instruction": "continue"})
    assert not busy.success and "SESSION_BUSY" in busy.error
    assert busy.raw_output["code"] == -32001
    forbidden = await DeviceTool(broker, "session.read").invoke({"deviceId": "d", "sessionId": "s"})
    assert not forbidden.success and "FORBIDDEN" in forbidden.error


@pytest.mark.asyncio
async def test_consumption_suppresses_push_but_repeated_wait_returns_same_result():
    from box_agent.schema import Message
    calls = []
    result = {"operationId": "op", "deviceId": "d", "sessionId": "s", "turnId": "t", "status": "succeeded", "seq": 9, "complete": True, "source": "device", "result": {"text": "full output"}}
    async def request(method, params):
        calls.append(params)
        return result if params["operation"] == "session.get_turn" else {"results": [result], "timedOut": False}
    broker = DeviceDelegations("p", request, lambda: "parent")
    got = await DeviceTool(broker, "session.get_turn").invoke(target())
    broker.save({**result, "parentTurnId": "parent"})
    assert broker.request_injections([])
    assert not broker.request_injections([
        Message(role="tool", content=got.content, tool_call_id="full"),
    ])
    assert broker.request_injections([
        Message(role="tool", content=got.content[:20], tool_call_id="preview"),
    ])
    assert broker.consumed == {}
    broker.confirm_receipts(broker.response_receipts([Message(role="tool", content=got.content[:20], tool_call_id="short")]))
    assert broker.consumed == {}
    broker.confirm_receipts(broker.response_receipts([Message(role="tool", content=got.content, tool_call_id="full")]))
    assert broker._consumed_seq(target()) == 9
    for _ in range(2):
        reply = await DeviceTool(broker, "session.wait").invoke({"targets": [target()], "timeoutMs": 0})
        assert reply.raw_output["results"][0]["status"] == "succeeded"
        assert reply.raw_output["results"][0]["result"] == result["result"]
    assert calls[-1]["args"]["targets"] == [target()]
    assert broker.request_injections([]) == ()
    assert all(c["operation"] != "session.cancel" for c in calls)


@pytest.mark.asyncio
async def test_incomplete_tool_result_is_not_consumed_even_after_model_delivery():
    from box_agent.schema import Message
    async def request(*args):
        return {**target(), "status": "succeeded", "seq": 2, "complete": False, "source": "device", "result": "preview"}
    broker = DeviceDelegations("p", request, lambda: "t")
    result = await DeviceTool(broker, "session.get_turn").invoke(target())
    assert broker.offered == {}
    broker.confirm_receipts(broker.response_receipts([Message(role="tool", content=result.content, tool_call_id="x")]))
    assert broker.consumed == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args,device_id,response", [
    ("list", {}, None, {"devices": []}),
    ("session.list", {"limit": 1}, "d", {"sessions": [], "nextCursor": "p2"}),
    ("session.read", {"sessionId": "existing", "limit": 1}, "d", {"sessionId": "existing", "messages": []}),
    ("session.start", {"instruction": "write"}, "d", {"deviceId": "d", "sessionId": "existing", "turnId": "child", "status": "accepted"}),
    ("session.send", {"sessionId": "existing", "instruction": "continue"}, "d", {"deviceId": "d", "sessionId": "existing", "turnId": "child", "status": "accepted"}),
    ("session.get_turn", {"sessionId": "existing", "turnId": "child"}, "d", {"operationId": "op-existing", "deviceId": "d", "sessionId": "existing", "turnId": "child", "seq": 2, "status": "succeeded", "complete": True, "result": {"text": "ok"}, "source": "device", "deviceOnline": True, "observedAt": "now"}),
    ("session.wait", {"targets": [target("d", "existing", "child")], "timeoutMs": 0}, None, {"results": [], "timedOut": True, "states": [{**target("d", "existing", "child"), "status": "running", "source": "device"}]}),
    ("session.cancel", {"sessionId": "existing", "turnId": "child"}, "d", {"deviceId": "d", "sessionId": "existing", "turnId": "child", "status": "accepted"}),
])
async def test_each_device_operation_runs_through_real_parent_model_dispatch(tmp_path, operation, args, device_id, response):
    import json
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, ToolsConfig, LLMConfig, AgentConfig
    from examples.device_parent_demo import DeterministicLLM
    calls = []
    class Conn:
        async def ext_method(self, method, params):
            calls.append((method, params))
            return response
        async def session_update(self, *args, **kwargs):
            pass
    adapter = BoxACPAgent(Conn(), Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"), agent=AgentConfig(workspace_dir=str(tmp_path))), DeterministicLLM(), [], "test")
    try:
        created = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        state = adapter._sessions[created.session_id]
        state.device_delegations.save({"operationId": "op-existing", "deviceId": "d", "sessionId": "existing", "turnId": "child", "parentTurnId": "older-parent", "seq": 0, "status": "accepted"})
        payload = {"operation": operation, "args": args}
        if device_id:
            payload["deviceId"] = device_id
        await asyncio.wait_for(adapter.prompt(created.session_id, [text_block(json.dumps(payload))]), 20)
        assert len(calls) == 1
        assert calls[0][0] == "device/request"
        assert calls[0][1]["operation"] == operation
        assert calls[0][1].get("deviceId") == ("d" if operation == "session.wait" else device_id)
        if operation == "session.get_turn":
            assert state.device_delegations._consumed_seq(target("d", "existing", "child")) == 2
    finally:
        await adapter.aclose()

@pytest.mark.asyncio
async def test_consumed_sequences_restore_and_do_not_regress_on_old_results(tmp_path):
    import json
    from box_agent.session_log import SessionLog
    from box_agent.schema import Message
    async def request(*args):
        return {"results": [], "timedOut": True}
    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="parent", cwd=tmp_path)
    broker = DeviceDelegations("p", request, lambda: "parent", log)
    messages = []
    responses = []
    for seq in (4, 9, 4):
        result = {"operationId": "op", "status": "succeeded", "seq": seq, "complete": True, "result": "full"}
        content = json.dumps(result)
        broker.offer(content, result)
        message = Message(role="tool", content=content, tool_call_id=str(seq))
        messages.append(message)
        receipts = broker.response_receipts([message])
        if receipts:
            response = Message(role="assistant", content=f"Response for {seq}")
            log.mark_runtime_input_receipts(response, receipts)
            log.append_unlogged_messages(
                [*responses, response], turn=1, step=seq,
            )
            log.flush()
            responses.append(response)
            broker.confirm_receipts(receipts)
    assert broker.consumed == {"op": 9}
    log.close()
    reopened = SessionLog.open(root, session_id="parent", cwd=tmp_path)
    try:
        restored = DeviceDelegations("p", request, lambda: "next-parent", reopened)
        assert restored.consumed == {"op": 9}
        restored.confirm_receipts(restored.response_receipts(messages))
        assert restored.consumed == {"op": 9}
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,args,response", [
    ("list", {}, {"devices": "malformed"}),
    ("session.read", {"deviceId": "d", "sessionId": "s"}, {"sessionId": "other", "messages": []}),
    ("session.get_turn", target(), {**target("other"), "status": "running"}),
    ("session.wait", {"targets": [target()]}, {"results": [], "timedOut": "false"}),
])
async def test_query_tools_reject_malformed_or_mismatched_responses(operation, args, response):
    async def request(*args):
        return response
    broker = DeviceDelegations("p", request, lambda: "t")
    if operation == "session.wait":
        bind_wait_targets(broker, *args["targets"])
    result = await DeviceTool(broker, operation).invoke(args)
    assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError, ConnectionError])
async def test_transport_failure_is_not_reported_as_offline_or_child_failure(failure):
    async def request(*args):
        raise failure("device RPC response lost")
    broker = DeviceDelegations("p", request, lambda: "t")
    result = await DeviceTool(broker, "session.get_turn").invoke(target())
    assert not result.success
    assert "DEVICE_DELIVERY_UNKNOWN" in result.error
    assert result.raw_output == {"code": -32008, "message": "DEVICE_DELIVERY_UNKNOWN",
                                 "data": {"deviceId": "d", "delivery": "unknown"}}
    assert broker.operations == {}
    assert broker.consumed == {}

@pytest.mark.asyncio
async def test_real_provider_failure_before_output_does_not_consume_runtime_injection(tmp_path):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.schema import StreamEvent
    from examples.device_parent_demo import DeterministicLLM

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            assert any("pending evidence" in str(m.content) for m in messages)
            yield StreamEvent(type="activity", activity={"phase": "provider_wait"})
            raise RuntimeError("provider failed before output")

    class Conn:
        async def ext_method(self, *args):
            return {}
        async def session_update(self, *args, **kwargs):
            pass

    config = Config(agent=AgentConfig(workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(), llm=LLMConfig(api_key="test"))
    adapter = BoxACPAgent(Conn(), config, Model(), [], "test")
    try:
        created = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        state = adapter._sessions[created.session_id]
        broker = state.device_delegations
        broker.save({**target(), "operationId": "op", "parentTurnId": "p",
                     "status": "succeeded", "seq": 2, "result": "pending evidence"})
        await adapter.prompt(created.session_id, [text_block("Review the device result.")], turnId="C2")
        assert broker.consumed == {}
        assert broker.request_injections([])
        assert not any("pending evidence" in str(m.content) for m in state.agent.messages)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_terminal_gap_survives_restart_and_wake_intent_reuses_id(tmp_path):
    from box_agent.session_log import SessionLog
    async def request(*args):
        return {}
    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="parent", cwd=tmp_path)
    broker = DeviceDelegations("p", request, lambda: "C1", log)
    broker.save({"operationId": "op", "deviceId": "d", "parentTurnId": "C1",
                 "status": "accepted", "seq": 0})
    broker.end("C1")
    def event(seq, status):
        return {"version": 1, "sessionId": "p", "event": {
            "operationId": "op", "deviceId": "d", "sessionId": "s",
            "turnId": "t", "seq": seq, "status": status}}
    terminal = event(3, "interrupted")
    assert broker.event(terminal) == {"recorded": True, "throughSeq": 0, "nextSeq": 1}
    assert broker.next_wake() is None
    log.close()
    log = SessionLog.open(root, session_id="parent", cwd=tmp_path)
    try:
        broker = DeviceDelegations("p", request, lambda: "C2", log)
        assert broker.event(terminal)["nextSeq"] == 1
        assert broker.event(event(1, "approval_required"))["nextSeq"] == 2
        assert broker.operations["op"]["status"] == "waiting_approval"
        assert broker.event(event(2, "running"))["throughSeq"] == 3
        assert broker.operations["op"]["status"] == "interrupted"
        turn = broker.next_wake()
        assert turn.startswith("device-wake-")
        assert broker.next_wake() == turn
    finally:
        log.close()
    log = SessionLog.open(root, session_id="parent", cwd=tmp_path)
    try:
        assert DeviceDelegations("p", request, lambda: "C2", log).next_wake() == turn
    finally:
        log.close()


@pytest.mark.asyncio
async def test_real_parent_wakes_once_and_stale_stop_cannot_cancel_next_turn(tmp_path, monkeypatch):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM
    from box_agent.injections import InjectionKind, MessageInjection
    seen = []
    injected = []
    original_apply = MessageInjection.apply

    def observe_injection(self, messages):
        event = original_apply(self, messages)
        if self.injection_id and self.injection_id.startswith("device-state-"):
            injected.append((event, messages[-1].source))
        return event

    monkeypatch.setattr(MessageInjection, "apply", observe_injection)
    wake_started = asyncio.Event()
    release_wake = asyncio.Event()

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            snapshot = [m.content for m in messages if isinstance(m.content, str)]
            seen.extend(snapshot)
            if any("Review the device delegation updates" in text for text in snapshot):
                wake_started.set()
                await release_wake.wait()
            async for event in super().generate_stream(messages, *args, **kwargs):
                yield event

    class Conn:
        async def ext_method(self, method, params):
            assert params["operation"] == "session.start"
            return {"deviceId": "d", "sessionId": "s", "turnId": "child", "status": "accepted"}
        async def session_update(self, *args, **kwargs):
            pass

    def open_adapter():
        return BoxACPAgent(Conn(), Config(agent=AgentConfig(workspace_dir=str(tmp_path)),
            tools=ToolsConfig(), llm=LLMConfig(api_key="test")), Model(), [], "test")

    adapter = open_adapter()
    session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="stable-parent")
    sid = session.session_id
    try:
        response = await adapter.prompt(sid, [text_block('{"deviceId":"d","instruction":"write"}')], turnId="C1")
        assert response.stop_reason == "end_turn"
        assert any('"status": "accepted"' in text for text in seen)
        state = adapter._sessions[sid]
        broker = state.device_delegations
        op = next(iter(broker.operations))
        payload = {"version": 1, "sessionId": sid, "event": {"operationId": op,
            "deviceId": "d", "sessionId": "s", "turnId": "child", "seq": 1,
            "status": "succeeded", "result": {"text": "from actual device"}}}
        assert await adapter.ext_method("device/event", payload) == {"recorded": True}
        await asyncio.wait_for(wake_started.wait(), 10)
        assert state.current_turn_id.startswith("device-wake-")
        assert await adapter.ext_method("device/cancel_turn", {"sessionId": sid, "turnId": "C1"}) == {"accepted": False}
        assert not state.cancelled
        release_wake.set()
        await asyncio.wait_for(state.device_wake_task, 10)
        assert any("from actual device" in text for text in seen)
        assert not any("from actual device" in str(m.content) for m in state.agent.messages)
        assert any(event.kind == InjectionKind.RUNTIME_STATE.value and not event.user_visible
                   and source == "runtime" for event, source in injected)
        assert broker._consumed_seq(target("d", "s", "child")) == 1
        assert len(broker.wakes) == 1
        await adapter.ext_method("device/event", payload)
        supplement = {**payload, "event": {**payload["event"], "seq": 2, "summary": "artifact retry"}}
        await adapter.ext_method("device/event", supplement)
        if state.device_wake_task:
            await state.device_wake_task
        assert broker.operations[op]["seq"] == 1
        assert len(broker.wakes) == 1
    finally:
        release_wake.set()
        await adapter.aclose()
    reopened = open_adapter()
    try:
        restored = await reopened.new_session(str(tmp_path), [], device={"version": 1}, session_id="stable-parent")
        broker = reopened._sessions[restored.session_id].device_delegations
        assert len(broker.wakes) == 1
        assert broker.next_wake() is None
    finally:
        await reopened.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled"])
async def test_reopened_parent_reports_outcome_of_durable_uncommitted_wake(tmp_path, monkeypatch, outcome):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Conn:
        def __init__(self):
            self.finished = asyncio.Event()
            self.notifications = []

        async def ext_method(self, method, params):
            return {"deviceId": "d", "sessionId": "s", "turnId": "child", "status": "accepted"}

        async def session_update(self, *args, **kwargs):
            pass

        async def ext_notification(self, method, params):
            assert method == "device/turn"
            self.notifications.append(params)
            if params["phase"] == "finished":
                self.finished.set()

    def open_adapter(conn):
        config = Config(agent=AgentConfig(workspace_dir=str(tmp_path)), tools=ToolsConfig(),
                        llm=LLMConfig(api_key="test"))
        return BoxACPAgent(conn, config, DeterministicLLM(), [], "test")

    conn = Conn()
    adapter = open_adapter(conn)
    session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="restart-parent")
    sid = session.session_id
    try:
        await adapter.prompt(sid, [text_block('{"deviceId":"d","instruction":"write"}')], turnId="C1")
        state = adapter._sessions[sid]
        if state.device_wake_task:
            await state.device_wake_task
        broker = state.device_delegations
        op = next(iter(broker.operations))
        broker.event({"version": 1, "sessionId": sid, "event": {"operationId": op,
            "deviceId": "d", "sessionId": "s", "turnId": "child", "seq": 1,
            "status": "succeeded", "result": "ready before restart"}})
        turn = broker.next_wake()
        assert not broker.log.has_committed_input(turn)
    finally:
        await adapter.aclose()
    conn = Conn()
    adapter = open_adapter(conn)
    try:
        if outcome != "completed":
            async def interrupt_wake(*args, **kwargs):
                if outcome == "cancelled":
                    raise asyncio.CancelledError()
                raise RequestError(-32000, "test wake failure", {"reason": "test interruption"})

            monkeypatch.setattr(adapter, "_prompt_for_session", interrupt_wake)
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="restart-parent")
        await asyncio.wait_for(conn.finished.wait(), 10)
        started, finished = conn.notifications
        identity = {"version": 1, "sessionId": session.session_id, "turnId": turn}
        assert started == {**identity, "phase": "started"}
        if outcome == "completed":
            assert finished == {**identity, "phase": "finished", "result": finished["result"]}
            assert finished["result"]["stopReason"] == "end_turn"
            assert "_meta" in finished["result"]
            broker = adapter._sessions[session.session_id].device_delegations
            assert broker.log.has_committed_input(turn)
            assert broker._consumed_seq(target("d", "s", "child")) == 1
        elif outcome == "cancelled":
            assert finished == {**identity, "phase": "finished", "result": {"stopReason": "cancelled"}}
        else:
            assert finished == {**identity, "phase": "finished",
                                "error": {"code": -32000, "message": "test wake failure",
                                          "data": {"reason": "test interruption"}}}
    finally:
        await adapter.aclose()

@pytest.mark.asyncio
async def test_turn_ids_scope_wait_and_cancel_to_exact_device_and_round(tmp_path):
    from box_agent.schema import Message
    from box_agent.session_log import SessionLog
    calls = []
    first = target("FixtureDesktopA", "same-session", "same-turn")
    other_device = target("FixtureDesktopB", "same-session", "same-turn")
    next_turn = target("FixtureDesktopA", "same-session", "next-turn")

    async def request(method, params):
        calls.append(params)
        operation = params["operation"]
        if operation == "session.get_turn":
            return {**first, "status": "succeeded", "seq": 7, "complete": True, "source": "device", "result": "ok"}
        if operation == "session.wait":
            if params["deviceId"] == other_device["deviceId"]:
                return {"results": [{**other_device, "status": "waiting_approval", "seq": 1,
                                     "complete": False, "source": "device", "approval": approval()}],
                        "timedOut": False}
            return {"results": [], "timedOut": True, "states": [
                {**item, "status": "running", "source": "device"} for item in params["args"]["targets"]
            ]}
        return {"accepted": False, "sessionId": first["sessionId"], "turnId": first["turnId"]}

    root = tmp_path / "logs"
    log = SessionLog.create(root, session_id="parent", cwd=tmp_path)
    broker = DeviceDelegations("cloud", request, lambda: "parent", log)
    for idx, identity in enumerate((first, other_device, next_turn)):
        broker.save({**identity, "operationId": f"private-{idx}", "seq": 0,
                     "parentTurnId": "parent", "status": "accepted"})
    result = await DeviceTool(broker, "session.get_turn").invoke(first)
    assert result.success
    assert calls[-1]["args"] == {"sessionId": first["sessionId"], "turnId": first["turnId"]}
    receipts = broker.response_receipts([Message(role="tool", content=result.content, tool_call_id="get")])
    response = Message(role="assistant", content="read")
    log.mark_runtime_input_receipts(response, receipts)
    log.append_unlogged_messages([response], turn=1, step=1)
    log.flush()
    # Crash after commit and before in-memory confirmation.
    assert broker.consumed == {}
    log.close()
    log = SessionLog.open(root, session_id="parent", cwd=tmp_path)
    try:
        broker = DeviceDelegations("cloud", request, lambda: "parent-2", log)
        wait = await DeviceTool(broker, "session.wait").invoke({"targets": [first, other_device, next_turn]})
        assert wait.success
        wait_calls = {call["deviceId"]: call["args"]["targets"] for call in calls if call["operation"] == "session.wait"}
        assert wait_calls == {first["deviceId"]: [first, next_turn], other_device["deviceId"]: [other_device]}
        assert broker._consumed_seq(first) == 7
        assert broker._consumed_seq(other_device) == 0
        for _ in range(2):
            cancelled = await DeviceTool(broker, "session.cancel").invoke(first)
            assert cancelled.success and cancelled.raw_output["accepted"] is False
        assert calls[-1] == calls[-2]
        assert calls[-1]["deviceId"] == "FixtureDesktopA"
        assert calls[-1]["args"]["turnId"] == "same-turn"
        assert calls[-1]["operationId"] != "private-0"
        assert not broker.operations["private-1"].get("cancelRequested")
        assert not broker.operations["private-2"].get("cancelRequested")
        denied = await DeviceTool(broker, "session.cancel").invoke(target("FixtureDesktopC"))
        assert not denied.success
    finally:
        log.close()

@pytest.mark.asyncio
async def test_start_rejection_leaves_no_phantom_delegation_and_unknown_retries_same_key(tmp_path):
    from acp import RequestError
    from box_agent.session_log import SessionLog

    outcomes = iter((
        RequestError(-32001, "SESSION_BUSY"),
        asyncio.TimeoutError("lost acceptance"),
        {"deviceId": "d", "sessionId": "s", "turnId": "child", "status": "accepted"},
    ))
    calls = []

    async def request(method, params):
        calls.append(params)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    root = tmp_path / "session-store"
    with_log = SessionLog.create(root, session_id="cloud", cwd=tmp_path)
    broker = DeviceDelegations("cloud", request, lambda: "C1", with_log)
    try:
        with pytest.raises(RequestError, match="SESSION_BUSY"):
            await broker.start("d", "continue", session_id="s")
        assert broker.operations[calls[0]["operationId"]]["status"] == "rejected"
        assert broker.request_injections([]) == ()
        with pytest.raises(RequestError, match="DEVICE_DELIVERY_UNKNOWN"):
            await broker.start("d", "continue", session_id="s")
        assert calls[0]["operationId"] != calls[1]["operationId"]
        assert broker.operations[calls[1]["operationId"]]["status"] == "unknown_delivery"
    finally:
        with_log.close()
    with_log = SessionLog.open(root, session_id="cloud", cwd=tmp_path)
    try:
        restored = DeviceDelegations("cloud", request, lambda: "C1", with_log)
        accepted = await restored.start("d", "continue", session_id="s")
        assert calls[1] == calls[2]
        assert accepted["turnId"] == "child"
        assert restored.operations[calls[2]["operationId"]]["parentTurnId"] == "C1"
    finally:
        with_log.close()


@pytest.mark.asyncio
async def test_delegation_provenance_requires_trusted_host_and_survives_load(tmp_path):
    from acp import RequestError, text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Conn:
        def __init__(self):
            self.updates = []

        async def session_update(self, session_id, update):
            self.updates.append(update)

    conn = Conn()
    config = Config(agent=AgentConfig(workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(), llm=LLMConfig(api_key="test"))
    adapter = BoxACPAgent(conn, config, DeterministicLLM(), [], "test")
    try:
        session = await adapter.new_session(str(tmp_path), [], session_id="child")
        sid = session.session_id
        origin = {"parentSessionId": "cloud", "operationId": "start-id", "parentTurnId": "C1"}
        with pytest.raises(RequestError, match="DEVICE_DELEGATION_RECEIVER_REQUIRED"):
            await adapter.prompt(sid, [text_block("same text")], turnId="first", deviceDelegation=origin)
        await adapter.initialize(1, deviceDelegationReceiver={"version": 1})
        with pytest.raises(RequestError, match="INVALID_DEVICE_DELEGATION"):
            await adapter.prompt(sid, [text_block("same text")], turnId="first", deviceDelegation={})
        await adapter.prompt(sid, [text_block("same text")], turnId="first", deviceDelegation=origin)
        with pytest.raises(RequestError, match="DEVICE_DELEGATION_TURN_MISMATCH"):
            await adapter.prompt(sid, [text_block("same text")], turnId="forged-rebind", deviceDelegation=origin)
        with pytest.raises(ValueError, match="TURN_ALREADY_ACCEPTED"):
            await adapter.prompt(sid, [text_block("same text")], turnId="first", deviceDelegation=origin)
        await adapter.prompt(sid, [text_block("same text")], turnId="manual")
        history = adapter._sessions[sid].agent.session_log.history_messages()
        sources = {m.input_facts["id"]: m.source for _, m, _ in history if m.input_facts}
        assert sources == {"first": "runtime", "manual": "user"}
        conn.updates.clear()
        await adapter.load_session(str(tmp_path), sid)
        inputs = [update.field_meta for update in conn.updates if update.session_update == "user_message_chunk"]
        assert inputs[0]["inputKind"] == "device_delegation"
        assert inputs[0]["inputSource"] == "runtime"
        assert inputs[0]["deviceDelegation"] == origin
        assert "inputKind" not in inputs[1]
        conn.updates.clear()
        await adapter.resume_session(str(tmp_path), sid, replayFromTurnId="first")
        resumed = [update.field_meta for update in conn.updates if update.session_update == "user_message_chunk"]
        assert resumed[0]["inputKind"] == "device_delegation"
        assert resumed[0]["turnId"] == "first"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_restart_after_wake_input_commit_before_model_response_reconciles_once(tmp_path):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    inflight = asyncio.Event()
    original_requests = []
    recovery_requests = []

    class BlockWakeModel(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            if any((m.input_facts or {}).get("kind") == "device_wake" for m in messages):
                original_requests.append(messages)
                inflight.set()
                await asyncio.Event().wait()
            async for event in super().generate_stream(messages, *args, **kwargs):
                yield event

    class RecoveryModel(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            recovery_requests.append(messages)
            async for event in super().generate_stream(messages, *args, **kwargs):
                yield event

    class Conn:
        def __init__(self):
            self.finished = asyncio.Event()
            self.updates = []

        async def ext_method(self, method, params):
            assert params["operation"] == "session.start"
            return {"deviceId": "d", "sessionId": "s", "turnId": "child", "status": "accepted"}

        async def ext_notification(self, method, params):
            if params["phase"] == "finished":
                self.finished.set()

        async def session_update(self, session_id, update):
            self.updates.append(update)

    config = Config(agent=AgentConfig(workspace_dir=str(tmp_path)),
                    tools=ToolsConfig(), llm=LLMConfig(api_key="test"))
    adapter = BoxACPAgent(Conn(), config, BlockWakeModel(), [], "test")
    try:
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="crash-parent")
        sid = session.session_id
        await adapter.prompt(sid, [text_block('{"deviceId":"d","instruction":"work"}')], turnId="C1")
        broker = adapter._sessions[sid].device_delegations
        op = next(iter(broker.operations))
        await adapter.ext_method("device/event", {"version": 1, "sessionId": sid, "event": {
            **target("d", "s", "child"), "operationId": op, "seq": 1,
            "status": "succeeded", "result": "unread after commit",
        }})
        await asyncio.wait_for(inflight.wait(), 10)
        first = next(iter(broker.wakes))
        assert broker.log.has_committed_input(first)
        assert broker.consumed == {}
    finally:
        await adapter.aclose()

    conn = Conn()
    adapter = BoxACPAgent(conn, config, RecoveryModel(), [], "test")
    try:
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="crash-parent")
        await asyncio.wait_for(conn.finished.wait(), 10)
        broker = adapter._sessions[session.session_id].device_delegations
        history = broker.log.history_messages()
        wakes = [(m.input_facts["id"], m.source) for _, m, _ in history
                 if (m.input_facts or {}).get("kind") == "device_wake"]
        assert wakes[0] == (first, "runtime")
        assert len(wakes) == 2 and wakes[1][0].startswith("device-wake-recovery-")
        assert wakes[1][1] == "runtime"
        assert len(original_requests) == 1
        assert len(recovery_requests) == 1
        assert broker._consumed_seq(target("d", "s", "child")) == 1
        assert broker.next_wake() is None
        conn.updates.clear()
        await adapter.resume_session(
            str(tmp_path), session.session_id, replayFromTurnId=first, device={"version": 1},
        )
        inputs = [update.field_meta for update in conn.updates if update.session_update == "user_message_chunk"]
        assert [meta["turnId"] for meta in inputs] == [turn_id for turn_id, _ in wakes]
        assert all(meta["inputKind"] == "device_wake" and meta["inputSource"] == "runtime" for meta in inputs)
    finally:
        await adapter.aclose()

@pytest.mark.asyncio
async def test_unknown_delivery_does_not_claim_identical_instruction_in_a_new_parent_turn():
    calls = []
    turn = "C1"

    async def request(method, params):
        calls.append(params)
        if len(calls) == 1:
            raise asyncio.TimeoutError("unknown")
        if params["operation"] == "session.start":
            return {"deviceId": "d", "sessionId": "s", "turnId": "new-child", "status": "accepted"}
        return {"accepted": True}

    broker = DeviceDelegations("cloud", request, lambda: turn)
    with pytest.raises(RequestError, match="DEVICE_DELIVERY_UNKNOWN"):
        await broker.start("d", "same instruction")
    turn = "C2"
    accepted = await broker.start("d", "same instruction")
    assert calls[0]["operationId"] != calls[1]["operationId"]
    assert broker.operations[accepted["operationId"]]["parentTurnId"] == "C2"
    broker.stop("C2")
    await asyncio.wait_for(asyncio.gather(*broker.pending), 5)
    assert calls[-1]["operation"] == "session.cancel"
    assert calls[-1]["args"] == {"sessionId": "s", "turnId": "new-child"}
    assert broker.operations[calls[0]["operationId"]]["status"] == "unknown_delivery"
    assert "cancelRequested" not in broker.operations[calls[0]["operationId"]]

@pytest.mark.asyncio
async def test_stdio_hard_kill_after_wake_commit_recovers_without_replaying_tools(tmp_path):
    import json
    import os
    import sys

    script = """
import asyncio, sys, threading
from examples import device_parent_demo as demo
from box_agent.session_log import SessionLog
original_mark = SessionLog.mark_runtime_input_receipts
def block_before_write(self, message, receipts):
    original_mark(self, message, receipts)
    print("WAKE_RESPONSE_BEFORE_LOG_WRITE", file=sys.stderr, flush=True)
    threading.Event().wait()
SessionLog.mark_runtime_input_receipts = block_before_write
asyncio.run(demo.main())
"""
    env = {**os.environ, "BOX_AGENT_HOME": str(tmp_path / "profile"),
           "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
           "PYTHONIOENCODING": "utf-8"}
    cmd = sys._base_executable
    processes = []

    async def launch(*args):
        proc = await asyncio.create_subprocess_exec(
            cmd, *args, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        processes.append(proc)
        return proc

    async def send(proc, **payload):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", **payload}) + "\n").encode())
        await proc.stdin.drain()

    async def receive(proc, predicate, *, forbid_device_request=False):
        async def read():
            while line := await proc.stdout.readline():
                frame = json.loads(line)
                if forbid_device_request:
                    assert frame.get("method") != "_device/request", frame
                if predicate(frame):
                    return frame
            pytest.fail("ACP stdio closed before expected frame")

        return await asyncio.wait_for(read(), 20)

    async def open_session(proc):
        await send(proc, id=1, method="initialize", params={"protocolVersion": 1})
        await receive(proc, lambda m: m.get("id") == 1)
        await send(proc, id=2, method="session/new", params={
            "cwd": str(tmp_path), "mcpServers": [],
            "_meta": {"device": {"version": 1}, "session_id": "hard-crash"},
        })
        response = await receive(proc, lambda m: m.get("id") == 2)
        return response["result"]["sessionId"]

    async def prompt(proc, request_id, sid, turn_id, text):
        await send(proc, id=request_id, method="session/prompt", params={
            "sessionId": sid, "prompt": [{"type": "text", "text": text}],
            "_meta": {"turnId": turn_id},
        })

    def load_log():
        path = next((tmp_path / "profile" / "sessions").glob("*/session.jsonl"))
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    try:
        crashed = await launch("-c", script)
        sid = await open_session(crashed)
        await prompt(crashed, 3, sid, "prior-write", "write a file")
        await receive(crashed, lambda m: m.get("id") == 3)
        assert (tmp_path / "result.txt").read_text() == "ok"
        await prompt(crashed, 4, sid, "C1", '{"deviceId":"d","instruction":"child task"}')
        request = await receive(crashed, lambda m: m.get("method") == "_device/request")
        await send(crashed, id=request["id"], result={**target("d", "s", "child"), "status": "accepted"})
        await receive(crashed, lambda m: m.get("id") == 4)
        await send(crashed, id=5, method="_device/event", params={"version": 1, "sessionId": sid, "event": {
            **target("d", "s", "child"), "operationId": request["params"]["operationId"],
            "seq": 1, "status": "succeeded", "result": "result was unread at crash",
        }})
        async def wait_for_barrier():
            while b"WAKE_RESPONSE_BEFORE_LOG_WRITE" not in (line := await crashed.stderr.readline()):
                assert line, "model never reached the crash barrier"

        await asyncio.wait_for(wait_for_barrier(), 20)
        before = load_log()
        first = next(e["data"]["turnId"] for e in before if e["type"] == "device/wake")
        assert any(e["type"] == "user/message" and (e["data"].get("input_facts") or {}).get("id") == first
                   for e in before)
        assert not any(e["data"].get("runtimeInputReceipts") for e in before if e["type"] == "assistant/message")
        crashed.kill()
        await asyncio.wait_for(crashed.wait(), 10)

        restarted = await launch("-m", "examples.device_parent_demo")
        assert await open_session(restarted) == sid
        finished = await receive(restarted, lambda m: m.get("method") == "_device/turn"
                                 and m["params"]["phase"] == "finished", forbid_device_request=True)
        assert finished["params"]["result"]["stopReason"] == "end_turn"
        assert "_meta" in finished["params"]["result"]
        after = load_log()
        recovery = [e["data"] for e in after if e["type"] == "device/wake" and e["data"].get("recoveryOf")]
        assert len(recovery) == 1 and recovery[0]["recoveryOf"] == first
        assert any(e["data"].get("runtimeInputReceipts") for e in after if e["type"] == "assistant/message")
        original_tools = [e for e in before if e["type"] == "tool/result"]
        assert original_tools == [e for e in after if e["type"] == "tool/result"]
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 10)

@pytest.mark.asyncio
async def test_late_running_event_cancels_stopped_unknown_delivery_without_blocking_ack():
    calls = []
    release = asyncio.Event()
    dispatched = asyncio.Event()
    async def request(method, params):
        calls.append(params)
        if params["operation"] == "session.start":
            raise asyncio.TimeoutError("lost response")
        dispatched.set()
        await release.wait()
        return {"accepted": True}

    broker = DeviceDelegations("cloud", request, lambda: "C1")
    with pytest.raises(RequestError, match="DEVICE_DELIVERY_UNKNOWN"):
        await broker.start("d", "work")
    op = calls[0]["operationId"]
    broker.stop("C1")
    assert len(calls) == 1
    update = {"version": 1, "sessionId": "cloud", "event": {
        **target("d", "s", "late-child"), "operationId": op, "seq": 1, "status": "running",
    }}
    assert broker.event(update) == {"recorded": True}
    assert broker.event(update) == {"recorded": True}
    await asyncio.wait_for(dispatched.wait(), 5)
    assert calls[-1]["operation"] == "session.cancel"
    assert calls[-1]["operationId"] != op
    assert calls[-1]["args"] == {"sessionId": "s", "turnId": "late-child"}
    assert len(calls) == 2
    release.set()
    await asyncio.gather(*broker.pending)
    assert broker.event(update) == {"recorded": True}
    assert len(calls) == 2

@pytest.mark.asyncio
async def test_wait_timeout_only_returns_current_status_without_followup_get():
    calls = []
    first, second = target(), target("d2")

    async def request(method, params):
        calls.append((method, params))
        state = ({**first, "status": "running", "seq": 3, "summary": "intermediate tool output",
                  "toolCalls": [{"name": "ignored"}]} if params["deviceId"] == "d"
                 else {**second, "status": "unknown"})
        return {"timedOut": True, "results": [], "states": [{**state, "source": "device"}]}

    broker = DeviceDelegations("p", request, lambda: "turn")
    bind_wait_targets(broker, first, second)
    tool = DeviceTool(broker, "session.wait")
    result = await tool.invoke({"targets": [first, second], "timeoutMs": 0})
    assert result.success
    assert result.raw_output == {"timedOut": True, "results": [], "states": [
        {**first, "status": "running", "seq": 3, "source": "device"},
        {**second, "status": "unknown", "source": "device"},
    ]}
    assert {params["deviceId"] for _, params in calls} == {"d", "d2"}
    assert len({params["operationId"] for _, params in calls}) == 2
    for _, params in calls:
        assert params["operation"] == "session.wait"
        assert params["args"] == {"targets": [target(params["deviceId"])], "timeoutMs": 0}


@pytest.mark.asyncio
async def test_wait_returns_ready_group_while_other_devices_are_offline_or_silent():
    silent_started = asyncio.Event()
    silent_released = asyncio.Event()
    offline_replied = asyncio.Event()
    ready = {**target("ready"), "status": "succeeded", "source": "device",
             "seq": 2, "complete": True, "result": "available"}
    calls = []

    async def request(_, params):
        calls.append(params)
        if params["deviceId"] == "offline":
            offline_replied.set()
            raise RequestError(-32004, "DEVICE_OFFLINE", {"deviceId": "offline", "delivery": "not_sent"})
        if params["deviceId"] == "silent":
            silent_started.set()
            try:
                await asyncio.Future()
            finally:
                silent_released.set()
        await silent_started.wait()
        await offline_replied.wait()
        return {"timedOut": False, "results": [ready]}

    broker = DeviceDelegations("p", request, lambda: "parent")
    bind_wait_targets(broker, target("offline"), target("silent"), target("ready"))
    before = deepcopy(broker.operations)
    result = await asyncio.wait_for(DeviceTool(broker, "session.wait").invoke({
        "targets": [target("offline"), target("silent"), target("ready")],
    }), 1)
    assert result.success
    assert result.raw_output == {"timedOut": False, "results": [ready], "unavailable": [
        {"deviceId": "offline", "reason": "DEVICE_OFFLINE", "delivery": "not_sent"},
    ]}
    assert silent_released.is_set()
    assert {item["operation"] for item in calls} == {"session.wait"}
    assert broker.operations == before


@pytest.mark.asyncio
@pytest.mark.parametrize("has_business_reply", [False, True])
async def test_wait_reports_only_observed_states_and_preserves_delivery_errors(has_business_reply):
    observed = {**target("online"), "status": "running", "source": "device", "seq": 1}

    async def request(_, params):
        device_id = params["deviceId"]
        if has_business_reply and device_id == "online":
            return {"timedOut": True, "results": [], "states": [observed]}
        name, delivery = (("DEVICE_OFFLINE", "not_sent") if device_id == "offline"
                          else ("DEVICE_DELIVERY_UNKNOWN", "unknown"))
        raise RequestError(-32004 if delivery == "not_sent" else -32008, name, {
            "deviceId": device_id, "delivery": delivery,
        })

    broker = DeviceDelegations("p", request, lambda: "parent")
    bind_wait_targets(broker, target("offline"), target("online"))
    result = await asyncio.wait_for(DeviceTool(broker, "session.wait").invoke({
        "targets": [target("offline"), target("online")], "timeoutMs": 60000,
    }), 1)
    assert result.success == has_business_reply
    assert result.raw_output["unavailable"][0] == {
        "deviceId": "offline", "reason": "DEVICE_OFFLINE", "delivery": "not_sent",
    }
    if has_business_reply:
        assert result.raw_output["timedOut"] is True
        assert result.raw_output["states"] == [observed]
    else:
        assert result.raw_output["error"][1] == {
            "code": -32008, "message": "DEVICE_DELIVERY_UNKNOWN",
            "data": {"deviceId": "online", "delivery": "unknown"},
        }


@pytest.mark.asyncio
async def test_wait_shares_a_hard_deadline_and_reports_silence_as_unknown(monkeypatch):
    monkeypatch.setattr("box_agent.device_delegation.RPC_TIMEOUT_SECONDS", 0.02)
    observed = {**target("online"), "status": "running", "source": "device", "seq": 1}
    arrivals = []
    cancelled = []
    all_arrived = asyncio.Event()

    async def request(_, params):
        device_id = params["deviceId"]
        arrivals.append(device_id)
        if len(arrivals) == 3:
            all_arrived.set()
        await all_arrived.wait()
        if device_id == "online":
            return {"timedOut": True, "results": [], "states": [observed]}
        try:
            await asyncio.Future()
        finally:
            cancelled.append(device_id)

    broker = DeviceDelegations("p", request, lambda: "parent")
    targets = [target("silent-a"), target("online"), target("silent-b")]
    bind_wait_targets(broker, *targets)
    result = await asyncio.wait_for(DeviceTool(broker, "session.wait").invoke({
        "targets": targets, "timeoutMs": 10,
    }), 1)
    assert result.success
    assert result.raw_output["timedOut"] is True
    assert result.raw_output["states"] == [observed]
    assert {item["deviceId"]: item["delivery"] for item in result.raw_output["unavailable"]} == {
        "silent-a": "unknown", "silent-b": "unknown",
    }
    assert set(cancelled) == {"silent-a", "silent-b"}


@pytest.mark.asyncio
async def test_acp_stop_cancels_all_wait_groups_without_cancelling_other_parent_turns(tmp_path):
    import json
    import threading

    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import Config, ToolsConfig, LLMConfig, AgentConfig
    from box_agent.session_log import run_session_write
    from examples.device_parent_demo import DeterministicLLM

    entered = asyncio.Event()
    checkpoint_started = asyncio.Event()
    release_checkpoint = threading.Event()
    waits_released = asyncio.Event()
    loop = asyncio.get_running_loop()
    started = set()
    released = set()
    targets = [target("first"), target("second")]

    class Conn:
        async def ext_method(self, method, params):
            assert params["operation"] == "session.wait"
            device = params["deviceId"]
            started.add(device)
            if started == {"first", "second"}:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                released.add(device)
                if released == {"first", "second"}:
                    waits_released.set()

        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Conn(), config, DeterministicLLM(), [], "test")
    tasks = []
    try:
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1})
        sid = session.session_id
        broker = adapter._sessions[sid].device_delegations
        bind_wait_targets(broker, *targets)
        text = json.dumps({"operation": "session.wait", "args": {"targets": targets, "timeoutMs": 60000}})
        prompt = asyncio.create_task(adapter.prompt(sid, [text_block(text)], turnId="waiting"))
        tasks.append(prompt)
        await asyncio.wait_for(entered.wait(), 5)

        def checkpoint():
            loop.call_soon_threadsafe(checkpoint_started.set)
            assert release_checkpoint.wait(10)

        tasks.append(asyncio.create_task(run_session_write(broker.log, checkpoint)))
        await asyncio.wait_for(checkpoint_started.wait(), 5)
        stop = asyncio.create_task(adapter.ext_method("device/cancel_turn", {"sessionId": sid, "turnId": "waiting"}))
        tasks.append(stop)
        await asyncio.wait_for(waits_released.wait(), 5)
        assert not stop.done()
        assert "waiting" not in broker.stopped_turns
        release_checkpoint.set()
        assert await asyncio.wait_for(stop, 5) == {"accepted": True}
        assert "waiting" in broker.stopped_turns
        await asyncio.wait_for(prompt, 5)
        assert released == started == {"first", "second"}
        assert {record["status"] for record in broker.operations.values()} == {"accepted"}
        assert all("cancelOperationId" not in record for record in broker.operations.values())
    finally:
        release_checkpoint.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["duplicate", "different_device", "unknown_target"])
async def test_invalid_wait_batch_fails_before_contacting_any_device(invalid):
    async def request(*args):
        pytest.fail("invalid wait reached the transport")

    broker = DeviceDelegations("p", request, lambda: "parent")
    broker.save({**target(), "operationId": "known", "status": "accepted", "seq": 0,
                 "parentTurnId": "p1"})
    other = {"duplicate": target(), "different_device": target("wrong-device"),
             "unknown_target": target("unknown-device", "unknown-session", "unknown-turn")}[invalid]
    result = await DeviceTool(broker, "session.wait").invoke({"targets": [target(), other]})
    assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["wrong_group", "backend_source", "incomplete", "invalid_seq"])
async def test_wait_does_not_expose_an_invalid_ready_snapshot(defect):
    snapshot = {**target(), "status": "succeeded", "source": "device",
                "complete": True, "seq": 1, "result": "should not be exposed"}
    snapshot.update({"wrong_group": {"deviceId": "other"}, "backend_source": {"source": "backend"},
                     "incomplete": {"complete": False}, "invalid_seq": {"seq": True}}[defect])

    async def request(*args):
        return {"timedOut": False, "results": [snapshot]}

    broker = DeviceDelegations("p", request, lambda: "parent")
    bind_wait_targets(broker, target(), target("other"))
    result = await DeviceTool(broker, "session.wait").invoke({"targets": [target(), target("other")]})
    assert not result.success
    assert result.raw_output is None
    assert "should not be exposed" not in result.error
    assert not broker.offered


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["session.start", "session.send"])
async def test_start_and_send_retry_unknown_delivery_after_a_not_sent_attempt(operation):
    returned = iter(["not_sent", "unknown", "not_sent", "accepted"])
    calls = []

    async def request(_, params):
        calls.append(params)
        delivery = next(returned)
        if delivery == "accepted":
            return {**target(), "status": "accepted"}
        code, message = ((-32004, "DEVICE_OFFLINE") if delivery == "not_sent"
                         else (-32008, "DEVICE_DELIVERY_UNKNOWN"))
        raise RequestError(code, message, {"deviceId": "d", "delivery": delivery})

    broker = DeviceDelegations("p", request, lambda: "p1")
    tool = DeviceTool(broker, operation)
    args = {"deviceId": "d", "instruction": "write"}
    if operation == "session.send":
        args["sessionId"] = "s"
    for expected in ("not_sent", "unknown", "not_sent"):
        result = await tool.invoke(args)
        assert not result.success
        assert result.raw_output["data"] == {"deviceId": "d", "delivery": expected}
        assert expected in result.error
    assert broker.operations[calls[0]["operationId"]]["status"] == "rejected"
    assert broker.operations[calls[1]["operationId"]]["status"] == "unknown_delivery"
    assert (await tool.invoke(args)).success
    assert calls[0]["operationId"] != calls[1]["operationId"]
    assert calls[1] == calls[2] == calls[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["session.start", "session.send"])
@pytest.mark.parametrize("rpc_error", [True, False], ids=["rpc-denial", "negative-receipt"])
async def test_retry_after_lost_receipt_and_denial_executes_child_once(operation, rpc_error):
    received = []
    executions = {}
    log = Log()

    async def request(_, params):
        operation_id = params["operationId"]
        received.append(operation_id)
        if len(received) == 2:
            if rpc_error:
                raise RequestError(-32003, "DEVICE_NOT_AUTHORIZED")
            return {"accepted": False, "error": "DEVICE_NOT_AUTHORIZED"}
        # The desktop durably accepts the first attempt before its reply gets lost.
        receipt = executions.setdefault(operation_id, {**target(), "status": "accepted"})
        if len(received) == 1:
            raise ConnectionError("receipt lost")
        return receipt

    args = {"deviceId": "d", "instruction": "append a row"}
    if operation == "session.send":
        args["sessionId"] = "s"
    broker = DeviceDelegations("p", request, lambda: "p1", log)
    tool = DeviceTool(broker, operation)
    assert not (await tool.invoke(args)).success
    assert not (await tool.invoke(args)).success
    assert broker.operations[received[0]]["status"] == "unknown_delivery"
    reopened = DeviceDelegations("p", request, lambda: "p1", log)
    assert (await DeviceTool(reopened, operation).invoke(args)).success
    assert received == [received[0]] * 3
    assert list(executions) == [received[0]]
    assert reopened.operations[received[0]]["status"] == "accepted"


@pytest.mark.asyncio
async def test_pending_approval_survives_reopen_and_is_consumed_without_hiding_repeat_wait(tmp_path):
    from box_agent.schema import Message
    from box_agent.session_log import SessionLog

    async def request(method, params):
        assert params["operation"] == "session.wait"
        return {"timedOut": False, "results": [
            {**target(), "status": "waiting_approval", "approval": approval(),
             "seq": 1, "complete": False, "source": "device"},
        ]}

    root = tmp_path / "log"
    log = SessionLog.create(root, session_id="p", cwd=tmp_path)
    broker = DeviceDelegations("p", request, lambda: "parent", log)
    broker.save({**target(), "operationId": "op", "parentTurnId": "parent",
                 "status": "accepted", "seq": 0})
    envelope = {"version": 1, "sessionId": "p"}
    frame = {**target(), "operationId": "op", "seq": 1, "status": "waiting_approval"}
    with pytest.raises(ValueError, match="approval descriptor"):
        broker.event({**envelope, "event": frame})
    assert broker.event({**envelope, "event": {**frame, "approval": approval()}}) == {"recorded": True}
    log.close()
    log = SessionLog.open(root, session_id="p", cwd=tmp_path)
    try:
        restored = DeviceDelegations("p", request, lambda: "parent", log)
        assert restored.operations["op"]["approval"] == approval()
        assert approval()["approvalRequestId"] in restored.request_injections([])[0].content
        wait = DeviceTool(restored, "session.wait")
        first = await wait.invoke({"targets": [target()]})
        receipt = restored.response_receipts([Message(role="tool", content=first.content)])
        assert receipt
        restored.confirm_receipts(receipt)
        assert restored.request_injections([]) == ()
        again = await wait.invoke({"targets": [target()]})
        assert again.raw_output == first.raw_output
        restored.event({**envelope, "event": {**frame, "seq": 2, "status": "running"}})
        assert "approval" not in restored.operations["op"]
    finally:
        log.close()


@pytest.mark.asyncio
async def test_legacy_approval_without_descriptor_waits_for_actionable_update(tmp_path):
    from box_agent.schema import Message
    from box_agent.session_log import SessionLog

    async def request(*args):
        return {}

    log = SessionLog.create(tmp_path / "log", session_id="p", cwd=tmp_path)
    broker = DeviceDelegations("p", request, lambda: "parent", log)
    try:
        broker.save({**target(), "operationId": "op", "parentTurnId": "parent",
                     "status": "accepted", "seq": 0})
        broker.end("parent")
        envelope = {"version": 1, "sessionId": "p"}
        frame = {**target(), "operationId": "op", "seq": 1, "status": "approval_required"}
        assert broker.event({**envelope, "event": frame}) == {"recorded": True}
        assert broker.operations["op"]["status"] == "waiting_approval"
        assert broker.next_wake() is None
        injection = broker.request_injections([])[0]
        assert broker.response_receipts([Message(
            role="user", source="runtime", content=injection.model_content(),
        )]) == {}

        restored = DeviceDelegations("p", request, lambda: "next", log)
        assert restored.next_wake() is None
        assert restored.event({**envelope, "event": {
            **frame, "seq": 2, "status": "waiting_approval", "approval": approval(),
        }}) == {"recorded": True}
        assert restored.next_wake() is not None
        actionable = restored.request_injections([])[0]
        assert approval()["approvalRequestId"] in actionable.content
        receipts = restored.response_receipts([Message(
            role="user", source="runtime", content=actionable.model_content(),
        )])
        assert receipts
        restored.confirm_receipts(receipts)
        assert restored.next_wake() is None
    finally:
        await broker.aclose()
        log.close()


@pytest.mark.asyncio
async def test_legacy_approval_snapshot_without_descriptor_remains_readable():
    from box_agent.device_delegation import current_snapshot

    snapshot = {**target(), "status": "approval_required", "seq": 1,
                "complete": False, "source": "device"}
    with pytest.raises(ValueError, match="approval descriptor"):
        current_snapshot({**snapshot, "status": "waiting_approval"})

    async def request(_, params):
        if params["operation"] == "session.wait":
            return {"timedOut": False, "results": [snapshot]}
        return snapshot

    broker = DeviceDelegations("p", request, lambda: "parent", Log())
    broker.save({**target(), "operationId": "op", "parentTurnId": "parent",
                 "status": "accepted", "seq": 0})
    result = await broker.query("session.get_turn", target())
    assert result["status"] == "waiting_approval" and "approval" not in result
    waited = await broker.query("session.wait", {"targets": [target()]})
    assert waited["results"] == [result]


@pytest.mark.asyncio
async def test_box_cannot_submit_permission_decision_from_tool_or_extension(tmp_path):
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Conn:
        async def ext_method(self, *args):
            pytest.fail("approval control must never route from Box")

    cfg = Config(agent=AgentConfig(workspace_dir=str(tmp_path)), tools=ToolsConfig(),
                 llm=LLMConfig(api_key="test"))
    agent = BoxACPAgent(Conn(), cfg, DeterministicLLM(), [], "test")
    try:
        session = await agent.new_session(str(tmp_path), [], device={"version": 1})
        tools = agent._sessions[session.session_id].agent.tools
        assert len([tool for tool in tools.values() if isinstance(tool, DeviceTool)]) == 8
        assert "device_permission_decision" not in tools
        response = await agent.ext_method("device/permission_decision", {
            **target(), "cloudSessionId": session.session_id,
            "approvalRequestId": "approval-1", "decision": "allow_once",
        })
        assert response == {"error": "unknown_method: device/permission_decision"}
    finally:
        await agent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision,fallback", [("allow_once", False), ("deny", False), ("deny", True)])
async def test_demo_uses_real_acp_permission_and_only_allow_once_writes(tmp_path, decision, fallback):
    import json
    import os
    import sys

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    target_file = home / "device-demo-approval.txt"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "examples.device_parent_demo",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
             "DEVICE_DEMO_APPROVAL_HOME": str(home)},
    )
    async def send(**payload):
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", **payload}) + "\n").encode())
        await proc.stdin.drain()

    async def receive(predicate):
        async def read():
            while line := await proc.stdout.readline():
                frame = json.loads(line)
                if predicate(frame):
                    return frame
            pytest.fail((await proc.stderr.read()).decode(errors="replace"))
        return await asyncio.wait_for(read(), 20)

    try:
        await send(id=1, method="initialize", params={
            "protocolVersion": 1, "_meta": {"deviceDelegationReceiver": {"version": 1}},
        })
        await receive(lambda frame: frame.get("id") == 1)
        await send(id=2, method="session/new", params={
            "cwd": str(workspace), "mcpServers": [], "_meta": {"permissionMode": "default"},
        })
        opened = await receive(lambda frame: frame.get("id") == 2)
        sid = opened["result"]["sessionId"]
        await send(id=3, method="session/prompt", params={
            "sessionId": sid, "prompt": [{"type": "text", "text": "approval write fallback" if fallback else "approval write"}],
            "_meta": {"turnId": "child-turn", "deviceDelegation": {
                "parentSessionId": "cloud-parent", "operationId": "device-op",
            }},
        })
        permission = await receive(lambda frame: frame.get("method") == "session/request_permission"
                                   or frame.get("id") == 3)
        assert permission.get("method") == "session/request_permission", permission
        assert {o["kind"]: o["optionId"] for o in permission["params"]["options"]} == {
            "allow_once": "approve", "reject_once": "reject",
        }
        resource = permission["params"]["toolCall"]["rawInput"]
        assert resource["scope"] == "filesystem"
        assert resource["operation"] == "write"
        assert resource["deviceApprovalTurnId"] == "child-turn"
        assert resource["path"] == str(target_file.resolve())
        assert resource["exactFilesystem"] == {
            "path": resource["path"], "operation": resource["operation"], "toolName": "write_file",
        }
        assert not target_file.exists()
        option = "approve" if decision == "allow_once" else "reject"
        await send(id=permission["id"], result={"outcome": {"outcome": "selected", "optionId": option}})
        await receive(lambda frame: frame.get("id") == 3)
        assert sorted(file.name for file in home.glob("*.txt")) == (
            [target_file.name] if decision == "allow_once" else []
        )
        if decision == "allow_once":
            assert target_file.read_text(encoding="utf-8") == "ok"
        assert sorted(file.name for file in workspace.glob("*.txt")) == (["fallback.txt"] if fallback else [])
        if fallback:
            assert (workspace / "fallback.txt").read_text(encoding="utf-8") == "ok"
    finally:
        if proc.returncode is None:
            proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

@pytest.mark.asyncio
@pytest.mark.parametrize("delegated", [True, False])
async def test_acp_allow_once_is_exact_for_delegation_and_local_grants_stay_compatible(tmp_path, delegated):
    from acp import text_block
    from acp.schema import AllowedOutcome, RequestPermissionResponse
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.schema import StreamEvent
    from examples.device_parent_demo import DeterministicLLM
    from pathlib import Path

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = Path.home() / "device-private-fixture"
    private.mkdir()
    first, second = private / "a.txt", private / "b.txt"
    first.write_text("allowed-first", encoding="utf-8")
    second.write_text("denied-neighbor-secret", encoding="utf-8")
    calls = [
        ("read_file", {"path": str(first)}),
        ("read_file", {"path": str(second)}),
        ("write_file", {"path": str(first), "content": "should not write with read permission"}),
        ("read_file", {"path": str(first), "refresh": True}),
    ]
    seen_tool_results = []

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            replies = [m.content for m in messages if m.role == "tool"]
            seen_tool_results[:] = replies
            if len(replies) < len(calls):
                yield self._tool_call(*calls[len(replies)])
            else:
                yield StreamEvent(type="text", delta="finished")
                yield StreamEvent(type="finish", finish_reason="stop")

    class Conn:
        def __init__(self):
            self.requests = []

        async def request_permission(self, session_id, tool_call, options):
            self.requests.append((tool_call.raw_input, [o.kind for o in options]))
            option = "approve" if len(self.requests) == 1 else "reject"
            return RequestPermissionResponse(
                outcome=AllowedOutcome(outcome="selected", option_id=option),
            )

        async def session_update(self, *args, **kwargs):
            pass

    conn = Conn()
    config = Config(agent=AgentConfig(workspace_dir=str(workspace), max_steps=8),
                    tools=ToolsConfig(), llm=LLMConfig(api_key="test"))
    agent = BoxACPAgent(conn, config, Model(), [], "test")
    try:
        await agent.initialize(1, deviceDelegationReceiver={"version": 1})
        opened = await agent.new_session(str(workspace), [], permissionMode="default")
        origin = {"deviceDelegation": {"parentSessionId": "cloud", "operationId": "op"}} if delegated else {}
        await agent.prompt(opened.session_id, [text_block("Read files and try a write.")],
                           turnId="turn", **origin)
        assert "allowed-first" in str(seen_tool_results[0])
        if delegated:
            assert [(payload["operation"], payload["path"]) for payload, _ in conn.requests] == [
                ("read", str(first.resolve())), ("read", str(second.resolve())),
                ("write", str(first.resolve())), ("read", str(first.resolve())),
            ]
            assert all(kinds == ["allow_once", "reject_once"] for _, kinds in conn.requests)
            assert all(payload["deviceApprovalTurnId"] == "turn" for payload, _ in conn.requests)
            assert all("denied-neighbor-secret" not in str(result) for result in seen_tool_results)
            assert first.read_text(encoding="utf-8") == "allowed-first"
        else:
            assert len(conn.requests) == 1
            assert "operation" not in conn.requests[0][0]
            assert "deviceApprovalTurnId" not in conn.requests[0][0]
            assert "allow_always" in conn.requests[0][1]
            assert "denied-neighbor-secret" in str(seen_tool_results[1])
            assert first.read_text(encoding="utf-8") == "should not write with read permission"
    finally:
        await agent.aclose()


@pytest.mark.asyncio
async def test_exact_filesystem_approval_does_not_leak_between_parallel_retries(tmp_path):
    from pathlib import Path
    from box_agent.tools.permissions import (
        CapabilityPolicy, PermissionEngine, bind_retry_approval, scoped_exact_approvals,
    )

    workspace = tmp_path / "work"
    workspace.mkdir()
    path = Path.home() / "approval-target.txt"
    sibling = path.with_name("approval-sibling.txt")
    engine = PermissionEngine(CapabilityPolicy(session_workspace_root=str(workspace)), workspace)
    approved = asyncio.Event()
    sibling_checked = asyncio.Event()

    def can_read(file):
        return engine.check("filesystem.read", {"path": str(file)}, "read_file").allowed

    async def selected_retry():
        request = engine.check("filesystem.read", {"path": str(path)}, "read_file").permission_request
        with bind_retry_approval(request, "read_file"):
            assert can_read(path)
            assert not can_read(sibling)
            assert not can_read(workspace / "otherwise-allowed.txt")
            approved.set()
            await sibling_checked.wait()
        assert not can_read(path)

    async def unrelated_retry():
        await approved.wait()
        try:
            assert not can_read(path)
        finally:
            sibling_checked.set()

    with scoped_exact_approvals("child-turn"):
        await asyncio.wait_for(asyncio.gather(selected_retry(), unrelated_retry()), 5)

@pytest.mark.asyncio
async def test_delegated_approval_denies_nonfile_escalation_before_host_rpc(tmp_path):
    from pathlib import Path
    from box_agent.acp import _PermissionNegotiator
    from box_agent.tools.permissions import CapabilityPolicy, GrantStore, PermissionEngine, scoped_exact_approvals

    class Conn:
        async def request_permission(self, **kwargs):
            pytest.fail("unsupported permission must never reach the remote approval host")

    workspace = tmp_path / "work"
    workspace.mkdir()
    user_dir = Path.home() / "outside"
    user_dir.mkdir()
    engine = PermissionEngine(CapabilityPolicy(session_workspace_root=str(workspace)), workspace)
    negotiator = _PermissionNegotiator(Conn(), "child", GrantStore())
    with scoped_exact_approvals("child-turn"):
        for path, tool in ((user_dir, "read_file"), (user_dir / "file", "bash"),
                           (tmp_path / "outside-user-home", "write_file")):
            decision = engine.check("filesystem.read", {"path": str(path)}, tool)
            assert not decision.allowed and not decision.permission_request
        assert not await negotiator.negotiate({
            "scope": "safety", "requested_scope": "dangerous_command",
            "command": "fixture only", "reason": "synthetic request",
        })
        assert not await negotiator.negotiate({
            "scope": "filesystem", "requested_scope": "user_home",
            "path": str(user_dir), "reason": "no trusted exact target marker",
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("name,operation", [
    ("custom_tool", "write"), ("write_file", "read"), ("write_file", "write"),
])
async def test_self_reported_device_approval_is_denied_even_with_builtin_name(name, operation):
    from pathlib import Path
    from box_agent.tools.base import Tool, ToolResult
    from box_agent.tools.engine.execution import invoke_tool_once, _negotiate_tool_permission_chain
    from box_agent.tools.permissions import scoped_exact_approvals

    path = str((Path.home() / "approval-target.txt").resolve())
    request = {
        "scope": "filesystem", "requested_scope": "user_home", "path": path, "operation": operation,
        "exactFilesystem": {"path": path, "operation": operation, "toolName": name},
    }

    class CustomTool(Tool):
        description = "Returns a self-reported approval descriptor."
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        @property
        def name(self):
            return name

        def approve_permission_request(self, request):
            pytest.fail("a custom tool must not receive an exact grant")

        async def execute(self, path):
            return ToolResult(success=False, error="Denied", permission_request=request)

    class Negotiator:
        async def negotiate(self, request):
            pytest.fail("a self-reported approval must not reach the host")

    tool = CustomTool()
    with scoped_exact_approvals("child-turn"):
        result = await invoke_tool_once(tool, {"path": path})
        assert not result.success and result.permission_request is None
        result, _ = await _negotiate_tool_permission_chain(
            result=await tool.execute(path), permission_negotiator=Negotiator(), tool_name=name, tool=tool,
            arguments={"path": path}, retry_offer_error=lambda: None,
        )
        assert not result.success and result.permission_request is None
        retried = await invoke_tool_once(tool, {"path": path}, approved_permission_request=request)
        assert not retried.success and "does not match" in retried.error


@pytest.mark.asyncio
async def test_builtin_write_retry_requires_the_approved_file(tmp_path):
    from pathlib import Path
    from box_agent.tools.engine.execution import invoke_tool_once
    from box_agent.tools.file_tools import WriteTool
    from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine, scoped_exact_approvals

    approved_file = Path.home() / "approved.txt"
    workspace_file = tmp_path / "workspace.txt"
    engine = PermissionEngine(CapabilityPolicy(session_workspace_root=str(tmp_path)), tmp_path)
    tool = WriteTool(str(tmp_path), permission_engine=engine)
    with scoped_exact_approvals("child-turn"):
        denied = await invoke_tool_once(tool, {"path": str(approved_file), "content": "approved"})
        assert not denied.success and denied.permission_request is not None
        retried = await invoke_tool_once(
            tool, {"path": str(workspace_file), "content": "different"},
            approved_permission_request=denied.permission_request,
        )
    assert not retried.success and "does not match" in retried.error
    assert not approved_file.exists()
    assert not workspace_file.exists()


@pytest.mark.asyncio
async def test_success_keeps_summary_and_separate_permission_issue_after_reopen(tmp_path):
    import json
    from box_agent.session_log import SessionLog

    issue = {"code": "PERMISSION_TIMEOUT", "message": "One earlier approval expired"}
    terminal = {**target(), "status": "succeeded", "seq": 1, "complete": True, "source": "device",
                "summary": "Finished the available work",
                "toolTrace": ["do not expose"], "privateDiagnostics": {"private": True},
                "permissionIssue": {**issue, "privateToolData": "omit this"}}

    async def request(method, params):
        if params["operation"] == "session.get_turn":
            return terminal
        return {"timedOut": False, "results": [terminal]}

    root = tmp_path / "log"
    log = SessionLog.create(root, session_id="p", cwd=tmp_path)
    broker = DeviceDelegations("p", request, lambda: "parent", log)
    broker.save({**target(), "operationId": "op", "parentTurnId": "parent",
                 "status": "accepted", "seq": 0})
    broker.event({"version": 1, "sessionId": "p", "event": {**terminal, "operationId": "op"}})
    log.close()
    log = SessionLog.open(root, session_id="p", cwd=tmp_path)
    try:
        restored = DeviceDelegations("p", request, lambda: "parent", log)
        pushed = json.loads(restored.request_injections([])[0].content.split("\n", 1)[1])[0]
        fetched = await DeviceTool(restored, "session.get_turn").invoke(target())
        waited = await DeviceTool(restored, "session.wait").invoke({"targets": [target()]})
        for snapshot in (pushed, fetched.raw_output, waited.raw_output["results"][0]):
            assert snapshot["status"] == "succeeded"
            assert snapshot["summary"] == terminal["summary"]
            assert snapshot["permissionIssue"] == issue
            assert "toolTrace" not in snapshot and "privateDiagnostics" not in snapshot
    finally:
        log.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("aborted", [False, True])
async def test_wait_uses_final_ready_snapshot_only_from_a_successful_response(aborted, monkeypatch):
    issue = {"code": "APPROVAL_EXPIRED", "message": "A prior approval expired"}
    first = {**target(), "status": "succeeded", "summary": "Available output",
             "seq": 1, "complete": True, "source": "device",
             "permissionIssue": issue, "toolTrace": ["private"]}
    second = {**target(turn="second"), "status": "running", "source": "device"}
    calls = []

    async def request(method, params):
        calls.append(params["operation"])
        envelope = {"error": {"code": "TIMED_OUT", "message": "Wait ended"}} if aborted else {
            "timedOut": True, "results": [],
        }
        return {**envelope, "states": [first, second]}

    broker = DeviceDelegations("p", request, lambda: "parent")
    bind_wait_targets(broker, target(), target(turn="second"))
    from unittest.mock import Mock
    snapshot = Mock(wraps=broker._snapshot)
    monkeypatch.setattr(broker, "_snapshot", snapshot)
    result = await DeviceTool(broker, "session.wait").invoke({
        "targets": [target(), target(turn="second")], "timeoutMs": 0,
    })
    if aborted:
        assert not result.success
        assert result.raw_output == {"error": {"code": "TIMED_OUT", "message": "Wait ended"}}
        snapshot.assert_not_called()
    else:
        assert result.success
        assert result.raw_output == {"timedOut": False, "results": [{
            key: item for key, item in first.items() if key != "toolTrace"
        }]}
        assert snapshot.call_count == 2
    assert calls == ["session.wait"]

@pytest.mark.asyncio
async def test_wait_does_not_expose_snapshots_from_an_authorization_error():
    async def request(*args):
        return {"error": {"code": "FORBIDDEN", "message": "Not authorized"},
                "states": [{**target(), "status": "succeeded", "summary": "private"}]}

    broker = DeviceDelegations("p", request, lambda: "parent")
    bind_wait_targets(broker, target())
    result = await DeviceTool(broker, "session.wait").invoke({"targets": [target()]})
    assert not result.success
    assert result.raw_output == {"error": {"code": "FORBIDDEN", "message": "Not authorized"}}

@pytest.mark.asyncio
async def test_device_send_schema_uses_receipt_session_without_a_turn_id():
    async def request(*args):
        pytest.fail("invalid device send arguments must not reach the transport")

    tool = DeviceTool(DeviceDelegations("p", request, lambda: "parent"), "session.send")
    schema = tool.parameters
    assert set(schema["required"]) == {"deviceId", "sessionId", "instruction"}
    assert set(schema["properties"]) == {"deviceId", "sessionId", "instruction"}
    assert "previous accepted start/send receipt" in schema["properties"]["deviceId"]["description"]
    assert "previous accepted start/send receipt" in schema["properties"]["sessionId"]["description"]
    assert "report it directly" in tool.description
    assert not (await tool.invoke({"deviceId": "d", "instruction": "continue"})).success
    assert not (await tool.invoke({"deviceId": "d", "sessionId": "s", "instruction": "continue",
                                   "turnId": "old-turn"})).success


@pytest.mark.parametrize("operation", ["session.get_turn", "session.wait"])
def test_device_tool_guides_parent_to_yield_when_approval_is_pending(operation):
    async def request(*args):
        pytest.fail("reading the device tool description must not reach the transport")

    tool = DeviceTool(DeviceDelegations("p", request, lambda: "parent"), operation)
    description = tool.description
    assert "the approval card has already been delivered to the user" in description
    assert "Continue other available work" in description
    assert "If there is none, tell the user approval is pending and end the parent turn" in description
    assert "The Harness delivers the terminal result" in description
    assert "Do not sleep or poll" in description
    if operation == "session.wait":
        assert "return immediately, including repeat waits for the same pending approval" in description


@pytest.mark.asyncio
async def test_wrong_existing_session_event_does_not_bind_send_before_acceptance():
    log = Log()
    broker = None

    async def request(method, params):
        event = {"version": 1, "sessionId": "parent", "event": {
            **target("desktop", "another-session", "wrong-turn"), "seq": 1, "status": "running",
            "operationId": params["operationId"],
        }}
        before = deepcopy(log.events)
        with pytest.raises(ValueError, match="device event identity mismatch"):
            broker.event(event)
        assert log.events == before
        return {**target("desktop", "existing", "actual-child"), "status": "accepted"}

    broker = DeviceDelegations("parent", request, lambda: "turn", log)
    result = await broker.start("desktop", "continue", session_id="existing")
    assert broker.operations[result["operationId"]]["turnId"] == "actual-child"


@pytest.mark.asyncio
async def test_reopened_stopped_parent_retries_unacknowledged_cancel_with_same_operation(monkeypatch):
    import box_agent.device_delegation as delegation

    monkeypatch.setattr(delegation, "RPC_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(delegation, "CANCEL_RETRY_INITIAL_SECONDS", 60)
    log = Log()
    calls = []
    acknowledge = False
    first_timeout = asyncio.Event()

    async def request(method, params):
        if params["operation"] == "session.start":
            return {**target("d", "s", "t"), "status": "accepted"}
        calls.append(params)
        if not acknowledge:
            try:
                await asyncio.Event().wait()
            finally:
                first_timeout.set()
        return {"accepted": True}

    first = DeviceDelegations("parent", request, lambda: "original", log)
    accepted = await first.start("d", "run")
    first.stop("original")
    await asyncio.wait_for(first_timeout.wait(), 5)
    await first.aclose()
    assert len(calls) == 1
    operation = accepted["operationId"]
    assert first.operations[operation]["cancelOperationId"] == calls[0]["operationId"]

    acknowledge = True
    reopened = DeviceDelegations("parent", request, lambda: "new", log)
    envelope = {"version": 1, "sessionId": "parent", "event": {
        **target(), "seq": 1, "status": "running", "operationId": operation,
    }}
    assert reopened.event(envelope) == {"recorded": True}
    await asyncio.wait_for(asyncio.gather(*reopened.pending), 5)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert reopened.operations[operation]["stopCancelAcknowledged"] is True
    assert reopened.event(envelope) == {"recorded": True}
    assert not reopened.pending

    envelope["event"].update(seq=2, status="cancelled")
    reopened.event(envelope)
    reopened.retry_stopped_cancellations()
    assert not reopened.pending


@pytest.mark.asyncio
async def test_opt_in_default_turn_id_is_durable_and_unique_after_stopping_and_reopening(tmp_path):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    calls = []
    started = asyncio.Event()

    class Connection:
        async def ext_method(self, method, params):
            if params["operation"] == "session.cancel":
                return {"accepted": True}
            calls.append(params)
            started.set()
            return {**target("d", "s", f"child-{len(calls)}"), "status": "accepted"}

        async def session_update(self, *args, **kwargs):
            pass

    def open_adapter():
        config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                        agent=AgentConfig(workspace_dir=str(tmp_path)))
        return BoxACPAgent(Connection(), config, DeterministicLLM(), [], "test")

    adapter = open_adapter()
    try:
        session = await adapter.new_session(str(tmp_path), [], session_id="durable-device-id", device={"version": 1})
        prompt = asyncio.create_task(adapter.prompt(session.session_id, [
            text_block('{"deviceId":"d","instruction":"wait","hold":true}'),
        ]))
        await asyncio.wait_for(started.wait(), 10)
        await adapter.cancel(session.session_id)
        assert (await asyncio.wait_for(prompt, 10)).stop_reason == "cancelled"
        first_id = calls[0]["parentTurnId"]
    finally:
        await adapter.aclose()

    reopened = open_adapter()
    try:
        session = await reopened.new_session(str(tmp_path), [], session_id="durable-device-id", device={"version": 1})
        state = reopened._sessions[session.session_id]
        assert first_id in state.device_delegations.stopped_turns
        assert state.agent.session_log.turn_binding(first_id) is not None
        response = await reopened.prompt(session.session_id, [
            text_block('{"deviceId":"d","instruction":"write"}'),
        ])
        second_id = response.field_meta["usage"]["turnId"]
        assert response.stop_reason == "end_turn"
        assert len(calls) == 2 and calls[1]["parentTurnId"] == second_id != first_id
        assert state.agent.session_log.turn_binding(second_id) is not None
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_targeted_cancel_during_mcp_preparation_finishes_task_registry(tmp_path, monkeypatch):
    import json

    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Connection:
        async def session_update(self, *args, **kwargs):
            pass

    class Model(DeterministicLLM):
        async def generate_stream(self, *args, **kwargs):
            pytest.fail("a prompt stopped during preparation must never begin model execution")
            yield

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, Model(), [], "test")
    await adapter.initialize(1, deviceDelegationReceiver={"version": 1})
    try:
        session = await adapter.new_session(str(tmp_path), [])
        preparing = asyncio.Event()
        release = asyncio.Event()

        async def prepare():
            preparing.set()
            await release.wait()

        monkeypatch.setattr(adapter, "_ensure_mcp_loaded", prepare)
        prompt = asyncio.create_task(adapter.prompt(
            session.session_id, [text_block("write result.txt")], turnId="prepare", taskId="task-prep",
        ))
        await asyncio.wait_for(preparing.wait(), 5)
        registry = tmp_path / ".box-agent" / "task-registry" / "tasks" / "task-prep.json"
        assert json.loads(registry.read_text(encoding="utf-8"))["execution_status"] == "running"
        try:
            receipt = await adapter.ext_method("device/cancel_turn", {
                "sessionId": session.session_id, "turnId": "prepare",
            })
            assert receipt == {"accepted": True}
        finally:
            release.set()
        response = await asyncio.wait_for(prompt, 5)
        assert response.stop_reason == "cancelled"
        assert response.field_meta["completed"] is False
        assert json.loads(registry.read_text(encoding="utf-8"))["execution_status"] == "cancelled"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_device_runtime_input_skips_user_memory_and_next_manual_prompt_binds_its_own_input(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Connection:
        async def session_update(self, *args, **kwargs):
            pass

    captured = []

    @asynccontextmanager
    async def memory_scope(state, **kwargs):
        captured.append((kwargs["turn_id"], kwargs["user_message"]))
        yield

    monkeypatch.setattr("box_agent.acp.memory_user_turn", memory_scope)
    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, DeterministicLLM(), [], "test")
    await adapter.initialize(1, deviceDelegationReceiver={"version": 1})
    try:
        session = await adapter.new_session(str(tmp_path), [])
        await adapter.prompt(session.session_id, [text_block("first manual request")], turnId="manual-1")
        await adapter.prompt(session.session_id, [text_block("device instruction")], turnId="device-turn",
                             deviceDelegation={"parentSessionId": "parent", "operationId": "start"})
        await adapter.prompt(session.session_id, [text_block("second manual request")], turnId="manual-2")
        assert [(turn, message.input_facts["id"], message.content, message.source) for turn, message in captured] == [
            ("manual-1", "manual-1", "first manual request", "user"),
            ("manual-2", "manual-2", "second manual request", "user"),
        ]
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_waiting_input_is_ready_for_get_and_wait_and_clears_when_the_child_runs():
    ready = {**target(), "status": "waiting_input", "seq": 1, "complete": False, "source": "device",
             "inputRequest": {"question": "Which project code?", "private": "drop"}}
    broker = None

    async def request(method, params):
        operation = params["operation"]
        if operation == "session.start":
            return {**target(), "status": "accepted"}
        if operation == "session.get_turn":
            return ready
        return {"timedOut": True, "results": [], "states": [ready]}

    broker = DeviceDelegations("parent", request, lambda: "turn")
    accepted = await broker.start("d", "run")
    update = {"version": 1, "sessionId": "parent", "event": {**ready, "operationId": accepted["operationId"]}}
    assert broker.event(update) == {"recorded": True}
    expected_request = {"question": "Which project code?"}
    get = DeviceTool(broker, "session.get_turn")
    wait = DeviceTool(broker, "session.wait")
    result = await get.invoke(target())
    assert result.success and result.raw_output["inputRequest"] == expected_request
    for _ in range(2):
        result = await wait.invoke({"targets": [target()], "timeoutMs": 0})
        assert result.success and not result.raw_output["timedOut"]
        assert result.raw_output["results"][0]["inputRequest"] == expected_request

    update["event"].update(seq=2, status="running")
    assert broker.event(update) == {"recorded": True}
    assert broker.operations[accepted["operationId"]]["status"] == "running"
    assert "inputRequest" not in broker.operations[accepted["operationId"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("during_parent_turn", [True, False])
async def test_waiting_input_reaches_the_model_once_and_does_not_end_the_child(
    tmp_path, during_parent_turn, monkeypatch,
):
    import threading

    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    seen = []

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            seen.extend(message.content for message in messages
                        if message.source == "runtime" and isinstance(message.content, str)
                        and "Which project code?" in message.content)
            async for event in super().generate_stream(messages, *args, **kwargs):
                yield event

    async def report(params, sid, seq, status, **fields):
        return await adapter.ext_method("device/event", {"version": 1, "sessionId": sid, "event": {
            **target(), "operationId": params["operationId"], "seq": seq, "status": status, **fields,
        }})

    start_request = None

    class Connection:
        async def ext_method(self, method, params):
            nonlocal start_request
            assert params["operation"] == "session.start"
            start_request = params
            if during_parent_turn:
                assert await report(params, params["sessionId"], 1, "waiting_input",
                                    inputRequest={"question": "Which project code?"}) == {"recorded": True}
            return {**target(), "status": "accepted"}

        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, Model(), [], "test")
    try:
        session = await adapter.new_session(str(tmp_path), [], session_id="parent", device={"version": 1})
        sid = session.session_id
        state = adapter._sessions[sid]
        broker = state.device_delegations
        log = broker.log
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        mark_receipts = log.mark_runtime_input_receipts

        def mark_on_worker(message, receipts):
            assert threading.get_ident() != loop_thread
            mark_receipts(message, receipts)

        def confirm_on_loop(receipts):
            assert asyncio.get_running_loop() is loop
            assert any(event["data"].get("runtimeInputReceipts") == receipts
                       for event in log.events[:log._durable_event_count] if event["type"] == "assistant/message")
            broker.confirm_receipts(receipts)

        monkeypatch.setattr(log, "mark_runtime_input_receipts", mark_on_worker)
        state.inject_queue.on_model_commit = confirm_on_loop
        response = await adapter.prompt(sid, [text_block('{"deviceId":"d","instruction":"begin"}')])
        if not during_parent_turn:
            assert not seen
            assert await report(start_request, sid, 1, "waiting_input",
                                inputRequest={"question": "Which project code?"}) == {"recorded": True}
            await asyncio.wait_for(state.device_wake_task, 10)

        assert response.stop_reason == "end_turn"
        assert len(seen) == 1
        assert broker._consumed_seq(target()) == 1
        assert len(broker.wakes) == (0 if during_parent_turn else 1)
        assert broker.operations[start_request["operationId"]]["status"] == "waiting_input"
        await report(start_request, sid, 1, "waiting_input")
        if state.device_wake_task:
            await asyncio.wait_for(state.device_wake_task, 10)
        assert len(seen) == 1
        await report(start_request, sid, 2, "running")
        await report(start_request, sid, 3, "succeeded", summary="Complete", complete=True)
        if state.device_wake_task:
            await asyncio.wait_for(state.device_wake_task, 10)
        assert broker.operations[start_request["operationId"]]["status"] == "succeeded"
        assert broker._consumed_seq(target()) == 3
        assert len(seen) == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_deterministic_delegated_child_requests_input_and_next_local_turn_writes_file(tmp_path):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    class Connection:
        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, DeterministicLLM(), [], "test")
    await adapter.initialize(1, deviceDelegationReceiver={"version": 1})
    try:
        session = await adapter.new_session(str(tmp_path), [], session_id="device-child")
        sid = session.session_id
        response = await adapter.prompt(sid, [text_block("ask for project code then write result.txt")],
                                        turnId="delegated", deviceDelegation={
                                            "parentSessionId": "parent", "operationId": "device-operation",
                                        })
        assert response.field_meta["runStatus"] == "waiting_for_user"
        assert not (tmp_path / "result.txt").exists()
        log = adapter._sessions[sid].agent.session_log
        request = next(event["data"]["result"]["rawOutput"]
                       for event in log.events_of_type("tool/result")
                       if event["data"]["message"]["name"] == "request_user_input")
        assert request["question"] == "Which project code should I use?"
        assert request["missingFields"] == ["project_code"]
        response = await adapter.prompt(sid, [text_block("Use code RC12")], turnId="answer")
        assert response.field_meta["runStatus"] == "completed"
        assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "ok"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_stopped_child_cancel_retries_without_events_but_stops_on_definite_rejection(monkeypatch, permanent):
    import box_agent.device_delegation as delegation

    monkeypatch.setattr(delegation, "CANCEL_RETRY_INITIAL_SECONDS", 0.01)
    calls = []

    async def request(method, params):
        if params["operation"] == "session.start":
            return {**target(), "status": "accepted"}
        calls.append(params)
        if len(calls) == 1:
            return {"error": {"code": "TURN_NOT_AUTHORIZED" if permanent else "DEVICE_OFFLINE"}}
        return {"accepted": True}

    broker = DeviceDelegations("parent", request, lambda: "turn")
    result = await broker.start("d", "work")
    broker.stop("turn")
    try:
        await asyncio.wait_for(asyncio.gather(*broker.pending), 5)
        state = broker.operations[result["operationId"]]
        assert state["status"] == "accepted"
        if permanent:
            assert len(calls) == 1
            assert state["stopCancelRejected"] is True
        else:
            assert len(calls) == 2 and calls[0] == calls[1]
            assert state["stopCancelAcknowledged"] is True
    finally:
        await broker.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [
    ("session.get_turn", target()), ("session.wait", {"targets": [target()], "timeoutMs": 25}),
])
async def test_get_and_wait_enforce_local_deadline_without_inventing_child_state(monkeypatch, operation, arguments):
    import box_agent.device_delegation as delegation

    monkeypatch.setattr(delegation, "RPC_TIMEOUT_SECONDS", 0.01)
    inflight = asyncio.Event()

    async def request(method, params):
        try:
            await asyncio.Event().wait()
        finally:
            inflight.set()

    broker = DeviceDelegations("parent", request, lambda: "turn")
    if operation == "session.wait":
        bind_wait_targets(broker, *arguments["targets"])
    before = deepcopy(broker.operations)
    result = await asyncio.wait_for(DeviceTool(broker, operation).invoke(arguments), 1)
    assert inflight.is_set()
    assert not result.success
    assert result.raw_output == {"code": -32008, "message": "DEVICE_DELIVERY_UNKNOWN",
                                 "data": {"deviceId": "d", "delivery": "unknown"}}
    assert broker.operations == before


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_before_edit", [False, True])
async def test_history_edit_hides_removed_device_turn_and_keeps_late_event_receipts_after_reopen(
    tmp_path, monkeypatch, queued_before_edit,
):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.schema import StreamEvent
    from examples.device_parent_demo import DeterministicLLM

    requests = []
    dispatched = []

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            content = [message.content for message in messages if isinstance(message.content, str)]
            requests.append(content)
            if any("replacement request" in text or "follow-up request" in text for text in content):
                yield StreamEvent(type="text", delta="branch reply")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            async for event in super().generate_stream(messages, *args, **kwargs):
                yield event

    class Connection:
        async def ext_method(self, method, params):
            assert params["operation"] == "session.start"
            dispatched.append(params)
            return {**target(), "status": "accepted"}

        async def session_update(self, *args, **kwargs):
            pass

    def open_adapter():
        config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                        agent=AgentConfig(workspace_dir=str(tmp_path)))
        return BoxACPAgent(Connection(), config, Model(), [], "test")

    async def yield_to_queued_wake(session_id):
        await asyncio.sleep(0)

    monkeypatch.setattr("box_agent.acp.close_browser_session", yield_to_queued_wake)
    first = open_adapter()
    try:
        session = await first.new_session(str(tmp_path), [], device={"version": 1}, session_id="history-device")
        sid = session.session_id
        await first.prompt(sid, [text_block('{"deviceId":"d","instruction":"old task"}')], turnId="old-turn")
        state = first._sessions[sid]
        broker = state.device_delegations
        operation = dispatched[0]["operationId"]
        terminal = {"version": 1, "sessionId": sid, "event": {
            **target(), "operationId": operation, "seq": 1,
            "status": "succeeded", "complete": True, "summary": "OLD_BRANCH_RESULT",
        }}
        requests.clear()
        if queued_before_edit:
            assert await first.ext_method("device/event", terminal) == {"recorded": True}
            assert state.device_wake_task is not None and not state.device_wake_task.done()

        reply = await first.prompt(sid, [text_block("replacement request")], turnId="replacement",
                                  history_edit={"target_turn_id": "old-turn", "expected_revision_id": "initial"})
        assert reply.field_meta["history_revision_id"] == "replacement"
        if not queued_before_edit:
            assert await first.ext_method("device/event", terminal) == {"recorded": True}
        if state.device_wake_task:
            await asyncio.wait_for(state.device_wake_task, 10)
        assert len(requests) == 1
        assert all("OLD_BRANCH_RESULT" not in text and '"status": "accepted"' not in text for text in requests[0])
        assert not broker.wakes
        assert broker.operations[operation]["status"] == "succeeded"
    finally:
        await first.aclose()

    reopened = open_adapter()
    try:
        session = await reopened.new_session(str(tmp_path), [], device={"version": 1}, session_id="history-device")
        state = reopened._sessions[session.session_id]
        broker = state.device_delegations
        assert broker.operations[operation]["status"] == "succeeded"
        assert await reopened.ext_method("device/event", terminal) == {"recorded": True}
        if state.device_wake_task:
            await asyncio.wait_for(state.device_wake_task, 10)
        assert not broker.wakes
        requests.clear()
        await reopened.prompt(session.session_id, [text_block("follow-up request")], turnId="follow-up")
        assert len(requests) == 1
        assert all("OLD_BRANCH_RESULT" not in text for text in requests[0])
        assert len(dispatched) == 1
    finally:
        await reopened.aclose()


@pytest.mark.asyncio
async def test_rebinding_device_events_retry_without_starting_work_on_the_closed_broker(tmp_path, monkeypatch):
    from acp import RequestError
    from box_agent.acp import BoxACPAgent
    from box_agent.agent_session import AgentSession
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    calls = []

    class Connection:
        async def ext_method(self, method, params):
            calls.append(params)
            return {"accepted": True}

        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, DeterministicLLM(), [], "test")
    enter_close = asyncio.Event()
    finish_close = asyncio.Event()
    try:
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="close-fence")
        sid = session.session_id
        state = adapter._sessions[sid]
        old_broker = state.device_delegations
        old_broker.save({"operationId": "operation", "deviceId": "d", "parentTurnId": "parent",
                         "status": "unknown_delivery", "seq": 0})
        old_broker.stop("parent")
        original_close = AgentSession.aclose

        async def paused_close(self):
            if self is state:
                enter_close.set()
                await finish_close.wait()
            await original_close(self)

        monkeypatch.setattr(AgentSession, "aclose", paused_close)
        rebind = asyncio.create_task(adapter.resume_session(str(tmp_path), sid, device={"version": 1}))
        await asyncio.wait_for(enter_close.wait(), 5)
        envelope = {"version": 1, "sessionId": sid, "event": {
            **target(), "seq": 1, "status": "running", "operationId": "operation",
        }}
        try:
            assert old_broker.closed
            with pytest.raises(RequestError) as caught:
                await adapter.ext_method("device/event", envelope)
            assert caught.value.code == -32010
            assert not old_broker.pending and not calls
            assert old_broker.operations["operation"]["status"] == "unknown_delivery"
        finally:
            finish_close.set()
        await asyncio.wait_for(rebind, 5)
        new_broker = adapter._sessions[sid].device_delegations
        assert new_broker is not old_broker
        assert await adapter.ext_method("device/event", envelope) == {"recorded": True}
        await asyncio.wait_for(asyncio.gather(*new_broker.pending), 5)
        assert len(calls) == 1 and calls[0]["operation"] == "session.cancel"
        assert not old_broker.pending
    finally:
        finish_close.set()
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("rebind_while_waiting", [False, True])
async def test_parent_waiting_for_its_user_holds_child_result_until_actual_reply(tmp_path, rebind_while_waiting):
    from acp import text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import DeterministicLLM

    calls = []
    ready_model_requests = []

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            for message in messages:
                if (message.source == "runtime" and isinstance(message.content, str)
                        and "CHILD_READY_FOR_PARENT" in message.content):
                    ready_model_requests.append(message.content)
            async for event in super().generate_stream(messages, *args, **kwargs):
                yield event

    class Connection:
        async def ext_method(self, method, params):
            calls.append(params)
            return {**target(), "status": "accepted"}

        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, Model(), [], "test")
    try:
        session = await adapter.new_session(str(tmp_path), [], device={"version": 1}, session_id="ask-parent")
        sid = session.session_id
        await adapter.prompt(sid, [text_block('{"deviceId":"d","instruction":"run"}')], turnId="first")
        wait = await adapter.prompt(sid, [text_block("ask for project code then write result.txt")], turnId="ask")
        assert wait.field_meta["runStatus"] == "waiting_for_user"
        if rebind_while_waiting:
            await adapter.resume_session(str(tmp_path), sid, device={"version": 1})
        state = adapter._sessions[sid]
        broker = state.device_delegations
        assert state.waiting_for_user_input
        envelope = {"version": 1, "sessionId": sid, "event": {
            **target(), "seq": 1, "status": "succeeded", "operationId": calls[0]["operationId"],
            "complete": True, "summary": "CHILD_READY_FOR_PARENT",
        }}
        assert await adapter.ext_method("device/event", envelope) == {"recorded": True}
        await asyncio.sleep(0)
        assert state.waiting_for_user_input and not state.device_wake_task and not ready_model_requests
        assert broker.operations[calls[0]["operationId"]]["status"] == "succeeded"
        answered = await adapter.prompt(sid, [text_block("Use code RC12")], turnId="answer")
        assert answered.field_meta["runStatus"] == "completed"
        assert not state.waiting_for_user_input
        assert len(ready_model_requests) == 1
        assert not broker.wakes
        assert broker._consumed_seq(target()) == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("allow", [True, False])
async def test_trusted_local_answer_keeps_original_device_approval_identity_and_cannot_be_replayed(
    tmp_path, monkeypatch, allow,
):
    from acp import RequestError, text_block
    from acp.schema import AllowedOutcome, RequestPermissionResponse
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from examples.device_parent_demo import ASK_FOR_PROJECT_CODE_WITH_APPROVAL, DeterministicLLM

    home = tmp_path / "user-home"
    workspace = tmp_path / "work"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("DEVICE_DEMO_APPROVAL_HOME", str(home))
    target_file = home / "device-demo-approval.txt"
    requests = []

    class Connection:
        async def request_permission(self, session_id, tool_call, options):
            requests.append((session_id, tool_call.raw_input, [opt.kind for opt in options]))
            return RequestPermissionResponse(outcome=AllowedOutcome(
                outcome="selected", option_id="approve" if allow else "reject",
            ))

        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(workspace)))
    adapter = BoxACPAgent(Connection(), config, DeterministicLLM(), [], "test")
    await adapter.initialize(1, deviceDelegationReceiver={"version": 1})
    try:
        session = await adapter.new_session(str(workspace), [], session_id="ask-approval",
                                            permissionMode="default")
        sid = session.session_id
        origin = {"parentSessionId": "cloud", "parentTurnId": "cloud-turn", "operationId": "delegation"}
        awaiting = await adapter.prompt(sid, [text_block(ASK_FOR_PROJECT_CODE_WITH_APPROVAL)],
                                        turnId="root-device-turn", deviceDelegation=origin)
        assert awaiting.field_meta["runStatus"] == "waiting_for_user"
        assert not target_file.exists() and not requests

        log = adapter._sessions[sid].agent.session_log
        call_id = next(event["data"]["message"]["tool_call_id"]
                       for event in log.events_of_type("tool/result")
                       if event["data"]["message"]["name"] == "request_user_input")
        response = {
            "request_id": call_id, "tool_call_id": call_id, "request_type": "input",
            "decision_kind": "missing_input", "missing_fields": ["project_code"],
            "custom_text": "Use code RC12", "trigger": "user",
        }
        continuation = {**origin, "continuationOfTurnId": "root-device-turn"}
        prompt = [text_block("Use code RC12")]
        with pytest.raises(RequestError, match="CONTINUATION_MISMATCH"):
            await adapter.prompt("another-session", prompt, turnId="forged", deviceDelegation=continuation,
                                 userDecision=response)
        assert list(adapter._sessions) == [sid]
        with pytest.raises(RequestError, match="CONTINUATION_INVALID_RESPONSE"):
            await adapter.prompt(sid, prompt, turnId="forged", deviceDelegation=continuation,
                                 userDecision={**response, "request_id": "forged-request-id"})
        with pytest.raises(RequestError, match="CONTINUATION_MISMATCH"):
            await adapter.prompt(sid, prompt, turnId="wrong-parent", userDecision=response,
                                 deviceDelegation={**continuation, "parentTurnId": "another-parent-turn"})
        assert not requests and not target_file.exists()

        # Verify the proof is durable and the same logical turn survives binding changes.
        await adapter.resume_session(str(workspace), sid, permissionMode="default")
        camel_response = {
            "requestId": call_id, "toolCallId": call_id, "requestType": "input",
            "decisionKind": "missing_input", "missingFields": ["project_code"],
            "customText": "Use code RC12", "trigger": "user",
        }
        with pytest.raises(RequestError, match="CONTINUATION_INVALID_RESPONSE"):
            await adapter.prompt(sid, prompt, turnId="bad-type", deviceDelegation=continuation,
                                 userDecision={**camel_response, "customText": 12})
        result = await adapter.prompt(sid, prompt, turnId="answer", deviceDelegation=continuation,
                                      userDecision=camel_response if allow else response)
        assert result.field_meta["runStatus"] == "completed"
        assert len(requests) == 1
        approval_sid, approval, options = requests[0]
        assert approval_sid == sid
        assert approval["deviceApprovalTurnId"] == "root-device-turn"
        assert approval["exactFilesystem"] == {
            "path": str(target_file.resolve()), "operation": "write", "toolName": "write_file",
        }
        assert options == ["allow_once", "reject_once"]
        assert target_file.exists() is allow
        if allow:
            assert target_file.read_text(encoding="utf-8") == "ok"
        log = adapter._sessions[sid].agent.session_log
        accepted = next(event["data"] for event in log.events_of_type("user/message")
                        if event["data"].get("input_facts", {}).get("id") == "answer")
        assert accepted["source"] == "user"
        assert accepted["input_facts"]["deviceDelegation"] == continuation
        assert accepted["input_facts"]["kind"] == "device_delegation_continuation"
        assert accepted["input_facts"]["policy"]["user_decision_response"]["custom_text"] == "Use code RC12"

        with pytest.raises(ValueError, match="TURN_ALREADY_ACCEPTED"):
            await adapter.prompt(sid, prompt, turnId="answer", deviceDelegation=continuation,
                                 userDecision=camel_response if allow else response)
        with pytest.raises(RequestError, match="CONTINUATION_MISMATCH"):
            await adapter.prompt(sid, prompt, turnId="another-answer", deviceDelegation=continuation,
                                 userDecision=response)
        with pytest.raises(RequestError, match="CONTINUATION_NOT_WAITING"):
            await adapter.prompt(sid, prompt, turnId="unasked-follow-up",
                                 deviceDelegation={**continuation, "continuationOfTurnId": "answer"},
                                 userDecision=response)
        assert len(requests) == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_delegated_decision_requires_a_recorded_choice_and_keeps_timeout_provenance(tmp_path):
    from acp import RequestError, text_block
    from box_agent.acp import BoxACPAgent
    from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
    from box_agent.schema import StreamEvent
    from examples.device_parent_demo import DeterministicLLM

    class Model(DeterministicLLM):
        async def generate_stream(self, messages, *args, **kwargs):
            latest = next(message.input_facts for message in reversed(messages) if message.input_facts)
            if latest["id"] == "device-turn":
                yield self._tool_call("request_user_decision", {
                    "question": "Which output format should I use?", "decision_kind": "delivery_format",
                    "options": [{"id": "text", "label": "Text"}, {"id": "markdown", "label": "Markdown"}],
                    "default_option_id": "text", "requested_auto_submit_seconds": 30,
                    "risk_level": "low", "reversible": True, "preserves_user_intent": True,
                })
                return
            yield StreamEvent(type="text", delta="Decision applied.")
            yield StreamEvent(type="finish", finish_reason="stop")

    class Connection:
        async def session_update(self, *args, **kwargs):
            pass

    config = Config(tools=ToolsConfig(), llm=LLMConfig(api_key="test"),
                    agent=AgentConfig(workspace_dir=str(tmp_path)))
    adapter = BoxACPAgent(Connection(), config, Model(), [], "test")
    await adapter.initialize(1, deviceDelegationReceiver={"version": 1})
    try:
        session = await adapter.new_session(str(tmp_path), [], session_id="structured-decision")
        sid = session.session_id
        origin = {"parentSessionId": "cloud", "operationId": "structured"}
        initial = await adapter.prompt(sid, [text_block("Ask about the format")],
                                       turnId="device-turn", deviceDelegation=origin)
        assert initial.field_meta["runStatus"] == "waiting_for_user"
        log = adapter._sessions[sid].agent.session_log
        tool_result = next(event["data"] for event in log.events_of_type("tool/result")
                           if event["data"]["message"]["name"] == "request_user_decision")
        decision = tool_result["result"]["rawOutput"]
        raw = {
            "request_id": decision["requestId"], "tool_call_id": tool_result["message"]["tool_call_id"],
            "request_type": "decision", "decision_kind": "delivery_format",
            "selected_option_id": "text", "trigger": "timeout",
        }
        continuation = {**origin, "continuationOfTurnId": "device-turn"}
        with pytest.raises(RequestError, match="CONTINUATION_INVALID_RESPONSE"):
            await adapter.prompt(sid, [text_block("Pick a format")], turnId="bad",
                                 deviceDelegation=continuation,
                                 userDecision={**raw, "selected_option_id": "markdown"})
        response = await adapter.prompt(sid, [text_block("Default format is text")], turnId="default",
                                        deviceDelegation=continuation, userDecision=raw)
        assert response.field_meta["runStatus"] == "completed"
        saved = next(event["data"] for event in log.events_of_type("user/message")
                     if event["data"].get("input_facts", {}).get("id") == "default")
        assert saved["source"] == "runtime"
        assert saved["input_facts"]["policy"]["user_decision_response"]["trigger"] == "timeout"
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_history_edit_redelivers_retained_child_result_when_its_model_receipt_is_removed(tmp_path):
    from box_agent.session_history import revision_request
    from box_agent.session_log import SessionLog

    async def request(method, params):
        return {**target(), "status": "accepted"}

    log = SessionLog.create(tmp_path / "logs", session_id="branch", cwd=tmp_path)
    broker = DeviceDelegations("parent", request, lambda: "kept-parent", log)
    try:
        result = await broker.start("d", "work")
        broker.end("kept-parent")
        log.append("turn/binding", {"turn_id": "discarded-parent", "prompt_fingerprint": "fixture"})
        broker.event({"version": 1, "sessionId": "parent", "event": {
            **target(), "seq": 1, "status": "succeeded", "operationId": result["operationId"],
            "complete": True, "summary": "result of retained device work",
        }})
        discarded_wake = broker.next_wake()
        assert discarded_wake
        receipt = {"device": {broker._cursor_key(target()): 1}}
        log.append("assistant/message", {
            "turn": 2, "step": 1, "message": {"role": "assistant", "content": "I saw the result."},
            "runtimeInputReceipts": receipt,
        })
        broker.confirm_receipts(receipt)
        assert broker._consumed_seq(target()) == 1 and broker.next_wake() is None
        revision = revision_request(list(log.events), target_turn_id="discarded-parent",
                                    turn_id="replacement", expected_revision_id="initial", fingerprint="edited")
        log.append("history/revise", revision)
        log.flush()
        broker.reconcile_history()
        assert broker._consumed_seq(target()) == 0
        assert "result of retained device work" in broker.request_injections([])[0].content
        replacement_wake = broker.next_wake()
        assert replacement_wake and replacement_wake != discarded_wake
        reopened = DeviceDelegations("parent", request, lambda: "replacement", log)
        assert reopened.next_wake() == replacement_wake
    finally:
        await broker.aclose()
        log.close()


def test_consumed_child_turn_lookup_uses_restored_index_without_rescanning_operations():
    log = Log()
    broker = DeviceDelegations("parent", None, lambda: "p", log)
    for i in range(128):
        broker.save({**target(turn=str(i)), "operationId": f"op-{i}", "parentTurnId": "p",
                     "status": "succeeded", "seq": 2})
    restored = DeviceDelegations("parent", None, lambda: "p", log)

    class IndexedOperations(dict):
        def items(self):
            pytest.fail("a turn lookup rescanned the operation table")

    restored.operations = IndexedOperations(restored.operations)
    restored.consumed = {op: 2 for op in restored.operations}
    assert restored.request_injections([]) == ()
    assert restored._consumed_seq(target(turn="127")) == 2
    assert restored._operation_for(target(turn="missing")) is None

def test_empty_device_offers_skip_history_hashing(monkeypatch):
    from box_agent.schema import Message

    broker = DeviceDelegations("p", None, lambda: "parent")
    history = [Message(role="tool", content="unrelated tool output")]
    monkeypatch.setattr("box_agent.device_delegation.sha256", lambda *_: pytest.fail("unexpected hash"))
    assert broker.request_injections(history) == ()
    assert broker.response_receipts(history) == {}


def test_wait_timeout_rejects_malformed_status_before_checking_readiness():
    broker = DeviceDelegations("p", None, lambda: "parent")
    with pytest.raises(ValueError, match="invalid device status snapshot"):
        broker._wait_result({
            "timedOut": True, "results": [], "states": [{**target(), "status": {}, "source": "device"}],
        }, [target()])
