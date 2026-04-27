"""Tests for memory tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from box_agent.memory import MemoryManager
from box_agent.tools.memory_tool import MemoryWriteTool


@pytest.fixture
def mgr(tmp_path: Path) -> MemoryManager:
    return MemoryManager(memory_dir=str(tmp_path / "memory"))


async def test_memory_write_context_uses_llm_merge_when_available(mgr: MemoryManager):
    mgr.write_context("- Q2 goal: dashboard")
    llm = MagicMock()
    response = MagicMock()
    response.content = (
        '{"operations": ['
        '{"action": "replace", "old": "- Q2 goal: dashboard", "new": "- Q2 goal: launch dashboard by 6/30", "reason": "specific deadline"}'
        ']}'
    )
    llm.generate = AsyncMock(return_value=response)
    tool = MemoryWriteTool(mgr, llm=llm)

    result = await tool.execute("- Q2 goal is launching dashboard by 6/30", category="context")

    assert result.success is True
    assert "context, applied" in result.content
    assert "launch dashboard by 6/30" in mgr.read_context()
    assert llm.generate.await_count == 1


async def test_memory_write_context_without_llm_uses_append_dedup(mgr: MemoryManager):
    mgr.write_context("- Q2 goal: dashboard")
    tool = MemoryWriteTool(mgr)

    result = await tool.execute("- q2 goal: dashboard\n- team lead: Bob", category="context")

    assert result.success is True
    assert "context, append_dedup" in result.content
    context = mgr.read_context()
    assert context.lower().count("q2 goal: dashboard") == 1
    assert "team lead: Bob" in context
