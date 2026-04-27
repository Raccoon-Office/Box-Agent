"""Tests for box_agent.memory — MemoryManager (core + context)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.memory import MemoryManager


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memory"
    d.mkdir()
    return d


@pytest.fixture
def mgr(memory_dir: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(memory_dir))


# ── Core memory (MEMORY.md) ───────────────────────────────────


def test_read_write_core(mgr: MemoryManager):
    assert mgr.read_core() == ""
    mgr.write_core("- user prefers Chinese")
    assert "user prefers Chinese" in mgr.read_core()

    mgr.write_core("- new preference")
    content = mgr.read_core()
    assert "new preference" in content
    assert "user prefers Chinese" not in content


def test_append_core(mgr: MemoryManager):
    mgr.append_core("- item 1")
    mgr.append_core("- item 2")
    content = mgr.read_core()
    assert "item 1" in content
    assert "item 2" in content


def test_read_core_missing_file(mgr: MemoryManager):
    assert mgr.read_core() == ""


# ── Legacy aliases ────────────────────────────────────────────


def test_legacy_aliases(mgr: MemoryManager):
    mgr.write_manual_memory("- legacy content")
    assert "legacy content" in mgr.read_manual_memory()
    assert "legacy content" in mgr.read_all()


# ── Context memory (CONTEXT.md) ──────────────────────────────


def test_read_write_context(mgr: MemoryManager):
    assert mgr.read_context() == ""
    mgr.write_context("- Q2 goal: dashboard")
    assert "Q2 goal" in mgr.read_context()


def test_append_context(mgr: MemoryManager):
    mgr.append_context("- project A")
    mgr.append_context("- project B")
    content = mgr.read_context()
    assert "project A" in content
    assert "project B" in content


def test_core_and_context_independent(mgr: MemoryManager):
    """Writing to one file doesn't affect the other."""
    mgr.write_core("- user: Alice")
    mgr.write_context("- project: Dashboard")

    assert "Alice" in mgr.read_core()
    assert "Dashboard" not in mgr.read_core()
    assert "Dashboard" in mgr.read_context()
    assert "Alice" not in mgr.read_context()


def test_append_context_dedup_against_core(mgr: MemoryManager):
    """Lines already in Core are filtered out when appending to Context."""
    mgr.write_core("- user: Alice\n- prefers Chinese")
    mgr.append_context("- user: Alice\n- Q2 goal: dashboard\n- prefers Chinese")
    context = mgr.read_context()
    assert "Q2 goal" in context
    assert "Alice" not in context
    assert "Chinese" not in context


def test_append_context_dedup_against_existing_context(mgr: MemoryManager):
    """Lines already in Context are not appended again."""
    mgr.write_context("- Q2 goal: dashboard\n- weekly report format: progress/issues/plan")
    mgr.append_context("- q2 goal: dashboard\n- team lead: Bob\n- weekly report format: progress/issues/plan")

    context = mgr.read_context()
    assert context.count("Q2 goal") == 1
    assert context.count("weekly report format") == 1
    assert "team lead: Bob" in context


def test_append_context_dedup_within_single_append(mgr: MemoryManager):
    """Duplicate lines within one append call are only saved once."""
    mgr.append_context("- project A\n- Project A\n- project B")
    context = mgr.read_context()
    assert context.lower().count("project a") == 1
    assert "project B" in context


def test_append_context_all_filtered(mgr: MemoryManager):
    """If all lines are Core duplicates, nothing is written to Context."""
    mgr.write_core("- user: Alice")
    mgr.append_context("- user: Alice")
    assert mgr.read_context() == ""


