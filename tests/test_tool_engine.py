"""Direct behavior tests for kernel-owned tool invocation mechanics."""

from __future__ import annotations

import asyncio
import gc
from typing import Any

import pytest

from box_agent.kernel.tool_engine import (
    ToolBatchCompleted,
    ToolEngine,
    ToolEngineActivity,
    ToolEngineProgress,
    ToolInvocationCompleted,
    ToolInvocationRequest,
)
from box_agent.session_log import SessionLogDurabilityError
from box_agent.tools.base import EventEmittingTool, Tool, ToolResult


class _TestTool(Tool):
    @property
    def description(self) -> str:
        return "deterministic test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "delay": {"type": "number"},
            },
            "additionalProperties": False,
        }


def _engine(
    tools: dict[str, Tool],
    *,
    is_cancelled=lambda: False,
    activity_interval_seconds: float = 0.005,
    cancel_grace_seconds: float = 0.02,
    max_parallel_tools: int = 2,
    batch_timeout_seconds: float | None = 0.1,
    web_search_concurrency: int = 1,
) -> ToolEngine:
    return ToolEngine(
        tools=tools,
        is_cancelled=is_cancelled,
        activity_interval_seconds=activity_interval_seconds,
        event_poll_interval_seconds=0.002,
        cancel_grace_seconds=cancel_grace_seconds,
        max_parallel_tools=max_parallel_tools,
        batch_timeout_seconds=batch_timeout_seconds,
        web_search_concurrency=web_search_concurrency,
        web_search_tool_name="web_search",
        passthrough_exceptions=(SessionLogDurabilityError,),
    )


@pytest.mark.asyncio
async def test_serial_ordinary_invocation_relays_activity_before_real_result() -> None:
    expected = ToolResult(
        success=True,
        content="ordinary complete",
        raw_output={"kind": "ordinary"},
    )

    class OrdinaryTool(_TestTool):
        finished = False

        @property
        def name(self) -> str:
            return "ordinary"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            await asyncio.sleep(0.02)
            self.finished = True
            return expected

    tool = OrdinaryTool()
    records = []
    activity_observed_while_running = False
    async for record in _engine({"ordinary": tool}).invoke_serial(
        ToolInvocationRequest(
            call_id="ordinary-1",
            tool_name="ordinary",
            arguments={},
        )
    ):
        records.append(record)
        if isinstance(record, ToolEngineActivity):
            activity_observed_while_running = (
                activity_observed_while_running or not tool.finished
            )

    assert activity_observed_while_running is True
    assert records[0] == ToolEngineActivity(tool_name="ordinary")
    assert isinstance(records[-1], ToolInvocationCompleted)
    assert records[-1].call_id == "ordinary-1"
    assert records[-1].index == 0
    assert records[-1].result is expected


@pytest.mark.asyncio
async def test_serial_event_tool_relays_queued_event_before_completion() -> None:
    progress_event = {"phase": "halfway", "call_id": "event-1"}
    expected = ToolResult(success=True, content="event complete")

    class ProgressTool(EventEmittingTool, _TestTool):
        def __init__(self) -> None:
            super().__init__()
            self.finished = False

        @property
        def name(self) -> str:
            return "event_tool"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            assert self._event_queue is not None
            self._event_queue.put_nowait(progress_event)
            await asyncio.sleep(0.01)
            self.finished = True
            return expected

    tool = ProgressTool()
    records = []
    progress_was_live = False
    async for record in _engine(
        {"event_tool": tool},
        activity_interval_seconds=1.0,
    ).invoke_serial(
        ToolInvocationRequest(
            call_id="event-1",
            tool_name="event_tool",
            arguments={},
        )
    ):
        records.append(record)
        if isinstance(record, ToolEngineProgress):
            progress_was_live = not tool.finished

    assert progress_was_live is True
    assert records[0] == ToolEngineProgress(event=progress_event)
    assert isinstance(records[-1], ToolInvocationCompleted)
    assert records[-1].result is expected


