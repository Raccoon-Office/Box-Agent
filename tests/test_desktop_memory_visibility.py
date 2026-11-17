"""Full reads qualify only through the real final-input callback lifecycle."""

import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

import pytest

from box_agent.context_input import DefaultContextEngine, PreparedContext
from box_agent.desktop_memory.transport import MemoryProxyError
from box_agent.desktop_memory.visibility import CoreRead, CoreVisibilityGuard, DesktopInputGuard
from box_agent.kernel.context_engine import REQUEST_INPUT_HEADROOM_TOKENS
from box_agent.schema import Message
from box_agent.tools.engine.preparation import prepare_tools


def read_record(path="memory.md", text="<mem priority=\"0\">完整资料</mem>"):
    read_id = str(uuid.uuid4())
    return CoreRead(path, read_id, hashlib.sha256(text.encode()).hexdigest(),
                    json.dumps({"path": path, "content": text, "read_id": read_id}, ensure_ascii=False))


@pytest.fixture
def bound():
    events = []
    guard = CoreVisibilityGuard(lambda *args: events.append(args))
    engine = DefaultContextEngine()
    engine.configure_run()
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    messages = [Message(role="system", content="BASE"), Message(role="user", content="remember")]
    engine.prepare_request(messages, prepared_tools=prepare_tools([]), token_limit=10000)
    return guard, engine, messages, events


def project(engine, messages, record, **updates):
    reply = Message(role="tool", name="memory_read", tool_call_id="read-call", content=record.model_content)
    return engine.prepare_request([*messages, reply.model_copy(update=updates)],
                                  prepared_tools=prepare_tools([]), token_limit=10000)


@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_utf8_full_read_at_the_real_conservative_budget_boundary(monkeypatch, delta):
    events, measured = [], []
    guard = DesktopInputGuard(lambda *args: events.append(args), lambda _: "")
    original = guard._input_tokens

    def measure(messages, definitions):
        cost = original(messages, definitions)
        measured.append(cost)
        return cost

    monkeypatch.setattr(guard, "_input_tokens", measure)
    engine = DefaultContextEngine()
    engine.configure_run()
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    messages = [Message(role="system", content="BASE"), Message(role="user", content="remember")]
    tools = prepare_tools([])
    record = read_record(text=('中文😀 "quoted" \\ escaped\n' * 128))
    engine.prepare_request(messages, prepared_tools=tools, token_limit=100000)
    guard.admit(record)
    cost = measured[-1]
    assert cost >= len(record.model_content.encode("utf-8"))
    guard.forget(record.path)
    limit = cost + REQUEST_INPUT_HEADROOM_TOKENS + delta
    engine.prepare_request(messages, prepared_tools=tools, token_limit=limit)
    if delta < 0:
        with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
            guard.admit(record)
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await guard.require(record.path)
        assert not any(event[1] for event in events)
        return
    guard.admit(record)
    assert measured[-1] == cost
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    reply = Message(role="tool", name="memory_read", tool_call_id=record.read_id,
                    content=record.model_content)
    request = engine.prepare_request([*messages, reply], prepared_tools=tools, token_limit=limit)
    assert request.messages[-1].content == record.model_content
    request.on_committed()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    request.on_response()
    assert await guard.require(record.path) is record


async def test_successful_http_read_alone_never_qualifies_mutation(bound):
    guard, _, _, _ = bound
    record = read_record()
    guard.admit(record)
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)


async def test_complete_tool_reply_qualifies_only_after_commit_and_successful_response(bound):
    guard, engine, messages, events = bound
    record = read_record()
    guard.admit(record)
    request = project(engine, messages, record)
    assert record.model_content == request.messages[-1].content
    request.on_response()  # An uncommitted request may not acknowledge delivery.
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    request.on_committed()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    request.on_response()
    assert await guard.require(record.path) is record
    assert len([event for event in events if event[1]]) == 1


