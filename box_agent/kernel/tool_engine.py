"""Kernel-owned async scheduling for prepared tool invocations."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from ..tools.base import (
    EventEmittingTool,
    Tool,
    ToolInvocationContext,
    ToolResult,
)
from .state import ToolExecutionState


_log = logging.getLogger("box_agent.core")


@dataclass(frozen=True, slots=True)
class ToolInvocationRequest:
    """A core-prepared invocation, optionally carrying an immediate result."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    immediate_result: ToolResult | None = None


@dataclass(frozen=True, slots=True)
class ToolEngineProgress:
    """One event emitted by an ``EventEmittingTool`` while it is running."""

    event: Any


@dataclass(frozen=True, slots=True)
class ToolEngineActivity:
    """A liveness heartbeat to be translated by the core event adapter."""

    tool_name: str


@dataclass(frozen=True, slots=True)
class ToolInvocationCompleted:
    """The outcome of one invocation at its original request index."""

    call_id: str
    index: int
    result: ToolResult


@dataclass(frozen=True, slots=True)
class ToolBatchCompleted:
    """One complete, original-order view of a parallel batch."""

    outcomes: tuple[ToolInvocationCompleted, ...]
    outcomes_by_id: Mapping[str, ToolInvocationCompleted]
    timed_out: bool = False
    cancelled: bool = False


ToolEngineRecord = (
    ToolEngineProgress
    | ToolEngineActivity
    | ToolInvocationCompleted
    | ToolBatchCompleted
)


