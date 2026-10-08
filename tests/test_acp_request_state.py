"""Payload ownership and SDK dispatch/reply contracts for long-lived ACP."""

import asyncio
from contextlib import asynccontextmanager
import gc
import json
from types import SimpleNamespace
import weakref

import pytest
from acp.connection import Connection
from acp.exceptions import RequestError
from acp.task.dispatcher import DefaultMessageDispatcher
from acp.task.queue import InMemoryMessageQueue
from acp.task.supervisor import TaskSupervisor

from box_agent.acp.request_state import RequestStateStore


class Payload:
    def __init__(self):
        self.content = bytearray(256 * 1024)


@pytest.mark.parametrize("outcome", ["success", "error"])
def test_finished_request_does_not_keep_body_result_or_error_locals(outcome):
    store = RequestStateStore()

    def finish():
        body, response = Payload(), Payload()
        refs = weakref.ref(body), weakref.ref(response)
        record = store.begin_incoming("session/prompt", {"body": body})
        if outcome == "success":
            store.complete_incoming(record, {"response": response})
        else:
            try:
                raise RuntimeError("request failed with locals in its traceback")
            except RuntimeError as error:
                store.fail_incoming(record, error)
        return record, refs

    # Even if a diagnostic caller holds the record, it must not retain payloads.
    record, refs = finish()
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert record.status == ("completed" if outcome == "success" else "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_cancelled_sdk_request_releases_body_without_completion_callback(started):
    store = RequestStateStore()
    supervisor = TaskSupervisor(source="request-state-test")
    entered = asyncio.Event()

    async def request(message):
        entered.set()
        await asyncio.Event().wait()

    async def notification(message):
        pass

    dispatcher = DefaultMessageDispatcher(
        queue=InMemoryMessageQueue(), supervisor=supervisor, store=store,
        request_runner=request, notification_runner=notification,
    )
    body = Payload()
    ref = weakref.ref(body)
    try:
        await dispatcher._dispatch_request({"method": "session/new", "params": body})
        del body
        if started:
            await asyncio.wait_for(entered.wait(), 2)
    finally:
        # Call inline so the False case cancels before the child ever starts.
        await supervisor.shutdown()
    assert entered.is_set() is started
    await asyncio.sleep(0)
    gc.collect()
    assert ref() is None


@pytest.mark.asyncio
async def test_host_reply_futures_survive_unrelated_incoming_requests():
    store = RequestStateStore()
    permission_a = store.register_outgoing(10, "session/request_permission")
    permission_b = store.register_outgoing(11, "session/request_permission")
    host_read = store.register_outgoing(12, "fs/read_text_file")
    for _ in range(32):
        store.complete_incoming(store.begin_incoming("session/prompt", {}), {"stopReason": "end_turn"})
    assert not permission_a.done() and not permission_b.done() and not host_read.done()
    store.resolve_outgoing(11, {"outcome": "selected", "session": "b"})
    assert await permission_b == {"outcome": "selected", "session": "b"}
    assert not permission_a.done()
    store.reject_outgoing(12, RequestError(-32000, "host read failed"))
    with pytest.raises(RequestError, match="host read failed"):
        await host_read
    store.reject_all_outgoing(ConnectionError("host disconnected"))
    with pytest.raises(ConnectionError, match="host disconnected"):
        await permission_a


@pytest.mark.asyncio
async def test_sdk_delivers_response_and_error_before_releasing_request_payload():
    writes = asyncio.Queue()

    class Writer:
        def write(self, data):
            writes.put_nowait(json.loads(data))

        async def drain(self):
            await asyncio.sleep(0)

    async def handle(method, params, is_notification):
        if method == "_fail":
            raise RequestError(-32010, "SESSION_BUSY", {"sessionId": params["sessionId"]})
        return {"sessionId": params["sessionId"], "content": params["content"]}

    reader = asyncio.StreamReader()
    store = RequestStateStore()
    connection = Connection(handle, Writer(), reader, state_store=store)
    try:
        for request_id, method in enumerate(["_echo", "_fail", "_echo"]):
            content = "response-body-" * 100
            reader.feed_data((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                "params": {"sessionId": "a" if request_id % 2 == 0 else "b", "content": content}}) + "\n").encode())
            response = await asyncio.wait_for(writes.get(), 2)
            assert response["id"] == request_id
            if method == "_fail":
                assert response["error"] == {"code": -32010, "message": "SESSION_BUSY", "data": {"sessionId": "b"}}
            else:
                assert response["result"] == {"sessionId": "a", "content": content}
    finally:
        reader.feed_eof()
        await asyncio.wait_for(connection.close(), 2)


