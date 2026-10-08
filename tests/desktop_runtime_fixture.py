"""Opt-in deterministic ACP server for a development desktop, never a release entry."""

import asyncio
from contextvars import ContextVar
import json
import os
from pathlib import Path
import re

from box_agent.agent_service import AgentService
from box_agent.api import RunDeliveryOptions
from box_agent.config import AgentConfig, Config, LLMConfig, ToolsConfig
from box_agent.schema import FunctionCall, LLMResponse, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult
from box_agent.tools.delegated_budget import DelegatedBudget, bind_budgets, current_budgets
from box_agent.tools.engine.execution import invoke_tool_once


scenario = ContextVar("desktop_test_scenario", default="normal")
executions = ContextVar("desktop_test_executions", default=None)


def record_lifecycle(event, **details):
    root = Path(os.environ["BOX_AGENT_DESKTOP_TEST_ROOT"])
    with (root / "lifecycle.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps({"event": event, **details}) + "\n")


class CountTool(Tool):
    name = "fixture_count"
    description = "Record a synthetic test execution; no external side effects."
    parallel_safe = True
    parameters = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}

    async def execute(self, n):
        executions.get().append({"tool": self.name, "n": n})
        await asyncio.sleep(0)
        return ToolResult(success=True, content=f"counted {n}")


class NestedTool(CountTool):
    name = "fixture_nested"

    async def execute(self, n):
        await super().execute(n)
        inner = DelegatedBudget(1)
        with bind_budgets((*current_budgets(), inner)):
            results = [await invoke_tool_once(CountTool(), {"n": n * 10 + index}) for index in range(3)]
        return ToolResult(success=True, content=json.dumps({"inner_used": inner.used,
                                                          "executed": sum(r.success for r in results)}))


class PermissionTool(Tool):
    name = "fixture_permission"
    description = "Request host approval for a synthetic test operation."
    parameters = {"type": "object", "properties": {}}
    approved = False

    def approve_permission_request(self, request):
        self.approved = True

    async def execute(self):
        if not self.approved:
            return ToolResult(success=False, permission_request={
                "scope": "safety", "requested_scope": "dangerous_command",
                "command": "fixture-permission", "reason": "Desktop regression: no real command executes",
                "temporary_supported": True, "persistent_supported": False,
            })
        executions.get().append({"tool": self.name})
        self.approved = False
        return ToolResult(success=True, content="fixture approved")


def call(name, arguments, index):
    return ToolCall(id=f"fixture-{name}-{index}", type="function",
                    function=FunctionCall(name=name, arguments=arguments))


class FixtureLLM:
    model = "desktop-fixture"

    def __init__(self, **kwargs):
        pass

    def for_model(self, *args, **kwargs):
        return self

    async def generate(self, *args, **kwargs):
        return LLMResponse(content='{"continue": false}', finish_reason="stop")

    async def aclose(self):
        record_lifecycle("llm_closed")

    async def generate_stream(self, messages, tools=None, **kwargs):
        users = "\n".join(str(message.content) for message in messages if message.role == "user")
        has_results = any(message.role == "tool" for message in messages)
        mode = scenario.get()
        if "fixture-leaf" in users and not has_results:
            name = "fixture_nested" if mode == "nested" else "fixture_count"
            leaf = int(re.search(r"fixture-leaf (\d+)", users)[1])
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                call(name, {"n": leaf * 10 + index}, index)
                for index in range(1 if mode == "nested" else 4)
            ])
            return
        if not has_results and mode in {"budget", "nested"}:
            required = "fixture_nested" if mode == "nested" else "fixture_count"
            yield StreamEvent(type="finish", finish_reason="tool_use", tool_calls=[
                call("sub_agent", {"task": f"fixture-leaf {index}", "required_tools": [required]}, index)
                for index in range(2)
            ])
            return
        if not has_results and mode.startswith("permission"):
            yield StreamEvent(type="finish", finish_reason="tool_use",
                              tool_calls=[call("fixture_permission", {}, 0)])
            return
        if mode == "stream_wait":
            try:
                yield StreamEvent(type="text", delta="STREAM_WAIT_READY")
                await asyncio.Event().wait()
            finally:
                record_lifecycle("stream_closed")
            return
        if mode == "oversize":
            yield StreamEvent(type="text", delta="x" * 70000)
        elif mode == "stdout_pressure":
            yield StreamEvent(type="text", delta="x" * (512 * 1024))
        elif mode in {"slow", "timeout"}:
            for index in range(40):
                yield StreamEvent(type="text", delta=f"piece-{index};")
        else:
            yield StreamEvent(type="text", delta=json.dumps({"fixture": mode, "executions": executions.get()}))
        yield StreamEvent(type="finish", finish_reason="stop")


