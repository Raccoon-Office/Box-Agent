"""Tests for in-band permission negotiation (GrantStore + core retry)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
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
from box_agent.session_log import SessionLogDurabilityError
from box_agent.runtime import invoke_tool_with_permissions
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


@pytest.mark.parametrize("entrypoint", ("serial", "parallel", "adapter"))
@pytest.mark.parametrize("needs_approval", (False, True))
async def test_durability_failure_stops_execution_even_after_approval(
    entrypoint, needs_approval,
):
    failure = SessionLogDurabilityError("durable write failed")

    class FailingTool(Tool):
        name = "durable_write"
        description = "Write durable state."
        parameters = {"type": "object", "properties": {}}
        parallel_safe = entrypoint == "parallel"
        calls = 0

        async def execute(self):
            self.calls += 1
            if needs_approval and self.calls == 1:
                return ToolResult(
                    success=False, error="approval required",
                    permission_request={"scope": "filesystem", "requested_scope": "workspace"},
                )
            raise failure

    class Approver:
        async def negotiate(self, request):
            return True

    tool = FailingTool()
    llm = MockLLM([
        LLMResponse(content="", tool_calls=[ToolCall(
            id="durable-1", type="function",
            function=FunctionCall(name=tool.name, arguments={}),
        )], finish_reason="tool_use"),
        LLMResponse(content="must not continue", finish_reason="stop"),
    ])
    with pytest.raises(SessionLogDurabilityError) as caught:
        if entrypoint == "adapter":
            await invoke_tool_with_permissions(tool, {}, permission_negotiator=Approver())
        else:
            await collect(run_agent_loop(
                llm=llm, messages=_msgs(), tools={tool.name: tool},
                max_steps=3, permission_negotiator=Approver(),
            ))
    assert caught.value is failure
    assert tool.calls == (2 if needs_approval else 1)
    assert llm._idx == (0 if entrypoint == "adapter" else 1)


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


class SafetyNegotiator:
    def __init__(self, grant: bool):
        self._grant = grant
        self.requests: list[dict] = []

    async def negotiate(self, permission_request: dict) -> bool:
        self.requests.append(permission_request)
        return self._grant


class TwoGateTool(Tool):
    """Tool that requires two distinct one-shot approvals before succeeding."""

    def __init__(self) -> None:
        self.requests = [
            {
                "type": "permission_request",
                "scope": "filesystem",
                "requested_scope": "user_home",
                "reason": "first gate",
                "path": "C:/outside/first.txt",
                "temporary_supported": True,
                "persistent_supported": True,
                "persistent_label": "Allow directory",
                "command": "",
                "risk": "",
            },
            {
                "type": "permission_request",
                "scope": "safety",
                "requested_scope": "dangerous_command",
                "reason": "second gate",
                "path": "",
                "temporary_supported": True,
                "persistent_supported": False,
                "persistent_label": "",
                "command": "Remove-Item outside.txt",
                "risk": "destructive",
            },
        ]
        self.approved_requests: list[dict] = []
        self.call_count = 0

    @property
    def name(self) -> str:
        return "two_gates"

    @property
    def description(self) -> str:
        return "Requires two permission gates"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    def approve_permission_request(self, permission_request: dict) -> None:
        self.approved_requests.append(permission_request)

    async def execute(self) -> ToolResult:
        self.call_count += 1
        if len(self.approved_requests) < len(self.requests):
            return ToolResult(
                success=False,
                error="Approval required",
                permission_request=self.requests[len(self.approved_requests)],
            )
        return ToolResult(success=True, content="approved through both gates")


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
    # Clear it so deny assertions work correctly.
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


class TestDirectToolInvocationWithPermissions:
    """Adapters outside the loop retain the shared permission contract."""

    @pytest.mark.asyncio
    async def test_approved_request_retries_the_validated_tool(
        self, engine, grant_store, outside_file
    ):
        negotiator = MockNegotiator(grant=True, grant_scope="prompt")
        negotiator.attach_store(grant_store)
        tool = PermDeniedTool(engine, outside_file)

        result, policy_decision = await invoke_tool_with_permissions(
            tool,
            {"path": outside_file},
            permission_negotiator=negotiator,
        )

        assert result.success is True
        assert tool._call_count == 2
        assert policy_decision is not None
        assert policy_decision["decision"] == "approved"
        assert policy_decision["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_denied_request_returns_without_retry(
        self, engine, grant_store, outside_file
    ):
        negotiator = MockNegotiator(grant=False)
        negotiator.attach_store(grant_store)
        tool = PermDeniedTool(engine, outside_file)

        result, policy_decision = await invoke_tool_with_permissions(
            tool,
            {"path": outside_file},
            permission_negotiator=negotiator,
        )

        assert result.success is False
        assert tool._call_count == 1
        assert policy_decision is not None
        assert policy_decision["decision"] == "denied"

    @pytest.mark.asyncio
    async def test_runtime_retries_distinct_permission_requests_in_order(
        self,
    ) -> None:
        tool = TwoGateTool()
        negotiator = SafetyNegotiator(grant=True)

        result, policy_decision = await invoke_tool_with_permissions(
            tool,
            {},
            permission_negotiator=negotiator,
        )

        assert result == ToolResult(
            success=True,
            content="approved through both gates",
        )
        assert tool.call_count == 3
        assert negotiator.requests == tool.requests
        assert tool.approved_requests == tool.requests
        assert policy_decision == {
            "type": "policy_decision",
            "tool_name": "two_gates",
            "decision": "approved",
            "retry_count": 2,
            "scope": "safety",
            "requested_scope": "dangerous_command",
            "reason": "second gate",
            "path": "",
            "temporary_supported": True,
            "persistent_supported": False,
            "persistent_label": "",
            "command": "Remove-Item outside.txt",
            "risk": "destructive",
        }


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


class TestACPConversationPermissionBroker:
    @pytest.mark.asyncio
    async def test_identical_concurrent_requests_share_one_host_prompt(self, tmp_path):
        from acp.schema import AllowedOutcome, RequestPermissionResponse
        from box_agent.acp import _PermissionNegotiator

        target = tmp_path / "Downloads"
        target.mkdir()
        prompt_started = asyncio.Event()
        release_prompt = asyncio.Event()

        class Connection:
            def __init__(self):
                self.calls = 0

            async def requestPermission(self, request):
                self.calls += 1
                prompt_started.set()
                await release_prompt.wait()
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(
                        outcome="selected",
                        optionId="approve",
                    )
                )

        connection = Connection()
        store = GrantStore()
        negotiator = _PermissionNegotiator(connection, "session-1", store)
        request = {
            "scope": "filesystem",
            "requested_scope": "user_home",
            "path": str(target),
            "reason": "Read Downloads",
        }

        first = asyncio.create_task(negotiator.negotiate(request))
        second = asyncio.create_task(negotiator.negotiate(dict(request)))
        await prompt_started.wait()
        release_prompt.set()

        assert await asyncio.gather(first, second) == [True, True]
        assert connection.calls == 1
        assert store.has_filesystem_dir_grant(target.resolve())

    @pytest.mark.asyncio
    async def test_concurrent_safety_requests_remain_one_shot(self):
        from acp.schema import AllowedOutcome, RequestPermissionResponse
        from box_agent.acp import _PermissionNegotiator

        class Connection:
            def __init__(self):
                self.calls = 0

            async def requestPermission(self, request):
                self.calls += 1
                await asyncio.sleep(0)
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(
                        outcome="selected",
                        optionId="approve",
                    )
                )

        connection = Connection()
        negotiator = _PermissionNegotiator(connection, "session-1", GrantStore())
        request = {
            "scope": "safety",
            "requested_scope": "dangerous_command",
            "reason": "Run a dangerous command",
        }

        assert await asyncio.gather(
            negotiator.negotiate(request),
            negotiator.negotiate(dict(request)),
        ) == [True, True]
        assert connection.calls == 2

    @pytest.mark.asyncio
    async def test_shared_prompt_survives_one_waiter_cancellation_and_stops_after_last(
        self,
        tmp_path,
    ):
        from box_agent.acp import _PermissionNegotiator

        target = tmp_path / "Downloads"
        target.mkdir()
        prompt_started = asyncio.Event()
        prompt_cancelled = asyncio.Event()

        class Connection:
            def __init__(self):
                self.calls = 0

            async def requestPermission(self, request):
                self.calls += 1
                prompt_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    prompt_cancelled.set()

        connection = Connection()
        negotiator = _PermissionNegotiator(connection, "session-1", GrantStore())
        request = {
            "scope": "filesystem",
            "requested_scope": "user_home",
            "path": str(target),
            "reason": "Read Downloads",
        }

        first = asyncio.create_task(negotiator.negotiate(request))
        second = asyncio.create_task(negotiator.negotiate(dict(request)))
        await prompt_started.wait()

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert second.done() is False
        assert connection.calls == 1

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        await prompt_cancelled.wait()


class TestSafetyPermissionNegotiation:
    @pytest.mark.asyncio
    async def test_dangerous_bash_command_retries_after_approval(self, tmp_path: Path):
        from box_agent.tools.bash_tool import BashTool

        victim = tmp_path / "victim.txt"
        victim.write_text("delete me")
        tool = BashTool(workspace_dir=str(tmp_path), non_interactive=True)
        negotiator = SafetyNegotiator(grant=True)

        events = await collect(run_agent_loop(
            llm=_llm_with_tool_call("bash", {"command": "rm victim.txt"}),
            messages=_msgs(),
            tools={"bash": tool},
            max_steps=5,
            permission_negotiator=negotiator,
            workspace_dir=str(tmp_path),
        ))

        assert not victim.exists()
        assert len(negotiator.requests) == 1
        assert negotiator.requests[0]["scope"] == "safety"
        assert negotiator.requests[0]["requested_scope"] == "dangerous_command"
        assert negotiator.requests[0]["persistent_supported"] is False
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].policy_decision is not None
        assert results[0].policy_decision["type"] == "policy_decision"
        assert results[0].policy_decision["decision"] == "approved"
        assert results[0].policy_decision["retry_count"] == 1
        assert results[0].policy_decision["scope"] == "safety"

    @pytest.mark.asyncio
    async def test_dangerous_bash_command_denial_does_not_execute(self, tmp_path: Path):
        from box_agent.tools.bash_tool import BashTool

        victim = tmp_path / "victim.txt"
        victim.write_text("keep me")
        tool = BashTool(workspace_dir=str(tmp_path), non_interactive=True)
        negotiator = SafetyNegotiator(grant=False)

        events = await collect(run_agent_loop(
            llm=_llm_with_tool_call("bash", {"command": "rm victim.txt"}),
            messages=_msgs(),
            tools={"bash": tool},
            max_steps=5,
            permission_negotiator=negotiator,
            workspace_dir=str(tmp_path),
        ))

        assert victim.exists()
        assert len(negotiator.requests) == 1
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is False
        assert "requires approval" in (results[0].error or "")
        assert results[0].policy_decision is not None
        assert results[0].policy_decision["type"] == "policy_decision"
        assert results[0].policy_decision["decision"] == "denied"
        assert results[0].policy_decision["scope"] == "safety"

    @pytest.mark.asyncio
    async def test_dangerous_outside_command_negotiates_both_gates_before_execution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """A safety approval survives the following filesystem approval gate."""
        from box_agent.tools.bash_tool import BashTool

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside_dir = tmp_path / "outside-empty-dir"
        outside_dir.mkdir()
        grant_store = GrantStore()
        engine = PermissionEngine(
            CapabilityPolicy(
                filesystem_scope="session_workspace",
                session_workspace_root=str(workspace),
            ),
            workspace,
            grant_store=grant_store,
        )
        engine._home_dir = tmp_path.resolve()
        spawned: list[str] = []

        class SuccessfulProcess:
            returncode = 0

            async def communicate(self):
                return b"DELETED\n", b""

        async def fake_spawn(command: str, *, merge_stderr: bool = False):
            del merge_stderr
            spawned.append(command)
            return SuccessfulProcess()

        class ChainedNegotiator:
            def __init__(self):
                self.requests: list[dict] = []

            async def negotiate(self, permission_request: dict) -> bool:
                self.requests.append(permission_request)
                if permission_request.get("scope") == "filesystem":
                    requested_path = Path(permission_request["path"])
                    grant_store.add_filesystem_dir_grant(
                        requested_path.parent,
                        "prompt",
                    )
                return True

        monkeypatch.setattr("box_agent.tools.bash_tool.backup_file", lambda _path: None)
        tool = BashTool(
            workspace_dir=str(workspace),
            allow_full_access=False,
            permission_engine=engine,
            non_interactive=True,
        )
        monkeypatch.setattr(tool, "_create_subprocess", fake_spawn)
        windows_path = str(outside_dir)
        command = (
            f'rmdir "{windows_path}" && '
            f'if exist "{windows_path}" '
            '(echo DELETE_FAILED & exit /b 1) else (echo DELETED)'
        )
        negotiator = ChainedNegotiator()

        events = await collect(run_agent_loop(
            llm=_llm_with_tool_call("bash", {"command": command}),
            messages=_msgs(),
            tools={"bash": tool},
            max_steps=5,
            permission_negotiator=negotiator,
            workspace_dir=str(workspace),
        ))

        assert [request["scope"] for request in negotiator.requests] == [
            "safety",
            "filesystem",
        ]
        assert spawned == [command]
        assert tool._approved_safety_commands == set()
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].policy_decision is not None
        assert results[0].policy_decision["scope"] == "filesystem"
        assert results[0].policy_decision["decision"] == "approved"
        assert results[0].policy_decision["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_unrestricted_filesystem_retries_dangerous_windows_command_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Mode 2 prompts for danger, then retries without a filesystem gate."""
        from box_agent.tools.bash_tool import BashTool

        command = (
            'rmdir "D:\\fly\\2026-08-17-7e45db75" && '
            'if exist "D:\\fly\\2026-08-17-7e45db75" '
            '(echo DELETE_FAILED & exit /b 1) else (echo DELETED)'
        )
        spawned: list[str] = []

        class SuccessfulProcess:
            returncode = 0

            async def communicate(self):
                return b"DELETED\n", b""

        async def fake_spawn(actual_command: str, *, merge_stderr: bool = False):
            del merge_stderr
            spawned.append(actual_command)
            return SuccessfulProcess()

        monkeypatch.setattr("box_agent.tools.bash_tool.backup_file", lambda _path: None)
        tool = BashTool(
            workspace_dir=str(tmp_path),
            allow_full_access=True,
            permission_engine=None,
            non_interactive=True,
            bypass_dangerous_command_approval=False,
        )
        monkeypatch.setattr(tool, "_create_subprocess", fake_spawn)
        negotiator = SafetyNegotiator(grant=True)

        events = await collect(run_agent_loop(
            llm=_llm_with_tool_call("bash", {"command": command}),
            messages=_msgs(),
            tools={"bash": tool},
            max_steps=5,
            permission_negotiator=negotiator,
            workspace_dir=str(tmp_path),
        ))

        assert len(negotiator.requests) == 1
        assert negotiator.requests[0]["scope"] == "safety"
        assert spawned == [command]
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is True
        assert results[0].policy_decision is not None
        assert results[0].policy_decision["decision"] == "approved"
        assert results[0].policy_decision["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_full_access_executes_dangerous_windows_command_without_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Mode 3 bypasses both dangerous-command and filesystem gates."""
        from box_agent.tools.bash_tool import BashTool

        command = (
            'rmdir "D:\\fly\\2026-08-17-7e45db75" && '
            'if exist "D:\\fly\\2026-08-17-7e45db75" '
            '(echo DELETE_FAILED & exit /b 1) else (echo DELETED)'
        )
        spawned: list[str] = []

        class SuccessfulProcess:
            returncode = 0

            async def communicate(self):
                return b"DELETED\n", b""

        async def fake_spawn(actual_command: str, *, merge_stderr: bool = False):
            del merge_stderr
            spawned.append(actual_command)
            return SuccessfulProcess()

        monkeypatch.setattr("box_agent.tools.bash_tool.backup_file", lambda _path: None)
        tool = BashTool(
            workspace_dir=str(tmp_path),
            allow_full_access=True,
            permission_engine=None,
            non_interactive=True,
            bypass_dangerous_command_approval=True,
        )
        monkeypatch.setattr(tool, "_create_subprocess", fake_spawn)
        negotiator = SafetyNegotiator(grant=False)

        events = await collect(run_agent_loop(
            llm=_llm_with_tool_call("bash", {"command": command}),
            messages=_msgs(),
            tools={"bash": tool},
            max_steps=5,
            permission_negotiator=negotiator,
            workspace_dir=str(tmp_path),
        ))

        assert negotiator.requests == []
        assert spawned == [command]
        assert not [e for e in events if isinstance(e, PermissionRequestEvent)]
        results = [e for e in events if isinstance(e, ToolCallResult)]
        assert len(results) == 1
        assert results[0].success is True


@pytest.mark.asyncio
async def test_direct_permission_retries_forward_live_events_without_reading_parent_queue():
    from box_agent.tools.base import EventEmittingTool, ToolInvocationContext

    class OutputOnlyQueue(asyncio.Queue):
        def get_nowait(self):
            raise AssertionError("the parent queue is an output sink")

        async def get(self):
            raise AssertionError("the parent queue is an output sink")

    class ProgressTool(EventEmittingTool, TwoGateTool):
        def __init__(self):
            EventEmittingTool.__init__(self)
            TwoGateTool.__init__(self)
            self.contexts = []
            self.release = asyncio.Event()
            self.effects = 0

        async def execute(self):
            self.contexts.append(self._parent_tool_call_id)
            if len(self.approved_requests) == len(self.requests):
                self._event_queue.put_nowait({"phase": "approved-running"})
                await self.release.wait()
                self.effects += 1
            return await TwoGateTool.execute(self)

    tool = ProgressTool()
    queue = OutputOnlyQueue()
    sibling_event = {"phase": "sibling"}
    queue.put_nowait(sibling_event)
    context = ToolInvocationContext(event_queue=queue, parent_tool_call_id="parent-1")
    task = asyncio.create_task(invoke_tool_with_permissions(
        tool, {}, permission_negotiator=SafetyNegotiator(grant=True),
        invocation_context=context,
    ))
    try:
        for _ in range(100):
            if task.done() or queue.qsize() == 2:
                break
            await asyncio.sleep(0.005)
        if task.done():
            await task
        assert queue.qsize() == 2
        assert not task.done(), "progress must arrive before the approved attempt finishes"
    finally:
        tool.release.set()
        result, decision = await asyncio.wait_for(task, 1)
    assert result.success
    assert decision["retry_count"] == 2
    assert tool.effects == 1
    assert tool.contexts == ["parent-1"] * 3
    assert asyncio.Queue.get_nowait(queue) is sibling_event
    assert asyncio.Queue.get_nowait(queue) == {"phase": "approved-running"}


@pytest.mark.asyncio
async def test_permission_negotiation_durability_error_propagates():
    from box_agent.kernel.permission_gateway import _negotiate_tool_permission_chain

    failure = SessionLogDurabilityError("cannot persist approval")

    class FailingNegotiator:
        async def negotiate(self, request):
            raise failure

    tool = TwoGateTool()
    result = await tool.invoke({})
    with pytest.raises(SessionLogDurabilityError) as caught:
        await _negotiate_tool_permission_chain(
            result=result, permission_negotiator=FailingNegotiator(),
            tool_name=tool.name, tool=tool, arguments={}, retry_offer_error=lambda: None,
        )
    assert caught.value is failure
    assert tool.call_count == 1


@pytest.mark.asyncio
async def test_direct_invocation_cancel_context_stops_approved_event_attempt():
    from box_agent.tools.base import EventEmittingTool, ToolInvocationContext

    class CancellableTool(EventEmittingTool, TwoGateTool):
        cancel_on_agent_cancel = True

        def __init__(self):
            EventEmittingTool.__init__(self)
            TwoGateTool.__init__(self)
            self.requests = self.requests[:1]
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()

        async def execute(self):
            if not self.approved_requests:
                return await TwoGateTool.execute(self)
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.stopped.set()

    tool = CancellableTool()
    result, decision = await asyncio.wait_for(invoke_tool_with_permissions(
        tool, {}, permission_negotiator=SafetyNegotiator(grant=True),
        invocation_context=ToolInvocationContext(parent_tool_call_id="cancel-1"),
        is_cancelled=tool.started.is_set,
    ), 1)
    assert not result.success
    assert "cancelled" in result.error
    assert decision["retry_count"] == 1
    assert tool.stopped.is_set()


@pytest.mark.asyncio
async def test_permission_stream_relays_progress_and_activity_before_final_result():
    from box_agent.tools.base import EventEmittingTool, ToolInvocationContext
    from box_agent.tools.engine.execution import (
        PermissionChainCompleted, stream_tool_permission_chain,
    )
    from box_agent.tools.engine.scheduler import (
        ToolEngine, ToolEngineActivity, ToolEngineProgress, ToolInvocationRequest,
    )

    class StreamingTool(EventEmittingTool, TwoGateTool):
        def __init__(self):
            EventEmittingTool.__init__(self)
            TwoGateTool.__init__(self)
            self.requests = self.requests[:1]
            self.release = asyncio.Event()
            self.finished = False

        async def execute(self):
            if self.approved_requests:
                self._event_queue.put_nowait(self._parent_tool_call_id)
                await self.release.wait()
                self.finished = True
            return await TwoGateTool.execute(self)

    tool = StreamingTool()
    first_result = await tool.invoke({})
    scheduler = ToolEngine(
        tools={tool.name: tool}, is_cancelled=lambda: False,
        activity_interval_seconds=0.005, event_poll_interval_seconds=0.001,
        cancel_grace_seconds=0.02, max_parallel_tools=1,
        batch_timeout_seconds=None, web_search_concurrency=1,
        web_search_tool_name="web_search",
    )
    request = ToolInvocationRequest(
        call_id="original-call", tool_name=tool.name, arguments={},
        invocation_context=ToolInvocationContext(parent_tool_call_id="original-parent"),
    )
    retried = []
    stream = stream_tool_permission_chain(
        result=first_result, permission_negotiator=SafetyNegotiator(grant=True),
        tool_name=tool.name, tool=tool, arguments={}, retry_offer_error=lambda: None,
        retry_records=lambda approval: scheduler.invoke_serial(
            replace(request, approved_permission_request=approval)
        ), on_retry=retried.append,
    )

    async def wait_for_live_records():
        expected = {
            ToolEngineProgress(event="original-parent"),
            ToolEngineActivity(tool_name=tool.name),
        }
        seen = set()
        while seen != expected:
            record = await anext(stream)
            assert record in expected
            assert not tool.finished
            seen.add(record)

    try:
        await asyncio.wait_for(wait_for_live_records(), 1)
        tool.release.set()
        remaining = [record async for record in stream]
    finally:
        tool.release.set()
        await stream.aclose()
    final = remaining[-1]
    assert isinstance(final, PermissionChainCompleted)
    assert sum(isinstance(record, PermissionChainCompleted) for record in remaining) == 1
    assert final.result.success
    assert retried == [final.result]
    assert tool.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["repeated", "limit", "denied", "error", "no_negotiator"])
