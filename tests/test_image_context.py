"""Image lifetime is independent of source, tool execution and text history."""

import base64
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from PIL import Image
import pytest

from box_agent.agent import Agent
from box_agent.config import Config, ImageContextConfig
from box_agent.context_input import DefaultContextEngine
from box_agent.events import DoneEvent, ErrorEvent, StopReason, ToolCallResult
from box_agent.image_context import ImageContext
from box_agent.kernel.context_engine import _fallback_context_estimate, request_input_tokens
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall, TokenUsage
from box_agent.session_log import SessionLog
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.engine.preparation import prepare_tools
from box_agent.tools.image_inspection_tool import ImageInspectionTool


def image_block(color="red"):
    output = BytesIO()
    Image.new("RGB", (64, 48), color).save(output, format="PNG")
    return {"type": "input_image", "media_type": "image/png", "width": 64, "height": 48,
            "data": base64.b64encode(output.getvalue()).decode("ascii")}


def image_blocks(messages):
    return [block for message in messages if isinstance(message.content, list)
            for block in message.content if block.get("type") == "input_image"]


class RecordingLLM:
    model = "test-vision"
    capabilities = {"image_input": True}
    max_output_tokens = 1024

    def __init__(self, responses=(), log=None):
        self.responses = list(responses)
        self.requests = []
        self.log = log

    async def generate_stream(self, *, messages, **kwargs):
        self.requests.append(deepcopy(messages))
        if self.log is not None:
            durable = self.log.path.read_text()
            for block in image_blocks(messages):
                assert block["data"] not in durable
                assert block["sha256"] in durable
        response = self.responses.pop(0) if self.responses else "done"
        if isinstance(response, ToolCall):
            yield StreamEvent(type="finish", finish_reason="tool_calls", tool_calls=[response])
        elif response == "stale":
            yield StreamEvent(type="finish", finish_reason="provider_stale")
        elif response == "discarded":
            yield StreamEvent(type="finish", finish_reason="length")
        else:
            yield StreamEvent(type="text", delta=response)
            yield StreamEvent(type="finish", finish_reason="stop")


def call(name, index=1):
    return ToolCall(id=f"call-{index}", type="function", function=FunctionCall(name=name, arguments={}))


class ImageTool(Tool):
    name = "capture"
    description = "Capture an image."
    parameters = {"type": "object", "properties": {}}

    def __init__(self, source):
        self.source = source
        self.calls = 0

    async def execute(self, **kwargs):
        self.calls += 1
        return ToolResult(success=True, content="Capture completed; image prepared for the next request.",
                          image_content=[image_block()], image_source=self.source)


def make_agent(tmp_path, llm, log=None, **kwargs):
    return Agent(llm_client=llm, system_prompt="system", tools=kwargs.pop("tools", []),
                 workspace_dir=str(tmp_path), session_log=log, enable_builtin_tools=False,
                 deferred_mcp_loading_enabled=False, max_steps=6, **kwargs)


async def run(agent):
    events = [event async for event in agent.run_events()]
    assert not [event for event in events if isinstance(event, ErrorEvent) and event.is_fatal], events
    return events


@pytest.mark.parametrize("key", ["keep_rounds", "max_encoded_bytes"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "3"])
def test_image_limits_require_positive_integers(key, value):
    with pytest.raises(ValueError):
        ImageContextConfig(**{key: value})


def test_nested_yaml_image_settings_and_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("api_key: test\nagent:\n  image_context:\n    keep_rounds: 5\n    max_encoded_bytes: 128\n")
    assert Config.from_yaml(path).agent.image_context == ImageContextConfig(keep_rounds=5, max_encoded_bytes=128)
    assert ImageContextConfig().model_dump() == {"keep_rounds": 3, "max_encoded_bytes": 10485760}


