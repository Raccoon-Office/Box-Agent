"""Compatibility checks for the stable kernel package surface."""

import asyncio
import gc
import inspect

import pytest

from box_agent.events import ContentEvent, LLMActivityEvent
from box_agent.schema import FunctionCall, Message, StreamEvent, ToolCall
from box_agent.tools.base import Tool, ToolResult


CONTEXT_SYMBOLS = (
    "CompactionOutcome",
    "_create_summary",
    "_maybe_summarize",
    "_restore_runtime_state",
    "_select_recent_messages",
    "_estimate_context_from_latest_response",
    "_fallback_context_estimate",
    "_bound_text_middle",
)

STREAM_CONTROLLER_SYMBOLS = (
    "resolve_provider_stale_seconds",
    "stream_with_activity",
)

TOOL_RESULT_HELPER_GROUPS = (
    ("browser snapshot persistence", "_prepare_browser_snapshot_output"),
    ("model-history placeholder recovery", "_model_history_placeholder_argument"),
    ("context-resource history", "_context_resource_history_decision"),
    ("artifact detection", "_detect_artifacts"),
    ("web-search normalization", "_dedupe_web_search_content"),
    ("dangling tool-call cleanup", "_sanitize_dangling_tool_calls"),
)

LEGACY_LOOP_SYMBOLS = (
    "_DEFAULT_AGENT_CONFIG",
    "PARALLEL_TOOL_CANCEL_GRACE_SECONDS",
    "LLM_ACTIVITY_INTERVAL_SECONDS",
    "TOOL_ACTIVITY_INTERVAL_SECONDS",
    "TOOL_EVENT_POLL_INTERVAL_SECONDS",
    "LLM_PROVIDER_STALE_SECONDS",
    "MAX_PROVIDER_STALE_RECOVERIES",
    "_PROVIDER_STALE_SECONDS_ENV",
    "CancelChecker",
    "ActiveSkillActivator",
    "_MODEL_HISTORY_PLACEHOLDER_REPAIR_LIMIT",
    "_MODEL_HISTORY_PLACEHOLDER_TOOL_ERROR",
    "_MODEL_HISTORY_PLACEHOLDER_REPAIR_GUIDANCE",
    "_OUTPUT_LENGTH_TOOL_RECOVERY",
    "_OUTPUT_LENGTH_WRITE_FILE_RECOVERY",
    "_FORCED_PLAN_GUIDANCE",
    "_FORCED_PLAN_RETRY_GUIDANCE",
    "_FORCED_PLAN_APPROVAL_GUIDANCE",
    "_PLAN_APPROVAL_SKIP_MESSAGE",
    "_PLAN_APPROVAL_DONE_CONTENT",
    "_WAITING_FOR_USER_DONE_CONTENT",
    "FINAL_SUMMARY_TOOL_CALL_THRESHOLD",
    "final_summary_wrapup_text",
    "empty_final_answer_retry_text",
    "_EMPTY_FINAL_ANSWER_ERROR",
    "_message_text",
    "_latest_user_text",
    "_should_emit_plan_start",
    "_plan_approval_is_approved",
    "_plan_approval_payload",
    "_attach_plan_approval_payload",
    "_plan_start_payload",
    "_auto_match_memory_for_latest_prompt",
)

SERVICE_OWNED_RUN_ARGUMENTS = frozenset(
    {
        "llm",
        "summary_llm",
        "tools",
        "permission_negotiator",
        "hooks",
        "memory_manager",
        "memory_extractor",
        "session_log",
        "tool_exposure_manager",
        "tool_result_storage",
    }
)


REQUIRED_PARAMETER = object()


def _signature_snapshot(callable_object) -> tuple[tuple[str, object, object], ...]:
    return tuple(
        (
            name,
            parameter.kind,
            (
                REQUIRED_PARAMETER
                if parameter.default is inspect.Parameter.empty
                else parameter.default
            ),
        )
        for name, parameter in inspect.signature(callable_object).parameters.items()
    )