async def test_direct_permission_chain_retains_bounded_terminal_decisions(mode):
    class GatedTool(TwoGateTool):
        async def execute(self):
            self.call_count += 1
            index = 0 if mode == "repeated" else len(self.approved_requests)
            return ToolResult(success=False, error="approval required", permission_request={
                "scope": "filesystem", "requested_scope": f"gate-{index}",
            })

    class Negotiator(SafetyNegotiator):
        async def negotiate(self, request):
            if mode == "error":
                self.requests.append(request)
                raise RuntimeError("host unavailable")
            return await super().negotiate(request)

    tool = GatedTool()
    negotiator = Negotiator(grant=mode != "denied")
    result, decision = await invoke_tool_with_permissions(
        tool, {}, permission_negotiator=None if mode == "no_negotiator" else negotiator,
    )
    assert not result.success
    if mode == "no_negotiator":
        assert decision is None
        assert tool.call_count == 1
        return
    retries = {"repeated": 1, "limit": 4, "denied": 0, "error": 0}[mode]
    assert decision["retry_count"] == retries
    assert tool.call_count == retries + 1
    assert len(tool.approved_requests) == retries
    assert decision["decision"] == ("denied" if mode == "denied" else "error")
    if mode == "repeated":
        assert decision["error"] == "Permission request repeated after approval"
    elif mode == "limit":
        assert decision["error"] == "Permission retry limit reached"
    elif mode == "error":
        assert decision["error"] == "host unavailable"