@pytest.mark.asyncio
async def test_serial_opt_in_cancellation_waits_for_grace_boundary() -> None:
    class CancellationTool(EventEmittingTool, _TestTool):
        cancel_on_agent_cancel = True

        def __init__(self) -> None:
            super().__init__()
            self.started = False
            self.cancel_count = 0
            self.release = asyncio.Event()
            self.stopped = asyncio.Event()

        @property
        def name(self) -> str:
            return "cancellable"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            self.started = True
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_count += 1
            self.stopped.set()
            return ToolResult(success=True, content="late completion")

    tool = CancellationTool()
    grace_seconds = 0.02
    async def collect_records():
        return [
            record
            async for record in _engine(
                {"cancellable": tool},
                is_cancelled=lambda: tool.started,
                activity_interval_seconds=1.0,
                cancel_grace_seconds=grace_seconds,
            ).invoke_serial(
                ToolInvocationRequest(
                    call_id="cancel-1",
                    tool_name="cancellable",
                    arguments={},
                )
            )
        ]

    runner = asyncio.create_task(collect_records())
    try:
        await asyncio.sleep(0.05)
        assert runner.done() is True
        records = await runner

        completion = records[-1]
        assert isinstance(completion, ToolInvocationCompleted)
        assert completion.result == ToolResult(
            success=False,
            content="",
            error="Tool execution cancelled before completion.",
        )
        assert tool.cancel_count >= 2
        assert tool.stopped.is_set() is False
    finally:
        tool.release.set()
        if not runner.done():
            await asyncio.wait_for(runner, timeout=0.1)
        await asyncio.wait_for(tool.stopped.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_parallel_batch_caps_concurrency_and_is_addressable_in_call_order() -> None:
    class ParallelTool(_TestTool):
        parallel_safe = True

        def __init__(self) -> None:
            self.current = 0
            self.peak = 0
            self.completed = 0
            self.finish_order: list[str] = []
            self.release_first = asyncio.Event()

        @property
        def name(self) -> str:
            return "parallel"

        async def execute(
            self,
            value: str = "",
            delay: float = 0.0,
        ) -> ToolResult:
            self.current += 1
            self.peak = max(self.peak, self.current)
            try:
                if value == "0":
                    await self.release_first.wait()
                elif value == "3":
                    self.release_first.set()
                else:
                    await asyncio.sleep(delay)
                return ToolResult(success=True, content=f"ok:{value}")
            finally:
                self.current -= 1
                self.completed += 1
                self.finish_order.append(value)

    tool = ParallelTool()
    delays = (0.0, 0.012, 0.0, 0.0)
    requests = tuple(
        ToolInvocationRequest(
            call_id=f"parallel-{index}",
            tool_name="parallel",
            arguments={"value": str(index), "delay": delays[index]},
        )
        for index in range(4)
    )
    completion: ToolBatchCompleted | None = None
    activity_was_live = False
    async for record in _engine(
        {"parallel": tool},
        max_parallel_tools=2,
        batch_timeout_seconds=0.2,
    ).invoke_parallel(requests):
        if isinstance(record, ToolEngineActivity):
            activity_was_live = (
                activity_was_live or tool.completed < len(requests)
            )
        elif isinstance(record, ToolBatchCompleted):
            completion = record

    assert activity_was_live is True
    assert tool.peak == 2
    assert tool.finish_order == ["1", "2", "3", "0"]
    assert completion is not None
    assert [outcome.call_id for outcome in completion.outcomes] == [
        "parallel-0",
        "parallel-1",
        "parallel-2",
        "parallel-3",
    ]
    assert [outcome.index for outcome in completion.outcomes] == [0, 1, 2, 3]
    assert [outcome.result.content for outcome in completion.outcomes] == [
        "ok:0",
        "ok:1",
        "ok:2",
        "ok:3",
    ]
    assert completion.outcomes_by_id["parallel-2"] is completion.outcomes[2]


@pytest.mark.asyncio
async def test_parallel_timeout_keeps_completed_result_and_synthesizes_missing() -> None:
    class PartlyHangingTool(_TestTool):
        parallel_safe = True

        def __init__(self) -> None:
            self.cancelled = False

        @property
        def name(self) -> str:
            return "partial"

        async def execute(
            self,
            value: str = "",
            delay: float = 0.0,
        ) -> ToolResult:
            if value == "hang":
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True
            await asyncio.sleep(delay)
            return ToolResult(success=True, content=f"ok:{value}")

    tool = PartlyHangingTool()
    records = [
        record
        async for record in _engine(
            {"partial": tool},
            activity_interval_seconds=1.0,
            max_parallel_tools=2,
            batch_timeout_seconds=0.02,
        ).invoke_parallel(
            (
                ToolInvocationRequest(
                    call_id="fast",
                    tool_name="partial",
                    arguments={"value": "fast", "delay": 0.005},
                ),
                ToolInvocationRequest(
                    call_id="hang",
                    tool_name="partial",
                    arguments={"value": "hang"},
                ),
            )
        )
    ]

    completion = records[-1]
    assert isinstance(completion, ToolBatchCompleted)
    assert completion.timed_out is True
    assert completion.cancelled is False
    assert completion.outcomes_by_id["fast"].result == ToolResult(
        success=True,
        content="ok:fast",
    )
    assert completion.outcomes_by_id["hang"].result == ToolResult(
        success=False,
        content="",
        error=(
            "Tool execution timed out after 0.02s; "
            "continuing with partial parallel results."
        ),
    )
    assert tool.cancelled is True


@pytest.mark.asyncio
async def test_parallel_cancellation_synthesizes_existing_error() -> None:
    class BlockingTool(_TestTool):
        parallel_safe = True

        def __init__(self) -> None:
            self.started = False

        @property
        def name(self) -> str:
            return "blocking"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            self.started = True
            await asyncio.Event().wait()
            return ToolResult(success=True, content="unreachable")

    tool = BlockingTool()
    records = [
        record
        async for record in _engine(
            {"blocking": tool},
            is_cancelled=lambda: tool.started,
            activity_interval_seconds=1.0,
            batch_timeout_seconds=None,
        ).invoke_parallel(
            (
                ToolInvocationRequest(
                    call_id="blocking-1",
                    tool_name="blocking",
                    arguments={},
                ),
            )
        )
    ]

    completion = records[-1]
    assert isinstance(completion, ToolBatchCompleted)
    assert completion.timed_out is False
    assert completion.cancelled is True
    assert completion.outcomes_by_id["blocking-1"].result == ToolResult(
        success=False,
        content="",
        error="Tool execution cancelled before completion.",
    )


@pytest.mark.asyncio
async def test_configured_durability_exception_propagates_from_invocation() -> None:
    failure = SessionLogDurabilityError("disk full")

    class DurabilityFailureTool(_TestTool):
        @property
        def name(self) -> str:
            return "durability_failure"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            raise failure

    engine = _engine({"durability_failure": DurabilityFailureTool()})

    with pytest.raises(SessionLogDurabilityError) as captured:
        async for _record in engine.invoke_serial(
            ToolInvocationRequest(
                call_id="durability-1",
                tool_name="durability_failure",
                arguments={},
            )
        ):
            pass

    assert captured.value is failure


@pytest.mark.asyncio
async def test_serial_cancellation_propagates_durability_failure_during_grace() -> None:
    failure = SessionLogDurabilityError("cancel cleanup flush failed")

    class CancelCleanupFailureTool(EventEmittingTool, _TestTool):
        cancel_on_agent_cancel = True

        def __init__(self) -> None:
            super().__init__()
            self.started = False

        @property
        def name(self) -> str:
            return "cancel_cleanup_failure"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise failure

    tool = CancelCleanupFailureTool()

    with pytest.raises(SessionLogDurabilityError) as captured:
        async for _record in _engine(
            {"cancel_cleanup_failure": tool},
            is_cancelled=lambda: tool.started,
            activity_interval_seconds=1.0,
            cancel_grace_seconds=0.05,
        ).invoke_serial(
            ToolInvocationRequest(
                call_id="cancel-cleanup-1",
                tool_name="cancel_cleanup_failure",
                arguments={},
            )
        ):
            pass

    assert captured.value is failure


@pytest.mark.asyncio
async def test_serial_cancellation_return_during_grace_keeps_synthetic_result() -> None:
    class CancelCleanupReturnsTool(EventEmittingTool, _TestTool):
        cancel_on_agent_cancel = True

        def __init__(self) -> None:
            super().__init__()
            self.started = False
            self.completed_during_grace = False

        @property
        def name(self) -> str:
            return "cancel_cleanup_returns"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.005)
                self.completed_during_grace = True
                return ToolResult(success=True, content="late success")

    tool = CancelCleanupReturnsTool()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(
        lambda _loop, context: loop_contexts.append(context)
    )
    stream = _engine(
        {"cancel_cleanup_returns": tool},
        is_cancelled=lambda: tool.started,
        activity_interval_seconds=1.0,
        cancel_grace_seconds=0.05,
    ).invoke_serial(
        ToolInvocationRequest(
            call_id="cancel-cleanup-return-1",
            tool_name="cancel_cleanup_returns",
            arguments={},
        )
    )

    try:
        records = [record async for record in stream]
        await stream.aclose()
        del stream
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert tool.completed_during_grace is True
    completion = records[-1]
    assert isinstance(completion, ToolInvocationCompleted)
    assert completion.result == ToolResult(
        success=False,
        content="",
        error="Tool execution cancelled before completion.",
    )
    assert not any(
        context.get("message") == "Task exception was never retrieved"
        for context in loop_contexts
    )