AGENT_RUN_EVENTS_SIGNATURE = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, REQUIRED_PARAMETER),
    ("cancel_event", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
    ("options", inspect.Parameter.KEYWORD_ONLY, None),
    ("force_plan_start", inspect.Parameter.KEYWORD_ONLY, None),
    ("require_plan_approval", inspect.Parameter.KEYWORD_ONLY, None),
    ("plan_approval", inspect.Parameter.KEYWORD_ONLY, None),
    ("pause_after_plan_write", inspect.Parameter.KEYWORD_ONLY, None),
    ("artifact_detection_enabled", inspect.Parameter.KEYWORD_ONLY, None),
)

AGENT_RUN_SIGNATURE = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD, REQUIRED_PARAMETER),
    ("cancel_event", inspect.Parameter.POSITIONAL_OR_KEYWORD, None),
    ("force_plan_start", inspect.Parameter.KEYWORD_ONLY, False),
    ("require_plan_approval", inspect.Parameter.KEYWORD_ONLY, False),
    ("plan_approval", inspect.Parameter.KEYWORD_ONLY, None),
    ("pause_after_plan_write", inspect.Parameter.KEYWORD_ONLY, False),
    ("artifact_detection_enabled", inspect.Parameter.KEYWORD_ONLY, True),
    ("current_turn_text", inspect.Parameter.KEYWORD_ONLY, None),
)

LOOP_SIGNATURE = (
    ("llm", inspect.Parameter.KEYWORD_ONLY, REQUIRED_PARAMETER),
    ("summary_llm", inspect.Parameter.KEYWORD_ONLY, None),
    ("messages", inspect.Parameter.KEYWORD_ONLY, REQUIRED_PARAMETER),
    ("tools", inspect.Parameter.KEYWORD_ONLY, REQUIRED_PARAMETER),
    ("max_steps", inspect.Parameter.KEYWORD_ONLY, 300),
    ("tool_limits", inspect.Parameter.KEYWORD_ONLY, None),
    ("max_tool_calls", inspect.Parameter.KEYWORD_ONLY, None),
    ("max_delegated_tool_calls", inspect.Parameter.KEYWORD_ONLY, None),
    ("web_search_total_limit", inspect.Parameter.KEYWORD_ONLY, None),
    ("token_limit", inspect.Parameter.KEYWORD_ONLY, 113400),
    ("is_cancelled", inspect.Parameter.KEYWORD_ONLY, None),
    ("logger", inspect.Parameter.KEYWORD_ONLY, None),
    ("workspace_dir", inspect.Parameter.KEYWORD_ONLY, None),
    ("permission_negotiator", inspect.Parameter.KEYWORD_ONLY, None),
    ("hooks", inspect.Parameter.KEYWORD_ONLY, None),
    ("memory_manager", inspect.Parameter.KEYWORD_ONLY, None),
    ("memory_extractor", inspect.Parameter.KEYWORD_ONLY, None),
    ("memory_turn_id", inspect.Parameter.KEYWORD_ONLY, ""),
    ("memory_promotion_enabled", inspect.Parameter.KEYWORD_ONLY, False),
    ("memory_promotion_hit_threshold", inspect.Parameter.KEYWORD_ONLY, 5),
    ("memory_promotion_cooldown_days", inspect.Parameter.KEYWORD_ONLY, 14),
    ("inject_queue", inspect.Parameter.KEYWORD_ONLY, None),
    ("thinking_enabled", inspect.Parameter.KEYWORD_ONLY, False),
    ("session_id", inspect.Parameter.KEYWORD_ONLY, ""),
    ("turn_id", inspect.Parameter.KEYWORD_ONLY, ""),
    ("title", inspect.Parameter.KEYWORD_ONLY, ""),
    ("call_kind", inspect.Parameter.KEYWORD_ONLY, ""),
    ("force_plan_start", inspect.Parameter.KEYWORD_ONLY, False),
    ("require_plan_approval", inspect.Parameter.KEYWORD_ONLY, False),
    ("plan_approval", inspect.Parameter.KEYWORD_ONLY, None),
    ("plan_start_text", inspect.Parameter.KEYWORD_ONLY, None),
    ("pause_after_plan_write", inspect.Parameter.KEYWORD_ONLY, False),
    ("no_progress_limit", inspect.Parameter.KEYWORD_ONLY, None),
    ("max_parallel_tools", inspect.Parameter.KEYWORD_ONLY, 8),
    ("parallel_tool_timeout_seconds", inspect.Parameter.KEYWORD_ONLY, 900.0),
    ("provider_stale_seconds", inspect.Parameter.KEYWORD_ONLY, None),
    ("truncation_continuation_enabled", inspect.Parameter.KEYWORD_ONLY, True),
    ("max_truncation_continuations", inspect.Parameter.KEYWORD_ONLY, 3),
    ("max_truncated_tool_call_retries", inspect.Parameter.KEYWORD_ONLY, 3),
    ("truncated_tool_call_boost_cap", inspect.Parameter.KEYWORD_ONLY, 32768),
    ("artifact_detection_enabled", inspect.Parameter.KEYWORD_ONLY, True),
    ("artifact_root_dir", inspect.Parameter.KEYWORD_ONLY, None),
    ("cache_fingerprint_context", inspect.Parameter.KEYWORD_ONLY, None),
    ("cache_fingerprint_sink", inspect.Parameter.KEYWORD_ONLY, None),
    ("active_skill_activator", inspect.Parameter.KEYWORD_ONLY, None),
    ("current_turn_text", inspect.Parameter.KEYWORD_ONLY, None),
    ("context_resource_ledger", inspect.Parameter.KEYWORD_ONLY, None),
    ("context_resource_dedup_enabled", inspect.Parameter.KEYWORD_ONLY, True),
    ("tool_exposure_manager", inspect.Parameter.KEYWORD_ONLY, None),
    ("tool_result_storage", inspect.Parameter.KEYWORD_ONLY, None),
    ("session_log", inspect.Parameter.KEYWORD_ONLY, None),
    ("session_turn", inspect.Parameter.KEYWORD_ONLY, None),
)

