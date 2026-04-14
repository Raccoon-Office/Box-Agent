"""Tests for MemoryExtractor — writes to CONTEXT.md."""

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


# ── Extraction → CONTEXT.md ──────────────────────────────────


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


async def test_extract_merges(mgr: MemoryManager):
    """Merges replace existing text in CONTEXT.md."""
    from box_agent.schema import Message

    mgr.write_context("- user name is Alice")

    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- user name is Alice", "new": "- user name is Alice Zhang"}]}',
    )
    messages = [
        Message(role="user", content="Actually my full name is Alice Zhang"),
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
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "loop_end")
    ctx = mgr.read_context()
    assert "project deadline" in ctx
    assert "Alice" not in ctx


async def test_extract_does_not_touch_core(mgr: MemoryManager):
    """Extraction only modifies CONTEXT.md, not MEMORY.md."""
    from box_agent.schema import Message

    mgr.write_core("- user wrote this manually")
    extractor = _make_extractor(
        mgr,
        '{"additions": ["- auto extracted fact"], "merges": []}',
    )
    messages = [Message(role="user", content="Something interesting")]

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
    messages = [Message(role="user", content="test")]

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
    messages = [Message(role="user", content="test")]

    assert await extractor.maybe_extract(messages, "step_interval") is False
    assert await extractor.maybe_extract(messages, "step_interval") is False
    assert await extractor.maybe_extract(messages, "step_interval") is True


async def test_loop_end_ignores_cooldown(mgr: MemoryManager):
    """loop_end trigger always runs regardless of cooldown."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=9999,
    )
    messages = [Message(role="user", content="test")]

    assert await extractor.maybe_extract(messages, "loop_end") is True
    assert await extractor.maybe_extract(messages, "loop_end") is True


# ── Edge cases ────────────────────────────────────────────────


async def test_invalid_json_from_llm(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(mgr, "This is not JSON at all")
    messages = [Message(role="user", content="test")]

    result = await extractor.maybe_extract(messages, "loop_end")
    assert result is True
    assert mgr.read_context() == ""


async def test_extract_with_markdown_fences(mgr: MemoryManager):
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '```json\n{"additions": ["- from fenced output"], "merges": []}\n```',
    )
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "loop_end")
    assert "from fenced output" in mgr.read_context()


async def test_merge_ambiguous_skipped(mgr: MemoryManager):
    from box_agent.schema import Message

    mgr.write_context("- uses Python\n- prefers dark mode\n- uses Python")
    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- uses Python", "new": "- uses Python 3.12"}]}',
    )
    messages = [Message(role="user", content="test")]

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
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "loop_end")
    assert "Alice Zhang" in mgr.read_context()
    assert "Bob" not in mgr.read_context()
