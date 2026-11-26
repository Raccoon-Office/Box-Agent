"""Compaction keeps complete API rounds and durably carries original user text."""

import pytest

from box_agent.kernel import context_engine
from box_agent.kernel.loop import _auto_match_memory_for_latest_prompt, _latest_user_text
from box_agent.llm.token_estimator import estimate_text_tokens
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.session_log import SessionLog


def _tool_round(index, result="result"):
    call = ToolCall(
        id=f"call-{index}",
        type="function",
        function=FunctionCall(name="read_file", arguments={"path": f"sample-{index}"}),
    )
    return [
        Message(role="assistant", content="", tool_calls=[call]),
        Message(role="tool", name="read_file", tool_call_id=call.id, content=result),
    ]


def test_recent_window_counts_assistant_rounds_and_charges_tokens():
    history = [Message(role="system", content="system")]
    for index in range(7):
        history.extend([Message(role="user", content=f"request {index}"), *_tool_round(index)])
    retained, indices = context_engine._select_recent_messages(history)
    assert retained == history[-15:]
    assert indices == set(range(len(history) - 15, len(history)))

    history = [history[0], history[1], *_tool_round(0, "汉" * 8_000),
               *_tool_round(1, "字" * 8_000)]
    retained, _ = context_engine._select_recent_messages(history)
    assert retained == history[-2:]
    assert context_engine._fallback_context_estimate(retained, None) <= 15_000


