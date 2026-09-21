"""Tests for MemoryExtractor — writes persistent memory."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.memory import MemoryExtractor, MemoryManager


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mgr(memory_dir: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(memory_dir))


def _make_llm(response_text: str):
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = response_text
    mock_llm.generate = AsyncMock(return_value=mock_response)
    return mock_llm


def _make_extractor(mgr: MemoryManager, response_text: str, **kwargs) -> MemoryExtractor:
    llm = _make_llm(response_text)
    return MemoryExtractor(llm=llm, memory_manager=mgr, **kwargs)


# ── Extraction → persistent memory ───────────────────────────


async def test_extract_additions(mgr: MemoryManager):
    """Additions are appended to CONTEXT.md."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- user name is Alice", "- prefers Python"], "merges": []}',
    )
    messages = [
        Message(role="user", content="My name is Alice and I prefer Python"),
        Message(role="assistant", content="Got it!"),
    ]

    result = await extractor.maybe_extract(messages, "loop_end")
    assert result is True
    ctx = mgr.read_context()
    assert "user name is Alice" in ctx
    assert "prefers Python" in ctx
    # Core untouched
    assert mgr.read_core() == ""


async def test_extraction_prompt_requires_portable_generalization(mgr: MemoryManager):
    """The extractor must be instructed to remove one-off task framing."""
    from box_agent.schema import Message

    extractor = _make_extractor(mgr, '{"additions": [], "merges": []}')
    await extractor.maybe_extract(
        [
            Message(role="user", content="记住这类多格式内容交付方法"),
            Message(role="assistant", content="好的"),
        ],
        "loop_end",
    )

    prompt = extractor._llm.generate.await_args.kwargs["messages"][1].content
    assert "portability test" in prompt
    assert "would this still be useful" in prompt
    assert "家园沟通类内容" in prompt


async def test_extract_threads_session_id(mgr: MemoryManager):
    from box_agent.schema import Message

    llm = _make_llm('{"additions": [], "merges": []}')
    extractor = MemoryExtractor(
        llm=llm,
        memory_manager=mgr,
        session_id="office-session-1",
        turn_id="office-turn-1",
    )

    result = await extractor.maybe_extract(
        [Message(role="user", content="Remember that I prefer Python")],
        "loop_end",
    )

    assert result is True
    assert llm.generate.await_args.kwargs["session_id"] == "office-session-1"
    assert llm.generate.await_args.kwargs["turn_id"] == "office-turn-1"


async def test_extract_additions_stamp_session_turn_and_trigger(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": [{"text": "- user prefers Python", "topic": "preferences"}], "merges": []}',
        session_id="office-session-1",
        turn_id="turn-42",
    )
    messages = [Message(role="user", content="Remember that I prefer Python")]

    result = await extractor.maybe_extract(messages, "loop_end")

    assert result is True
    entries = mgr.read_all_context_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == "extractor"
    assert entry.session_id == "office-session-1"
    assert entry.turn_id == "turn-42"
    assert entry.trigger == "loop_end"


async def test_extract_uses_turn_id_snapshot_over_mutable_state(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- user prefers Python"], "merges": []}',
        session_id="office-session-1",
        turn_id="stale-turn",
    )
    extractor.set_turn_id("newer-turn")
    messages = [Message(role="user", content="Remember that I prefer Python")]

    result = await extractor.maybe_extract(messages, "loop_end", turn_id="snapshot-turn")

    assert result is True
    entries = mgr.read_all_context_entries()
    assert len(entries) == 1
    assert entries[0].turn_id == "snapshot-turn"