@pytest.mark.parametrize("change", ["truncated", "user_role", "different_tool", "only_nonce", "missing"])
async def test_read_is_not_proven_by_partial_or_untrusted_context(bound, change):
    guard, engine, messages, events = bound
    record = read_record()
    guard.admit(record)
    updates = {"content": record.model_content[:-1]} if change == "truncated" else {}
    if change == "user_role":
        updates = {"role": "user"}
    elif change == "different_tool":
        updates = {"name": "ordinary_tool"}
    elif change == "only_nonce":
        updates = {"content": record.read_id}
    elif change == "missing":
        updates = {"content": "summary"}
    request = project(engine, messages, record, **updates)
    request.on_committed()
    request.on_response()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    assert not any(event[1] for event in events)


async def test_later_context_projection_revokes_previous_qualification(bound):
    guard, engine, messages, events = bound
    record = read_record()
    guard.admit(record)
    request = project(engine, messages, record)
    request.on_committed()
    request.on_response()
    assert await guard.require(record.path) is record
    next_request = engine.prepare_request(messages, prepared_tools=prepare_tools([]), token_limit=10000)
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    next_request.on_committed()
    next_request.on_response()
    assert events[-1][0] is record and events[-1][1] is False
    assert events[-1][2] == ""
    restored = project(engine, messages, record)
    restored.on_committed()
    restored.on_response()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)


async def test_old_success_callback_cannot_qualify_new_request_or_new_read(bound):
    guard, engine, messages, _ = bound
    first = read_record()
    guard.admit(first)
    old = project(engine, messages, first)
    old.on_committed()
    second = read_record()
    guard.admit(second)
    project(engine, messages, second)
    old.on_response()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(first.path)


async def test_consuming_or_resetting_a_read_never_auto_regrants_it(bound):
    guard, engine, messages, _ = bound
    record = read_record()
    guard.admit(record)
    request = project(engine, messages, record)
    request.on_committed()
    request.on_response()
    guard.forget(record.path)
    request.on_response()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    guard.admit(record)
    guard.reset()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)


async def test_capacity_failure_is_explicit_and_revokes_previous_path(bound):
    guard, engine, messages, events = bound
    record = read_record()
    guard.admit(record)
    request = project(engine, messages, record)
    request.on_committed()
    request.on_response()
    huge = read_record(text="中" * 20000)
    with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
        guard.admit(huge)
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    assert any(event[0] is record and not event[1] for event in events)
    assert any(event[0] is huge and not event[1] for event in events)


def test_conservative_history_budget_omission_is_diagnosable_without_payload(caplog):
    from box_agent.memory import place_memory_block
    guard = DesktopInputGuard(lambda *_: None, lambda size: "PRIVATE_PRELOAD" if size else "")
    content = place_memory_block("BASE", "--- MEMORY START ---\nPRIVATE_PRELOAD\n--- MEMORY END ---", reserve=True)
    history = [Message(role="system", content=content), Message(role="user", content="PRIVATE_HISTORY " * 8000)]
    projected = guard.project_messages(history, tools=prepare_tools([]), token_limit=90000)
    assert "PRIVATE_PRELOAD" not in projected[0].content
    assert projected[1].content == history[1].content
    assert "reason=history_over_budget" in caplog.text and "limit=90000" in caplog.text
    assert "PRIVATE_PRELOAD" not in caplog.text and "PRIVATE_HISTORY" not in caplog.text


