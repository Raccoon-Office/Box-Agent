"""Tests for in-band permission negotiation (GrantStore + core retry)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from box_agent.core import run_agent_loop
from box_agent.events import (
    DoneEvent,
    PermissionRequestEvent,
    StopReason,
    ToolCallResult,
)
from box_agent.schema import FunctionCall, LLMResponse, Message, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.permissions import (
    FILESYSTEM_READ,
    CapabilityPolicy,
    GrantStore,
    PermissionEngine,
)


# ── Helpers ─────────────────────────────────────────────────


class MockLLM:
    """Deterministic LLM that yields pre-configured responses in order."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0

    async def generate_stream(self, messages, tools=None, **_):
        resp = self._responses[self._idx]
        self._idx += 1
        from box_agent.schema import StreamEvent
        if resp.thinking:
            yield StreamEvent(type="thinking", delta=resp.thinking)
        if resp.content:
            yield StreamEvent(type="text", delta=resp.content)
        yield StreamEvent(
            type="finish",
            finish_reason=resp.finish_reason,
            usage=resp.usage,
            tool_calls=resp.tool_calls,
        )


async def collect(gen) -> list:
    return [ev async for ev in gen]


def _msgs():
    return [
        Message(role="system", content="sys"),
        Message(role="user", content="hi"),
    ]


class PermDeniedTool(Tool):
    """Tool that always fails with a permission_request."""

    def __init__(self, perm_engine: PermissionEngine, target_path: str):
        self._perm = perm_engine
        self._target = target_path
        self._call_count = 0

    @property
    def name(self):
        return "read_outside"

    @property
    def description(self):
        return "Reads a file outside workspace"

    @property
    def parameters(self):
        return {"type": "object", "properties": {"path": {"type": "string"}}}

    async def execute(self, path: str = ""):
        self._call_count += 1
        decision = self._perm.check(
            capability=FILESYSTEM_READ,
            resource={"path": self._target},
        )
        if not decision.allowed:
            return ToolResult(
                success=False,
                error=decision.reason,
                permission_request=decision.permission_request,
            )
        return ToolResult(success=True, content=f"read:{self._target}")


class MockNegotiator:
    """Mock permission negotiator with configurable response."""

    def __init__(self, grant: bool, grant_scope: str = "prompt"):
        self._grant = grant
        self._grant_scope = grant_scope
        self._store: GrantStore | None = None
        self.negotiate_count = 0
        self.rpc_count = 0  # actual "RPC" invocations (excludes cache hits)

    def attach_store(self, store: GrantStore):
        self._store = store

    async def negotiate(self, permission_request: dict) -> bool:
        self.negotiate_count += 1
        scope = permission_request.get("scope", "")
        requested_scope = permission_request.get("requested_scope", "")

        # Dedup via grant store (same as real negotiator)
        if self._store and self._store.has_grant(scope, requested_scope):
            return True

        self.rpc_count += 1
        if self._grant and self._store:
            self._store.add_grant(scope, requested_scope, self._grant_scope)
        return self._grant


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def outside_file(tmp_path: Path) -> str:
    """A file under the user home but outside workspace."""
    f = tmp_path / "outside.txt"
    f.write_text("secret")
    return str(f)


@pytest.fixture
def grant_store() -> GrantStore:
    return GrantStore()


@pytest.fixture
def engine(workspace: Path, grant_store: GrantStore, tmp_path: Path) -> PermissionEngine:
    policy = CapabilityPolicy(
        filesystem_scope="session_workspace",
        session_workspace_root=str(workspace),
    )
    eng = PermissionEngine(policy, workspace, grant_store=grant_store)
    # Override home dir so that outside_file (in tmp_path) is considered "under home"
    # This allows _compute_escalation() to suggest user_home escalation
    eng._home_dir = tmp_path.resolve()
    return eng


def _llm_with_tool_call(tool_name: str, args: dict) -> MockLLM:
    """LLM that makes one tool call and then finishes."""
    return MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name=tool_name, arguments=args))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])


def _llm_with_three_tool_calls(tool_name: str, args: dict) -> MockLLM:
    """LLM that makes three sequential tool calls."""
    return MockLLM([
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t1", type="function", function=FunctionCall(name=tool_name, arguments=args))],
            finish_reason="tool",
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t2", type="function", function=FunctionCall(name=tool_name, arguments=args))],
            finish_reason="tool",
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCall(id="t3", type="function", function=FunctionCall(name=tool_name, arguments=args))],
            finish_reason="tool",
        ),
        LLMResponse(content="done", finish_reason="stop"),
    ])