@pytest.mark.asyncio
async def test_serial_cancellation_cancelled_error_during_grace_keeps_synthetic_result() -> None:
    class CancelCleanupCancelsTool(EventEmittingTool, _TestTool):
        cancel_on_agent_cancel = True

        def __init__(self) -> None:
            super().__init__()
            self.started = False
            self.completed_during_grace = False

        @property
        def name(self) -> str:
            return "cancel_cleanup_cancels"

        async def execute(self, **_kwargs: Any) -> ToolResult:
            self.started = True
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                self.completed_during_grace = True
                raise

    tool = CancelCleanupCancelsTool()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(
        lambda _loop, context: loop_contexts.append(context)
    )
    stream = _engine(
        {"cancel_cleanup_cancels": tool},
        is_cancelled=lambda: tool.started,
        activity_interval_seconds=1.0,
        cancel_grace_seconds=0.05,
    ).invoke_serial(
        ToolInvocationRequest(
            call_id="cancel-cleanup-cancelled-1",
            tool_name="cancel_cleanup_cancels",
            arguments={},
        )
    )

    try:
        records = [record async for record in stream]
        await stream.aclose()
        del stream
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert tool.completed_during_grace is True
    completion = records[-1]
    assert isinstance(completion, ToolInvocationCompleted)
    assert completion.result == ToolResult(
        success=False,
        content="",
        error="Tool execution cancelled before completion.",
    )
    assert not any(
        context.get("message") == "Task exception was never retrieved"
        for context in loop_contexts
    )


