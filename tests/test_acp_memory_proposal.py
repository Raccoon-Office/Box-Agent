"""Tests for ACP ``memory_proposal_list`` / ``memory_proposal_apply`` ext methods."""

from __future__ import annotations

from pathlib import Path

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.memory import MemoryManager, write_context_file
from tests.test_memory_promotion import _entry  # reuse helper


class DummyConn:
    async def sessionUpdate(self, payload):
        pass


class DummyLLM:
    async def generate(self, messages, tools):
        raise AssertionError("LLM not expected during ext-method tests")

    async def generate_stream(self, messages, tools, **_):
        raise AssertionError("LLM not expected during ext-method tests")
        yield  # pragma: no cover


def _make_agent(tmp_path: Path, *, hit_threshold: int = 5, cooldown_days: int = 14):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    memory_mgr = MemoryManager(memory_dir=str(memory_dir))
    config = Config(
        llm=LLMConfig(api_key="test-key"),
        agent=AgentConfig(
            max_steps=3,
            workspace_dir=str(tmp_path),
            memory_dir=str(memory_dir),
            memory_promotion_hit_threshold=hit_threshold,
            memory_promotion_cooldown_days=cooldown_days,
        ),
        tools=ToolsConfig(),
    )
    agent = BoxACPAgent(DummyConn(), config, DummyLLM(), [], "system", memory_manager=memory_mgr)
    return agent, memory_mgr


# ── memory_proposal_list ───────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_proposal_list_returns_eligible_candidates(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    write_context_file(mgr.context_file, [
        _entry("- low hits", hits=2),
        _entry("- promote me", hits=7),
        _entry("- already rejected", hits=8, core_status="rejected"),
    ])

    result = await agent.ext_method("memory_proposal_list", {"sessionId": ""})
    contents = {c["content"] for c in result["candidates"]}

    assert contents == {"- promote me"}
    # Wire fields surfaced for the host UI.
    sample = result["candidates"][0]
    assert sample["hits"] == 7
    assert "created" in sample and "last_used" in sample and "last_proposed" in sample


@pytest.mark.asyncio
async def test_memory_proposal_list_respects_cooldown(tmp_path: Path):
    from datetime import datetime, timezone

    agent, mgr = _make_agent(tmp_path, cooldown_days=14)
    recent = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    write_context_file(mgr.context_file, [
        _entry("- in cooldown", hits=10, last_proposed=recent),
        _entry("- never proposed", hits=10),
    ])

    default = await agent.ext_method("memory_proposal_list", {"sessionId": ""})
    assert {c["content"] for c in default["candidates"]} == {"- never proposed"}

    bypass = await agent.ext_method(
        "memory_proposal_list", {"sessionId": "", "includeCooldown": True}
    )
    assert {c["content"] for c in bypass["candidates"]} == {"- in cooldown", "- never proposed"}


@pytest.mark.asyncio
async def test_memory_proposal_list_empty_returns_empty_array(tmp_path: Path):
    agent, _ = _make_agent(tmp_path)
    result = await agent.ext_method("memory_proposal_list", {"sessionId": ""})
    assert result == {"candidates": []}


@pytest.mark.asyncio
async def test_memory_proposal_list_unknown_session(tmp_path: Path):
    agent, _ = _make_agent(tmp_path)
    result = await agent.ext_method(
        "memory_proposal_list", {"sessionId": "no-such-session"}
    )
    assert result == {"error": "session_not_found"}


# ── memory_proposal_apply ──────────────────────────────────────


@pytest.mark.asyncio
async def test_memory_proposal_apply_pins_and_returns_core(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    pin = _entry("- pin me", hits=10)
    reject = _entry("- reject me", hits=10)
    skip = _entry("- skip me", hits=10)
    write_context_file(mgr.context_file, [pin, reject, skip])

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "decisions": {pin.id: "pin", reject.id: "reject", skip.id: "skip"},
        },
    )

    assert result["pinned"] == 1
    assert result["rejected"] == 1
    assert result["skipped"] == 1
    # core text in response matches the persisted file (host can refresh in place).
    assert result["core"] == mgr.read_core()
    assert "- pin me" in result["core"]
    # CONTEXT.md no longer contains the pinned entry; rejected entry now flagged.
    remaining = {e.content: e for e in mgr._read_context_entries()}
    assert "- pin me" not in remaining
    assert remaining["- reject me"].core_status == "rejected"
    assert remaining["- skip me"].core_status == "none"


@pytest.mark.asyncio
async def test_memory_proposal_apply_ignores_invalid_decisions(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    keep = _entry("- keep me", hits=10)
    write_context_file(mgr.context_file, [keep])

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "decisions": {keep.id: "bogus", "ghost-id": "pin"},
        },
    )

    assert result == {
        "pinned": 0,
        "rejected": 0,
        "skipped": 0,
        "core": mgr.read_core(),
    }


@pytest.mark.asyncio
async def test_memory_proposal_apply_rejects_malformed_payload(tmp_path: Path):
    agent, _ = _make_agent(tmp_path)
    result = await agent.ext_method(
        "memory_proposal_apply", {"sessionId": "", "decisions": "not-a-dict"}
    )
    assert result == {"error": "invalid_decisions"}
