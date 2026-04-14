"""Tests for MemoryManager section read/write and MemoryExtractor."""

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
    return MemoryManager(memory_dir=str(memory_dir), recall_days=3)


# ── Section read/write ────────────────────────────────────────


def test_read_section_empty(mgr: MemoryManager):
    assert mgr.read_section("Manual Memory") == ""
    assert mgr.read_section("Auto Memory") == ""


def test_write_section_creates_file(mgr: MemoryManager):
    mgr.write_section("Manual Memory", "- user likes Python")
    assert "user likes Python" in mgr.read_section("Manual Memory")


def test_write_section_preserves_other_sections(mgr: MemoryManager):
    mgr.write_section("Manual Memory", "- name: Alice")
    mgr.write_section("Auto Memory", "- prefers short answers")

    # Both sections exist
    assert "name: Alice" in mgr.read_section("Manual Memory")
    assert "prefers short answers" in mgr.read_section("Auto Memory")

    # Update only Auto section
    mgr.write_section("Auto Memory", "- prefers short answers\n- uses uv")

    # Manual section unchanged
    assert "name: Alice" in mgr.read_section("Manual Memory")
    assert "uses uv" in mgr.read_section("Auto Memory")


def test_append_to_section(mgr: MemoryManager):
    mgr.append_to_section("Manual Memory", "- item 1")
    mgr.append_to_section("Manual Memory", "- item 2")
    content = mgr.read_section("Manual Memory")
    assert "item 1" in content
    assert "item 2" in content


def test_section_roundtrip_preserves_preamble(mgr: MemoryManager):
    """Content before the first ## heading is preserved."""
    mgr.write_manual_memory("Some preamble\n\n## Manual Memory\n- fact A\n\n## Auto Memory\n- fact B")
    assert "fact A" in mgr.read_section("Manual Memory")
    assert "fact B" in mgr.read_section("Auto Memory")

    # Update Auto section — preamble should survive
    mgr.write_section("Auto Memory", "- fact C")
    raw = mgr.read_manual_memory()
    assert "Some preamble" in raw
    assert "fact A" in raw
    assert "fact C" in raw


# ── Recall with sections ──────────────────────────────────────


def test_recall_includes_auto_memory(mgr: MemoryManager):
    mgr.write_section("Manual Memory", "- user pref")
    mgr.write_section("Auto Memory", "- auto fact")
    block = mgr.recall()
    assert "[Manual Memory]" in block
    assert "[Auto Memory]" in block
    assert "user pref" in block
    assert "auto fact" in block


def test_build_memory_block_with_auto():
    block = MemoryManager.build_memory_block(
        manual="- manual item",
        openclaw="",
        auto="- auto item",
    )
    assert "[Manual Memory]" in block
    assert "[Auto Memory]" in block
    assert "manual item" in block
    assert "auto item" in block


# ── MemoryExtractor ───────────────────────────────────────────


def _make_llm(response_text: str):
    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = response_text
    mock_llm.generate = AsyncMock(return_value=mock_response)
    return mock_llm


def _make_extractor(mgr: MemoryManager, response_text: str, **kwargs) -> MemoryExtractor:
    llm = _make_llm(response_text)
    return MemoryExtractor(llm=llm, memory_manager=mgr, **kwargs)


async def test_extract_additions(mgr: MemoryManager):
    """Additions are appended to Auto Memory section."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- user name is Alice", "- prefers Python"], "merges": []}',
    )
    messages = [
        Message(role="user", content="My name is Alice and I prefer Python"),
        Message(role="assistant", content="Got it!"),
    ]

    result = await extractor.maybe_extract(messages, "session_end")
    assert result is True
    auto = mgr.read_section("Auto Memory")
    assert "user name is Alice" in auto
    assert "prefers Python" in auto


async def test_extract_merges(mgr: MemoryManager):
    """Merges replace existing text in Auto Memory."""
    from box_agent.schema import Message

    # Pre-populate Auto Memory
    mgr.write_section("Auto Memory", "- user name is Alice")

    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- user name is Alice", "new": "- user name is Alice Zhang"}]}',
    )
    messages = [
        Message(role="user", content="Actually my full name is Alice Zhang"),
        Message(role="assistant", content="Updated!"),
    ]

    await extractor.maybe_extract(messages, "session_end")
    auto = mgr.read_section("Auto Memory")
    assert "Alice Zhang" in auto
    assert auto.count("Alice") == 1  # old entry replaced, not duplicated


async def test_extract_empty_output_no_change(mgr: MemoryManager):
    """When LLM says nothing to remember, Auto Memory is unchanged."""
    from box_agent.schema import Message

    mgr.write_section("Auto Memory", "- existing fact")
    extractor = _make_extractor(mgr, '{"additions": [], "merges": []}')
    messages = [Message(role="user", content="What time is it?")]

    await extractor.maybe_extract(messages, "session_end")
    assert mgr.read_section("Auto Memory") == "- existing fact"


async def test_extract_does_not_touch_manual(mgr: MemoryManager):
    """Extraction only modifies Auto Memory, not Manual Memory."""
    from box_agent.schema import Message

    mgr.write_section("Manual Memory", "- user wrote this manually")
    extractor = _make_extractor(
        mgr,
        '{"additions": ["- auto extracted fact"], "merges": []}',
    )
    messages = [Message(role="user", content="Something interesting")]

    await extractor.maybe_extract(messages, "session_end")
    assert mgr.read_section("Manual Memory") == "- user wrote this manually"
    assert "auto extracted fact" in mgr.read_section("Auto Memory")


async def test_cooldown_prevents_repeated_extraction(mgr: MemoryManager):
    """Within cooldown period, step_interval triggers are skipped."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=9999,  # very long cooldown
        step_interval=1,
    )
    messages = [Message(role="user", content="test")]

    # First call succeeds (session_end ignores cooldown)
    assert await extractor.maybe_extract(messages, "session_end") is True

    # Subsequent step_interval call is blocked by cooldown
    assert await extractor.maybe_extract(messages, "step_interval") is False


