"""Tests for suspected-truncation detection + in-turn continuation.

Covers:
- ``loop_guards.looks_like_truncated_output`` (pure, conservative detector)
- the in-turn continuation wiring in ``run_agent_loop``: it re-prompts once
  when a reply ends mid-sentence under a normal finish, is bounded by
  ``max_truncation_continuations``, respects the on/off switch, and skips
  short replies via the completion-token floor.
"""

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import DoneEvent, InjectedMessageEvent, StopReason
from box_agent.loop_guards import (
    MIN_TOKENS_FOR_TRUNCATION_CHECK,
    looks_like_truncated_output,
    truncation_continuation_text,
)
from box_agent.schema import LLMResponse, Message, StreamEvent, TokenUsage


# ── Pure detector ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        # The real incident: a half sentence ending on a bare CJK char.
        "面对阿根廷这种顶级强队时，手里的牌太",
        "塞拉米的问题不是“不会带队”，而是",
        "the model stopped writing in the middle of a",
        "未闭合的 **加粗",
        "一句没有结尾的话",
    ],
)
def test_looks_like_truncated_true(text):
    assert looks_like_truncated_output(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "这是一句完整的话。",
        "Done!",
        "真的吗？",
        "省略一下…",
        "结尾是一个列表项的引导：",
        "**加粗收尾**",
        "行内代码 `code`",
        "引用闭合”",
        "（括号闭合）",
        "| 列 | 值 |",
        "```",
        "- 一个完整的列表项",
        "* another bullet",
        "1. numbered item",
        "",
        "   \n  ",
    ],
)
def test_looks_like_truncated_false(text):
    assert looks_like_truncated_output(text) is False


def test_truncation_continuation_text_mentions_tail_and_rules():
    txt = truncation_continuation_text("手里的牌太")
    assert "手里的牌太" in txt
    assert "不要重复" in txt
    assert "重新开头" in txt


# ── Loop wiring ─────────────────────────────────────────────────


class MockLLM:
    """Deterministic streaming LLM yielding pre-configured responses."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls = 0

    async def generate_stream(self, messages, tools=None, **_):
        resp = self._responses[self.calls]
        self.calls += 1
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="给我讲讲这个"),
    ]


async def _collect(gen):
    return [ev async for ev in gen]


def _truncated(content: str, completion_tokens: int = 200) -> LLMResponse:
    return LLMResponse(
        content=content,
        finish_reason="stop",
        usage=TokenUsage(completion_tokens=completion_tokens, total_tokens=completion_tokens),
    )


async def test_continues_once_on_suspected_truncation():
    """A mid-sentence stop is re-prompted; the completed reply ends the turn."""
    llm = MockLLM([_truncated("手里的牌太"), _truncated("薄，难以与阿根廷抗衡。")])
    messages = _msgs()
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={},
            max_steps=5,
            truncation_continuation_enabled=True,
            max_truncation_continuations=1,
        )
    )

    assert llm.calls == 2  # original + one continuation
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    assert injected[0].user_visible is False

    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN

    # Same prompt turn: two assistant segments with the continuation user
    # message wedged between them (in-turn → seamless single message).
    roles = [m.role for m in messages]
    assert roles.count("assistant") == 2
    assistant_idxs = [i for i, m in enumerate(messages) if m.role == "assistant"]
    between = messages[assistant_idxs[0] + 1 : assistant_idxs[1]]
    assert any(m.role == "user" for m in between)


async def test_continuation_is_bounded():
    """With a cap of 1, a persistently-truncating model is not chased forever."""
    llm = MockLLM([_truncated("第一段没写完太"), _truncated("第二段也没写完太"), _truncated("第三段太")])
    messages = _msgs()
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=messages,
            tools={},
            max_steps=5,
            truncation_continuation_enabled=True,
            max_truncation_continuations=1,
        )
    )

    assert llm.calls == 2  # original + exactly one continuation, then stop
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN


async def test_disabled_switch_skips_continuation():
    llm = MockLLM([_truncated("手里的牌太")])
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            truncation_continuation_enabled=False,
        )
    )
    assert llm.calls == 1
    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN


async def test_short_reply_below_token_floor_not_continued():
    """A truncated-looking but tiny reply is left alone (avoid chasing noise)."""
    llm = MockLLM(
        [_truncated("太", completion_tokens=MIN_TOKENS_FOR_TRUNCATION_CHECK - 1)]
    )
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            truncation_continuation_enabled=True,
        )
    )
    assert llm.calls == 1
    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN


async def test_short_reply_without_usage_not_continued():
    """Char-count fallback: no usage + short body → not chased as truncation."""
    llm = MockLLM([LLMResponse(content="手里的牌太", finish_reason="stop")])
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            truncation_continuation_enabled=True,
        )
    )
    assert llm.calls == 1
    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN


async def test_long_reply_without_usage_is_continued():
    """Char-count fallback: no usage + long mid-sentence body → continued once."""
    long_tail = "这是一段很长的分析" * 6 + "最后停在了半句话这里太"  # >40 chars, no period
    llm = MockLLM(
        [
            LLMResponse(content=long_tail, finish_reason="stop"),
            LLMResponse(content="薄，到此补完。", finish_reason="stop"),
        ]
    )
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            truncation_continuation_enabled=True,
            max_truncation_continuations=1,
        )
    )
    assert llm.calls == 2
    injected = [e for e in events if isinstance(e, InjectedMessageEvent)]
    assert len(injected) == 1 and injected[0].user_visible is False


async def test_clean_ending_not_continued():
    """A normal, well-terminated reply ends the turn without a continuation."""
    llm = MockLLM([_truncated("分析完毕，以上就是结论。")])
    events = await _collect(
        run_agent_loop(
            llm=llm,
            messages=_msgs(),
            tools={},
            max_steps=5,
            truncation_continuation_enabled=True,
        )
    )
    assert llm.calls == 1
    assert not [e for e in events if isinstance(e, InjectedMessageEvent)]
    done = [e for e in events if isinstance(e, DoneEvent)][-1]
    assert done.stop_reason == StopReason.END_TURN