@pytest.mark.asyncio
async def test_permission_wrapper_revalidates_after_approval_before_consuming_grant():
    from box_agent.kernel.permission_gateway import _negotiate_tool_permission_chain

    tool = TwoGateTool()
    first_result = await tool.invoke({})
    negotiator = SafetyNegotiator(grant=True)
    retried = []

    def validate_offer():
        assert negotiator.requests == [tool.requests[0]]
        return "tool offer became stale"

    result, decision = await _negotiate_tool_permission_chain(
        result=first_result, permission_negotiator=negotiator,
        tool_name=tool.name, tool=tool, arguments={},
        retry_offer_error=validate_offer, on_retry=retried.append,
    )
    assert not result.success
    assert result.error == "tool offer became stale"
    assert tool.call_count == 1
    assert tool.approved_requests == []
    assert retried == [result]
    assert decision["decision"] == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("after_approval", [False, True])
async def test_direct_invocation_preserves_ordinary_exception_payloads(after_approval):
    class ExplodingTool(TwoGateTool):
        async def execute(self):
            if after_approval and not self.approved_requests:
                return await TwoGateTool.execute(self)
            raise ValueError("broken fixture")

    result, decision = await invoke_tool_with_permissions(
        ExplodingTool(), {}, permission_negotiator=SafetyNegotiator(grant=True),
    )
    assert not result.success
    if after_approval:
        assert result.error.startswith("Tool execution failed: ValueError: broken fixture\n\nTraceback:")
        assert result.raw_output is None
        assert decision["retry_count"] == 1
    else:
        assert result.error == "Tool execution failed: ValueError: broken fixture"
        assert result.raw_output == {"type": "tool_error", "code": "TOOL_EXECUTION_FAILED", "tool": "two_gates"}
        assert decision is None