@pytest.mark.asyncio
async def test_parallel_passthrough_failure_consumes_failed_sibling() -> None:
    primary_failure = SessionLogDurabilityError("primary flush failed")
    sibling_failure = SessionLogDurabilityError("sibling flush failed")

    class ParallelDurabilityFailureTool(_TestTool):
        parallel_safe = True

        @property
        def name(self) -> str:
            return "parallel_durability_failure"

        async def execute(self, value: str = "", **_kwargs: Any) -> ToolResult:
            await asyncio.sleep(0)
            if value == "primary":
                raise primary_failure
            raise sibling_failure

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop_contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(
        lambda _loop, context: loop_contexts.append(context)
    )
    stream = _engine(
        {"parallel_durability_failure": ParallelDurabilityFailureTool()},
        activity_interval_seconds=1.0,
        batch_timeout_seconds=0.1,
    ).invoke_parallel(
        (
            ToolInvocationRequest(
                call_id="primary",
                tool_name="parallel_durability_failure",
                arguments={"value": "primary"},
            ),
            ToolInvocationRequest(
                call_id="sibling",
                tool_name="parallel_durability_failure",
                arguments={"value": "sibling"},
            ),
        )
    )

    try:
        with pytest.raises(SessionLogDurabilityError) as captured:
            async for _record in stream:
                pass
        assert captured.value is primary_failure
        primary_failure.__traceback__ = None
        del captured
        await stream.aclose()
        del stream
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert not any(
        context.get("message") == "Task exception was never retrieved"
        and context.get("exception") is sibling_failure
        for context in loop_contexts
    )
