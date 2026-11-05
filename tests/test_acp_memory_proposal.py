"""Tests for ACP ``memory_proposal_list`` / ``memory_proposal_apply`` ext methods."""

from __future__ import annotations

from tests.acp_helpers import acp_params

import asyncio
import threading
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest

from box_agent.acp import BoxACPAgent
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.llm import SessionBoundLLM
from box_agent.memory import MemoryManager, write_context_file
from tests.test_memory_promotion import _entry  # reuse helper


class DummyConn:
    async def session_update(self, session_id, update, **meta):
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


async def _run_while_memory_transaction_is_held(mgr, operation):
    transaction_acquired = threading.Event()
    release_transaction = threading.Event()

    def hold_transaction() -> None:
        with mgr.context_transaction():
            transaction_acquired.set()
            assert release_transaction.wait(timeout=2.0)

    holder = asyncio.create_task(asyncio.to_thread(hold_transaction))
    assert await asyncio.to_thread(transaction_acquired.wait, 2.0)

    operation_task = asyncio.create_task(operation)
    heartbeat_at = 0.0
    started_at = monotonic()

    async def heartbeat() -> None:
        nonlocal heartbeat_at
        await asyncio.sleep(0.01)
        heartbeat_at = monotonic()

    heartbeat_task = asyncio.create_task(heartbeat())
    release_timer = threading.Timer(0.3, release_transaction.set)
    release_timer.start()
    try:
        await heartbeat_task
        heartbeat_delay = heartbeat_at - started_at
        assert not operation_task.done()
    finally:
        release_transaction.set()
        release_timer.cancel()
        await asyncio.wait_for(holder, timeout=2.0)

    result = await asyncio.wait_for(operation_task, timeout=2.0)
    assert heartbeat_delay < 0.15
    return result


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
async def test_memory_proposal_list_waits_off_event_loop(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    write_context_file(mgr.context_file, [_entry("- promote me", hits=7)])

    result = await _run_while_memory_transaction_is_held(
        mgr,
        agent.ext_method("memory_proposal_list", {"sessionId": ""}),
    )

    assert [candidate["content"] for candidate in result["candidates"]] == [
        "- promote me"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("core_change", ["unchanged", "rewritten", "deleted"])
async def test_session_recall_only_reuses_unchanged_core_while_memory_is_busy(tmp_path: Path, core_change):
    agent, mgr = _make_agent(tmp_path)
    mgr.write_context("- project memory", topic="project")
    mgr.write_core("- cached preference")
    assert "cached preference" in mgr.recall()
    if core_change == "rewritten":
        mgr.write_core("- updated preference")
    elif core_change == "deleted":
        mgr.memory_file.unlink()
    transaction_acquired = threading.Event()
    release_transaction = threading.Event()

    def hold_transaction() -> None:
        with mgr.context_transaction():
            transaction_acquired.set()
            assert release_transaction.wait(timeout=10.0)

    holder = asyncio.create_task(asyncio.to_thread(hold_transaction))
    try:
        assert await asyncio.to_thread(transaction_acquired.wait, 2.0)
        session = await asyncio.wait_for(
            agent.new_session(**acp_params(SimpleNamespace(
                cwd=str(tmp_path), field_meta={"session_mode": "general"},
            ))),
            timeout=3.0,
        )
        assert not release_transaction.is_set()
        assert not holder.done()
        prompt = agent._sessions[session.sessionId].agent.system_prompt
        assert "updated preference" not in prompt
        assert ("cached preference" in prompt) == (core_change == "unchanged")
        assert "`project`" in prompt
    finally:
        release_transaction.set()
        await asyncio.wait_for(holder, timeout=2.0)


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


# ── memory_proposal_list — includePlan ─────────────────────────


@pytest.mark.asyncio
async def test_memory_proposal_list_include_plan_attaches_plan(
    tmp_path: Path, monkeypatch
):
    agent, mgr = _make_agent(tmp_path)
    write_context_file(mgr.context_file, [_entry("- promote me", hits=7)])
    captured_llm = None

    from box_agent.events import MemoryPromotionPlan

    async def fake_plan_promotion(entries, llm):
        nonlocal captured_llm
        captured_llm = llm
        ids = tuple(e.id for e in entries)
        return MemoryPromotionPlan(
            current_core="",
            new_core="- promote me",
            consumed_entry_ids=ids,
            rationale="hot enough",
        )

    monkeypatch.setattr(mgr, "plan_promotion", fake_plan_promotion)

    result = await agent.ext_method(
        "memory_proposal_list", {"sessionId": "", "includePlan": True}
    )

    assert len(result["candidates"]) == 1
    plan = result["plan"]
    assert plan["newCore"] == "- promote me"
    assert plan["rationale"] == "hot enough"
    assert isinstance(plan["consumedEntryIds"], list)
    assert plan["consumedEntryIds"] == [result["candidates"][0]["id"]]
    assert isinstance(captured_llm, SessionBoundLLM)
    assert captured_llm._session_id.startswith("local-agent-memory-review-")
    assert captured_llm._turn_id == captured_llm._session_id
    assert captured_llm._title == "本地 Agent 记忆整理"


@pytest.mark.asyncio
async def test_memory_proposal_list_include_plan_default_off(
    tmp_path: Path, monkeypatch
):
    agent, mgr = _make_agent(tmp_path)
    write_context_file(mgr.context_file, [_entry("- promote me", hits=7)])

    called = False

    async def fake_plan_promotion(entries, llm):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(mgr, "plan_promotion", fake_plan_promotion)

    result = await agent.ext_method("memory_proposal_list", {"sessionId": ""})

    assert "plan" not in result
    assert called is False


@pytest.mark.asyncio
async def test_memory_proposal_list_include_plan_planner_failure_yields_no_plan(
    tmp_path: Path, monkeypatch
):
    agent, mgr = _make_agent(tmp_path)
    write_context_file(mgr.context_file, [_entry("- promote me", hits=7)])

    async def fake_plan_promotion(entries, llm):
        raise RuntimeError("LLM exploded")

    monkeypatch.setattr(mgr, "plan_promotion", fake_plan_promotion)

    result = await agent.ext_method(
        "memory_proposal_list", {"sessionId": "", "includePlan": True}
    )

    assert len(result["candidates"]) == 1
    assert "plan" not in result


@pytest.mark.asyncio
async def test_memory_proposal_list_include_plan_no_candidates_skips_planner(
    tmp_path: Path, monkeypatch
):
    agent, mgr = _make_agent(tmp_path)
    called = False

    async def fake_plan_promotion(entries, llm):
        nonlocal called
        called = True
        return None

    monkeypatch.setattr(mgr, "plan_promotion", fake_plan_promotion)

    result = await agent.ext_method(
        "memory_proposal_list", {"sessionId": "", "includePlan": True}
    )

    assert result == {"candidates": []}
    assert called is False


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
async def test_memory_proposal_apply_waits_off_event_loop(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    pin = _entry("- pin me", hits=10)
    write_context_file(mgr.context_file, [pin])

    result = await _run_while_memory_transaction_is_held(
        mgr,
        agent.ext_method(
            "memory_proposal_apply",
            {
                "sessionId": "",
                "decisions": {pin.id: "pin"},
            },
        ),
    )

    assert result["pinned"] == 1
    assert "- pin me" in result["core"]


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


# ── memory_proposal_apply — plan mode (delayed decision) ───────


@pytest.mark.asyncio
async def test_memory_proposal_apply_plan_apply_overwrites_core(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    a = _entry("- A", hits=10)
    b = _entry("- B", hits=10)
    write_context_file(mgr.context_file, [a, b])
    mgr.write_core("- old core")

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "plan": {
                "currentCore": "- old core",
                "newCore": "- new core\n- A folded\n- B folded",
                "consumedEntryIds": [a.id, b.id],
                "rationale": "fold both",
            },
            "decision": "apply",
        },
    )

    assert result["applied"] == 1
    assert result["consumed"] == 2
    assert result["core"] == "- new core\n- A folded\n- B folded"
    assert mgr.read_core() == "- new core\n- A folded\n- B folded"
    assert mgr._read_context_entries() == []


@pytest.mark.asyncio
async def test_memory_proposal_apply_plan_reject_marks_candidates(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    a = _entry("- A", hits=10)
    b = _entry("- B", hits=10)
    write_context_file(mgr.context_file, [a, b])
    mgr.write_core("- untouched core")

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "plan": {
                "currentCore": "- untouched core",
                "newCore": "- ignored",
                "consumedEntryIds": [a.id],
                "rationale": "user says no",
            },
            "decision": "reject",
        },
    )

    assert result["rejected"] == 1
    # core unchanged
    assert result["core"] == "- untouched core"
    assert mgr.read_core() == "- untouched core"
    by_id = {e.id: e for e in mgr._read_context_entries()}
    assert by_id[a.id].core_status == "rejected"
    assert by_id[b.id].core_status == "none"


@pytest.mark.asyncio
async def test_memory_proposal_apply_plan_skip_is_noop(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    a = _entry("- A", hits=10)
    write_context_file(mgr.context_file, [a])
    mgr.write_core("- core stays")
    before = {e.id: e.core_status for e in mgr._read_context_entries()}

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "plan": {
                "currentCore": "- core stays",
                "newCore": "- whatever",
                "consumedEntryIds": [a.id],
                "rationale": "later",
            },
            "decision": "skip",
        },
    )

    assert result == {"skipped": 1, "core": "- core stays"}
    after = {e.id: e.core_status for e in mgr._read_context_entries()}
    assert before == after