async def test_step_interval_counting(mgr: MemoryManager):
    """Extraction only fires after step_interval steps."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=0,  # no cooldown
        step_interval=3,
    )
    messages = [Message(role="user", content="test")]

    # Steps 1, 2: not enough
    assert await extractor.maybe_extract(messages, "step_interval") is False
    assert await extractor.maybe_extract(messages, "step_interval") is False

    # Step 3: fires
    assert await extractor.maybe_extract(messages, "step_interval") is True


async def test_session_end_ignores_cooldown(mgr: MemoryManager):
    """session_end trigger always runs regardless of cooldown."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '{"additions": ["- fact"], "merges": []}',
        cooldown=9999,
    )
    messages = [Message(role="user", content="test")]

    # First session_end
    assert await extractor.maybe_extract(messages, "session_end") is True
    # Second session_end immediately — still runs
    assert await extractor.maybe_extract(messages, "session_end") is True


async def test_invalid_json_from_llm(mgr: MemoryManager):
    """Invalid JSON from LLM is handled gracefully."""
    from box_agent.schema import Message

    extractor = _make_extractor(mgr, "This is not JSON at all")
    messages = [Message(role="user", content="test")]

    # Should not crash, should return True (extraction ran but no updates)
    result = await extractor.maybe_extract(messages, "session_end")
    assert result is True
    assert mgr.read_section("Auto Memory") == ""


async def test_extract_with_markdown_fences(mgr: MemoryManager):
    """LLM output wrapped in markdown code fences is handled."""
    from box_agent.schema import Message

    extractor = _make_extractor(
        mgr,
        '```json\n{"additions": ["- from fenced output"], "merges": []}\n```',
    )
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "session_end")
    assert "from fenced output" in mgr.read_section("Auto Memory")


async def test_merge_ambiguous_skipped(mgr: MemoryManager):
    """Ambiguous merges (multiple matching lines) are rejected."""
    from box_agent.schema import Message

    # Two identical lines in Auto Memory
    mgr.write_section("Auto Memory", "- uses Python\n- prefers dark mode\n- uses Python")

    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- uses Python", "new": "- uses Python 3.12"}]}',
    )
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "session_end")
    auto = mgr.read_section("Auto Memory")
    # Both lines should be untouched — ambiguous merge rejected
    assert auto.count("- uses Python") == 2
    assert "3.12" not in auto


async def test_merge_no_match_ignored(mgr: MemoryManager):
    """Merges targeting non-existent lines are silently ignored."""
    from box_agent.schema import Message

    mgr.write_section("Auto Memory", "- existing fact")
    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "- nonexistent line", "new": "- replacement"}]}',
    )
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "session_end")
    assert mgr.read_section("Auto Memory") == "- existing fact"


async def test_merge_substring_does_not_match(mgr: MemoryManager):
    """Substring of a line does NOT trigger a merge — must be exact line."""
    from box_agent.schema import Message

    mgr.write_section("Auto Memory", "- user name is Alice Zhang")
    extractor = _make_extractor(
        mgr,
        '{"additions": [], "merges": [{"old": "Alice", "new": "Bob"}]}',
    )
    messages = [Message(role="user", content="test")]

    await extractor.maybe_extract(messages, "session_end")
    # "Alice" is a substring, not an exact line — should not be replaced
    assert "Alice Zhang" in mgr.read_section("Auto Memory")
    assert "Bob" not in mgr.read_section("Auto Memory")
