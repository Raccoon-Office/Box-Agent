"""Regression tests for the ``finish_reason="length"`` retry ladder in
``run_agent_loop`` (introduced by PR #11 — "让 OpenAI 协议的输出截断先自愈再报错").

Before this test suite the loop unconditionally treated a ``length`` finish
as a SAME-messages retry — but streaming ``ContentEvent`` deltas had already
been forwarded to the host, so if any visible text was produced before the
cutoff the second attempt would stream its own (usually longer) reply on top
of the first partial. On CLI stdout the user saw ``partial partial partial …
partial complete`` glued together; on ACP hosts the ``update_agent_message``
deltas concatenated into the same growing message.

The loop splits ``length``/`max_tokens`` handling by whether a tool call was
attempted:

* Any tool-call attempt, parseable or broken: discard it without execution and
  inject a hidden user recovery instruction. A write_file attempt is told to
  use ordered chunks.
* Visible text present: hand off to the ``truncation_continuation`` machinery
  — the partial is appended as an assistant turn and the next LLM call is
  asked to CONTINUE from the tail rather than restart.

These tests pin those two paths and, most importantly, guarantee that a
``length`` finish with visible text does NOT re-emit that same text through
``ContentEvent``.
"""

from __future__ import annotations

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import (
    ContentEvent,
    DoneEvent,
    InjectedMessageEvent,
    LLMOutputEvent,
    StopReason,
)
from box_agent.schema import (
    FunctionCall,
    Message,
    StreamEvent,
    TokenUsage,
    ToolCall,
)
from box_agent.tools.file_tools import WriteTool


class _ScriptedLLM:
    """Streaming LLM double whose replies come from a scripted queue.

    Each entry drives one ``generate_stream`` invocation. ``text`` (if any)
    is emitted as a single ``StreamEvent(type="text")`` chunk followed by the
    finish event carrying ``finish_reason`` and the diagnostics fields the
    OpenAI client now attaches (``truncated_tool_calls``, ``raw_finish_reason``,
    ``stream_dropped_mid_tool``). Snapshots of the incoming ``messages`` list
    are captured so tests can assert whether SAME-messages retry or append-
    -then-continue was taken.
    """

    max_output_tokens = 4096

    def __init__(self, script: list[dict]):
        self._script = list(script)
        self.calls = 0
        self.message_snapshots: list[list[Message]] = []
        self.ephemeral_max_tokens_history: list[int | None] = []
        self._ephemeral_max_output_tokens: int | None = None

    def set_ephemeral_max_output_tokens(self, value: int | None) -> None:
        self._ephemeral_max_output_tokens = value

    async def generate_stream(self, messages, tools=None, **_):
        self.message_snapshots.append([m.model_copy(deep=True) for m in messages])
        self.ephemeral_max_tokens_history.append(self._ephemeral_max_output_tokens)
        self._ephemeral_max_output_tokens = None  # matches the real client's consume

        entry = self._script[self.calls]
        self.calls += 1

        if entry.get("text"):
            yield StreamEvent(type="text", delta=entry["text"])
        yield StreamEvent(
            type="finish",
            finish_reason=entry.get("finish_reason", "stop"),
            usage=entry.get("usage")
            or TokenUsage(completion_tokens=200, total_tokens=200),
            tool_calls=entry.get("tool_calls"),
            truncated_tool_calls=entry.get("truncated_tool_calls"),
            raw_finish_reason=entry.get("raw_finish_reason", entry.get("finish_reason")),
            stream_dropped_mid_tool=entry.get("stream_dropped_mid_tool", False),
            oversized_tool_calls=entry.get("oversized_tool_calls"),
        )


class _PresentationArgumentRepairPolicy:
    checkpoint_injection_id = "test-presentation-checkpoint"
    evidence_read_batch_size = 1
    tool_argument_repair_limit = 2

    def build_checkpoint(self):
        return None

    def allows_completion_continuation(self):
        return False

    def suppresses_generic_final_summary(self):
        return False


def _msgs() -> list[Message]:
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="q"),
    ]


async def _collect(gen) -> list:
    return [ev async for ev in gen]


# ── Case 3 (P1 regression): visible text + length → NO double stream ──