@pytest.mark.parametrize("kind", ["async_method", "returned_coroutine", "non_mapping"])
def test_optional_preflight_cannot_put_awaitables_or_mocks_in_session_metadata(kind):
    async def metrics():
        return {"fits": True}

    preflight = {
        "async_method": AsyncMock(),
        "returned_coroutine": lambda *args, **kwargs: metrics(),
        "non_mapping": Mock(),
    }[kind]
    context = ImageContext()
    projection = context.prepare([Message(role="user", content="hello")],
                                 llm=SimpleNamespace(preflight_image_request=preflight),
                                 tools=[], thinking_enabled=False)
    assert "requestSize" not in projection.manifest
    json.dumps(projection.manifest)
    if kind == "async_method":
        preflight.assert_not_called()


@pytest.mark.asyncio
async def test_upload_last_three_committed_rounds_survives_restore_and_never_revives(tmp_path):
    log = SessionLog.create(tmp_path / "sessions", session_id="upload", cwd=tmp_path)
    llm = RecordingLLM(log=log)
    agent = make_agent(tmp_path, llm, log)
    agent.add_user_message("look", image_content=[image_block()])
    for _ in range(2):
        await run(agent)
        agent.add_user_message("continue")
    assert [len(image_blocks(request)) for request in llm.requests] == [1, 1]
    assert log.replay().model_round == 2
    assert log.replay().images[0]["delivered"]
    log.close()
    log = SessionLog.open(tmp_path / "sessions", session_id="upload", cwd=tmp_path)
    restored_llm = RecordingLLM(log=log)
    restored = make_agent(tmp_path, restored_llm, log)
    await run(restored)
    restored.add_user_message("continue")
    await run(restored)
    assert [len(image_blocks(request)) for request in restored_llm.requests] == [1, 0]
    assert log.replay().model_round == 4
    assert log.replay().images[0]["evictionReason"] == "round_limit"
    assert not image_blocks(restored.messages)
    assert log.header["version"] == 1
    assert list((log.path.parent / "images").glob("*.png"))
    log.close()
    log = SessionLog.open(tmp_path / "sessions", session_id="upload", cwd=tmp_path)
    bigger = make_agent(tmp_path, RecordingLLM(), log, image_context=ImageContextConfig(keep_rounds=30))
    bigger.add_user_message("continue")
    await run(bigger)
    assert not image_blocks(bigger.llm.requests[0])
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["image_inspect", "cua"])
async def test_tool_images_share_retention_and_remain_only_in_event_metadata(tmp_path, source):
    log = SessionLog.create(tmp_path / "sessions", session_id=source, cwd=tmp_path)
    tool = ImageTool(source)
    llm = RecordingLLM([call("capture"), "done"], log)
    agent = make_agent(tmp_path, llm, log, tools=[tool])
    agent.add_user_message("capture")
    await run(agent)
    for _ in range(3):
        agent.add_user_message("continue")
        await run(agent)
    assert [len(image_blocks(request)) for request in llm.requests] == [0, 1, 1, 1, 0]
    assert tool.calls == 1
    event = next(e for e in log.events if e["type"] == "tool/result")
    assert event["data"]["images"][0]["source"] == source
    assert "images" not in event["data"]["message"]
    assert event["data"]["images"][0]["toolCallId"] == "call-1"
    assert all(not image_blocks([message]) for message in log.replay().messages)
    log.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retained_image", [False, True])