INVOKE_TOOL_SIGNATURE = (
    ("tool", inspect.Parameter.POSITIONAL_OR_KEYWORD, REQUIRED_PARAMETER),
    ("arguments", inspect.Parameter.POSITIONAL_OR_KEYWORD, REQUIRED_PARAMETER),
    ("permission_negotiator", inspect.Parameter.KEYWORD_ONLY, None),
)


def test_stable_public_execution_signatures_and_defaults() -> None:
    from box_agent.agent import Agent
    import box_agent.core as core
    import box_agent.runtime as runtime

    assert _signature_snapshot(Agent.run_events) == AGENT_RUN_EVENTS_SIGNATURE
    assert _signature_snapshot(Agent.run) == AGENT_RUN_SIGNATURE
    assert _signature_snapshot(core.run_agent_loop) == LOOP_SIGNATURE
    assert _signature_snapshot(runtime.run_agent_loop) == LOOP_SIGNATURE
    assert inspect.isasyncgenfunction(core.run_agent_loop)
    assert (
        _signature_snapshot(runtime.invoke_tool_with_permissions)
        == INVOKE_TOOL_SIGNATURE
    )
    assert {
        "PluginHost",
        "Registry",
        "KernelServices",
        "_services",
    }.isdisjoint(inspect.signature(core.run_agent_loop).parameters)


@pytest.mark.asyncio
async def test_core_loop_writes_no_protocol_output(capsys) -> None:
    import box_agent.core as core

    class FinalLLM:
        async def generate(self, **_kwargs):
            raise AssertionError("this fixture must not need context summarization")

        async def generate_stream(self, **_kwargs):
            yield StreamEvent(type="text", delta="final")
            yield StreamEvent(type="finish", finish_reason="stop")

    events = [
        event
        async for event in core.run_agent_loop(
            llm=FinalLLM(),
            messages=[Message(role="user", content="answer")],
            tools={},
            max_steps=1,
            logger=None,
        )
    ]

    assert [type(event).__name__ for event in events] == [
        "StepStart",
        "ContentEvent",
        "LLMOutputEvent",
        "StepEnd",
        "DoneEvent",
    ]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_legacy_permission_negotiator_is_reexported_from_kernel_gateway() -> None:
    import box_agent.core as legacy_core
    import box_agent.kernel.permission_gateway as permission_gateway

    assert (
        legacy_core._negotiate_tool_permission_chain
        is permission_gateway._negotiate_tool_permission_chain
    )


