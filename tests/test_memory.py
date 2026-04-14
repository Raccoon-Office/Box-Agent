"""Tests for box_agent.memory — MemoryManager."""

from __future__ import annotations

from pathlib import Path

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
    return MemoryManager(memory_dir=str(memory_dir))


# ── Read/write (whole file) ───────────────────────────────────


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


# ── build_memory_block format ──────────────────────────────────


def test_build_memory_block_format():
    manual = "- item 1\n- item 2"
    block = MemoryManager.build_memory_block(manual)
    assert block.startswith("--- MEMORY START ---")
    assert block.endswith("--- MEMORY END ---")
    assert "[Manual Memory]" in block


def test_build_memory_block_empty():
    assert MemoryManager.build_memory_block("") == ""


def test_build_memory_block_with_all_sections():
    block = MemoryManager.build_memory_block("- manual", openclaw="- claw", auto="- auto")
    assert "[Manual Memory]" in block
    assert "[Auto Memory]" in block
    assert "[OpenClaw Memory]" in block