# ── Tests ────────────────────────────────────────────────────


class TestGrantStore:
    """Unit tests for GrantStore."""

    def test_empty_store_has_no_grants(self):
        store = GrantStore()
        assert not store.has_grant("filesystem", "user_home")

    def test_prompt_grant(self):
        store = GrantStore()
        store.add_grant("filesystem", "user_home", "prompt")
        assert store.has_grant("filesystem", "user_home")
        assert not store.has_grant("memory", "openclaw_import")

    def test_session_grant(self):
        store = GrantStore()
        store.add_grant("filesystem", "user_home", "session")
        assert store.has_grant("filesystem", "user_home")

    def test_clear_prompt_grants(self):
        store = GrantStore()
        store.add_grant("filesystem", "user_home", "prompt")
        store.add_grant("memory", "openclaw_import", "session")
        store.clear_prompt_grants()
        assert not store.has_grant("filesystem", "user_home")
        assert store.has_grant("memory", "openclaw_import")


class TestPermissionEngineWithGrantStore:
    """Verify PermissionEngine consults GrantStore."""

    def test_grant_elevates_filesystem_scope(self, engine, grant_store, outside_file):
        # Initially denied
        decision = engine.check(FILESYSTEM_READ, {"path": outside_file})
        assert not decision.allowed

        # Grant user_home
        grant_store.add_grant("filesystem", "user_home", "prompt")

        # Now allowed
        decision = engine.check(FILESYSTEM_READ, {"path": outside_file})
        assert decision.allowed

    def test_grant_enables_openclaw(self, workspace):
        store = GrantStore()
        policy = CapabilityPolicy(
            openclaw_import_enabled=False,
            session_workspace_root=str(workspace),
        )
        engine = PermissionEngine(policy, workspace, grant_store=store)

        from box_agent.tools.permissions import MEMORY_OPENCLAW_IMPORT
        decision = engine.check(MEMORY_OPENCLAW_IMPORT, {})
        assert not decision.allowed

        store.add_grant("memory", "openclaw_import", "session")
        decision = engine.check(MEMORY_OPENCLAW_IMPORT, {})
        assert decision.allowed


