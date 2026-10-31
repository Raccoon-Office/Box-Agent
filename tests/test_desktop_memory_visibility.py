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
        tools={"memory_read": Reader()}, memory_manager=lookup, max_steps=2, token_limit=30000,
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