def test_kernel_exports_agent_loop_implementation_entrypoints() -> None:
    import box_agent.core as core
    import box_agent.kernel as kernel
    from box_agent.kernel.loop import AgentLoopKernel, _run_agent_loop_impl

    assert kernel.__all__ == ["AgentLoopKernel", "run_agent_loop"]
    assert kernel.AgentLoopKernel is AgentLoopKernel
    assert callable(kernel.run_agent_loop)
    assert tuple(inspect.signature(core.run_agent_loop).parameters) == (
        "llm",
        "summary_llm",
        "messages",
        "tools",
        "max_steps",
        "tool_limits",
        "max_tool_calls",
        "max_delegated_tool_calls",
        "web_search_total_limit",
        "token_limit",
        "is_cancelled",
        "logger",
        "workspace_dir",
        "permission_negotiator",
        "hooks",
        "memory_manager",
        "memory_extractor",
        "memory_turn_id",
        "memory_promotion_enabled",
        "memory_promotion_hit_threshold",
        "memory_promotion_cooldown_days",
        "inject_queue",
        "thinking_enabled",
        "session_id",
        "turn_id",
        "title",
        "call_kind",
        "force_plan_start",
        "require_plan_approval",
        "plan_approval",
        "plan_start_text",
        "pause_after_plan_write",
        "no_progress_limit",
        "max_parallel_tools",
        "parallel_tool_timeout_seconds",
        "provider_stale_seconds",
        "truncation_continuation_enabled",
        "max_truncation_continuations",
        "max_truncated_tool_call_retries",
        "truncated_tool_call_boost_cap",
        "artifact_detection_enabled",
        "artifact_root_dir",
        "cache_fingerprint_context",
        "cache_fingerprint_sink",
        "active_skill_activator",
        "current_turn_text",
        "context_resource_ledger",
        "context_resource_dedup_enabled",
        "tool_exposure_manager",
        "tool_result_storage",
        "session_log",
        "session_turn",
    )
    kernel_services = inspect.signature(kernel.run_agent_loop).parameters["_services"]
    assert kernel_services.kind is inspect.Parameter.KEYWORD_ONLY
    assert kernel_services.default is inspect.Parameter.empty
    assert SERVICE_OWNED_RUN_ARGUMENTS.isdisjoint(
        inspect.signature(kernel.run_agent_loop).parameters
    )
    assert SERVICE_OWNED_RUN_ARGUMENTS.isdisjoint(
        inspect.signature(_run_agent_loop_impl).parameters
    )


@pytest.mark.parametrize("symbol", LEGACY_LOOP_SYMBOLS)
def test_legacy_loop_symbol_remains_importable_from_core(symbol: str) -> None:
    import box_agent.core as legacy_core

    assert hasattr(legacy_core, symbol)


@pytest.mark.parametrize("symbol", CONTEXT_SYMBOLS)
def test_legacy_context_symbol_is_reexported_from_kernel(symbol: str) -> None:
    import box_agent.core as legacy_core
    import box_agent.kernel.context_engine as context_engine

    assert getattr(legacy_core, symbol) is getattr(context_engine, symbol)


@pytest.mark.parametrize("symbol", STREAM_CONTROLLER_SYMBOLS)
def test_stream_controller_exposes_implementation_entrypoint(symbol: str) -> None:
    import box_agent.kernel.stream_controller as stream_controller

    assert callable(getattr(stream_controller, symbol))


