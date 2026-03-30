"""Tests for box_agent.memory — MemoryManager."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.memory import MemoryManager


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    """Provide a fresh temporary memory directory."""
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mgr(memory_dir: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(memory_dir), recall_days=3)


# ── Manual memory ──────────────────────────────────────────────


def test_read_write_manual_memory(mgr: MemoryManager):
    assert mgr.read_manual_memory() == ""

    mgr.write_manual_memory("- user prefers Chinese")
    assert "user prefers Chinese" in mgr.read_manual_memory()

    # Overwrite
    mgr.write_manual_memory("- new preference")
    content = mgr.read_manual_memory()
    assert "new preference" in content
    assert "user prefers Chinese" not in content


def test_read_manual_memory_missing_file(mgr: MemoryManager):
    """read_manual_memory returns '' when MEMORY.md doesn't exist."""
    assert mgr.read_manual_memory() == ""


# ── Session summaries ──────────────────────────────────────────


def test_save_session_summary(mgr: MemoryManager, memory_dir: Path):
    path = mgr.save_session_summary("sess-0-abc123", "# Session summary\nDid stuff.")
    assert path.exists()
    assert "Session summary" in path.read_text(encoding="utf-8")
    # File is in today's date directory
    today = date.today().isoformat()
    assert today in str(path)


# ── Recall ─────────────────────────────────────────────────────


def test_recall_empty(mgr: MemoryManager):
    """Empty memory → empty string, no block injected."""
    assert mgr.recall() == ""


def test_recall_with_manual_only(mgr: MemoryManager):
    mgr.write_manual_memory("- always use English")
    block = mgr.recall()
    assert "--- MEMORY START ---" in block
    assert "--- MEMORY END ---" in block
    assert "[Manual Memory]" in block
    assert "always use English" in block
    assert "[Recent Session Memory]" not in block


def test_recall_with_sessions(mgr: MemoryManager, memory_dir: Path):
    mgr.write_manual_memory("- pref A")
    # Create a session for today
    today = date.today().isoformat()
    day_dir = memory_dir / today
    day_dir.mkdir(parents=True, exist_ok=True)
    (day_dir / "sess-0-abc.md").write_text("Did data analysis", encoding="utf-8")

    block = mgr.recall()
    assert "[Manual Memory]" in block
    assert "[Recent Session Memory]" in block
    assert "sess-0-abc" in block
    assert "Did data analysis" in block


def test_recall_old_sessions_excluded(mgr: MemoryManager, memory_dir: Path):
    """Sessions older than recall_days are not included."""
    # Create a session 5 days ago (recall_days=3)
    old_date = (date.today() - timedelta(days=5)).isoformat()
    old_dir = memory_dir / old_date
    old_dir.mkdir(parents=True, exist_ok=True)
    (old_dir / "sess-old.md").write_text("Old session", encoding="utf-8")

    block = mgr.recall()
    assert block == ""  # No manual memory either, so empty


def test_recall_recent_sessions_included(mgr: MemoryManager, memory_dir: Path):
    """Sessions within recall_days are included."""
    # Yesterday (within recall_days=3)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    ydir = memory_dir / yesterday
    ydir.mkdir(parents=True, exist_ok=True)
    (ydir / "sess-y.md").write_text("Yesterday session", encoding="utf-8")

    block = mgr.recall()
    assert "Yesterday session" in block


# ── build_memory_block format ──────────────────────────────────


def test_build_memory_block_format():
    manual = "- item 1\n- item 2"
    sessions = [
        {"date": "2026-03-30", "session_id": "sess-0-abc", "content": "Did analysis."},
    ]
    block = MemoryManager.build_memory_block(manual, sessions)
    assert block.startswith("--- MEMORY START ---")
    assert block.endswith("--- MEMORY END ---")
    assert "[Manual Memory]" in block
    assert "[Recent Session Memory]" in block
    assert "2026-03-30 | session_id=sess-0-abc" in block


def test_build_memory_block_empty():
    assert MemoryManager.build_memory_block("", []) == ""


# ── generate_session_summary ──────────────────────────────────


@pytest.mark.asyncio
async def test_generate_session_summary(mgr: MemoryManager, memory_dir: Path):
    from box_agent.schema import Message

    mock_llm = MagicMock()
    mock_response = MagicMock()
    mock_response.content = "# Session: test-sess\nDate: 2026-03-30\nTask: testing\n\n## Summary\nDid stuff."
    mock_llm.generate = AsyncMock(return_value=mock_response)

    messages = [
        Message(role="system", content="You are helpful."),
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi there!"),
    ]

    summary = await mgr.generate_session_summary(
        llm=mock_llm, messages=messages, session_id="test-sess"
    )

    assert "test-sess" in summary
    assert mock_llm.generate.called

    # Check file was saved
    today = date.today().isoformat()
    saved = memory_dir / today / "test-sess.md"
    assert saved.exists()


@pytest.mark.asyncio
async def test_generate_session_summary_llm_failure(mgr: MemoryManager, memory_dir: Path):
    """Fallback summary when LLM call fails."""
    from box_agent.schema import Message

    mock_llm = MagicMock()
    mock_llm.generate = AsyncMock(side_effect=RuntimeError("API down"))

    messages = [
        Message(role="user", content="Hello"),
        Message(role="assistant", content="Hi!"),
    ]

    summary = await mgr.generate_session_summary(
        llm=mock_llm, messages=messages, session_id="fail-sess"
    )

    assert "fail-sess" in summary
    assert "auto-summary failed" in summary

    # File still saved with fallback
    today = date.today().isoformat()
    assert (memory_dir / today / "fail-sess.md").exists()


@pytest.mark.asyncio
async def test_generate_session_summary_empty_messages(mgr: MemoryManager):
    """No messages → empty summary, no file."""
    mock_llm = MagicMock()
    summary = await mgr.generate_session_summary(
        llm=mock_llm, messages=[], session_id="empty-sess"
    )
    assert summary == ""
    assert not mock_llm.generate.called