class SmallService(AgentService):
    async def start(self, *args, **kwargs):
        kwargs["delivery_options"] = (
            RunDeliveryOptions() if scenario.get() == "stdout_pressure"
            else RunDeliveryOptions(max_events=2, max_bytes=65536, congestion_timeout_seconds=0.25)
        )
        return await super().start(*args, **kwargs)


def install_fixture(acp, root):
    from box_agent.acp import stdio_compat
    from box_agent.tools.sub_agent_capabilities import BUILTIN_TOOL_CAPABILITIES, ToolCapabilityMetadata

    binding_phase = os.environ.get("BOX_AGENT_DESKTOP_TEST_BINDING_PHASE", "")
    block_binding = ContextVar("desktop_test_block_binding", default=False)
    binding_waited = False

    async def wait_for_binding_shutdown():
        nonlocal binding_waited
        if binding_waited:
            return
        binding_waited = True
        (root / "binding-waiting").touch()
        try:
            await asyncio.wait_for(asyncio.Event().wait(), 30)
        finally:
            record_lifecycle("binding_wait_closed")

    if binding_phase == "initialization":
        import box_agent.session_assembly as assembly
        original_finish = assembly.finish_session

        async def finish(*args, **kwargs):
            if block_binding.get():
                await wait_for_binding_shutdown()
            return await original_finish(*args, **kwargs)

        assembly.finish_session = finish
    elif binding_phase == "browser":
        original_browser_close = acp.close_browser_session

        async def close_browser(*args, **kwargs):
            if block_binding.get():
                await wait_for_binding_shutdown()
            return await original_browser_close(*args, **kwargs)

        acp.close_browser_session = close_browser

    class WriteProtocol(stdio_compat._WritePipeProtocol):
        def pause_writing(self):
            super().pause_writing()
            (root / "stdout-paused").touch()

    stdio_compat._WritePipeProtocol = WriteProtocol

    for name in ("fixture_count", "fixture_nested"):
        BUILTIN_TOOL_CAPABILITIES[name] = ToolCapabilityMetadata(read=True)

    class Connection:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        async def sessionUpdate(self, payload):
            if getattr(payload.update, "sessionUpdate", "") == "agent_message_chunk":
                await asyncio.sleep({"slow": 0.01, "timeout": 1}.get(scenario.get(), 0))
            return await self.wrapped.sessionUpdate(payload)

    original = acp.BoxACPAgent

    class Adapter(original):
        def __init__(self, conn, *args, **kwargs):
            super().__init__(Connection(conn), *args, **kwargs)

        def _llm_for_binding(self, binding):
            # Host bindings are retained on the session, but this opt-in test
            # process never loads credentials or contacts a real provider.
            return self._llm

        async def extMethod(self, method, params):
            if method == "fixture_rpc_state":
                store = self._conn._conn._state
                return {
                    "incoming_records": len(store._incoming),
                    "outgoing_requests": len(store._outgoing),
                    "sessions": {state.upstream_session_id: {
                        "handle": handle,
                        "users": [str(message.content) for message in state.agent.messages
                                  if message.role == "user"],
                    } for handle, state in self._sessions.items()},
                }
            return await super().extMethod(method, params)

        async def newSession(self, params):
            meta = getattr(params, "field_meta", None) or {}
            should_block = bool(binding_phase and meta.get("fixture_block_binding"))
            if should_block and binding_phase == "close":
                state = next(s for s in self._sessions.values()
                             if s.upstream_session_id == meta["session_id"])
                original_close = state.plugin_session.aclose

                async def close():
                    await wait_for_binding_shutdown()
                    return await original_close()

                state.plugin_session.aclose = close
            token = block_binding.set(should_block)
            try:
                return await super().newSession(params)
            finally:
                block_binding.reset(token)

        async def aclose(self):
            states = list(self._sessions.values())
            await super().aclose()
            record_lifecycle("adapter_closed", remaining_sessions=len(self._sessions),
                             closed_sessions=all(state._closed for state in states),
                             active_runs=sum(state.run_handle.is_active for state in states))
            if binding_phase:
                record_lifecycle("binding_shutdown",
                    creating=len(self._session_creation_tasks),
                    binding=len(self._session_bindings_in_progress),
                    opening=len(self._plugin_runtime._opening),
                    plugin_sessions=len(self._plugin_runtime._sessions))

        async def prompt(self, params):
            text = " ".join(str(getattr(block, "text", block.get("text", "") if isinstance(block, dict) else ""))
                            for block in params.prompt)
            match = re.search(r"fixture:(\w+)", text)
            mode = match[1] if match else "normal"
            token = scenario.set(mode)
            calls_token = executions.set([])
            try:
                return await super().prompt(params)
            finally:
                state = self._sessions.get(params.sessionId)
                handle = getattr(state, "_run_handle", None)
                if handle is not None and not handle.is_active:
                    result = await handle.result()
                    record = {"scenario": mode, "session_id": params.sessionId,
                              "status": result.status.value, "stop_reason": result.stop_reason,
                              "error": dict(result.error) if result.error else None,
                              "executions": executions.get()}
                    with (root / "results.jsonl").open("a", encoding="utf-8") as output:
                        output.write(json.dumps(record, ensure_ascii=False) + "\n")
                scenario.reset(token)
                executions.reset(calls_token)

    async def tools(*args, **kwargs):
        return [CountTool(), NestedTool(), PermissionTool()], None, None, None

    acp.LLMClient = FixtureLLM
    acp.initialize_base_tools = tools
    acp.AgentService = SmallService
    acp.BoxACPAgent = Adapter