async def test_glm_image_failures_after_model_switch_leave_task_running(tmp_path, retained_image):
    (tmp_path / "slide.png").write_bytes(base64.b64decode(image_block()["data"]))
    arguments = {"image_paths": ["slide.png"], "instruction": "Describe it."}
    calls = [ToolCall(id=f"inspect-{strategy}", type="function", function=FunctionCall(
        name="inspect_images", arguments={**arguments, "strategy": strategy},
    )) for strategy in ("proxy", "native")]

    class SwitchingLLM(RecordingLLM):
        model = "sn-kimi-k3"
        proxy_calls = 0

        async def generate(self, **kwargs):
            self.proxy_calls += 1
            raise RuntimeError("messages.content.type 参数非法，取值范围 ['text']")

        async def generate_stream(self, *, messages, **kwargs):
            if self.model == "sn-glm-5-3" and image_blocks(messages):
                raise RuntimeError("messages.content.type=image_url 当前纯文本模型不支持")
            async for event in super().generate_stream(messages=messages, **kwargs):
                yield event

    final = "Image inspection is unavailable; I can continue from the written context."
    log = SessionLog.create(tmp_path / "sessions", session_id="switch-model", cwd=tmp_path)
    llm = SwitchingLLM(["Previous written findings.", *calls, final], log)
    tool = ImageInspectionTool(llm=llm, workspace_dir=str(tmp_path),
                               native_supported=True, native_capability_llm=llm)
    agent = make_agent(tmp_path, llm, log, tools=[tool])
    try:
        agent.add_user_message("look", image_content=[image_block()] if retained_image else None)
        await run(agent)
        llm.model = "sn-glm-5-3"
        llm.capabilities = {}  # The host did not declare this binding's image support.
        agent.add_user_message("Review the image again and continue the task.")
        events = await run(agent)

        results = [event for event in events if isinstance(event, ToolCallResult)]
        assert [result.raw_output["code"] for result in results] == [
            "IMAGE_INPUT_UNSUPPORTED", "IMAGE_NATIVE_UNSUPPORTED",
        ]
        assert all(not result.success and result.image_content is None for result in results)
        assert llm.proxy_calls == 1
        assert len(llm.requests) == 4
        assert all(not image_blocks(request) for request in llm.requests[1:])
        assert any(message.content == "Previous written findings." for message in llm.requests[-1])
        done = next(event for event in events if isinstance(event, DoneEvent))
        assert done.stop_reason is StopReason.END_TURN
        assert done.final_content == final
        assert log.replay().messages[-1].content == final
        if retained_image:
            ref = log.replay().images[0]
            assert not ref["active"]
            assert ref["evictionReason"] == "model_image_support_unknown"
            assert (log.path.parent / ref["contentRef"]).exists()
    finally:
        log.close()


