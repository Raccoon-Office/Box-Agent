"""插件提供的 Hook 声明与宿主绑定的注册归属。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..kernel.hook_types import HookEventName, HookKind


@dataclass(frozen=True, slots=True)
class HookSpec:
    """插件中的一项有序 Hook 声明，注册时校验字段。"""

    hook_id: str
    events: tuple[HookEventName, ...]
    kind: HookKind
    handler: Any
    priority: int = 0
    matcher: Any = None
    timeout_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))


@dataclass(frozen=True, slots=True)
class HookOwner:
    """从真实插件描述符与当前 Run 绑定的归属信息。"""

    plugin_id: str
    plugin_version: str
    source: str
    run_id: str
    scope: str = "run"


@runtime_checkable
class HookProviderPort(Protocol):
    """多个 Provider 分别提供声明，装配层统一注册。"""

    def get_hooks(self) -> tuple[HookSpec, ...]: ...