class _SummaryProvider:
    context_window = 64_000

    async def generate_stream(self, **kwargs):
        yield StreamEvent(type="text", delta="<summary>Earlier conversation.</summary>")
        yield StreamEvent(type="finish", finish_reason="stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("result_sizes", [(18_000,), (3_000,) * 6], ids=["single", "parallel"])
async def test_summary_keeps_oversized_newest_round_byte_for_byte(result_sizes):
    system = Message(role="system", content="system")
    old_user = Message(role="user", content="Earlier request.")
    user = Message(role="user", content="Please continue.")
    calls = []
    results = []
    for index, size in enumerate(result_sizes):
        assistant, result = _tool_round(index, "汉" * size)
        calls.extend(assistant.tool_calls)
        results.append(result)
    newest = [Message(role="assistant", content="", tool_calls=calls), *results]
    messages = [system, old_user, *_tool_round("old"), user, *newest]
    before = [message.model_dump_json().encode() for message in newest]

    outcome = await context_engine._maybe_summarize(
        _SummaryProvider(), messages, 50_000, 0, False, force=True,
    )
    assert outcome.mode == "summary"
    assert outcome.messages[0] is system
    assert context_engine._is_summary_marker(outcome.messages[1])
    assert outcome.messages[1].earlier_user_messages == [old_user.content, user.content]
    assert user.content in outcome.messages[1].content
    assert [message.model_dump_json().encode() for message in outcome.messages[2:]] == before
    assert [call.id for call in outcome.messages[2].tool_calls] == [
        message.tool_call_id for message in outcome.messages[3:]
    ]
    assert outcome.degradation is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target, dropped", [(10_000, 1), (6_000, 2), (3_000, 3)])
async def test_summary_drops_recent_rounds_oldest_first_under_target_pressure(target, dropped):
    user = Message(role="user", content="Finish the task.")
    rounds = [_tool_round(index, "汉" * 4_000) for index in range(3)]
    messages = [Message(role="system", content="system"), user,
                *[message for group in rounds for message in group]]
    outcome = await context_engine._maybe_summarize(
        _SummaryProvider(), messages, 50_000, 0, False, force=True,
        request_headroom_tokens=50_000 - target,
    )

    assert outcome.mode == "summary"
    assert outcome.estimated_after <= target
    assert outcome.degradation["droppedRounds"] == dropped
    assert outcome.messages[1].earlier_user_messages == [user.content]
    assert outcome.messages[2:] == [message for group in rounds[dropped:] for message in group]


@pytest.mark.asyncio
async def test_summary_keeps_newest_round_and_large_following_user_together():
    system = Message(role="system", content="system")
    previous = Message(role="user", content="Earlier request.")
    newest = _tool_round(0, "汉" * 18_000)
    current = Message(role="user", content="Follow-up: " + "字" * 16_000)
    messages = [system, previous, *newest, current]
    outcome = await context_engine._maybe_summarize(
        _SummaryProvider(), messages, 50_000, 0, False, force=True,
    )
    assert outcome.mode == "summary"
    assert outcome.messages[1].earlier_user_messages == [previous.content]
    assert outcome.messages[2:] == [*newest, current]


def test_recent_window_stops_at_the_previous_summary():
    history = [Message(role="system", content="system"), *_tool_round("old"),
               context_engine._summary_with_users("Earlier work.", [], [], False, ""),
               Message(role="user", content="New request.")]
    assert context_engine._select_recent_messages(history) == (history[-1:], {len(history) - 1})


def test_earlier_user_token_budget_truncates_middle_and_omits_oldest_text():
    texts = [f"start {index} " + "汉" * 3_000 + f" end {index}" for index in range(8)]
    entries = context_engine._earlier_user_entries(texts, estimate_text_tokens, 10_000)
    message = context_engine._summary_with_users("summary", texts, entries, False, "")
    assert message.earlier_user_messages == texts
    assert "[4 earlier user messages are left out here; see the summary.]" in message.content
    assert "start 3" not in message.content
    assert "start 4" in message.content and "end 7" in message.content
    assert message.content.count(context_engine._EARLIER_USER_MIDDLE) == 4
    for text in texts:
        truncated = context_engine._bound_earlier_user_text(text, estimate_text_tokens)
        assert estimate_text_tokens(truncated) <= 2_000
    assert estimate_text_tokens(message.content[message.content.index("[Earlier User Messages]"):]) <= 10_000


@pytest.mark.asyncio
async def test_three_compactions_keep_original_user_text_across_session_replay(tmp_path):
    system = Message(role="system", content="system")
    messages = [system]
    root = tmp_path / "sessions"
    session_id = "retained-original-text"
    originals: list[str] = []
    session = SessionLog.create(root, session_id=session_id, cwd=tmp_path)
    for batch in range(3):
        try:
            for index in range(8):
                text = f"Request {batch}-{index}\n  unchanged text. "
                originals.append(text)
                messages.extend([
                    Message(role="user", content="host guidance" if index == 0 else text,
                            input_facts={"blocks": [{"type": "text", "text": text}]} if index == 0 else None),
                    Message(role="assistant", content=f"Answer {batch}-{index}"),
                ])
            messages.insert(-1, Message(role="user", source="runtime", content="Injected runtime guidance"))
            outcome = await context_engine._maybe_summarize(
                _SummaryProvider(), messages, 14_000, 0, False, force=True,
            )
            assert outcome.mode == "summary"
            assert outcome.messages[0] is system
            original_section = outcome.messages[1]
            assert original_section.source == "runtime"
            assert context_engine._is_summary_marker(original_section)
            tail = [message.content for message in outcome.messages if context_engine._is_real_user_message(message)]
            assert original_section.earlier_user_messages + tail == originals
            assert all(text in original_section.content for text in original_section.earlier_user_messages)
            assert "host guidance" not in original_section.content
            assert "Injected runtime guidance" not in original_section.content
            assert _latest_user_text(outcome.messages) == originals[-1]
            session.replace_surface(outcome.messages[1:], turn=batch + 1, step=1)
            session.flush()
        finally:
            session.close()
        session = SessionLog.open(root, session_id=session_id, cwd=tmp_path)
        messages = [system, *session.replay().messages]
        assert messages[1].earlier_user_messages == original_section.earlier_user_messages
    session.close()


@pytest.mark.asyncio
async def test_earlier_user_archive_does_not_drive_memory_recall():
    class Memory:
        def auto_match_context(self, user_text):
            assert user_text == "Current question"
            return []

    archive = context_engine._summary_with_users("summary", ["Prior request"], [], True, "")
    assert _latest_user_text([archive]) == ""
    await _auto_match_memory_for_latest_prompt(
        [Message(role="user", content="Current question"), archive], Memory(),
    )


@pytest.mark.asyncio
async def test_runtime_state_gives_all_large_sections_a_fair_share():
    class State:
        def __init__(self, label):
            self.label = label

        def compaction_state(self):
            return self.label, f"{self.label} beginning. " + "x" * 20_000 + f" {self.label} ending."

    state = await context_engine._restore_runtime_state(
        [], {label: State(label) for label in ("Todo", "Goal", "Plan")},
    )
    sections = state.content.split("\n\n## ")
    assert [section.splitlines()[0] for section in sections[1:]] == ["Goal", "Plan", "Todo"]
    assert len(state.content) <= 12_000
    assert all(3_900 < len(section) < 4_100 for section in sections[1:])
    for section in sections[1:]:
        label = section.splitlines()[0]
        assert f"{label} beginning." in section and f"{label} ending." in section


async def test_latest_user_before_cut_keeps_whitespace_and_attachments_after_reopen(session_log_for_images):
    import base64
    from copy import deepcopy
    from io import BytesIO

    from PIL import Image

    from box_agent.api.input import UserInput
    from box_agent.image_context import ImageContext
    from box_agent.input_preparation import prepare_user_input
    from box_agent.tools.image_inspection_tool import ImageInspectionTool
    from box_agent.tools.permissions import CapabilityPolicy, PermissionEngine
    from tests.test_image_context import RecordingLLM, image_block, image_blocks

    workspace, log = session_log_for_images
    workspace_image = workspace / "screenshot.png"
    workspace_block = image_block("blue")
    workspace_image.write_bytes(base64.b64decode(workspace_block["data"]))
    report = workspace / "report.xlsx"
    report.write_bytes(b"synthetic attachment")
    original = "  BEGIN\n" + "事实" * 7_000 + "\n END  "
    inline = image_block("red")
    value = UserInput(blocks=(
        {"type": "text", "text": original},
        {"type": "image", "mimeType": "image/png", "data": inline["data"]},
        {"type": "resource_link", "name": workspace_image.name, "uri": workspace_image.as_uri(), "mimeType": "image/png"},
        {"type": "resource_link", "name": report.name, "uri": report.as_uri(), "mimeType": "application/octet-stream"},
    ))
    facts, images = prepare_user_input(value, workspace)
    message = Message(role="user", content=original, input_facts=facts)
    image_context = ImageContext(log)
    refs = image_context.register(message, [*images, workspace_block], source="user_upload")
    message.input_facts["images"] = refs
    saved = deepcopy(message.input_facts)
    history = [Message(role="system", content="system"), message,
               *(Message(role="assistant", content=f"result {i}") for i in range(7))]
    log.append_unlogged_messages(history[1:], turn=1, step=1)
    log.flush()
    result = await context_engine._maybe_summarize(_SummaryProvider(), history, 50_000, 0, False,
                                                  session_log=log, force=True)
    archive = result.messages[1]
    assert result.mode == "summary" and result.messages[2:] == history[-5:]
    assert archive.content.index("Earlier conversation.") < archive.content.index(original) < archive.content.index(str(report))
    assert archive.earlier_user_messages[0].startswith(original + "\nAttachments:")
    inline_path = log.path.parent / refs[0]["contentRef"]
    assert str(inline_path) in archive.content and str(workspace_image) in archive.content
    assert refs[1]["contentRef"] not in archive.content and context_engine._EARLIER_USER_MIDDLE not in archive.content
    log.replace_surface(result.messages[1:], turn=1, step=2)
    log.flush()
    replayed = log.replay().messages
    assert not image_blocks(ImageContext(log).prepare(replayed, llm=RecordingLLM()).messages)
    assert replayed[0] == archive and log.history_messages()[0][1].input_facts == saved
    assert message.input_facts == saved
    reader = ImageInspectionTool(llm=RecordingLLM(), workspace_dir=str(workspace), native_supported=True,
        allow_full_access=False, permission_engine=PermissionEngine(CapabilityPolicy(session_workspace_root=str(workspace)), workspace))
    for path in (workspace_image, inline_path):
        read = await reader.execute(image_paths=[str(path)], instruction="inspect the synthetic image", strategy="native")
        assert read.success, read.error
        pixels = next(b for b in read.image_content if b["type"] == "input_image")
        with Image.open(BytesIO(base64.b64decode(pixels["data"]))) as actual, Image.open(path) as expected:
            assert actual.size == expected.size and actual.getpixel((0, 0)) == expected.getpixel((0, 0))


@pytest.fixture
def session_log_for_images(tmp_path, monkeypatch):
    from pathlib import Path
    from box_agent.user_paths import state_path

    monkeypatch.delenv("BOX_AGENT_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = SessionLog.create(state_path("sessions"), session_id="archived-images", cwd=workspace)
    try:
        yield workspace, log
    finally:
        log.close()


def test_archived_unavailable_image_has_an_explicit_notice():
    user = Message(role="user", content=[{"type": "input_image", "data": "unused"}])
    assert context_engine._original_user_text(user) == "Attachments: image was not saved and cannot be reopened"


def test_archive_keeps_original_text_when_legacy_attachment_facts_have_no_blocks():
    user = Message(role="user", content="  Keep this text.\n", input_facts={
        "context": {"files": [{"relativePath": "docs/report.xlsx"}]},
    })
    assert context_engine._original_user_text(user) == (
        "  Keep this text.\n\nAttachments: file report.xlsx → docs/report.xlsx"
    )