@pytest.mark.parametrize("preload", [False, True])
async def test_online_memory_does_not_block_a_long_first_message_without_a_core_read(preload):
    from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent
    from box_agent.memory import place_memory_block
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import StreamEvent

    class Provider:
        requests = []

        def estimate_text_tokens(self, text):
            return len(text.encode("utf-8")) // 2

        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                yield StreamEvent(type="text", delta="<summary>preserve the user's message</summary>")
            else:
                self.requests.append(messages)
                yield StreamEvent(type="text", delta="received")
            yield StreamEvent(type="finish", finish_reason="stop")

    guard = DesktopInputGuard(lambda *_: None, lambda size: "preload" if size and preload else "")
    lookup = SimpleNamespace(model_input_guard=guard, auto_match_context=lambda _: [], correction_curator=None)
    system = (place_memory_block("BASE", "--- MEMORY START ---\npreload\n--- MEMORY END ---", reserve=True)
              if preload else "BASE")
    content = "x" * 32000
    history = [Message(role="system", content=system), Message(role="user", content=content)]
    provider = Provider()
    ordinary = DefaultContextEngine(llm=provider).prepare_request(
        history, prepared_tools=prepare_tools([]), token_limit=30000)
    assert ordinary.blocked_reason is None
    events = [event async for event in run_agent_loop(
        llm=provider, messages=history, tools={}, memory_manager=lookup, token_limit=30000, max_steps=1, logger=None)]
    assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    assert not any(isinstance(event, SummarizationEvent) for event in events)
    assert len(provider.requests) == 1
    assert provider.requests[0][-1].content == content
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.END_TURN


