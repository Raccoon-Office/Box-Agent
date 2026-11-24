from copy import deepcopy

import pytest

from box_agent.device_delegation import DeviceDelegations, DeviceTool


class Log:
    def __init__(self):
        self.events = []

    def append(self, kind, data, **kwargs):
        self.events.append({"type": kind, "data": deepcopy(data)})

    def flush(self):
        pass

    def has_committed_input(self, turn):
        return False


target = {"deviceId": "workstation", "sessionId": "child", "turnId": "child-turn"}
ref = {"deviceId": target["deviceId"], "artifactId": "revision", "name": "report.csv"}
uri = "file:///sandbox/report.csv"


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [True, False])
async def test_cloud_delivery_survives_restart_and_wakes_parent_once(required):
    state = {}
    calls = []

    async def request(_, params):
        calls.append(params)
        if params["operation"] == "session.start":
            return {**target, "status": "accepted"}
        if params["operation"] == "session.get_turn":
            return state
        if params["operation"] == "session.wait":
            return {"timedOut": False, "results": [state]}
        return {**target, **ref, "cloudFileRef": uri}

    log = Log()
    def reopen():
        return DeviceDelegations("parent", request, lambda: "turn", log, artifact_upload=True)

    broker = reopen()
    args = {"deviceId": target["deviceId"], "instruction": "prepare report"}
    if required:
        args["artifactDelivery"] = "cloud"
    assert (await DeviceTool(broker, "session.start").invoke(args)).success
    first = calls[0]
    assert first["args"] == {key: value for key, value in args.items() if key != "deviceId"}
    op = first["operationId"]

    def update(seq, artifact):
        state.update({**target, "operationId": op, "status": "succeeded", "complete": True, "source": "device",
                      "seq": seq, "summary": "done", "result": {"artifactRefs": [artifact]}})
        return {"version": 1, "sessionId": "parent", "event": dict(state)}

    broker.event(update(1, ref))
    broker.end("turn")
    got = await broker.query("session.get_turn", target)
    if required:
        assert got["deliveryStatus"] == "pending" and got["complete"] is False
        assert broker.next_wake() is None
    else:
        assert got["status"] == "succeeded"
        assert broker.next_wake()
        broker.confirm_receipts({"device": {broker._cursor_key(target): 1}})
    enriched = update(2, {**ref, "cloudFileRef": uri})
    broker.event(enriched)
    broker.event(enriched)
    if not required:
        assert broker.next_wake() is None
    restored = reopen()
    assert restored.operations[op]["result"]["artifactRefs"] == [{**ref, "cloudFileRef": uri}]
    if required:
        assert restored.next_wake()
        assert restored.operations[op]["seq"] == 2
    for result in [
        await restored.query("session.get_turn", target),
        (await restored.query("session.wait", {"targets": [target]}))["results"][0],
    ]:
        assert result["result"]["artifactRefs"][0]["cloudFileRef"] == uri
    assert (await DeviceTool(restored, "artifact.upload").invoke({**target, "artifactId": ref["artifactId"]})).success
    with pytest.raises(ValueError, match="not owned"):
        await restored.query("artifact.upload", {**target, "turnId": "other", "artifactId": "revision"})
    with pytest.raises(ValueError, match="conflicting"):
        restored.event(update(3, {**ref, "cloudFileRef": uri + "-other"}))
    with pytest.raises(ValueError, match="unknown terminal"):
        restored.event(update(3, {**ref, "artifactId": "other", "cloudFileRef": uri}))


@pytest.mark.asyncio
async def test_transfer_failure_is_ready_and_older_sessions_keep_the_existing_tool_schema():
    async def request(_, params):
        return {**target, "status": "accepted"}
    old = DeviceDelegations("p", request, lambda: "t")
    assert "artifactDelivery" not in DeviceTool(old, "session.start").parameters["properties"]
    with pytest.raises(ValueError, match="unsupported"):
        DeviceTool(old, "artifact.upload")
    run = DeviceDelegations("p", request, lambda: "t", Log(), artifact_upload=True)
    ack = await run.start("workstation", "prepare", artifact_delivery="cloud")
    event = {**target, "operationId": ack["operationId"], "seq": 1, "status": "failed",
             "deliveryStatus": "failed", "error": "SOURCE_CHANGED", "result": {"artifactRefs": [ref]}}
    run.event({"version": 1, "sessionId": "p", "event": event})
    run.end("t")
    assert run.next_wake()
    assert "SOURCE_CHANGED" in run.request_injections([])[0].content
