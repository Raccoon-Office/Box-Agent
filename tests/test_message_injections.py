"""Message and host-event contracts for typed runtime injections."""

from dataclasses import asdict

import pytest

from box_agent.injections import (
    InjectionKind, InjectionManager, InjectionStatus, MessageInjection,
    parse_queued_injection,
)
from box_agent.schema import Message


@pytest.mark.parametrize("kind", list(InjectionKind))
def test_injection_type_selects_framing_source_and_visibility(kind):
    original = Message(role="user", content="Create a report")
    history = [original]
    content = "First line.\nSecond line with {literal braces}."
    event = MessageInjection(kind, content, injection_id="receipt-1").apply(history)

    assert history[0] is original
    assert len(history) == 2
    message = history[-1]
    assert message.role == "user"
    assert "source" not in message.model_dump()
    assert asdict(event) == {
        "content": content, "injection_id": "receipt-1",
        "user_visible": kind is InjectionKind.USER_SUPPLEMENT,
    }
    if kind is InjectionKind.USER_SUPPLEMENT:
        assert message.source == "user"
        assert message.content.startswith("The user sent the following message")
        assert message.content.endswith("Mid-turn user message:\n" + content)
        assert "Do not stop or switch tasks unless the user explicitly asks" in message.content
    else:
        assert message.source == "runtime"
        assert "The user sent" not in message.content
        if kind in {
            InjectionKind.STREAM_RECOVERY, InjectionKind.REQUEST_RECOVERY,
            InjectionKind.TEXT_CONTINUATION, InjectionKind.TASK_CONTINUATION,
        }:
            assert message.content == content
        else:
            assert message.content.startswith("The host runtime supplied")
            assert message.content.endswith("Runtime state update:\n" + content)


@pytest.mark.parametrize("item,kind,identifier,visible", [
    ("extra context", InjectionKind.USER_SUPPLEMENT, None, True),
    ({"content": "extra context", "id": "u1"}, InjectionKind.USER_SUPPLEMENT, "u1", True),
    ({"content": "connected", "source": "runtime", "id": "r1", "user_visible": False},
     InjectionKind.RUNTIME_STATE, "r1", False),
    ({"content": "connected", "source": "runtime"}, InjectionKind.RUNTIME_STATE, None, True),
    ({"content": "extra context", "id": 42, "user_visible": "false", "source": "unknown"},
     InjectionKind.USER_SUPPLEMENT, None, True),
    ({"content": "extra context", "kind": "budget"}, InjectionKind.USER_SUPPLEMENT, None, True),
])
def test_existing_queue_payloads_keep_their_routing_and_receipts(item, kind, identifier, visible):
    injection = parse_queued_injection(item)
    assert injection is not None
    assert injection.kind is kind
    history = []
    event = injection.apply(history)
    assert event.injection_id == identifier
    assert event.user_visible is visible
    assert event.content == (item if isinstance(item, str) else item["content"])
    assert history[-1].source == ("user" if kind is InjectionKind.USER_SUPPLEMENT else "runtime")


@pytest.mark.parametrize("item", ["", {}, {"content": ""}, {"content": None}])
def test_empty_queue_payload_does_not_create_an_injection(item):
    assert parse_queued_injection(item) is None


def test_unknown_type_fails_before_changing_conversation():
    history = [Message(role="user", content="original")]
    with pytest.raises(KeyError):
        MessageInjection("unknown", "payload").apply(history)
    assert len(history) == 1
    assert history[0].content == "original"


def test_source_cannot_misrepresent_the_information_type():
    with pytest.raises(ValueError, match="source"):
        MessageInjection(InjectionKind.USER_SUPPLEMENT, "extra", source="runtime")
    with pytest.raises(ValueError, match="source"):
        MessageInjection(InjectionKind.BUDGET, "stop", source="user")


def test_pending_and_injected_receipts_follow_actual_history_insertion():
    manager = InjectionManager()
    history = []
    item = {"id": "user-1", "content": "use Chinese"}
    assert manager.submit(item)
    item["content"] = "caller mutation"
    assert manager.status("user-1") is InjectionStatus.PENDING
    assert history == []
    injection, event = manager.apply_next(history)
    assert injection.source == "user"
    assert manager.status("user-1") is InjectionStatus.INJECTED
    assert event.injection_id == "user-1"
    assert event.content == "use Chinese"
    assert history[0].content.endswith("use Chinese")
    assert not manager.submit(item)
    assert not manager.cancel("user-1")
    assert not manager.submit(item)


@pytest.mark.asyncio
async def test_cancel_preserves_fifo_and_allows_only_cancelled_id_to_be_reused():
    manager = InjectionManager()
    await manager.put({"id": "one", "content": "first"})
    manager.put_nowait({"id": "two", "content": "cancel me"})
    manager.put_nowait(MessageInjection(
        InjectionKind.RUNTIME_STATE, "connected", "three",
    ))
    assert manager.cancel("two")
    assert manager.status("two") is InjectionStatus.CANCELLED
    assert not manager.cancel("two")
    assert manager.submit({"id": "two", "content": "replacement"})
    history = []
    events = [manager.apply_next(history)[1] for _ in range(3)]
    assert [event.injection_id for event in events] == ["one", "three", "two"]
    assert [message.source for message in history] == ["user", "runtime", "user"]
    assert [event.user_visible for event in events] == [True, False, True]
    await manager.join()


def test_taken_message_cannot_be_cancelled_or_queued_twice():
    manager = InjectionManager()
    item = {"id": "one", "content": "first"}
    manager.submit(item)
    assert manager.get_nowait() == item
    assert manager.status("one") is InjectionStatus.TAKEN
    assert not manager.cancel("one")
    assert not manager.submit(item)


def test_run_scope_and_session_scope_isolate_deduplication():
    first, second = InjectionManager(), InjectionManager()
    item = {"id": "one", "content": "first"}
    first.submit(item)
    first.apply_next([])
    assert second.submit(item)
    scope = first.run_id
    first.begin_run()
    assert first.run_id != scope
    assert first.submit(item)
    assert not second.submit(item)


@pytest.mark.asyncio
async def test_run_boundaries_discard_stale_input_and_keep_prequeued_input_when_requested():
    manager = InjectionManager()
    item = {"id": "one", "content": "first"}
    manager.submit(item)
    manager.begin_run()
    assert manager.status("one") is InjectionStatus.PENDING
    assert not manager.submit(item)
    assert manager.begin_run(discard_pending=True) == [item]
    assert manager.empty()
    assert manager.status("one") is None
    assert manager.submit(item)
    assert manager.discard_pending() == [item]
    assert manager.status("one") is InjectionStatus.DISCARDED
    await manager.join()


def test_empty_and_unknown_messages_are_not_accepted_into_the_queue():
    manager = InjectionManager()
    assert not manager.submit("")
    with pytest.raises(ValueError, match="unknown"):
        manager.submit(MessageInjection("unknown", "payload"))
    assert manager.empty()


@pytest.mark.asyncio
async def test_cancelling_one_message_does_not_finish_waiting_for_the_other_messages():
    import asyncio

    manager = InjectionManager()
    manager.submit({"id": "one", "content": "keep"})
    manager.submit({"id": "two", "content": "cancel"})
    waiter = asyncio.create_task(manager.join())
    try:
        await asyncio.sleep(0)
        manager.cancel("two")
        await asyncio.sleep(0)
        assert not waiter.done()
        manager.apply_next([])
        await asyncio.wait_for(waiter, timeout=1)
    finally:
        if not waiter.done():
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
