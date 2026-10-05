"""Run-state facade over an independent Agent session or legacy state object."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .api import (
    ControlCommand,
    ControlCommandKind,
    EventEnvelope,
    RunResult,
    RunStatus,
    RunDeliveryOptions,
)
from .run_events import RunDeliveryError, RunEventChannel
from .run_result import RunResultCollector
from .run_control import PermissionBroker, RunControl

if TYPE_CHECKING:
    from .config import Config


@dataclass(slots=True)
class AgentRunHandle:
    """A protocol-independent view over one session's mutable run state.

    AgentSession owns the state. This facade also accepts legacy state objects
    so adapters can retain their existing field access and turn signatures.
    """

    _state: Any
    _run_id: str | None = field(default=None, kw_only=True, repr=False)
    _events_factory: Callable[[], Any] | None = field(
        default=None, kw_only=True, repr=False,
    )
    _command_sink: Callable[[ControlCommand], Awaitable[None] | None] | None = field(
        default=None, kw_only=True, repr=False,
    )
    _control: RunControl | None = field(default=None, kw_only=True, repr=False)
    _permission_broker: PermissionBroker | None = field(default=None, kw_only=True, repr=False)
    _channel: RunEventChannel = field(default_factory=RunEventChannel, init=False, repr=False)
    _collector: RunResultCollector | None = field(default=None, init=False, repr=False)
    _runner_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _result_future: asyncio.Future[RunResult] | None = field(
        default=None, init=False, repr=False,
    )
    _events_consumed: bool = field(default=False, init=False, repr=False)
    _result_only: bool = field(default=False, init=False, repr=False)
    _drain_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _runner_error: BaseException | None = field(default=None, init=False, repr=False)
    _delivery_error: RunDeliveryError | None = field(default=None, init=False, repr=False)
    @classmethod
    def for_run(
        cls,
        *,
        state: Any,
        run_id: str,
        events_factory: Callable[[], Any],
        command_sink: Callable[[ControlCommand], Awaitable[None] | None] | None = None,
        control: RunControl | None = None,
        permission_broker: PermissionBroker | None = None,
        delivery_options: RunDeliveryOptions | None = None,
    ) -> "AgentRunHandle":
        """Create a protocol-facing handle while retaining session state access."""

        handle = cls(
            state,
            _run_id=run_id,
            _events_factory=events_factory,
            _command_sink=command_sink,
            _control=control,
            _permission_broker=permission_broker,
        )
        handle._channel = RunEventChannel(delivery_options)
        return handle

    @property
    def run_id(self) -> str:
        """Return the stable identifier of this run."""

        if self._run_id is None:
            return getattr(self._state, "current_turn_id", "")
        return self._run_id

    @property
    def is_active(self) -> bool:
        """Whether this handle currently owns an executing run."""

        return self._runner_task is not None and not self._runner_task.done()

    @property
    def state(self) -> Any:
        """Return the owning session or legacy state object being proxied."""

        return self._state

    @property
    def agent(self) -> Any:
        return self._state.agent

    @property
    def config(self) -> Config | None:
        """Return the owning session's config, if wrapping configured state."""

        return getattr(self._state, "config", None)

    def build_run_options(self, **overrides: Any) -> Any:
        """Build session options, retaining the Agent fallback for legacy state."""

        build_options = getattr(self._state, "build_run_options", None)
        if callable(build_options):
            return build_options(**overrides)
        return replace(self.agent.default_run_options(), **overrides)

    @property
    def cancelled(self) -> bool:
        return self._state.cancelled

    @cancelled.setter
    def cancelled(self, value: bool) -> None:
        self._state.cancelled = value

    @property
    def inject_queue(self) -> asyncio.Queue[Any]:
        return self._state.inject_queue

    @property
    def turn_active(self) -> bool:
        return self._state.turn_active

    @turn_active.setter
    def turn_active(self, value: bool) -> None:
        self._state.turn_active = value

    @property
    def control_state(self) -> str:
        """Return the run control state for SDK/host observability."""

        return self._control.state if self._control is not None else "legacy"

    @property
    def current_turn_id(self) -> str:
        return self._state.current_turn_id

    @current_turn_id.setter
    def current_turn_id(self, value: str) -> None:
        self._state.current_turn_id = value

    @property
    def goal(self) -> Any:
        """Expose the Agent-owned goal without duplicating it in the handle."""

        return self._state.agent.goal

    @property
    def explicitly_allowed_skill_names(self) -> set[str]:
        return self._state.explicitly_allowed_skill_names

    @property
    def skill_selector(self) -> Any:
        return self._state.skill_selector

    @property
    def skill_runtime_context(self) -> Any:
        return self._state.skill_runtime_context

    @property
    def preloaded_skill_names(self) -> list[str]:
        return self._state.preloaded_skill_names

    @property
    def preloaded_skill_hashes(self) -> dict[str, str]:
        return self._state.preloaded_skill_hashes

    @property
    def preloaded_skill_attributions(self) -> dict[str, Any]:
        return self._state.preloaded_skill_attributions

    def _require_run(self) -> None:
        if self._events_factory is None or not self._run_id:
            raise RuntimeError("this AgentRunHandle is a legacy session-state view")

    def _start(self) -> None:
        self._require_run()
        if self._runner_task is None:
            loop = asyncio.get_running_loop()
            self._collector = RunResultCollector(self.run_id)
            self._result_future = loop.create_future()
            self._runner_task = loop.create_task(self._run())

    async def _run(self) -> None:
        try:
            events = self._events_factory()
            try:
                async for payload in events:
                    if self._delivery_error is not None:
                        raise self._delivery_error
                    await self._channel.publish(self.run_id, payload)
                    self._collector.collect(payload)
                if self._delivery_error is not None:
                    raise self._delivery_error
            finally:
                close = getattr(events, "aclose", None)
                if callable(close):
                    await close()
        except BaseException as exc:
            exc = self._delivery_error or exc
            self._runner_error = exc
            # Closing a handle after DoneEvent may cancel pending cleanup.
            if (self._collector.result is None
                    or not isinstance(exc, asyncio.CancelledError)):
                self._collector.result = RunResult(
                    run_id=self.run_id,
                    status=RunStatus.CANCELLED if isinstance(exc, asyncio.CancelledError)
                    else RunStatus.FAILED,
                    stop_reason="cancelled" if isinstance(exc, asyncio.CancelledError)
                    else "error",
                    final_content="",
                    usage=self._collector.usage,
                    artifacts=tuple(self._collector.artifacts),
                    error={"type": type(exc).__name__, "message": str(exc),
                           **({"code": exc.code} if isinstance(exc, RunDeliveryError) else {})},
                )
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if self._permission_broker is not None:
                self._permission_broker.cancel()
            if self._collector.result is None:
                self._collector.result = RunResult(
                    run_id=self.run_id,
                    status=RunStatus.FAILED,
                    stop_reason="error",
                    final_content="",
                    usage=self._collector.usage,
                    artifacts=tuple(self._collector.artifacts),
                    error={
                        "type": "MissingTerminalEvent",
                        "message": "run ended without a DoneEvent",
                    },
                )
            if self._result_future is not None and not self._result_future.done():
                self._result_future.set_result(self._collector.result)
            self._channel.close()

    async def events(self) -> AsyncIterator[EventEnvelope]:
        """Yield ordered event envelopes for this run."""

        self._start()
        if self._result_only:
            raise RuntimeError("run uses result-only consumption; events are unavailable")
        if self._events_consumed:
            raise RuntimeError("run events already have a consumer")
        self._events_consumed = True
        try:
            while True:
                item = await self._channel.get()
                if item is self._channel.END:
                    if self._runner_error is not None:
                        raise self._runner_error
                    return
                yield item
        finally:
            if self.is_active:
                await self.aclose()

    async def send(self, command: ControlCommand) -> None:
        """Send a host command through the run control boundary."""

        self._start()
        if not isinstance(command, ControlCommand):
            raise TypeError("command must be a ControlCommand")
        if (not self.is_active or self._collector.result is not None
                or getattr(self._state, "_run_handle", self) is not self):
            raise RuntimeError("command does not belong to the active run")
        if command.kind is ControlCommandKind.PERMISSION_RESPONSE:
            if self._permission_broker is None:
                raise RuntimeError("permission response channel is not available")
            if not await self._permission_broker.respond(command):
                raise ValueError("permission response does not match a pending request")
            return
        if command.kind is ControlCommandKind.CANCEL:
            self.request_cancel()
        if self._command_sink is not None:
            result = self._command_sink(command)
            if result is not None:
                await result
            return
        if command.kind is ControlCommandKind.CANCEL:
            request_cancel = getattr(self._state, "request_cancel", None)
            if callable(request_cancel):
                request_cancel()
            else:
                self._state.cancelled = True
            return
        if command.kind is ControlCommandKind.PAUSE:
            if self._control is None:
                raise RuntimeError("run control is not available")
            self._control.request_pause()
            return
        if command.kind is ControlCommandKind.RESUME:
            if self._control is None:
                raise RuntimeError("run control is not available")
            self._control.request_resume()
            return
        if command.kind is ControlCommandKind.INJECT_MESSAGE:
            content = command.payload.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("inject_message command requires message content")
            item: Any = content
            if command.request_id is not None:
                item = {"id": command.request_id, "content": content}
            self._state.inject_queue.put_nowait(item)
            return
        raise NotImplementedError(
            f"control command {command.kind.value!r} is not supported yet"
        )

    def request_cancel(self) -> None:
        """Wake delivery and control waits from a synchronous session cancel."""
        self._channel.interrupt()
        if self._control is not None:
            self._control.request_cancel()
        if self._permission_broker is not None:
            self._permission_broker.cancel()

    async def publish(self, payload: Any) -> None:
        """Publish a host-facing event generated by a run-side broker."""

        try:
            await self._channel.publish(self.run_id, payload)
        except RunDeliveryError as exc:
            self._delivery_error = exc
            if self._control is not None:
                self._control.request_cancel()
            if self._runner_task is not None and self._runner_task is not asyncio.current_task():
                self._runner_task.cancel()
            raise

    async def cancel(self) -> None:
        """Request cooperative cancellation of this run."""

        await self.send(ControlCommand.cancel())

    async def result(self, *, consume_events: bool = True) -> RunResult:
        """Wait for the result; without a stream consumer, consume privately.

        Start iterating events first when the caller needs the event stream.
        Cancelling this waiter never cancels the run or its private consumer.
        consume_events=False 仅观察结果，事件仍由原宿主负责消费。
        """

        self._start()
        # 后台观察者可等待结算而不抢占宿主尚未开始消费的事件流。
        if consume_events and not self._events_consumed and not self._result_only:
            self._result_only = True
            if self._permission_broker is not None:
                self._permission_broker.use_result_only()
            self._drain_task = asyncio.create_task(self._drain_events())
        assert self._result_future is not None
        return await asyncio.shield(self._result_future)

    async def _drain_events(self) -> None:
        while await self._channel.get() is not self._channel.END:
            pass

    async def aclose(self) -> None:
        """Settle the runner and its resources when a host abandons the run."""

        if self._runner_task is None:
            return
        self._channel.interrupt()
        if self._permission_broker is not None:
            self._permission_broker.cancel()
        if not self._runner_task.done():
            self._runner_task.cancel()
        await asyncio.gather(self._runner_task, return_exceptions=True)
        # A task cancelled before its first step never enters _run's finally.
        if self._result_future is not None and not self._result_future.done():
            self._collector.result = RunResult(
                run_id=self.run_id, status=RunStatus.CANCELLED,
                stop_reason="cancelled", final_content="",
            )
            self._result_future.set_result(self._collector.result)
            self._channel.close()
        if self._drain_task is not None:
            await asyncio.gather(self._drain_task, return_exceptions=True)
        if self._permission_broker is not None:
            await self._permission_broker.aclose()

    async def __aenter__(self) -> "AgentRunHandle":
        self._start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()


__all__ = ["AgentRunHandle"]