@pytest.mark.parametrize("preload", [False, True])
def test_only_a_failed_core_read_requests_one_compaction_per_turn(preload, caplog):
    from box_agent.memory import place_memory_block

    llm = SimpleNamespace(estimate_text_tokens=lambda text: len(text.encode("utf-8")) // 2)
    guard = DesktopInputGuard(lambda *_: None, lambda size: "PRIVATE_PRELOAD" if size and preload else "")
    engine = DefaultContextEngine(llm=llm)
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    system = (place_memory_block("BASE", "--- MEMORY START ---\nPRIVATE_PRELOAD\n--- MEMORY END ---", reserve=True)
              if preload else "BASE")
    history = [Message(role="system", content=system),
               Message(role="assistant", content="PRIVATE_HISTORY " * 75000),
               Message(role="user", content="remember")]
    tools = prepare_tools([])
    ordinary = DefaultContextEngine(llm=llm).prepare_request(history, prepared_tools=tools, token_limit=810000)
    assert ordinary.blocked_reason is None
    request = engine.prepare_request(history, prepared_tools=tools, token_limit=810000)
    assert request.blocked_reason is None
    with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
        guard.admit(read_record())
    recovery = engine.prepare_request(history, prepared_tools=tools, token_limit=810000)
    assert recovery.budget_blocked and recovery.blocked_reason
    assert recovery.on_committed is None and recovery.on_response is None
    assert engine.prepare_request(history, prepared_tools=tools, token_limit=810000).blocked_reason is None
    with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
        guard.admit(read_record())
    assert engine.prepare_request(history, prepared_tools=tools, token_limit=810000).blocked_reason is None
    guard.reset()  # A new turn gets its own bounded recovery attempt.
    with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
        guard.admit(read_record())
    assert engine.prepare_request(history, prepared_tools=tools, token_limit=810000).budget_blocked
    assert "PRIVATE_PRELOAD" not in caplog.text and "PRIVATE_HISTORY" not in caplog.text
    assert history[1].content == "PRIVATE_HISTORY " * 75000


def test_desktop_image_budget_counts_pixels_without_counting_base64_as_text():
    guard = DesktopInputGuard(lambda *_: None, lambda _: "")
    image = {"type": "input_image", "media_type": "image/png", "width": 16, "height": 16, "data": "A" * 200000}
    message = Message(role="user", content=[image])
    short = message.model_copy(update={"content": [{**image, "data": "AA=="}]})
    cost = guard._input_tokens([message], [])
    assert cost == guard._input_tokens([short], [])
    assert 512 <= cost < 2000
    assert message.content[0]["data"] == "A" * 200000


@pytest.mark.parametrize("after_compaction", [False, True])
async def test_connector_snapshot_metadata_does_not_block_desktop_request_or_core_read(after_compaction):
    from box_agent.connector_context import append_connector_status

    snapshot = "<connector-status>\n" + "\n".join(
        f"service-{index}: synthetic knowledge connector connected" for index in range(120)
    ) + "\n</connector-status>"
    history = [Message(role="system", content="BASE")]
    for index in range(12):
        content = append_connector_status(history, f"question {index}", snapshot)
        history.append(Message(role="user", content=content, host_connector_status=snapshot))
    if after_compaction:
        history = [history[0], *history[-4:]]  # The visible snapshot was compacted away.
    before = [message.model_dump() for message in history]
    guard = DesktopInputGuard(lambda *_: None, lambda _: "")
    engine = DefaultContextEngine()
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    tools = prepare_tools([])
    request = engine.prepare_request(history, prepared_tools=tools, token_limit=16000)
    assert request.blocked_reason is None
    assert sum(snapshot in message.content for message in request.messages) == 1
    without_metadata = [message.model_copy(update={"host_connector_status": None}) for message in request.messages]
    assert guard._input_tokens(request.messages, []) == guard._input_tokens(without_metadata, [])
    assert [message.model_dump() for message in history] == before

    record = read_record()
    guard.admit(record)
    reply = Message(role="tool", name="memory_read", tool_call_id=record.read_id, content=record.model_content)
    delivered = engine.prepare_request([*history, reply], prepared_tools=tools, token_limit=16000)
    assert delivered.blocked_reason is None
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    delivered.on_committed()
    delivered.on_response()
    assert await guard.require(record.path) is record


def test_visible_connector_snapshot_still_counts_toward_desktop_budget():
    snapshot = "<connector-status>\n" + "synthetic connector connected\n" * 1000 + "</connector-status>"
    history = [Message(role="system", content="BASE"),
               Message(role="user", content="remember", host_connector_status=snapshot)]
    guard = DesktopInputGuard(lambda *_: None, lambda _: "")
    engine = DefaultContextEngine()
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    request = engine.prepare_request(history, prepared_tools=prepare_tools([]), token_limit=16000)
    assert snapshot in request.messages[-1].content
    with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
        guard.admit(read_record())
    assert history[-1].content == "remember" and history[-1].host_connector_status == snapshot


async def test_failed_core_read_revokes_grant_without_blocking_ordinary_projection():
    guard = DesktopInputGuard(lambda *_: None, lambda _: "")
    engine = DefaultContextEngine(llm=SimpleNamespace(estimate_text_tokens=lambda text: len(text) // 2))
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    messages = [Message(role="system", content="BASE"), Message(role="user", content="remember")]
    engine.prepare_request(messages, prepared_tools=prepare_tools([]), token_limit=10000)
    record = read_record()
    guard.admit(record)
    delivered = project(engine, messages, record)
    delivered.on_committed()
    delivered.on_response()
    assert await guard.require(record.path) is record
    ordinary = project(engine, [*messages, Message(role="assistant", content="x" * 14000)], record)
    assert ordinary.blocked_reason is None
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)
    with pytest.raises(MemoryProxyError, match="CORE_CONTEXT_TOO_LARGE"):
        guard.admit(read_record())
    delivered.on_response()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)


@pytest.mark.parametrize("summary_fails", [False, True])
async def test_irreducible_memory_read_failure_does_not_prevent_ordinary_reply_or_repeat_compaction(summary_fails):
    from box_agent.events import DoneEvent, ErrorEvent, StopReason, SummarizationEvent, ToolCallResult
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.tools.base import Tool, ToolResult

    activations = []
    guard = DesktopInputGuard(lambda *args: activations.append(args), lambda _: "")

    class Reader(Tool):
        name = "memory_read"
        description = "Read core memory."
        parameters = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, path):
            try:
                guard.admit(read_record(path=path))
            except MemoryProxyError as exc:
                return ToolResult(success=False, error=exc.code)
            pytest.fail("The irreducible history must still fail conservative core-read admission")

    class Provider:
        summary_calls = 0
        normal_calls = 0

        def estimate_text_tokens(self, text):
            return len(text.encode("utf-8")) // 2

        async def generate_stream(self, messages, **kwargs):
            if kwargs.get("call_kind") == "context_summary":
                self.summary_calls += 1
                if summary_fails:
                    raise RuntimeError("synthetic summary unavailable")
                yield StreamEvent(type="text", delta="<summary>retained intent</summary>")
            else:
                self.normal_calls += 1
                if self.normal_calls <= 2:
                    yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                        id=f"read-{self.normal_calls}", type="function",
                        function=FunctionCall(name="memory_read", arguments={"path": "memory.md"}))])
                    return
                yield StreamEvent(type="text", delta="Cannot admit the memory read, but ordinary replies still work.")
            yield StreamEvent(type="finish", finish_reason="stop")

    lookup = SimpleNamespace(model_input_guard=guard, auto_match_context=lambda _: [], correction_curator=None)
    provider = Provider()
    events = [event async for event in run_agent_loop(
        llm=provider, tools={"memory_read": Reader()}, memory_manager=lookup, token_limit=30000, max_steps=3, logger=None,
        messages=[Message(role="system", content="BASE"), Message(role="user", content="x" * 32000)])]
    assert provider.summary_calls == 1 and provider.normal_calls == 3
    assert len([event for event in events if isinstance(event, SummarizationEvent)]) == 1
    assert not [event.message for event in events if isinstance(event, ErrorEvent)]
    failures = [event for event in events if isinstance(event, ToolCallResult) and not event.success]
    assert len(failures) == 2 and all(event.error == "CORE_CONTEXT_TOO_LARGE" for event in failures)
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == StopReason.END_TURN
    assert not any(event[1] for event in activations)