@asynccontextmanager
async def outgoing_connection():
    writes = asyncio.Queue()
    sent = asyncio.Queue()
    drain_entered = asyncio.Event()
    drain_release = asyncio.Event()

    class Writer:
        block = False
        failure = None

        def write(self, data):
            writes.put_nowait(json.loads(data))

        async def drain(self):
            if self.block:
                drain_entered.set()
                await drain_release.wait()
            if self.failure is not None:
                raise self.failure

    async def handle(method, params, notification):
        return {}

    writer = Writer()
    reader = asyncio.StreamReader()
    store = RequestStateStore()
    connection = Connection(
        handle, writer, reader, state_store=store, sender_factory=store.create_sender,
    )
    connection.add_observer(lambda event: sent.put_nowait(event.message)
                            if event.direction.value == "outgoing" else None)

    async def reply(request_id, *, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": request_id}
        message.update({"error": error} if error is not None else {"result": result})
        reader.feed_data((json.dumps(message) + "\n").encode())
        # A subsequent inbound request proves the reply has been consumed.
        reader.feed_data(b'{"jsonrpc":"2.0","id":999,"method":"_barrier","params":{}}\n')
        while True:
            response = await asyncio.wait_for(sent.get(), 2)
            if response.get("id") == 999 and "result" in response:
                return

    try:
        yield SimpleNamespace(
            connection=connection, store=store, writer=writer, reader=reader,
            writes=writes, sent=sent, drain_entered=drain_entered,
            drain_release=drain_release, reply=reply,
        )
    finally:
        drain_release.set()
        reader.feed_eof()
        try:
            if writer.failure is None:
                await asyncio.wait_for(connection.close(), 2)
            else:
                # The SDK re-raises its sender failure on close. Assert that
                # expected transport error as well as releasing its tasks.
                with pytest.raises(OSError, match="host pipe failed"):
                    await asyncio.wait_for(connection.close(), 2)
        finally:
            await asyncio.wait_for(connection._tasks.shutdown(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["sending", "reply"])
@pytest.mark.parametrize("finish", ["cancel", "timeout"])
async def test_abandoned_host_request_is_released_without_affecting_peer(phase, finish):
    async with outgoing_connection() as link:
        peer = asyncio.create_task(link.connection.send_request("fs/read_text_file", {"sessionId": "b"}))
        peer_wire = await asyncio.wait_for(link.sent.get(), 2)
        link.writer.block = phase == "sending"
        request = asyncio.create_task(link.connection.send_request("session/request_permission", {"sessionId": "a"}))
        if phase == "sending":
            await asyncio.wait_for(link.drain_entered.wait(), 2)
        else:
            await asyncio.wait_for(link.sent.get(), 2)
        abandoned_id = next(key for key in link.store._outgoing if key != peer_wire["id"])
        abandoned_future = link.store._outgoing[abandoned_id].future
        if finish == "timeout":
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(request, .01)
        else:
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        await asyncio.sleep(0)
        assert abandoned_future.cancelled()
        assert set(link.store._outgoing) == {peer_wire["id"]}
        assert not peer.done()
        link.drain_release.set()
        # Duplicate, late and unknown replies must not complete the live peer.
        for request_id in (abandoned_id, abandoned_id, 123456):
            await link.reply(request_id, result={"outcome": "selected"})
            assert not peer.done()
        await link.reply(peer_wire["id"], result={"content": "peer result"})
        assert await peer == {"content": "peer result"}
        assert not link.store._outgoing


@pytest.mark.asyncio
async def test_serialization_failure_releases_request_before_owner_task_finishes():
    async with outgoing_connection() as link:
        peer = asyncio.create_task(link.connection.send_request("fs/read_text_file", {}))
        peer_wire = await asyncio.wait_for(link.sent.get(), 2)
        for _ in range(8):
            with pytest.raises(TypeError):
                await link.connection.send_request("session/request_permission", {"bad": object()})
            assert set(link.store._outgoing) == {peer_wire["id"]}
        # A response ID may equal a live outgoing ID; it is a separate namespace.
        for message in (
            {"id": peer_wire["id"], "result": object()},
            {"method": "session/update", "params": object()},
        ):
            with pytest.raises(TypeError):
                await link.connection._sender.send({"jsonrpc": "2.0", **message})
            assert set(link.store._outgoing) == {peer_wire["id"]}
        await link.reply(peer_wire["id"], result={"content": "still live"})
        assert await peer == {"content": "still live"}


@pytest.mark.asyncio
async def test_transport_send_failure_releases_unawaited_host_reply():
    async with outgoing_connection() as link:
        link.writer.failure = OSError("host pipe failed")
        with pytest.raises(OSError, match="host pipe failed"):
            await asyncio.wait_for(link.connection.send_request("session/request_permission", {}), 2)
        assert not link.store._outgoing


@pytest.mark.asyncio
async def test_host_error_before_send_cancellation_does_not_leave_unretrieved_future():
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
        async with outgoing_connection() as link:
            link.writer.block = True
            request = asyncio.create_task(link.connection.send_request("session/request_permission", {}))
            wire = await asyncio.wait_for(link.writes.get(), 2)
            await asyncio.wait_for(link.drain_entered.wait(), 2)
            future = link.store._outgoing[wire["id"]].future
            settled = asyncio.Event()
            future.add_done_callback(lambda _: settled.set())
            link.reader.feed_data((json.dumps({"jsonrpc": "2.0", "id": wire["id"],
                "error": {"code": -32001, "message": "host refused"}}) + "\n").encode())
            await asyncio.wait_for(settled.wait(), 2)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not link.store._outgoing
            del future, request
        gc.collect()
        await asyncio.sleep(0)
        assert not errors
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["reply", "cancel_last"])
async def test_shared_permission_rpc_stays_live_until_last_waiter_finishes(tmp_path, finish):
    from acp.schema import RequestPermissionResponse
    from box_agent.acp import _PermissionNegotiator
    from box_agent.tools.permissions import GrantStore

    async with outgoing_connection() as link:
        class Host:
            async def requestPermission(self, params):
                result = await link.connection.send_request(
                    "session/request_permission", params.model_dump(mode="json", by_alias=True),
                )
                return RequestPermissionResponse.model_validate(result)

        grants = GrantStore()
        negotiator = _PermissionNegotiator(Host(), "a", grants)
        params = {"scope": "filesystem", "requested_scope": "user_home",
                  "path": str(tmp_path), "reason": "Read test directory"}
        first = asyncio.create_task(negotiator.negotiate(params))
        second = asyncio.create_task(negotiator.negotiate(params))
        wire = await asyncio.wait_for(link.sent.get(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        assert list(link.store._outgoing) == [wire["id"]]
        future = link.store._outgoing[wire["id"]].future
        if finish == "reply":
            await link.reply(wire["id"], result={"outcome": {"outcome": "selected", "optionId": "approve"}})
            assert await asyncio.wait_for(second, 2) is True
            assert grants.has_filesystem_dir_grant(tmp_path.resolve())
        else:
            settled = asyncio.Event()
            future.add_done_callback(lambda _: settled.set())
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            await asyncio.wait_for(settled.wait(), 2)
            assert future.cancelled()
            assert not grants.has_filesystem_dir_grant(tmp_path.resolve())
        assert not link.store._outgoing