async def test_extract_core_additions_write_memory_md(mgr: MemoryManager):
    """Explicit user profile/default facts can go directly to core memory."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"core_additions": ["- User\\u0027s default city for local queries is Beijing"], '
        '"additions": [], "merges": []}',
    )
    messages = [
        Message(role="user", content="今天天气怎么样"),
        Message(role="assistant", content="你想查哪个城市的天气？"),
        Message(role="user", content="记住我在北京"),
    ]

    result = await extractor.maybe_extract(messages, "loop_end")

    assert result is True
    assert "default city for local queries is Beijing" in mgr.read_core()
    assert mgr.read_context() == ""


async def test_extract_core_additions_dedup_and_filter_context_duplicate(mgr: MemoryManager):
    """Core additions are deduped and duplicate context additions are skipped."""
    from box_agent.schema import Message

    mgr.write_core("- User's default city for local queries is Beijing")
    extractor = _make_extractor(
        mgr,
        '{"core_additions": ["- User\\u0027s default city for local queries is Beijing"], '
        '"additions": [{"text": "- User\\u0027s default city for local queries is Beijing", "topic": "user_profile"}], '
        '"merges": []}',
    )
    messages = [Message(role="user", content="记住我在北京")]

    await extractor.maybe_extract(messages, "loop_end")

    assert mgr.read_core().count("default city for local queries is Beijing") == 1
    assert mgr.read_context() == ""


async def test_extract_merges(mgr: MemoryManager):
    """Merges replace existing text in CONTEXT.md."""
    from box_agent.schema import Message

    mgr.write_context("- user name is Alice")

    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- user name is Alice", "new": "- user name is Alice Zhang"}]}',
    )
    messages = [
        Message(role="user", content="Remember that my full name is Alice Zhang"),
        Message(role="assistant", content="Updated!"),
    ]

    await extractor.maybe_extract(messages, "loop_end")
    ctx = mgr.read_context()
    assert "Alice Zhang" in ctx
    assert ctx.count("Alice") == 1


async def test_extract_empty_output_no_change(mgr: MemoryManager):
    """When LLM says nothing to remember, CONTEXT.md is unchanged."""
    from box_agent.schema import Message

    mgr.write_context("- existing fact")
    extractor = _make_extractor(mgr, '{"additions": [], "merges": []}')
    messages = [Message(role="user", content="What time is it?")]

    await extractor.maybe_extract(messages, "loop_end")
    assert mgr.read_context() == "- existing fact"


async def test_extract_additions_dedup_against_core(mgr: MemoryManager):
    """Additions that duplicate Core lines are filtered out."""
    from box_agent.schema import Message

    mgr.write_core("- user: Alice")
    extractor = _make_extractor(
        mgr,
        '{"additions": ["- user: Alice", "- project deadline: June"], "merges": []}',
    )
    messages = [Message(role="user", content="Remember that I prefer concise updates")]

    await extractor.maybe_extract(messages, "loop_end")
    ctx = mgr.read_context()
    assert "project deadline" in ctx
    assert "Alice" not in ctx


async def test_extract_additions_dedup_against_context(mgr: MemoryManager):
    """Additions that duplicate existing Context lines are filtered out."""
    from box_agent.schema import Message

    mgr.write_context("- Q2 goal: dashboard")
    extractor = _make_extractor(
        mgr,
        '{"additions": ["- q2 goal: dashboard", "- weekly report format: progress/issues/plan"], "merges": []}',
    )
    messages = [Message(role="user", content="Remember this weekly report preference")]

    await extractor.maybe_extract(messages, "loop_end")
    ctx = mgr.read_context()
    assert ctx.lower().count("q2 goal: dashboard") == 1
    assert "weekly report format" in ctx


async def test_context_only_extraction_does_not_touch_core(mgr: MemoryManager):
    """Context-only additions do not modify MEMORY.md."""
    from box_agent.schema import Message

    mgr.write_core("- user wrote this manually")
    extractor = _make_extractor(
        mgr,
        '{"additions": ["- auto extracted fact"], "merges": []}',
    )
    messages = [Message(role="user", content="Remember this context-only fact")]

    await extractor.maybe_extract(messages, "loop_end")
    assert mgr.read_core() == "- user wrote this manually"
    assert "auto extracted fact" in mgr.read_context()


# ── Cooldown / interval ──────────────────────────────────────


async def test_cooldown_prevents_repeated_extraction(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=9999,
        step_interval=1,
    )
    messages = [Message(role="user", content="Remember this fenced-output fact")]

    assert await extractor.maybe_extract(messages, "loop_end") is True
    assert await extractor.maybe_extract(messages, "step_interval") is False


async def test_step_interval_counting(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=0,
        step_interval=3,
    )
    messages = [Message(role="user", content="Remember this Python preference")]

    assert await extractor.maybe_extract(messages, "step_interval") is False
    assert await extractor.maybe_extract(messages, "step_interval") is False
    assert await extractor.maybe_extract(messages, "step_interval") is True


async def test_loop_end_ignores_cooldown_for_high_signal_turns(mgr: MemoryManager):
    """loop_end can run despite cooldown, but only for high-signal turns."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=9999,
    )
    messages = [Message(role="user", content="Remember that I prefer Python")]

    assert await extractor.maybe_extract(messages, "loop_end") is True
    assert await extractor.maybe_extract(messages, "loop_end") is True