@pytest.mark.parametrize(("_group", "symbol"), TOOL_RESULT_HELPER_GROUPS)
def test_legacy_tool_result_helper_is_reexported_from_kernel(
    _group: str,
    symbol: str,
) -> None:
    import box_agent.core as legacy_core
    import box_agent.kernel.tool_result_pipeline as tool_result_pipeline

    assert getattr(legacy_core, symbol) is getattr(tool_result_pipeline, symbol)


@pytest.mark.asyncio
async def test_runtime_core_facade_reads_monkeypatched_timing_defaults(
    monkeypatch,
) -> None:
    """Runtime's Core facade reads timing defaults at first iteration."""
    import box_agent.core as core
    from box_agent.runtime import run_agent_loop

    class SlowTool(Tool):
        @property
        def name(self) -> str:
            return "slow"

        @property
        def description(self) -> str:
            return "Exercise the tool liveness interval."

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self) -> ToolResult:
            await asyncio.sleep(0.012)
            return ToolResult(success=True, content="tool complete")

    class TimedLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def generate(self, **_kwargs):
            raise AssertionError("this fixture must not need context summarization")

        async def generate_stream(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(0.012)
                yield StreamEvent(
                    type="finish",
                    finish_reason="tool",
                    tool_calls=[
                        ToolCall(
                            id="slow-1",
                            type="function",
                            function=FunctionCall(name="slow", arguments={}),
                        )
                    ],
                )
                return
            if self.calls == 2:
                await asyncio.sleep(0.05)
                yield StreamEvent(type="text", delta="late provider output")
                yield StreamEvent(type="finish", finish_reason="stop")
                return
            yield StreamEvent(type="text", delta="recovered")
            yield StreamEvent(type="finish", finish_reason="stop")

    monkeypatch.delenv("BOX_AGENT_PROVIDER_STALE_SECONDS", raising=False)
    monkeypatch.setattr(core, "LLM_ACTIVITY_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(core, "TOOL_ACTIVITY_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr(core, "LLM_PROVIDER_STALE_SECONDS", 1.0)

    llm = TimedLLM()
    event_stream = run_agent_loop(
        llm=llm,
        messages=[Message(role="user", content="run the slow tool")],
        tools={"slow": SlowTool()},
        max_steps=5,
    )

    # Async-generator bodies begin at first iteration, so the compatibility
    # boundary must resolve module constants here rather than at object creation.
    monkeypatch.setattr(core, "LLM_ACTIVITY_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(core, "TOOL_ACTIVITY_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(core, "LLM_PROVIDER_STALE_SECONDS", 0.025)

    events = [event async for event in event_stream]

    activity_phases = {
        event.payload.get("phase")
        for event in events
        if isinstance(event, LLMActivityEvent)
    }
    visible_content = "".join(
        event.content for event in events if isinstance(event, ContentEvent)
    )

    assert {"provider_wait", "tool_running"} <= activity_phases
    assert llm.calls == 3
    assert visible_content == "recovered"


@pytest.mark.parametrize("entrypoint_name", ("core", "kernel"))
@pytest.mark.asyncio
async def test_early_aclose_synchronously_restores_logger_debug_sink(
    entrypoint_name: str,
    tmp_path,
) -> None:
    import box_agent.core as core
    import box_agent.kernel as kernel
    from box_agent.llm import debug_logging
    from box_agent.logger import AgentLogger

    class SuspendedLLM:
        async def generate(self, **_kwargs):
            raise AssertionError("this fixture must not need context summarization")

        async def generate_stream(self, **_kwargs):
            yield StreamEvent(type="text", delta="partial")
            await asyncio.Event().wait()

    entrypoint = {
        "core": core.run_agent_loop,
        "kernel": kernel.run_agent_loop,
    }[entrypoint_name]
    logger = AgentLogger()
    logger.log_dir = tmp_path / entrypoint_name
    logger.log_dir.mkdir()
    deferred_exceptions: list[dict] = []
    event_loop = asyncio.get_running_loop()
    previous_handler = event_loop.get_exception_handler()
    event_loop.set_exception_handler(
        lambda _loop, context: deferred_exceptions.append(context)
    )

    try:
        run_arguments = dict(
            llm=SuspendedLLM(),
            messages=[Message(role="user", content="start")],
            tools={},
            max_steps=1,
            logger=logger,
        )
        if entrypoint_name == "kernel":
            from box_agent.composition import compose_default_kernel_services

            run_arguments["_services"] = compose_default_kernel_services(
                run_arguments
            )
            run_arguments = {
                key: value
                for key, value in run_arguments.items()
                if key not in SERVICE_OWNED_RUN_ARGUMENTS
            }
        events = entrypoint(**run_arguments)
        while True:
            event = await anext(events)
            if isinstance(event, ContentEvent):
                break

        assert debug_logging._SINK.get() is not None

        await events.aclose()
        sink_immediately_after_close = debug_logging._SINK.get()

        gc.collect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert sink_immediately_after_close is None
        assert debug_logging._SINK.get() is None
        assert deferred_exceptions == []
    finally:
        debug_logging._SINK.set(None)
        event_loop.set_exception_handler(previous_handler)


@pytest.mark.parametrize("entrypoint_name", ("core", "kernel", "agent"))
@pytest.mark.parametrize("tool_kind", ("ordinary", "event", "parallel"))
@pytest.mark.asyncio
async def test_closing_run_waits_for_tool_cleanup(
    entrypoint_name: str,
    tool_kind: str,
    monkeypatch,
    tmp_path,
) -> None:
    import box_agent.core as core
    import box_agent.kernel.loop as loop
    from box_agent.agent import Agent
    from box_agent.tools.base import EventEmittingTool
    from box_agent.composition import compose_default_kernel_services
    from box_agent.tool_result_storage import ToolResultStorage

    cleaned_up = asyncio.Event()

    class BlockingTool(EventEmittingTool if tool_kind == "event" else Tool):
        name = "blocking"
        description = "Wait until cancelled."
        parameters = {"type": "object", "properties": {}}
        parallel_safe = tool_kind == "parallel"

        async def execute(self, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned_up.set()

    class ToolLLM:
        async def generate_stream(self, **_kwargs):
            yield StreamEvent(
                type="finish",
                finish_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id="blocking-1",
                        type="function",
                        function=FunctionCall(name="blocking", arguments={}),
                    ),
                ],
            )

    monkeypatch.setattr(core, "TOOL_ACTIVITY_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(core, "TOOL_EVENT_POLL_INTERVAL_SECONDS", 0.001)
    run_arguments = dict(
        llm=ToolLLM(),
        messages=[Message(role="user", content="run the tool")],
        tools={"blocking": BlockingTool()},
        max_steps=1,
        tool_result_storage=ToolResultStorage(tmp_path),
    )
    if entrypoint_name == "kernel":
        services = compose_default_kernel_services(run_arguments)
        events = loop.AgentLoopKernel(
            _services=services,
            _runtime_defaults=loop._LoopRuntimeDefaults(
                tool_activity_interval_seconds=0.001,
                tool_event_poll_interval_seconds=0.001,
            ),
            **{
                key: value
                for key, value in run_arguments.items()
                if key not in SERVICE_OWNED_RUN_ARGUMENTS
            },
        ).run()
    elif entrypoint_name == "agent":
        agent = Agent(
            run_arguments["llm"], "Run the tool.", list(run_arguments["tools"].values()),
            max_steps=1, workspace_dir=str(tmp_path), deferred_mcp_loading_enabled=False,
        )
        agent.logger.log_dir = tmp_path
        agent.tool_result_storage = run_arguments["tool_result_storage"]
        agent.messages.append(Message(role="user", content="run the tool"))
        events = agent.run_events()
    else:
        events = core.run_agent_loop(**run_arguments)

    try:
        async for event in events:
            if (
                isinstance(event, LLMActivityEvent)
                and event.payload.get("phase") == "tool_running"
            ):
                break
        await events.aclose()
        cleanup_finished_before_close_returned = cleaned_up.is_set()
    finally:
        await events.aclose()
        await asyncio.wait_for(cleaned_up.wait(), timeout=1)

    assert cleanup_finished_before_close_returned