def test_apply_context_operations_add_replace_drop_noop(mgr: MemoryManager):
    mgr.write_core("- user: Alice")
    mgr.write_context("- Q2 goal: dashboard\n- old transient detail")

    changed = mgr.apply_context_operations([
        {
            "action": "replace",
            "old": "- Q2 goal: dashboard",
            "new": "- Q2 goal: launch dashboard by 6/30",
        },
        {"action": "add", "content": "- user: Alice"},
        {"action": "add", "content": "- weekly report format: progress/issues/plan"},
        {"action": "drop", "content": "- old transient detail"},
        {"action": "noop", "content": "- ignored"},
    ])

    assert changed is True
    context = mgr.read_context()
    assert "launch dashboard by 6/30" in context
    assert "weekly report format" in context
    assert "old transient detail" not in context
    assert "user: Alice" not in context


async def test_update_context_with_llm_applies_model_plan(mgr: MemoryManager):
    mgr.write_context("- Q2 goal: dashboard")
    llm = MagicMock()
    response = MagicMock()
    response.content = (
        '{"operations": ['
        '{"action": "replace", "old": "- Q2 goal: dashboard", "new": "- Q2 goal: launch dashboard by 6/30", "reason": "more specific"},'
        '{"action": "add", "content": "- weekly report format: progress/issues/plan", "reason": "new template"}'
        ']}'
    )
    llm.generate = AsyncMock(return_value=response)

    status = await mgr.update_context_with_llm("- Q2 goal is to launch dashboard by 6/30", llm)

    assert status == "applied"
    context = mgr.read_context()
    assert "launch dashboard by 6/30" in context
    assert "weekly report format" in context
    assert llm.generate.await_count == 1


async def test_update_context_with_llm_falls_back_to_append_on_bad_json(mgr: MemoryManager):
    llm = MagicMock()
    response = MagicMock()
    response.content = "not json"
    llm.generate = AsyncMock(return_value=response)

    status = await mgr.update_context_with_llm("- project deadline: June", llm)

    assert status == "fallback_appended"
    assert "project deadline: June" in mgr.read_context()


# ── Search ────────────────────────────────────────────────────


def test_search_match(mgr: MemoryManager):
    mgr.write_context("- weekly report format: progress/issues/plan\n- Q2 goal: data dashboard\n- team lead: Bob")
    results = mgr.search("weekly")
    assert len(results) == 1
    assert "weekly report" in results[0]


def test_search_case_insensitive(mgr: MemoryManager):
    mgr.write_context("- Project Alpha is important")
    results = mgr.search("project alpha")
    assert len(results) == 1


def test_search_multiple_matches(mgr: MemoryManager):
    mgr.write_context("- report format A\n- report template B\n- unrelated item")
    results = mgr.search("report")
    assert len(results) == 2


def test_search_no_match(mgr: MemoryManager):
    mgr.write_context("- some content")
    results = mgr.search("nonexistent")
    assert results == []


def test_search_empty_context(mgr: MemoryManager):
    results = mgr.search("anything")
    assert results == []


def test_search_empty_query(mgr: MemoryManager):
    mgr.write_context("- some content")
    results = mgr.search("")
    assert results == []


# ── Recall ─────────────────────────────────────────────────────


def test_recall_empty(mgr: MemoryManager):
    assert mgr.recall() == ""


def test_recall_only_core(mgr: MemoryManager):
    mgr.write_core("- always use English")
    mgr.write_context("- project context that should NOT be recalled")
    block = mgr.recall()
    assert "--- MEMORY START ---" in block
    assert "always use English" in block
    assert "project context" not in block


def test_recall_does_not_include_context(mgr: MemoryManager):
    """Context memory must not appear in recall — only via search."""
    mgr.write_context("- secret context")
    block = mgr.recall()
    assert block == ""  # No core memory → empty


# ── build_memory_block ────────────────────────────────────────


def test_build_memory_block_format():
    block = MemoryManager.build_memory_block("- core item")
    assert block.startswith("--- MEMORY START ---")
    assert block.endswith("--- MEMORY END ---")
    assert "[Core Memory]" in block


def test_build_memory_block_empty():
    assert MemoryManager.build_memory_block("") == ""


def test_build_memory_block_core_only():
    block = MemoryManager.build_memory_block("- core item")
    assert "[Core Memory]" in block
    assert "core item" in block