class ToolEngine:
    """Invoke already-approved tools while preserving live async progress."""

    def __init__(
        self,
        *,
        tools: Mapping[str, Tool],
        is_cancelled: Callable[[], bool],
        activity_interval_seconds: float,
        event_poll_interval_seconds: float,
        cancel_grace_seconds: float,
        max_parallel_tools: int,
        batch_timeout_seconds: float | None,
        web_search_concurrency: int,
        web_search_tool_name: str,
        passthrough_exceptions: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._tools = tools
        self._is_cancelled = is_cancelled
        self._activity_interval_seconds = activity_interval_seconds
        self._event_poll_interval_seconds = event_poll_interval_seconds
        self._cancel_grace_seconds = cancel_grace_seconds
        self._max_parallel_tools = max_parallel_tools
        self._batch_timeout_seconds = batch_timeout_seconds
        self._web_search_concurrency = web_search_concurrency
        self._web_search_tool_name = web_search_tool_name
        self._passthrough_exceptions = passthrough_exceptions

    @staticmethod
    def _consume_late_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    def _failed_result(self, exc: Exception) -> ToolResult:
        if isinstance(exc, self._passthrough_exceptions):
            raise exc
        detail = f"{type(exc).__name__}: {exc!s}"
        trace = traceback.format_exc()
        return ToolResult(
            success=False,
            content="",
            error=f"Tool execution failed: {detail}\n\nTraceback:\n{trace}",
        )

    async def _invoke_with_optional_events(
        self,
        request: ToolInvocationRequest,
        event_queue: asyncio.Queue[Any] | None,
    ) -> ToolResult:
        tool = self._tools[request.tool_name]
        if isinstance(tool, EventEmittingTool) and event_queue is not None:
            return await tool.invoke(
                request.arguments,
                context=ToolInvocationContext(
                    event_queue=event_queue,
                    parent_tool_call_id=request.call_id,
                ),
            )
        return await tool.invoke(request.arguments)

    async def invoke_serial(
        self,
        request: ToolInvocationRequest,
    ) -> AsyncIterator[ToolEngineRecord]:
        """Invoke one request and yield progress before its completion record."""
        if request.immediate_result is not None:
            yield ToolInvocationCompleted(
                call_id=request.call_id,
                index=0,
                result=request.immediate_result,
            )
            return

        tool = self._tools[request.tool_name]
        if isinstance(tool, EventEmittingTool):
            async for record in self._invoke_serial_event_tool(request):
                yield record
            return

        exec_task: asyncio.Task[ToolResult] | None = None
        try:
            exec_task = asyncio.create_task(
                self._invoke_with_optional_events(request, None)
            )
            while True:
                done, _ = await asyncio.wait(
                    {exec_task},
                    timeout=self._activity_interval_seconds,
                )
                if done:
                    result = exec_task.result()
                    break
                yield ToolEngineActivity(tool_name=request.tool_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = self._failed_result(exc)
        finally:
            if exec_task is not None and not exec_task.done():
                exec_task.cancel()
                try:
                    await exec_task
                except BaseException:
                    pass

        yield ToolInvocationCompleted(
            call_id=request.call_id,
            index=0,
            result=result,
        )

    async def _invoke_serial_event_tool(
        self,
        request: ToolInvocationRequest,
    ) -> AsyncIterator[ToolEngineRecord]:
        event_queue: asyncio.Queue[Any] = asyncio.Queue()
        state = ToolExecutionState(last_activity_at=perf_counter())

        async def execute() -> ToolResult:
            try:
                return await self._invoke_with_optional_events(request, event_queue)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return self._failed_result(exc)

        exec_task = asyncio.create_task(execute())
        late_task_detached = False
        try:
            while not exec_task.done() or not event_queue.empty():
                if (
                    getattr(self._tools[request.tool_name], "cancel_on_agent_cancel", False)
                    and self._is_cancelled()
                    and not exec_task.done()
                ):
                    state.cancellation_observed = True
                    exec_task.cancel()
                    break
                try:
                    event = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=self._event_poll_interval_seconds,
                    )
                    yield ToolEngineProgress(event=event)
                    state.last_activity_at = perf_counter()
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                now = perf_counter()
                if (
                    not exec_task.done()
                    and now - state.last_activity_at
                    >= self._activity_interval_seconds
                ):
                    yield ToolEngineActivity(tool_name=request.tool_name)
                    state.last_activity_at = now

            while not event_queue.empty():
                yield ToolEngineProgress(event=event_queue.get_nowait())

            if state.cancellation_observed:
                done_tasks, pending_tasks = await asyncio.wait(
                    (exec_task,),
                    timeout=self._cancel_grace_seconds,
                )
                for task in done_tasks:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
                    except BaseException as exc:
                        if isinstance(exc, self._passthrough_exceptions):
                            raise
                for task in pending_tasks:
                    task.add_done_callback(self._consume_late_task)
                    task.cancel()
                    late_task_detached = True
                result = ToolResult(
                    success=False,
                    content="",
                    error="Tool execution cancelled before completion.",
                )
            else:
                result = await exec_task
        finally:
            if not exec_task.done() and not late_task_detached:
                exec_task.add_done_callback(self._consume_late_task)
                exec_task.cancel()

        yield ToolInvocationCompleted(
            call_id=request.call_id,
            index=0,
            result=result,
        )

    async def invoke_parallel(
        self,
        requests: Sequence[ToolInvocationRequest],
    ) -> AsyncIterator[ToolEngineRecord]:
        """Run a bounded batch and retain all completed sibling results."""
        event_queue: asyncio.Queue[Any] = asyncio.Queue()
        parallel_semaphore = asyncio.Semaphore(max(1, self._max_parallel_tools))
        web_search_semaphore = asyncio.Semaphore(self._web_search_concurrency)
        state = ToolExecutionState(last_activity_at=perf_counter())

        async def run_one(
            index: int,
            request: ToolInvocationRequest,
        ) -> ToolInvocationCompleted:
            if request.immediate_result is not None:
                return ToolInvocationCompleted(
                    call_id=request.call_id,
                    index=index,
                    result=request.immediate_result,
                )
            try:
                async with parallel_semaphore:
                    if request.tool_name == self._web_search_tool_name:
                        async with web_search_semaphore:
                            result = await self._invoke_with_optional_events(
                                request,
                                event_queue,
                            )
                    else:
                        result = await self._invoke_with_optional_events(
                            request,
                            event_queue,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = self._failed_result(exc)
            return ToolInvocationCompleted(
                call_id=request.call_id,
                index=index,
                result=result,
            )

        tasks = tuple(
            asyncio.create_task(run_one(index, request))
            for index, request in enumerate(requests)
        )
        timeout_seconds = (
            self._batch_timeout_seconds
            if self._batch_timeout_seconds is not None
            and self._batch_timeout_seconds > 0
            else None
        )
        timeout_deadline = (
            perf_counter() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        try:
            while True:
                all_done = all(task.done() for task in tasks)
                if all_done and event_queue.empty():
                    break
                if (
                    timeout_deadline is not None
                    and not all_done
                    and perf_counter() >= timeout_deadline
                ):
                    state.timed_out = True
                    _log.warning(
                        "parallel tool batch timed out after %.1fs; "
                        "continuing with partial results",
                        timeout_seconds,
                    )
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    break
                if self._is_cancelled() and not state.cancellation_observed:
                    state.cancellation_observed = True
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    break
                try:
                    event = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=self._event_poll_interval_seconds,
                    )
                    yield ToolEngineProgress(event=event)
                    state.last_activity_at = perf_counter()
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                now = perf_counter()
                if (
                    not all_done
                    and now - state.last_activity_at
                    >= self._activity_interval_seconds
                ):
                    yield ToolEngineActivity(tool_name="parallel_tools")
                    state.last_activity_at = now

            while not event_queue.empty():
                yield ToolEngineProgress(event=event_queue.get_nowait())

            if state.timed_out or state.cancellation_observed:
                _done, pending_tasks = await asyncio.wait(
                    tasks,
                    timeout=self._cancel_grace_seconds,
                )
                for task in pending_tasks:
                    task.add_done_callback(self._consume_late_task)
                    task.cancel()
                while not event_queue.empty():
                    yield ToolEngineProgress(event=event_queue.get_nowait())

            outcomes_by_id: dict[str, ToolInvocationCompleted] = {}
            for index, request in enumerate(requests):
                task = tasks[index]
                if not task.done():
                    outcome = ToolInvocationCompleted(
                        call_id=request.call_id,
                        index=index,
                        result=self._missing_result(
                            state=state,
                            timeout_seconds=timeout_seconds,
                        ),
                    )
                else:
                    try:
                        outcome = task.result()
                    except asyncio.CancelledError:
                        outcome = ToolInvocationCompleted(
                            call_id=request.call_id,
                            index=index,
                            result=self._cancelled_result(
                                timed_out=state.timed_out,
                                timeout_seconds=timeout_seconds,
                            ),
                        )
                    except BaseException as exc:
                        if isinstance(exc, self._passthrough_exceptions):
                            raise
                        outcome = ToolInvocationCompleted(
                            call_id=request.call_id,
                            index=index,
                            result=ToolResult(
                                success=False,
                                content="",
                                error=(
                                    "Tool execution failed: "
                                    f"{type(exc).__name__}: {exc!s}"
                                ),
                            ),
                        )
                outcomes_by_id[outcome.call_id] = outcome

            for index, request in enumerate(requests):
                if request.call_id not in outcomes_by_id:
                    outcomes_by_id[request.call_id] = ToolInvocationCompleted(
                        call_id=request.call_id,
                        index=index,
                        result=ToolResult(
                            success=False,
                            content="",
                            error=(
                                "Tool execution interrupted — no result returned."
                            ),
                        ),
                    )

            outcomes = tuple(
                outcomes_by_id[request.call_id]
                for request in requests
            )
            yield ToolBatchCompleted(
                outcomes=outcomes,
                outcomes_by_id=outcomes_by_id,
                timed_out=state.timed_out,
                cancelled=state.cancellation_observed,
            )
        finally:
            for task in tasks:
                if task.done():
                    self._consume_late_task(task)
                else:
                    task.add_done_callback(self._consume_late_task)
                    task.cancel()

    @staticmethod
    def _cancelled_result(
        *,
        timed_out: bool,
        timeout_seconds: float | None,
    ) -> ToolResult:
        if timed_out and timeout_seconds:
            error = (
                f"Tool execution timed out after {timeout_seconds:g}s; "
                "continuing with partial parallel results."
            )
        else:
            error = "Tool execution cancelled before completion."
        return ToolResult(success=False, content="", error=error)

    @classmethod
    def _missing_result(
        cls,
        *,
        state: ToolExecutionState,
        timeout_seconds: float | None,
    ) -> ToolResult:
        if state.timed_out and timeout_seconds:
            return cls._cancelled_result(
                timed_out=True,
                timeout_seconds=timeout_seconds,
            )
        if state.cancellation_observed:
            return cls._cancelled_result(
                timed_out=False,
                timeout_seconds=timeout_seconds,
            )
        return ToolResult(
            success=False,
            content="",
            error="Tool execution interrupted — no result returned.",
        )