async def test_length_with_visible_text_does_not_restream_content():
    """The blocking finding from PR #11 review.

    A ``length`` finish arrives with visible text already streamed to the
    host. The old code did a SAME-messages retry and the second successful
    reply re-streamed a full ``ContentEvent`` — the host UI shows the text
    twice. The fix appends the partial as an assistant turn and injects a
    (non-user-visible) continuation prompt so the next reply *continues*
    rather than restarts.
    """
    partial = "阿根廷是南美劲旅，手里的牌太"  # >40 chars, ends mid-thought
    completion = "薄，难以与之抗衡。"
    llm = _ScriptedLLM(
        [
            {"text": partial, "finish_reason": "length"},
            {"text": completion, "finish_reason": "stop"},
        ]
    )

    messages = _msgs()
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={},
            max_steps=5,
        )
    )

    # The turn completed successfully — not surfaced as MAX_TOKENS.
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN, (
        f"length with visible text should recover via continuation, got {done.stop_reason}"
    )

    # ContentEvent stream must contain each piece exactly once — no restart.
    streamed_content = "".join(
        e.content for e in events
        if isinstance(e, ContentEvent) and e._streaming and not e._header
    )
    assert streamed_content.count(partial) == 1, (
        f"partial streamed {streamed_content.count(partial)}× (regression): "
        f"{streamed_content!r}"
    )
    assert streamed_content.count(completion) == 1
    # And critically, the partial does not appear a second time after the
    # completion (the exact double-render pattern the review flagged).
    assert streamed_content == partial + completion, (
        f"unexpected streamed_content: {streamed_content!r}"
    )

    # Two LLM invocations: original + one continuation, NOT a SAME-messages
    # retry. The continuation is proven by the second call seeing MORE
    # messages (the appended assistant partial + the injected user nudge).
    assert llm.calls == 2
    first_len = len(llm.message_snapshots[0])
    second_len = len(llm.message_snapshots[1])
    assert second_len > first_len, (
        f"expected the continuation to grow the message list "
        f"({first_len} → {second_len}); SAME-messages retry would keep them equal"
    )

    # A non-user-visible injection was emitted (the continuation nudge).
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert injected and all(e.user_visible is False for e in injected)

    # And max_tokens was NOT boosted for the continuation — this path uses
    # the natural next-turn call, not the retry ladder.
    assert llm.ephemeral_max_tokens_history == [None, None]


# ── Case 1/2 (unchanged path): NO visible text → SAME-messages retry ──


async def test_stream_dropped_mid_tool_injects_recovery_without_boost():
    """When the SSE stream dies mid tool-call and nothing was rendered, the
    loop retries with SAME messages and does NOT boost ``max_tokens`` (boosting
    a stream-drop is pointless).
    """
    llm = _ScriptedLLM(
        [
            {
                "text": "",
                "finish_reason": "length",
                "raw_finish_reason": None,
                "stream_dropped_mid_tool": True,
                "truncated_tool_calls": [{"name": "web_search", "arguments_len": 42}],
            },
            {"text": "done.", "finish_reason": "stop"},
        ]
    )

    messages = _msgs()
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={},
            max_steps=5,
        )
    )

    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN
    assert llm.calls == 2
    assert len(llm.message_snapshots[1]) > len(llm.message_snapshots[0])
    # No boost on a stream-drop.
    assert llm.ephemeral_max_tokens_history == [None, None]
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "None of its tool calls were executed" in injected[0].content


async def test_output_cap_write_truncation_injects_chunk_recovery_without_boost():
    llm = _ScriptedLLM(
        [
            {
                "text": "",
                "finish_reason": "length",
                "raw_finish_reason": "length",
                "stream_dropped_mid_tool": False,
                "truncated_tool_calls": [{"name": "write_file", "arguments_len": 4090}],
            },
            {"text": "ok.", "finish_reason": "stop"},
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
        )
    )

    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN
    assert llm.calls == 2
    assert llm.ephemeral_max_tokens_history == [None, None]
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "next_chunk_index" in injected[0].content
    assert "only when no chunk has been accepted" in injected[0].content
    assert "that response made no file-system changes" in injected[0].content


@pytest.mark.parametrize("finish_reason", ["length", "max_tokens"])
async def test_parseable_output_length_write_call_is_not_executed(
    tmp_path,
    finish_reason,
):
    target = tmp_path / "large.html"
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": finish_reason,
                "tool_calls": [
                    ToolCall(
                        id="call_write",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={"path": "large.html", "content": "partial"},
                        ),
                    )
                ],
            },
            {"text": "recovered.", "finish_reason": "stop"},
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"write_file": WriteTool(workspace_dir=str(tmp_path))},
            max_steps=5,
        )
    )

    assert not target.exists()
    retry_history = llm.message_snapshots[1]
    assert not [message for message in retry_history if message.role == "tool"]
    assert not [message for message in retry_history if message.tool_calls]
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "None of the tool calls in that response were executed" in injected[0].content


async def test_output_length_recovery_can_resume_an_existing_write_transaction(
    tmp_path,
):
    target = tmp_path / "large.txt"
    write_tool = WriteTool(workspace_dir=str(tmp_path))
    accepted = await write_tool.execute(
        path="large.txt",
        content="first-",
        chunk_index=0,
        final=False,
    )
    assert accepted.success
    assert accepted.raw_output["next_chunk_index"] == 1

    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "length",
                "tool_calls": [
                    ToolCall(
                        id="discarded-chunk",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "large.txt",
                                "content": "must-not-be-written",
                                "chunk_index": 1,
                                "final": True,
                            },
                        ),
                    )
                ],
            },
            {
                "finish_reason": "tool_calls",
                "tool_calls": [
                    ToolCall(
                        id="resumed-chunk",
                        type="function",
                        function=FunctionCall(
                            name="write_file",
                            arguments={
                                "path": "large.txt",
                                "content": "second",
                                "chunk_index": 1,
                                "final": True,
                            },
                        ),
                    )
                ],
            },
            {"text": "done.", "finish_reason": "stop"},
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={"write_file": write_tool},
            max_steps=6,
        )
    )

    assert target.read_text(encoding="utf-8") == "first-second"
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "continue with the next_chunk_index" in injected[0].content