async def test_loop_end_skips_low_signal_turns(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=0,
    )

    assert await extractor.maybe_extract([Message(role="user", content="hi")], "loop_end") is False
    assert mgr.read_context() == ""


# ── Edge cases ────────────────────────────────────────────────


async def test_invalid_json_from_llm(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(mgr, "This is not JSON at all")
    messages = [Message(role="user", content="Remember this preference")]

    result = await extractor.maybe_extract(messages, "loop_end")
    assert result is True
    assert mgr.read_context() == ""


async def test_extract_with_markdown_fences(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '```json\n{"additions": ["- from fenced output"], "merges": []}\n```',
    )
    messages = [Message(role="user", content="Remember this name preference")]

    await extractor.maybe_extract(messages, "loop_end")
    assert "from fenced output" in mgr.read_context()


async def test_merge_ambiguous_skipped(mgr: MemoryManager):
    from box_agent.schema import Message

    mgr.write_context("- uses Python\n- prefers dark mode\n- uses Python")
    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- uses Python", "new": "- uses Python 3.12"}]}',
    )
    messages = [Message(role="user", content="Remember this Python preference")]

    await extractor.maybe_extract(messages, "loop_end")
    ctx = mgr.read_context()
    assert ctx.count("- uses Python") == 2
    assert "3.12" not in ctx


async def test_merge_substring_does_not_match(mgr: MemoryManager):
    from box_agent.schema import Message

    mgr.write_context("- user name is Alice Zhang")
    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "Alice", "new": "Bob"}]}',
    )
    messages = [Message(role="user", content="Remember this name preference")]

    await extractor.maybe_extract(messages, "loop_end")
    assert "Alice Zhang" in mgr.read_context()
    assert "Bob" not in mgr.read_context()


# ── Topic-tagged extraction ──────────────────────────────────


async def test_extract_additions_routed_to_topics(mgr: MemoryManager):
    """Object-form additions land in their tagged topic files."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ['
        '{"text": "- user is a backend engineer", "topic": "user_profile"}, '
        '{"text": "- prefers Chinese replies", "topic": "preferences"}'
        '], "merges": []}',
    )
    messages = [Message(role="user", content="Remember that I am a backend engineer")]

    await extractor.maybe_extract(messages, "loop_end")

    assert "backend engineer" in mgr.read_context_topic("user_profile")
    assert "Chinese replies" in mgr.read_context_topic("preferences")
    assert sorted(mgr.list_topics()) == ["preferences", "user_profile"]


async def test_extract_unknown_topic_falls_back_to_general(mgr: MemoryManager):
    """An out-of-vocabulary topic label is folded into 'general'."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": [{"text": "- some fact", "topic": "made_up_label"}], "merges": []}',
    )
    messages = [Message(role="user", content="Remember this durable fact")]

    await extractor.maybe_extract(messages, "loop_end")

    assert mgr.list_topics() == ["general"]
    assert "some fact" in mgr.read_context_topic("general")


async def test_extract_string_additions_still_general(mgr: MemoryManager):
    """Legacy string additions remain backward-compatible (→ general)."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- legacy style fact"], "merges": []}',
    )
    messages = [Message(role="user", content="Remember this legacy style fact")]

    await extractor.maybe_extract(messages, "loop_end")

    assert mgr.list_topics() == ["general"]
    assert "legacy style fact" in mgr.read_context_topic("general")