@pytest.mark.parametrize("capability", [False, None])
def test_upload_without_confirmed_image_support_keeps_text_and_reports_unseen_image(capability):
    context = ImageContext()
    user = Message(role="user", content="Continue the task even if the image is unavailable.")
    context.register(user, [image_block()], source="user_upload")

    request = context.prepare([user], llm=SimpleNamespace(capabilities={"image_input": capability}))

    assert not image_blocks(request.messages)
    assert request.messages[0].content == user.content
    assert request.notices
    assert "Do not assume its contents were seen" in str(request.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_response", ["stale", "discarded"])
async def test_discarded_requests_and_retries_do_not_consume_image_round(tmp_path, retry_response):
    log = SessionLog.create(tmp_path / "sessions", session_id="retry", cwd=tmp_path)
    llm = RecordingLLM([retry_response, "done"], log)
    agent = make_agent(tmp_path, llm, log)
    agent.add_user_message("look", image_content=[image_block()])
    await run(agent)
    assert [len(image_blocks(request)) for request in llm.requests] == [1, 1]
    assert log.replay().model_round == 1
    requests = [e["data"]["imageContext"] for e in log.events
                if e["type"] == "request/context" and "imageContext" in e["data"]]
    assert [request["requestRound"] for request in requests] == [1, 1]
    log.close()


@pytest.mark.parametrize("source", ["user_upload", "image_inspect", "cua"])
def test_occurrences_share_file_but_are_independently_charged_oldest_first(tmp_path, source):
    block = image_block()
    log = SessionLog.create(tmp_path, session_id=source, cwd=tmp_path)
    context = ImageContext(log, ImageContextConfig(max_encoded_bytes=len(block["data"])))
    user = Message(role="user", content="same image twice")
    refs = context.register(user, [block, block], source=source)
    log.append_unlogged_messages([user], turn=1, step=1)
    log.flush()
    request = context.prepare([user], llm=RecordingLLM())
    assert len(list(context.store.directory.glob("*.png"))) == 1
    assert refs[0]["imageId"] != refs[1]["imageId"]
    assert request.manifest["selected"][0]["imageId"] == refs[1]["imageId"]
    assert request.manifest["evicted"] == [{"imageId": refs[0]["imageId"],
                                           "reason": "encoded_byte_budget", "neverDelivered": True}]
    assert request.manifest["encodedBytes"] == len(block["data"])
    assert "was not delivered" in str(request.messages[-1].content)
    assert not log.replay().images[0]["active"]
    log.close()


def test_reordered_history_still_evicts_the_older_inference_round():
    block = image_block()
    context = ImageContext(config=ImageContextConfig(max_encoded_bytes=len(block["data"])))
    old = context.register(Message(role="user", content="old"), [block], source="user_upload")[0]
    context.model_round = 1
    new = context.register(Message(role="user", content="new"), [block], source="cua")[0]
    context.images.reverse()
    request = context.prepare([Message(role="user", content="reordered history")], llm=RecordingLLM())
    assert [ref["imageId"] for ref in request.manifest["selected"]] == [new["imageId"]]
    assert request.manifest["evicted"][0]["imageId"] == old["imageId"]


def test_restored_branch_orders_imported_images_before_later_local_images():
    context = ImageContext()
    # A sparse source catalog may give an old imported image a large ordinal.
    context.add_references([{"imageId": "second", "sourceImageOrder": 12},
                            {"imageId": "first", "sourceImageOrder": 9}])
    context.add_references([{"imageId": "local"}])
    restored = ImageContext()
    restored.add_references([context.images[1], context.images[0], context.images[2]])
    assert [ref["imageId"] for ref in restored.images] == ["first", "second", "local"]


@pytest.mark.asyncio
async def test_oversized_new_image_has_no_first_delivery_exemption(tmp_path):
    llm = RecordingLLM()
    agent = make_agent(tmp_path, llm, image_context=ImageContextConfig(max_encoded_bytes=1))
    agent.add_user_message("look", image_content=[image_block()])
    events = await run(agent)
    assert not image_blocks(llm.requests[0])
    assert "was not delivered" in str(llm.requests[0])
    assert any(isinstance(e, ErrorEvent) and e.error_code == "IMAGE_NOT_DELIVERED" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "corrupt", "write"])
async def test_image_failure_keeps_text_session_and_never_reexecutes_tool(tmp_path, failure, monkeypatch):
    log = SessionLog.create(tmp_path / "sessions", session_id="failure", cwd=tmp_path)
    tool = ImageTool("cua")
    llm = RecordingLLM([call("capture"), "done"], log)
    agent = make_agent(tmp_path, llm, log, tools=[tool])
    original = agent.image_context.store.persist_block

    def persist(block):
        if failure == "write":
            raise OSError("disk full")
        ref = original(block)
        path = log.path.parent / ref["contentRef"]
        if failure == "missing":
            path.unlink()
        else:
            path.write_bytes(b"bad bytes")
        return ref

    monkeypatch.setattr(agent.image_context.store, "persist_block", persist)
    agent.add_user_message("capture")
    await run(agent)
    assert tool.calls == 1
    assert not image_blocks(llm.requests[-1])
    assert "was not delivered" in str(llm.requests[-1])
    assert log.recovery_source is None
    assert log.replay().messages[0].content == "capture"
    assert not log.replay().images[0]["active"]
    log.close()


def test_summary_and_text_compaction_keep_image_catalog_and_do_not_advance_round(tmp_path):
    log = SessionLog.create(tmp_path, session_id="summary", cwd=tmp_path)
    context = ImageContext(log)
    user = Message(role="user", content="original")
    context.register(user, [image_block()], source="user_upload")
    log.append_unlogged_messages([user], turn=1, step=1)
    log.flush()
    engine = DefaultContextEngine(image_context=context, llm=RecordingLLM())
    summary = engine.prepare_summary([user], RecordingLLM())
    assert len(image_blocks(summary)) == 1
    assert context.model_round == log.replay().model_round == 0
    assert not log.replay().images[0]["delivered"]
    log.replace_surface([Message(role="user", content="summary text")], turn=1, step=2)
    restored = ImageContext(log)
    assert len(image_blocks(restored.prepare(log.replay().messages, llm=RecordingLLM()).messages)) == 1
    assert len(restored.images) == 1
    log.close()


