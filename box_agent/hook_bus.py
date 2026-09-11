"""每次 Run 独立的 Hook 注册、分发、决策和清理实现。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from time import monotonic
from typing import Any
from uuid import uuid4

from .kernel.hook_types import (
    OBSERVER_EVENTS, BeforeToolDecision, BeforeToolResolution, HookContext,
    ResultText, ResultTextDecision, ResultTextResolution, copy_data, freeze_data,
)
from .plugins.hooks import HookOwner, HookSpec
from .session_log import SessionLogDurabilityError


_log = logging.getLogger("box_agent.hooks")
_SUPPRESSED_TEXT = "工具已执行，结果文本已被 Hook 抑制。"
_LEGACY_METHODS = {
    "run.started": "on_agent_start", "step.started": "on_step_start",
    "llm.responded": "on_llm_response", "step.finished": "on_step_end",
    "run.error": "on_error", "run.finished": "on_done",
    "tool.before_execution": "on_tool_start", "tool.after_execution": "on_tool_result",
}


class HookConfigError(ValueError):
    """声明或注册归属不符合契约。"""


class HookStateError(RuntimeError):
    """总线阶段或运行身份不允许当前操作。"""


@dataclass(frozen=True, slots=True)
class RegistrationEntry:
    registration_id: str
    registration_order: int
    spec: HookSpec
    owner: HookOwner


@dataclass(frozen=True, slots=True)
class DispatchLease:
    dispatch_id: str


@dataclass(frozen=True, slots=True)
class HookExecution:
    status: str
    decision: Any = None
    error: str | None = None


class HookRegistration:
    """只撤销本次注册，释放操作幂等。"""

    def __init__(self, bus: HookBus, registration_id: str) -> None:
        self._bus = bus
        self.registration_id = registration_id
        self._disposed = False

    async def dispose(self) -> None:
        if not self._disposed:
            await self._bus._unregister(self.registration_id)
            self._disposed = True


class LegacyHookAdapter:
    """旧回调保留原始数据访问、调用范围和异常语义。"""

    def __init__(self, hook: object) -> None:
        self.hook = hook

    def get_hooks(self) -> tuple[HookSpec, ...]:
        specs = []
        for event, method in _LEGACY_METHODS.items():
            if not callable(getattr(self.hook, method, None)):
                continue
            kind = "observer" if event in OBSERVER_EVENTS else (
                "before_tool" if event == "tool.before_execution" else "result_text"
            )
            specs.append(HookSpec(method, (event,), kind, self))
        return tuple(specs)

    async def handle_legacy(self, event: str, payload: dict[str, Any]) -> Any:
        return await getattr(self.hook, _LEGACY_METHODS[event])(**payload)


def build_hook_context(*, base: HookContext, payload: Mapping, deadline: float | None) -> HookContext:
    """保留调用身份，为当前有效数据重建嵌套只读快照。"""
    return replace(base, payload=payload, deadline=deadline)


def _snapshot_legacy(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """仅把可序列化的事件数据交给新 Handler，服务对象留在旧调用里。"""
    if event == "run.started":
        return {
            "messages": [item.model_dump(mode="json") for item in payload["messages"]],
            "tool_names": list(payload["tools"]), "max_steps": payload["max_steps"],
        }
    if event == "llm.responded":
        return {"response": payload["response"].model_dump(mode="json")}
    result = {}
    for name, value in payload.items():
        if name == "exception":
            result[name] = type(value).__name__ if value is not None else None
        else:
            result[name] = value.value if isinstance(value, Enum) else value
    return result


class HookBus:
    """运行期索引固定，参数、文本和期限由每次分发独立持有。"""

    def __init__(
        self, *, run_id: str | None = None, session_id: str = "",
        is_cancelled: Any = None, run_deadline: float | None = None,
        observer_timeout_ms: int = 1000, interceptor_timeout_ms: int = 5000,
        chain_timeout_ms: int = 10000,
    ) -> None:
        for timeout in (observer_timeout_ms, interceptor_timeout_ms, chain_timeout_ms):
            if type(timeout) is not int or timeout <= 0:
                raise HookConfigError("Hook 时间预算必须是正整数毫秒")
        self.context = HookContext(
            "run.started", run_id or uuid4().hex, session_id=session_id,
            is_cancelled=is_cancelled or (lambda: False), deadline=run_deadline,
        )
        self.state = "Registering"
        self._entries: dict[str, RegistrationEntry] = {}
        self._tokens: list[HookRegistration] = []
        self._index: dict[tuple[str, str], tuple[RegistrationEntry, ...]] = {}
        self._order = 0
        self._dispatches: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()
        self._unobserved_tasks: set[asyncio.Task] = set()
        self._late_durability_errors: list[SessionLogDurabilityError] = []
        self._close_lock = asyncio.Lock()
        self._observer_timeout_ms = observer_timeout_ms
        self._interceptor_timeout_ms = interceptor_timeout_ms
        self._chain_timeout_ms = chain_timeout_ms
        self._step: int | None = None

    @property
    def hooks(self) -> list[Any]:
        """兼容原执行点的非空判断，返回副本避免运行期修改注册。"""
        return [entry.spec.handler for entry in self._entries.values()]

    def register(self, spec: HookSpec, owner: HookOwner) -> HookRegistration:
        if self.state != "Registering":
            raise HookStateError("只能在 Registering 阶段注册 Hook")
        if not isinstance(spec, HookSpec) or not isinstance(owner, HookOwner):
            raise HookConfigError("Hook 声明和归属类型无效")
        if owner.scope != "run" or owner.run_id != self.context.run_id:
            raise HookConfigError("Hook 注册必须属于当前 Run")
        if not all(isinstance(item, str) and item for item in (
            spec.hook_id, owner.plugin_id, owner.plugin_version, owner.source,
        )):
            raise HookConfigError("Hook 标识与来源不能为空")
        if any((entry.owner.plugin_id, entry.spec.hook_id) == (owner.plugin_id, spec.hook_id)
               for entry in self._entries.values()):
            raise HookConfigError(f"重复 Hook：{owner.plugin_id}/{spec.hook_id}")
        valid_events = {
            "observer": OBSERVER_EVENTS,
            "before_tool": {"tool.before_execution"}, "result_text": {"tool.after_execution"},
        }
        if not spec.events or spec.kind not in valid_events or not set(spec.events) <= valid_events[spec.kind]:
            raise HookConfigError("Hook 事件与处理类型不匹配")
        if type(spec.priority) is not int or (
            spec.timeout_ms is not None and (type(spec.timeout_ms) is not int or spec.timeout_ms <= 0)
        ):
            raise HookConfigError("Hook 优先级或期限无效")
        if not isinstance(spec.handler, LegacyHookAdapter):
            for value, method in ((spec.handler, "handle"), (spec.matcher, "matches")):
                if value is None and method == "matches":
                    continue
                callback = getattr(value, method, value)
                if not callable(callback):
                    raise HookConfigError(f"Hook {method} 必须可调用")
        self._order += 1
        registration_id = uuid4().hex
        self._entries[registration_id] = RegistrationEntry(registration_id, self._order, spec, owner)
        token = HookRegistration(self, registration_id)
        self._tokens.append(token)
        return token

    def freeze(self) -> None:
        if self.state == "Ready":
            return
        if self.state != "Registering":
            raise HookStateError("关闭中的 HookBus 不能冻结")
        index: dict[tuple[str, str], list[RegistrationEntry]] = {}
        ordered = sorted(self._entries.values(), key=lambda item: (item.spec.priority, item.registration_order))
        for entry in ordered:
            for event in set(entry.spec.events):
                index.setdefault((event, entry.spec.kind), []).append(entry)
        self._index = {key: tuple(value) for key, value in index.items()}
        self.state = "Ready"

    async def _begin_dispatch(self, context: HookContext) -> DispatchLease:
        # 协程内在首次让出执行权前完成检查与登记，关闭无法插入二者之间。
        if self.state != "Ready" or context.run_id != self.context.run_id:
            raise HookStateError("HookBus 状态或 Run 身份不匹配")
        if context.session_id != self.context.session_id:
            raise HookStateError("HookBus Session 身份不匹配")
        self._raise_late_errors()
        lease = DispatchLease(uuid4().hex)
        self._dispatches[lease.dispatch_id] = asyncio.current_task()
        return lease

    async def _end_dispatch(self, lease: DispatchLease) -> None:
        self._dispatches.pop(lease.dispatch_id, None)

    def _select_entries(self, *, event: str, kind: str, run_id: str) -> tuple[RegistrationEntry, ...]:
        if run_id != self.context.run_id:
            raise HookStateError("Hook Run 身份不匹配")
        return self._index.get((event, kind), ())

    def _effective_deadline(self, *, started_at: float, timeout_ms: int,
                            chain_deadline: float, run_deadline: float | None) -> float:
        values = [started_at + timeout_ms / 1000, chain_deadline]
        if run_deadline is not None:
            values.append(run_deadline)
        if self.context.deadline is not None:
            values.append(self.context.deadline)
        return min(values)

    def _task_finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        unobserved = task in self._unobserved_tasks
        self._unobserved_tasks.discard(task)
        if not task.cancelled():
            error = task.exception()
            if unobserved:
                self._remember_late_error(error)

    def _remember_late_error(self, error: BaseException | None) -> None:
        if isinstance(error, SessionLogDurabilityError) and all(
            error is not previous for previous in self._late_durability_errors
        ):
            self._late_durability_errors.append(error)

    def _raise_late_errors(self) -> None:
        if self._late_durability_errors:
            first, *others = self._late_durability_errors
            self._late_durability_errors.clear()
            if others:
                first.__notes__ = [*getattr(first, "__notes__", ()),
                                   f"Additional Hook drain failures: {others!r}"]
            raise first

    def _abandon_task(self, task: asyncio.Task) -> None:
        self._unobserved_tasks.add(task)
        if task.done():
            self._task_finished(task)
        else:
            task.cancel()

    async def _execute_entry(self, entry: RegistrationEntry, context: HookContext,
                             legacy_payload: dict[str, Any] | None = None) -> HookExecution:
        started = monotonic()
        execution = HookExecution("failed", error="Hook 调用被取消")

        async def invoke() -> HookExecution:
            spec = entry.spec
            if isinstance(spec.handler, LegacyHookAdapter):
                decision = await spec.handler.handle_legacy(context.event, legacy_payload)
                if context.event == "tool.after_execution" and decision is not None:
                    # Match HookManager's protected two-value unpacking. An
                    # invalid legacy return is an ordinary callback failure.
                    content, error = decision
                    decision = ResultText(content, error)
                return HookExecution("completed", decision)
            if spec.matcher is not None:
                matched = await getattr(spec.matcher, "matches", spec.matcher)(context)
                if type(matched) is not bool:
                    raise ValueError("matcher 必须返回 bool")
                if not matched:
                    return HookExecution("skipped")
            decision = await getattr(spec.handler, "handle", spec.handler)(context)
            expected = {
                "observer": type(None), "before_tool": BeforeToolDecision,
                "result_text": ResultTextDecision,
            }
            if not isinstance(decision, expected[spec.kind]):
                raise ValueError("Hook 返回值与声明类型不匹配")
            return HookExecution("completed", decision)

        task = None
        observed = False
        legacy = isinstance(entry.spec.handler, LegacyHookAdapter)
        try:
            if not legacy and context.is_cancelled():
                raise asyncio.CancelledError
            if not legacy and context.deadline is not None and monotonic() >= context.deadline:
                raise asyncio.TimeoutError
            if legacy:
                # 保留旧回调的协程与 ContextVar 语义，不增加单 Hook 期限。
                execution = await invoke()
            else:
                task = asyncio.create_task(invoke())
                self._tasks.add(task)
                task.add_done_callback(self._task_finished)
                while not task.done():
                    if context.is_cancelled():
                        raise asyncio.CancelledError
                    remaining = context.deadline - monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    await asyncio.wait({task}, timeout=min(remaining, 0.05))
                observed = True
                execution = task.result()
                if monotonic() > context.deadline:
                    raise asyncio.TimeoutError
            return execution
        except (asyncio.CancelledError, SessionLogDurabilityError) as error:
            # 取消和持久化失败属于宿主控制信号，不能降级成普通 Hook 决策。
            execution = HookExecution(
                "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                error=type(error).__name__,
            )
            if task is not None and not observed:
                self._abandon_task(task)
            raise
        except Exception as error:
            if task is not None and not observed:
                self._abandon_task(task)
            execution = HookExecution("failed", error=type(error).__name__)
            _log.warning("Hook %s/%s failed: %s", entry.owner.plugin_id, entry.spec.hook_id, error)
            return execution
        finally:
            self._record_execution({
                "event": context.event, "run_id": context.run_id, "session_id": context.session_id,
                "step": context.step, "tool_call_id": context.tool_call_id,
                "plugin_id": entry.owner.plugin_id, "hook_id": entry.spec.hook_id,
                "source": entry.owner.source, "registration_id": entry.registration_id,
                "priority": entry.spec.priority, "status": execution.status,
                "decision": getattr(execution.decision, "action", None),
                "elapsed_ms": (monotonic() - started) * 1000, "error": execution.error,
            })

    def _record_execution(self, record: dict[str, Any]) -> None:
        try:
            _log.debug("hook.execution %s", record)
        except Exception:
            # 诊断失败不改变已形成的业务决策。
            pass

    def _entry_context(self, entry: RegistrationEntry, base: HookContext,
                       payload: Mapping, chain_deadline: float) -> HookContext:
        maximum = self._observer_timeout_ms if entry.spec.kind == "observer" else self._interceptor_timeout_ms
        deadline = self._effective_deadline(
            started_at=monotonic(), timeout_ms=min(entry.spec.timeout_ms or maximum, maximum),
            chain_deadline=chain_deadline, run_deadline=base.deadline,
        )
        return build_hook_context(base=base, payload=payload, deadline=deadline)

    async def observe(self, context: HookContext) -> None:
        await self._observe(context)

    async def _observe(self, context: HookContext, legacy_payload: dict[str, Any] | None = None) -> None:
        lease = await self._begin_dispatch(context)
        try:
            if context.event not in OBSERVER_EVENTS:
                raise HookStateError("observe 只接受观察事件")
            if legacy_payload is None and context.is_cancelled():
                raise asyncio.CancelledError
            deadline = monotonic() + self._chain_timeout_ms / 1000
            for entry in self._select_entries(event=context.event, kind="observer", run_id=context.run_id):
                legacy = isinstance(entry.spec.handler, LegacyHookAdapter)
                if legacy and legacy_payload is None:
                    continue
                if not legacy and legacy_payload is not None and context.is_cancelled():
                    continue
                payload = context.payload
                if legacy_payload is not None and not legacy:
                    payload = _snapshot_legacy(context.event, legacy_payload)
                current = self._entry_context(entry, context, payload, deadline)
                await self._execute_entry(entry, current, legacy_payload)
        finally:
            await self._end_dispatch(lease)

    def _apply_before_decision(self, current: Mapping, decision: BeforeToolDecision,
                               owner: HookOwner, hook_id: str) -> BeforeToolResolution:
        if decision.action == "deny":
            return BeforeToolResolution("deny", reason=decision.reason, code=decision.code,
                                        hook_id=hook_id, source=owner.source)
        return BeforeToolResolution("allow", decision.arguments if decision.action == "modify" else current)

    async def before_tool(self, context: HookContext, arguments: Mapping) -> BeforeToolResolution:
        lease = await self._begin_dispatch(context)
        try:
            if context.event != "tool.before_execution":
                raise HookStateError("before_tool 事件类型错误")
            current = copy_data(freeze_data(arguments))
            deadline = monotonic() + self._chain_timeout_ms / 1000
            for entry in self._select_entries(event=context.event, kind="before_tool", run_id=context.run_id):
                payload = {**context.payload, "arguments": current}
                snapshot = self._entry_context(entry, context, payload, deadline)
                legacy_data = {"tool_call_id": context.tool_call_id,
                               "tool_name": context.payload["tool_name"], "arguments": current}
                execution = await self._execute_entry(entry, snapshot, legacy_data)
                legacy = isinstance(entry.spec.handler, LegacyHookAdapter)
                if execution.status == "skipped" or (legacy and execution.status == "failed"):
                    continue
                if legacy:
                    if execution.decision is not None:
                        current = execution.decision
                    continue
                decision = execution.decision if execution.status == "completed" else BeforeToolDecision.deny(
                    f"Hook {entry.spec.hook_id} 执行失败：{execution.error}", "HOOK_EXECUTION_FAILED",
                )
                resolution = self._apply_before_decision(current, decision, entry.owner, entry.spec.hook_id)
                if resolution.action == "deny":
                    return resolution
                current = copy_data(resolution.arguments)
            return BeforeToolResolution("allow", current)
        finally:
            await self._end_dispatch(lease)

    def _apply_text_decision(self, current: ResultText, decision: ResultTextDecision,
                             owner: HookOwner, hook_id: str) -> ResultTextResolution:
        if decision.action == "suppress":
            return ResultTextResolution(
                ResultText(_SUPPRESSED_TEXT), True, decision.reason, hook_id, owner.source, True,
            )
        return ResultTextResolution(decision.text if decision.action == "replace" else current,
                                    modified=decision.action == "replace")

    async def after_tool(self, context: HookContext, result_text: ResultText) -> ResultTextResolution:
        lease = await self._begin_dispatch(context)
        try:
            if context.event != "tool.after_execution":
                raise HookStateError("after_tool 事件类型错误")
            current = result_text
            modified = False
            deadline = monotonic() + self._chain_timeout_ms / 1000
            for entry in self._select_entries(event=context.event, kind="result_text", run_id=context.run_id):
                legacy = isinstance(entry.spec.handler, LegacyHookAdapter)
                # 旧结果回调继续收到可见拒绝结果，新处理器只处理真实执行后的文本。
                if not legacy and context.payload.get("executed") is False:
                    continue
                if legacy and context.payload.get("user_visible") is False:
                    continue
                payload = {**context.payload, "content": current.content, "error": current.error}
                snapshot = self._entry_context(entry, context, payload, deadline)
                legacy_data = {key: payload[key] for key in ("tool_name", "success", "content", "error")}
                legacy_data["tool_call_id"] = context.tool_call_id
                execution = await self._execute_entry(entry, snapshot, legacy_data)
                if execution.status == "skipped" or (legacy and execution.status == "failed"):
                    continue
                if legacy:
                    if execution.decision is not None:
                        current = execution.decision
                    continue
                decision = execution.decision if execution.status == "completed" else ResultTextDecision.suppress(
                    f"Hook {entry.spec.hook_id} 执行失败：{execution.error}",
                )
                resolution = self._apply_text_decision(current, decision, entry.owner, entry.spec.hook_id)
                if resolution.suppressed:
                    if context.payload.get("success") is False:
                        resolution = replace(resolution, text=ResultText(_SUPPRESSED_TEXT, _SUPPRESSED_TEXT))
                    return resolution
                modified = modified or resolution.modified
                current = resolution.text
            return ResultTextResolution(current, modified=modified)
        finally:
            await self._end_dispatch(lease)

    async def _fire(self, event: str, **payload: Any) -> None:
        if event == "step.started":
            self._step = payload["step"]
        context = replace(self.context, event=event, step=self._step)
        await self._observe(context, payload)

    async def fire_agent_start(self, *, messages: list, tools: dict, max_steps: int) -> None:
        await self._fire("run.started", messages=messages, tools=tools, max_steps=max_steps)

    async def fire_step_start(self, *, step: int, max_steps: int) -> None:
        await self._fire("step.started", step=step, max_steps=max_steps)

    async def fire_llm_response(self, *, response: Any) -> None:
        await self._fire("llm.responded", response=response)

    async def fire_step_end(self, *, step: int, elapsed_seconds: float, total_elapsed_seconds: float) -> None:
        await self._fire("step.finished", step=step, elapsed_seconds=elapsed_seconds,
                         total_elapsed_seconds=total_elapsed_seconds)

    async def fire_error(self, *, message: str, is_fatal: bool, exception: Exception | None) -> None:
        await self._fire("run.error", message=message, is_fatal=is_fatal, exception=exception)

    async def fire_done(self, *, stop_reason: Any, final_content: str) -> None:
        await self._fire("run.finished", stop_reason=stop_reason, final_content=final_content)

    async def fire_tool_start(self, *, tool_call_id: str, tool_name: str, arguments: dict) -> dict:
        result = await self.before_tool(replace(
            self.context, event="tool.before_execution", tool_call_id=tool_call_id,
            step=self._step, payload={"tool_name": tool_name},
        ), arguments)
        if result.action == "deny":
            # 旧返回类型无法表达拒绝，禁止将拒绝静默变成允许。
            raise HookStateError("结构化拒绝需要通过 HookDispatchPort 消费")
        return copy_data(result.arguments)

    async def fire_tool_result(self, *, tool_call_id: str, tool_name: str, success: bool,
                               content: str, error: str | None) -> tuple[str, str | None]:
        result = await self.after_tool(replace(
            self.context, event="tool.after_execution", tool_call_id=tool_call_id,
            step=self._step, payload={"tool_name": tool_name, "success": success, "executed": True},
        ), ResultText(content, error))
        return result.text.content, result.text.error

    async def _unregister(self, registration_id: str) -> None:
        if registration_id not in self._entries:
            return
        if self.state not in ("Registering", "Draining"):
            raise HookStateError("运行期间不能撤销 Hook 注册")
        self._entries.pop(registration_id)
        self._index = {}

    async def _drain(self) -> None:
        tasks = set(self._dispatches.values()) | self._tasks
        handlers = set(self._tasks)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            if task in handlers:
                self._abandon_task(task)
            elif not task.done():
                task.cancel()
        if tasks:
            # 超时后仍存活的 Handler 也必须结束，才能销毁其插件实例。
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if task in handlers:
                    self._task_finished(task)
                if isinstance(result, BaseException):
                    self._remember_late_error(result)
            self._tasks.difference_update(task for task in tasks if task.done())

    async def close(self) -> None:
        if asyncio.current_task() in self._tasks or asyncio.current_task() in self._dispatches.values():
            raise HookStateError("Hook 不能在自身分发过程中关闭总线")
        async with self._close_lock:
            if self.state == "Closed":
                return
            self.state = "Draining"
            await self._drain()
            for token in reversed(self._tokens):
                await token.dispose()
            self._tokens.clear()
            self.state = "Closed"
            self._raise_late_errors()