@pytest.mark.asyncio
async def test_memory_proposal_apply_plan_invalid_decision(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    a = _entry("- A", hits=10)
    write_context_file(mgr.context_file, [a])

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "plan": {
                "currentCore": "",
                "newCore": "- whatever",
                "consumedEntryIds": [a.id],
                "rationale": "",
            },
            "decision": "bogus",
        },
    )

    assert result == {"error": "invalid_decision"}


@pytest.mark.asyncio
async def test_memory_proposal_apply_plan_missing_consumed_ids(tmp_path: Path):
    agent, _ = _make_agent(tmp_path)
    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "plan": {
                "currentCore": "- x",
                "newCore": "- y",
                "consumedEntryIds": [],
                "rationale": "",
            },
            "decision": "apply",
        },
    )
    assert result == {"error": "invalid_plan"}


@pytest.mark.asyncio
async def test_memory_proposal_apply_plan_apply_with_empty_new_core(tmp_path: Path):
    agent, mgr = _make_agent(tmp_path)
    a = _entry("- A", hits=10)
    write_context_file(mgr.context_file, [a])

    result = await agent.ext_method(
        "memory_proposal_apply",
        {
            "sessionId": "",
            "plan": {
                "currentCore": "- x",
                "newCore": "   ",
                "consumedEntryIds": [a.id],
                "rationale": "",
            },
            "decision": "apply",
        },
    )
    assert result == {"error": "invalid_plan"}