@pytest.mark.asyncio
async def test_reset_retires_active_images_but_keeps_files_and_committed_round(tmp_path):
    log = SessionLog.create(tmp_path, session_id="clear", cwd=tmp_path)
    agent = make_agent(tmp_path, RecordingLLM(), log)
    agent.add_user_message("look", image_content=[image_block()])
    await run(agent)
    agent.clear_history()
    assert agent.image_context.images == log.replay().images == []
    assert agent.image_context.model_round == log.replay().model_round == 1
    assert list((log.path.parent / "images").glob("*.png"))
    log.close()


def test_proxy_archive_is_never_automatically_promoted_to_main_input(tmp_path):
    context = ImageContext()
    user = Message(role="user", content="proxy findings")
    context.register(user, [image_block()], source="image_inspect", eligible=False)
    assert not image_blocks(context.prepare([user], llm=RecordingLLM()).messages)
    assert list(context.store.directory.glob("*.png"))


def test_image_token_overlay_is_counted_once_after_usage_from_previous_request():
    context = ImageContext()
    user = Message(role="user", content="look")
    context.register(user, [image_block()], source="user_upload")
    engine = DefaultContextEngine(image_context=context, llm=RecordingLLM())
    messages = [Message(role="system", content="system"), user]
    first = engine.prepare_request(messages, prepared_tools=prepare_tools([]), token_limit=20000)
    assert first.request_only_input_tokens >= 512
    messages.append(Message(role="assistant", content="done",
                            request_only_input_tokens=first.request_only_input_tokens,
                            usage=TokenUsage(prompt_tokens=request_input_tokens(first.messages, ()),
                                        completion_tokens=1, total_tokens=request_input_tokens(first.messages, ()) + 1)))
    second = engine.prepare_request(messages, prepared_tools=prepare_tools([]), token_limit=20000)
    assert second.request_only_input_tokens == first.request_only_input_tokens
    assert len(image_blocks(second.messages)) == 1
    # A third projection cannot accumulate a second historical image charge.
    third = engine.prepare_request(messages, prepared_tools=prepare_tools([]), token_limit=20000)
    assert request_input_tokens(second.messages, ()) == request_input_tokens(third.messages, ())


@pytest.mark.asyncio
async def test_exhausted_empty_response_retries_do_not_commit_a_round_or_rewrite_history(tmp_path):
    log = SessionLog.create(tmp_path, session_id="empty", cwd=tmp_path)
    llm = RecordingLLM(["discarded"] * 8, log)
    agent = make_agent(tmp_path, llm, log)
    agent.add_user_message("look", image_content=[image_block()])
    events = [event async for event in agent.run_events()]
    assert any(isinstance(event, ErrorEvent) and event.is_fatal for event in events)
    assert log.replay().model_round == agent.image_context.model_round == 0
    assert not any(event["type"] == "assistant/message" for event in log.events)
    assert log.replay().images[0]["active"]
    assert [len(image_blocks(request)) for request in llm.requests] == [1] * len(llm.requests)
    log.close()