async def test_tool_argument_limit_injects_one_chunked_write_repair_without_boost():
    llm = _ScriptedLLM(
        [
            {
                "finish_reason": "tool_argument_limit",
                "oversized_tool_calls": [
                    {"name": "bash", "arguments_len": 10001, "limit": 10000}
                ],
            },
            {"text": "done.", "finish_reason": "stop"},
        ]
    )

    events = await _collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5)
    )

    assert [e for e in events if isinstance(e, DoneEvent)][-1].stop_reason == StopReason.END_TURN
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert "write_file" in injected[0].content
    assert "chunk_index=0" in injected[0].content
    assert "工具没有执行" in injected[0].content
    assert llm.ephemeral_max_tokens_history == [None, None]


async def test_repeated_tool_argument_limit_stops_after_one_repair():
    oversized = {
        "finish_reason": "tool_argument_limit",
        "oversized_tool_calls": [
            {"name": "bash", "arguments_len": 10001, "limit": 10000}
        ],
    }
    llm = _ScriptedLLM([oversized, oversized])

    events = await _collect(
        run_agent_loop(llm=llm, messages=_msgs(), tools={}, max_steps=5)
    )

    assert llm.calls == 2
    assert [e for e in events if isinstance(e, DoneEvent)][-1].stop_reason == StopReason.ERROR
    assert len([e for e in events if isinstance(e, InjectedMessageEvent)]) == 1
    assert llm.ephemeral_max_tokens_history == [None, None]


async def test_controlled_presentation_gets_two_chunked_write_repairs():
    oversized = {
        "finish_reason": "tool_argument_limit",
        "oversized_tool_calls": [
            {"name": "execute_code", "arguments_len": 16004, "limit": 16000}
        ],
    }
    llm = _ScriptedLLM(
        [oversized, oversized, {"text": "done.", "finish_reason": "stop"}]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            workflow_policy=_PresentationArgumentRepairPolicy(),
        )
    )

    assert llm.calls == 3
    assert [e for e in events if isinstance(e, DoneEvent)][-1].stop_reason == StopReason.END_TURN
    assert len([e for e in events if isinstance(e, InjectedMessageEvent)]) == 2
    assert llm.ephemeral_max_tokens_history == [None, None, None]


async def test_controlled_presentation_stops_after_two_argument_repairs():
    oversized = {
        "finish_reason": "tool_argument_limit",
        "oversized_tool_calls": [
            {"name": "execute_code", "arguments_len": 16004, "limit": 16000}
        ],
    }
    llm = _ScriptedLLM([oversized, oversized, oversized])

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            workflow_policy=_PresentationArgumentRepairPolicy(),
        )
    )

    assert llm.calls == 3
    assert [e for e in events if isinstance(e, DoneEvent)][-1].stop_reason == StopReason.ERROR
    assert len([e for e in events if isinstance(e, InjectedMessageEvent)]) == 2


# ── Case 3 bound: continuation budget is respected ──


async def test_length_with_visible_text_respects_continuation_cap():
    """A model that keeps returning length + visible text is bounded by
    ``max_truncation_continuations`` and eventually errors out — it must NOT
    loop forever, and every partial it did stream is rendered exactly once.
    """
    partial = "永远写不完的一段分析文字，" * 5 + "结尾停在了半句话这里太"
    llm = _ScriptedLLM(
        [
            {"text": partial, "finish_reason": "length"},
            {"text": partial, "finish_reason": "length"},
            {"text": partial, "finish_reason": "length"},
        ]
    )

    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=8,
            max_truncation_continuations=2,
        )
    )

    # Called original + exactly 2 continuations, then bailed out.
    assert llm.calls == 3
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.MAX_TOKENS
    # And still no double-stream inside each attempt: each of the three
    # streamed partials should appear once as its own ContentEvent chunk.
    streamed_chunks = [
        e.content for e in events
        if isinstance(e, ContentEvent) and e._streaming and not e._header
    ]
    assert streamed_chunks == [partial, partial, partial]


# ── LLMOutputEvent still carries the accurate finish_reason ──


async def test_llm_output_event_finish_reason_is_length_on_truncated_turn():
    """Independent of how the loop recovers, the per-turn ``LLMOutputEvent``
    for the truncated attempt must faithfully report ``finish_reason="length"``
    so downstream logging / telemetry are not misled by the recovery path.
    """
    partial = "一段被截断的分析" * 5 + "停在了这里太"
    llm = _ScriptedLLM(
        [
            {"text": partial, "finish_reason": "length"},
            {"text": "补完。", "finish_reason": "stop"},
        ]
    )
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
        )
    )
    finishes = [e.finish_reason for e in events if isinstance(e, LLMOutputEvent)]
    assert finishes[0] == "length"
    assert finishes[1] in ("stop", "end_turn", None)