def main():
    raw = os.environ.get("BOX_AGENT_DESKTOP_TEST_ROOT")
    if not raw:
        raise RuntimeError("This test entry requires BOX_AGENT_DESKTOP_TEST_ROOT")
    root = Path(raw).resolve()
    workspace = Path(__file__).resolve().parents[1] / "workspace"
    if not root.is_relative_to(workspace.resolve()):
        raise RuntimeError("Desktop test state must stay inside repository workspace")
    root.mkdir(parents=True, exist_ok=True)
    os.environ["BOX_AGENT_HOME"] = str(root / "profile")
    os.environ["BOX_AGENT_LOG_FILE"] = str(root / "profile/log/box-agent.log")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "profile/browsers")
    os.environ["BOX_AGENT_SESSION_TRACE_ENABLED"] = "0"
    import box_agent.acp as acp

    install_fixture(acp, root)
    config = Config(
        llm=LLMConfig(api_key="synthetic-test-key", model="desktop-fixture"),
        agent=AgentConfig(workspace_dir=str(root / "profile/workspaces"), max_steps=5,
                          enable_memory=False, enable_memory_extraction=False,
                          memory_maintainer_enabled=False, memory_promotion_proposal_enabled=False),
        tools=ToolsConfig(enable_file_tools=False, enable_bash=False, enable_todo=False,
                          enable_plan=False, enable_sub_agent=True, enable_skills=False, enable_mcp=False),
    )
    config.tool_limits.general.max_delegated_tool_calls = 3
    asyncio.run(acp.run_acp_server(config))


if __name__ == "__main__":
    main()