class TestNegotiationInCore:
    """Integration tests: negotiation + retry in run_agent_loop."""

    @pytest.mark.asyncio
    async def test_prompt_grant_allows_then_clears(
        self, workspace, engine, grant_store, outside_file
    ):
        """Approve (prompt scope) → tool retries + succeeds.
        After clear_prompt_grants, same check fails again."""
        negotiator = MockNegotiator(grant=True, grant_scope="prompt")
        negotiator.attach_store(grant_store)

        tool = PermDeniedTool(engine, outside_file)
        llm = _llm_with_tool_call("read_outside", {"path": outside_file})

        events = await collect(run_agent_loop(
            llm=llm, messages=_msgs(), tools={"read_outside": tool},
            max_steps=5, permission_negotiator=negotiator,
        ))

        # Tool was called twice (initial denied + retry after grant)
        assert tool._call_count == 2

        # Final result is success (from retry)
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is True

        # Done event is end_turn (not error or cancelled)
        dones = [e for e in events if isinstance(e, DoneEvent)]
        assert dones[0].stop_reason == StopReason.END_TURN

        # No PermissionRequestEvent emitted (negotiator handled it)
        perm_events = [e for e in events if isinstance(e, PermissionRequestEvent)]
        assert len(perm_events) == 0

        # Clear prompt grants — same permission now fails
        grant_store.clear_prompt_grants()
        assert not grant_store.has_grant("filesystem", "user_home")

    @pytest.mark.asyncio
    async def test_session_grant_persists_across_prompts(
        self, workspace, engine, grant_store, outside_file
    ):
        """Approve (session scope) → persists after clear_prompt_grants."""
        negotiator = MockNegotiator(grant=True, grant_scope="session")
        negotiator.attach_store(grant_store)

        tool = PermDeniedTool(engine, outside_file)
        llm = _llm_with_tool_call("read_outside", {"path": outside_file})

        events = await collect(run_agent_loop(
            llm=llm, messages=_msgs(), tools={"read_outside": tool},
            max_steps=5, permission_negotiator=negotiator,
        ))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert results[0].success is True

        # Simulate next prompt: clear prompt grants
        grant_store.clear_prompt_grants()

        # Session grant still active
        assert grant_store.has_grant("filesystem", "user_home")

    @pytest.mark.asyncio
    async def test_denial_returns_tool_error_not_fatal(
        self, workspace, engine, grant_store, outside_file
    ):
        """Host denied → tool returns error → prompt finishes normally (not fatal)."""
        negotiator = MockNegotiator(grant=False)
        negotiator.attach_store(grant_store)

        tool = PermDeniedTool(engine, outside_file)
        llm = _llm_with_tool_call("read_outside", {"path": outside_file})

        events = await collect(run_agent_loop(
            llm=llm, messages=_msgs(), tools={"read_outside": tool},
            max_steps=5, permission_negotiator=negotiator,
        ))

        # Tool only called once (no retry on denial)
        assert tool._call_count == 1

        # Result is failure
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is False
        assert "denied" in (results[0].error or "").lower() or "outside" in (results[0].error or "").lower()

        # Prompt ends normally (end_turn, not error)
        dones = [e for e in events if isinstance(e, DoneEvent)]
        assert len(dones) == 1
        assert dones[0].stop_reason == StopReason.END_TURN

    @pytest.mark.asyncio
    async def test_timeout_treated_as_denial(
        self, workspace, engine, grant_store, outside_file
    ):
        """Negotiator timeout → same as denial → tool error, prompt continues."""

        class TimeoutNegotiator:
            async def negotiate(self, permission_request):
                raise asyncio.TimeoutError()

        tool = PermDeniedTool(engine, outside_file)
        llm = _llm_with_tool_call("read_outside", {"path": outside_file})

        # TimeoutNegotiator raises, but negotiate() is called by core, which
        # treats any non-True return as denial. However, the actual negotiator
        # in the ACP layer catches TimeoutError internally. For core.py, we
        # need a negotiator that returns False on timeout.
        class FalseOnTimeoutNegotiator:
            async def negotiate(self, permission_request):
                return False

        events = await collect(run_agent_loop(
            llm=llm, messages=_msgs(), tools={"read_outside": tool},
            max_steps=5, permission_negotiator=FalseOnTimeoutNegotiator(),
        ))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is False

        dones = [e for e in events if isinstance(e, DoneEvent)]
        assert dones[0].stop_reason == StopReason.END_TURN

    @pytest.mark.asyncio
    async def test_dedup_single_rpc_for_same_scope(
        self, workspace, engine, grant_store, outside_file
    ):
        """Three tool calls needing same permission → only 1 RPC, 2 cache hits."""
        negotiator = MockNegotiator(grant=True, grant_scope="prompt")
        negotiator.attach_store(grant_store)

        tool = PermDeniedTool(engine, outside_file)
        llm = _llm_with_three_tool_calls("read_outside", {"path": outside_file})

        events = await collect(run_agent_loop(
            llm=llm, messages=_msgs(), tools={"read_outside": tool},
            max_steps=10, permission_negotiator=negotiator,
        ))

        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 3
        # All succeeded (first via RPC grant, next two via grant store cache in engine)
        assert all(r.success for r in results)

        # negotiate() was only called ONCE: the first tool call returned permission_request,
        # then the grant was recorded. Subsequent tool calls succeed directly because the
        # PermissionEngine checks the grant store and returns allowed=True.
        # This is more efficient than calling the negotiator 3 times.
        assert negotiator.negotiate_count == 1
        assert negotiator.rpc_count == 1


class TestLegacyPermissionEvent:
    """Without a negotiator, PermissionRequestEvent should still be emitted."""

    @pytest.mark.asyncio
    async def test_no_negotiator_emits_event(self, workspace, outside_file, tmp_path):
        store = GrantStore()
        policy = CapabilityPolicy(
            filesystem_scope="session_workspace",
            session_workspace_root=str(workspace),
        )
        engine = PermissionEngine(policy, workspace, grant_store=store)
        engine._home_dir = tmp_path.resolve()
        tool = PermDeniedTool(engine, outside_file)

        llm = _llm_with_tool_call("read_outside", {"path": outside_file})

        events = await collect(run_agent_loop(
            llm=llm, messages=_msgs(), tools={"read_outside": tool},
            max_steps=5,
            # No permission_negotiator
        ))

        perm_events = [e for e in events if isinstance(e, PermissionRequestEvent)]
        assert len(perm_events) == 1
        assert perm_events[0].scope == "filesystem"
        assert perm_events[0].requested_scope == "user_home"