async def test_blocked_model_input_never_grants_visibility(bound):
    guard, _, _, events = bound
    record = read_record()
    guard.admit(record)
    request = PreparedContext([Message(role="tool", name="memory_read", content=record.model_content)], [],
                              blocked_reason="too large", budget_blocked=True)
    blocked = guard.prepare_context(request)
    assert blocked.on_committed is None and blocked.on_response is None
    assert not any(event[1] for event in events)


async def test_existing_context_callbacks_are_preserved_and_failure_cannot_grant(bound):
    guard, _, _, _ = bound
    record = read_record()
    guard.admit(record)
    calls = []
    original = PreparedContext([Message(role="tool", name="memory_read", content=record.model_content)], [],
                               on_committed=lambda: calls.append("commit"), on_response=lambda: calls.append("response"))
    request = guard.prepare_context(original)
    request.on_committed()
    request.on_response()
    assert calls == ["commit", "response"]
    assert await guard.require(record.path) is record

    def broken():
        raise RuntimeError("logging failed before model dispatch")

    request = guard.prepare_context(replace(original, on_committed=broken))
    with pytest.raises(RuntimeError):
        request.on_committed()
    request.on_response()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await guard.require(record.path)


async def test_host_activation_must_succeed_before_write():
    waiting = asyncio.Event()

    async def rejected():
        await waiting.wait()
        raise MemoryProxyError("SESSION_REVOKED")

    guard = CoreVisibilityGuard(lambda read, active, request: asyncio.create_task(rejected()) if active else None)
    engine = DefaultContextEngine()
    engine.bind_memory(SimpleNamespace(model_input_guard=guard))
    engine.prepare_request([], prepared_tools=prepare_tools([]), token_limit=10000)
    record = read_record()
    guard.admit(record)
    request = project(engine, [], record)
    request.on_committed()
    request.on_response()
    checking = asyncio.create_task(guard.require(record.path))
    await asyncio.sleep(0)
    assert not checking.done()
    waiting.set()
    with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
        await checking


def test_generic_memory_and_mock_capabilities_do_not_install_a_guard():
    engine = DefaultContextEngine()
    engine.bind_memory(MagicMock())
    request = engine.prepare_request([], prepared_tools=prepare_tools([]), token_limit=10000)
    assert request.on_committed is None
    assert request.on_response is None