@pytest.mark.asyncio
async def test_eviction_before_send_survives_crash_with_an_undelivered_notice(tmp_path):
    log = SessionLog.create(tmp_path, session_id="notice", cwd=tmp_path)
    agent = make_agent(tmp_path, RecordingLLM(), log, image_context=ImageContextConfig(max_encoded_bytes=1))
    agent.add_user_message("look", image_content=[image_block()])
    request = agent.image_context.prepare(agent.messages, llm=agent.llm)
    assert request.notices
    log.close()  # preparation committed; no model response was accepted
    log = SessionLog.open(tmp_path, session_id="notice", cwd=tmp_path)
    restored = make_agent(tmp_path, RecordingLLM(log=log), log)
    await run(restored)
    assert "was not delivered" in str(restored.llm.requests[0])
    assert log.replay().images[0]["noticeDelivered"]
    assert not image_blocks(restored.llm.requests[0])
    again = ImageContext(log)
    assert not again.prepare(restored.messages, llm=restored.llm).notices
    log.close()


def test_small_summary_request_limit_does_not_retire_main_model_images():
    context = ImageContext()
    user = Message(role="user", content="look")
    context.register(user, [image_block()], source="user_upload")

    class TextSizedSummary(RecordingLLM):
        def preflight_image_request(self, messages, tools=None, **kwargs):
            return {"fits": not image_blocks(messages)}

    summary = context.prepare([user], llm=TextSizedSummary(), purpose="summary")
    assert not image_blocks(summary.messages)
    assert summary.manifest["excluded"][0]["reason"] == "provider_body_limit"
    assert not summary.manifest["evicted"]
    assert context.images[0]["active"]
    assert len(image_blocks(context.prepare([user], llm=RecordingLLM()).messages)) == 1


@pytest.mark.asyncio
async def test_tool_inference_rounds_within_one_user_turn_age_the_same_uploaded_image(tmp_path):
    class Ping(Tool):
        name = "ping"
        description = "Read a status."
        parameters = {"type": "object", "properties": {"n": {"type": "integer"}}}

        async def execute(self, **kwargs):
            return ToolResult(success=True, content="status available")

    calls = [ToolCall(id=f"ping-{i}", type="function",
                      function=FunctionCall(name="ping", arguments={"n": i})) for i in range(3)]
    llm = RecordingLLM([*calls, "done"])
    agent = make_agent(tmp_path, llm, tools=[Ping()])
    agent.add_user_message("look and check status", image_content=[image_block()])
    await run(agent)
    assert [len(image_blocks(request)) for request in llm.requests] == [1, 1, 1, 0]
    assert agent.image_context.model_round == 4
    assert len([message for message in agent.messages if message.role == "user" and message.source == "user"]) == 1


def test_rereading_identical_bytes_creates_a_new_age_without_reviving_old_occurrence():
    context = ImageContext()
    user = Message(role="user", content="look")
    original = context.register(user, [image_block()], source="user_upload")[0]
    context.prepare([user], llm=RecordingLLM())
    context.mark_response(Message(role="assistant", content="done"))
    second = context.register(user, [image_block()], source="cua")[0]
    for _ in range(2):
        context.prepare([user], llm=RecordingLLM())
        context.mark_response(Message(role="assistant", content="done"))
    request = context.prepare([user], llm=RecordingLLM())
    assert [ref["imageId"] for ref in request.manifest["selected"]] == [second["imageId"]]
    assert request.manifest["evicted"][0]["imageId"] == original["imageId"]
    assert len(list(context.store.directory.glob("*.png"))) == 1


def test_seeded_excluded_image_keeps_its_pending_not_delivered_notice(tmp_path):
    from box_agent.session_continuation import ContinuationMessage

    log = SessionLog.create(tmp_path, session_id="seed-notice", cwd=tmp_path)
    agent = make_agent(tmp_path, RecordingLLM(), log)
    ref = {"imageId": "excluded", "source": "user_upload", "active": False,
           "evicted": True, "delivered": False, "createdRound": 1,
           "evictionReason": "encoded_byte_budget"}
    agent.seed_continuation_messages((ContinuationMessage("user", "look", (ref,)),), model_round=2)
    prepared = agent.image_context.prepare(agent.messages, llm=agent.llm)
    assert prepared.notices
    assert prepared.manifest["noticeImageIds"] == ["excluded"]
    assert not image_blocks(prepared.messages)
    log.close()
