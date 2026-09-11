"""Hook 调用的只读数据、处理决策与运行结果。"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, TypeAlias, Union


JsonValue: TypeAlias = Union[
    None, bool, int, float, str, Mapping[str, "JsonValue"],
    list["JsonValue"], tuple["JsonValue", ...],
]
HookPayload = Mapping[str, JsonValue]
HookKind = Literal["observer", "before_tool", "result_text"]
HookEventName = Literal[
    "run.started", "step.started", "llm.responded", "tool.before_execution",
    "tool.after_execution", "tool.finished", "step.finished", "run.error", "run.finished",
]
OBSERVER_EVENTS = frozenset({
    "run.started", "step.started", "llm.responded", "tool.finished",
    "step.finished", "run.error", "run.finished",
})


def freeze_data(value: JsonValue) -> JsonValue:
    """复制 JSON 数据，并递归冻结容器；拒绝运行时服务对象。"""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("Hook 数据的键必须是字符串")
        return MappingProxyType({key: freeze_data(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_data(item) for item in value)
    raise ValueError(f"Hook 数据必须可表达为 JSON：{type(value).__name__}")


def copy_data(value: JsonValue) -> JsonValue:
    """将只读快照复制为可独立使用的 JSON 容器。"""
    if isinstance(value, Mapping):
        return {key: copy_data(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [copy_data(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class HookContext:
    """一次 Hook 调用的身份、只读 payload 与协作式取消信息。"""

    event: HookEventName
    run_id: str
    session_id: str = ""
    step: int | None = None
    tool_call_id: str | None = None
    payload: HookPayload = field(default_factory=dict)
    deadline: float | None = None
    is_cancelled: Callable[[], bool] = field(default=lambda: False, repr=False, compare=False)
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("box_agent.hooks"), repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id or not isinstance(self.session_id, str):
            raise ValueError("Hook 调用必须具有有效的 Run 和 Session 身份")
        if self.deadline is not None and (
            type(self.deadline) not in (int, float) or not math.isfinite(self.deadline)
        ):
            raise ValueError("Hook 截止时间必须是有限的单调时钟时间点")
        if not isinstance(self.payload, Mapping):
            raise ValueError("Hook payload 必须是对象")
        object.__setattr__(self, "payload", freeze_data(self.payload))


@dataclass(frozen=True, slots=True)
class BeforeToolDecision:
    """执行前决策：保留参数、完整替换参数或拒绝。"""

    action: Literal["allow", "modify", "deny"]
    arguments: Mapping[str, JsonValue] | None = None
    reason: str | None = None
    code: str | None = None

    def __post_init__(self) -> None:
        if self.action == "modify" and isinstance(self.arguments, Mapping):
            if self.reason is None and self.code is None:
                object.__setattr__(self, "arguments", freeze_data(self.arguments))
                return
        elif self.action == "allow":
            if self.arguments is None and self.reason is None and self.code is None:
                return
        elif self.action == "deny" and self.arguments is None:
            if isinstance(self.reason, str) and self.reason and isinstance(self.code, str) and self.code:
                return
        raise ValueError("执行前 Hook 决策无效或字段相互矛盾")

    @classmethod
    def allow(cls) -> BeforeToolDecision:
        return cls("allow")

    @classmethod
    def modify(cls, arguments: Mapping[str, JsonValue]) -> BeforeToolDecision:
        return cls("modify", arguments=arguments)

    @classmethod
    def deny(cls, reason: str, code: str = "HOOK_DENIED") -> BeforeToolDecision:
        return cls("deny", reason=reason, code=code)


@dataclass(frozen=True, slots=True)
class ResultText:
    """可见结果文本；执行成功与否由工具保管。"""

    content: str
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str) or (self.error is not None and not isinstance(self.error, str)):
            raise ValueError("Hook 结果文本必须是字符串")


@dataclass(frozen=True, slots=True)
class ResultTextDecision:
    """结果文本的保留、替换或抑制决策。"""

    action: Literal["keep", "replace", "suppress"]
    text: ResultText | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.action == "keep" and self.text is None and self.reason is None:
            return
        if self.action == "replace" and isinstance(self.text, ResultText) and self.reason is None:
            return
        if self.action == "suppress" and self.text is None and isinstance(self.reason, str) and self.reason:
            return
        raise ValueError("结果文本 Hook 决策无效或字段相互矛盾")

    @classmethod
    def keep(cls) -> ResultTextDecision:
        return cls("keep")

    @classmethod
    def replace(cls, content: str, error: str | None = None) -> ResultTextDecision:
        return cls("replace", text=ResultText(content, error))

    @classmethod
    def suppress(cls, reason: str) -> ResultTextDecision:
        return cls("suppress", reason=reason)


@dataclass(frozen=True, slots=True)
class BeforeToolResolution:
    """整条拦截链的有效参数或拒绝结论。"""

    action: Literal["allow", "deny"]
    arguments: Mapping[str, JsonValue] | None = None
    reason: str | None = None
    code: str | None = None
    hook_id: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        if self.action == "allow" and isinstance(self.arguments, Mapping):
            if all(item is None for item in (self.reason, self.code, self.hook_id, self.source)):
                object.__setattr__(self, "arguments", freeze_data(self.arguments))
                return
        if self.action == "deny" and self.arguments is None:
            if all(isinstance(item, str) and item for item in (self.reason, self.code, self.hook_id, self.source)):
                return
        raise ValueError("Hook 拦截链结果无效")


@dataclass(frozen=True, slots=True)
class ResultTextResolution:
    """文本处理链的结果及抑制来源。"""

    text: ResultText
    suppressed: bool = False
    reason: str | None = None
    hook_id: str | None = None
    source: str | None = None
    modified: bool = False


class ObserverHandler(Protocol):
    async def handle(self, context: HookContext) -> None: ...


class BeforeToolHandler(Protocol):
    async def handle(self, context: HookContext) -> BeforeToolDecision: ...


class ResultTextHandler(Protocol):
    async def handle(self, context: HookContext) -> ResultTextDecision: ...


class HookMatcher(Protocol):
    async def matches(self, context: HookContext) -> bool: ...