@pytest.mark.parametrize("result_kind", ["success", "failure", "truncated"])
async def test_real_kernel_qualifies_only_the_full_read_seen_by_successful_model(result_kind):
    from box_agent.runtime import run_agent_loop
    from box_agent.schema import FunctionCall, StreamEvent, ToolCall
    from box_agent.tools.base import Tool, ToolResult

    events = []
    guard = CoreVisibilityGuard(lambda *args: events.append(args))
    record = read_record(text='<mem priority="0">完整资料 ' + "stable details " * 3000 + "</mem>")

    class Reader(Tool):
        name = "memory_read"
        description = "Read the complete core."
        parameters = {"type": "object", "properties": {}}
        max_result_size_chars = float("inf")

        async def execute(self):
            guard.admit(record)
            return ToolResult(success=True, content=record.model_content, model_context=record.model_content)

    class Provider:
        def __init__(self):
            self.requests = []

        async def generate_stream(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[ToolCall(
                    id="read-core", type="function", function=FunctionCall(name="memory_read", arguments={}))])
                return
            actual = next(message for message in kwargs["messages"] if message.role == "tool" and message.name == "memory_read")
            assert actual.content == record.model_content
            with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
                await guard.require(record.path)
            if result_kind == "failure":
                raise RuntimeError("synthetic provider failed")
            yield StreamEvent(type="text", delta="seen")
            yield StreamEvent(type="finish", finish_reason="length" if result_kind == "truncated" else "stop")

    provider = Provider()
    lookup = SimpleNamespace(model_input_guard=guard, auto_match_context=lambda _: [], correction_curator=None)
    _ = [event async for event in run_agent_loop(
        llm=provider, messages=[Message(role="system", content="BASE"), Message(role="user", content="read")],
        # This fixture has no provider token meter, so budget for its UTF-8
        # upper bound (~45 KB), including request framing and headroom.
        tools={"memory_read": Reader()}, memory_manager=lookup, max_steps=2, token_limit=60000,
        truncation_continuation_enabled=False, logger=None)]
    assert len(provider.requests) == 2
    if result_kind == "success":
        assert await guard.require(record.path) is record
    else:
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await guard.require(record.path)
    assert any(event[1] for event in events) == (result_kind == "success")


@pytest.mark.parametrize("fits", [True, False])
async def test_image_projection_preserves_memory_delivery_and_blocks_grants_on_preflight_failure(bound, fits):
    import base64
    from io import BytesIO
    from PIL import Image

    guard, engine, messages, events = bound
    record = read_record()
    guard.admit(record)
    output = BytesIO()
    Image.new("RGB", (16, 16), "navy").save(output, format="PNG")
    engine.image_context.register(messages[-1], [{
        "type": "input_image", "media_type": "image/png",
        "data": base64.b64encode(output.getvalue()).decode("ascii"),
    }], source="user")
    engine.bind_model(SimpleNamespace(
        capabilities={"image_input": True},
        preflight_image_request=lambda *args, **kwargs: {"fits": fits},
    ))

    request = project(engine, messages, record)
    assert request.image_context["requestSize"]["fits"] is fits
    assert any(message.role == "tool" and message.content == record.model_content
               for message in request.messages)
    if fits:
        assert len(request.image_context["selected"]) == 1
        assert any(block.get("type") == "input_image" for message in request.messages
                   if isinstance(message.content, list) for block in message.content)
        assert request.blocked_reason is None
        request.on_committed()
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await guard.require(record.path)
        request.on_response()
        assert await guard.require(record.path) is record
    else:
        assert "body-size limit" in request.blocked_reason
        assert request.image_notices
        assert request.on_committed is None and request.on_response is None
        with pytest.raises(MemoryProxyError, match="CORE_READ_REQUIRED"):
            await guard.require(record.path)
        assert not any(event[1] for event in events)
