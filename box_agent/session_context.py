"""Explicit configuration and host inputs for managed Agent sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING
from uuid import uuid4

from .config import Config

if TYPE_CHECKING:
    from .agent import Agent, AgentRunOptions


@dataclass(slots=True)
class SessionOptions:
    """Normalized session settings; constructor state is kept by identity."""

    workspace_dir: str | Path | None = None
    token_limit: int | None = None
    utility: bool = False
    resume_session_log: bool = field(default=False, kw_only=True)
    profile: str = "python"
    sandbox_mode: bool = False
    non_interactive: bool = True
    session_mode: str | None = None
    allow_full_access: bool | None = None
    permission_mode: str | None = None
    effective_policy: Any = None
    workspace_layout: Any = None
    process_owner_id: str | None = None
    shell_python_path: str | None = None
    follow_up_suggestions_enabled: bool = False
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HostBindings:
    """Host callbacks and explicitly borrowed capabilities.

    ``tools`` and ``system_prompt`` together support the old prepared-resource
    contract. ``base_tools`` is a borrowed application catalog, to which the
    shared assembly adds session-specific workspace tools.
    """

    llm_client: Any = None
    summary_llm: Any = None
    tools: list[Any] | None = None
    base_tools: list[Any] | None = None
    system_prompt: str | None = None
    skill_loader: Any = None
    memory_manager: Any = None
    hooks: list[Any] | None = None
    session_log: Any = None
    mcp_task: Any = None
    skill_task: Any = None
    mcp_start_gate: Any = None
    output: Callable[[str], Any] | None = None
    base_tools_factory: Callable[..., Any] | None = None
    workspace_tools_factory: Callable[..., Any] | None = None
    capability_state_provider: Callable[[], str] | None = None
    skill_access_filter: Callable[[Any], bool] | None = None
    skill_catalog_filter: Callable[[Any], bool] | None = None
    extra_tools: list[Any] = field(default_factory=list)
    prompt_suffix: str | None = None
    diagnostics: Callable[[str, dict[str, Any]], Any] | None = None


@dataclass(slots=True)
class SessionContext:
    config: Config = field(repr=False)
    options: SessionOptions
    host: HostBindings
    session_key: str = field(default_factory=lambda: uuid4().hex)

    @property
    def workspace(self) -> Path:
        return Path(self.options.workspace_dir or self.config.agent.workspace_dir)

    def emit(self, event: str, **data: Any) -> None:
        """Diagnostic callbacks never alter model behavior or expose config."""
        if self.host.diagnostics is not None:
            try:
                self.host.diagnostics(event, {"session_key": self.session_key, **data})
            except Exception:
                pass


@dataclass(slots=True)
class RunContext:
    session: SessionContext
    agent: Agent
    options: AgentRunOptions
    run_id: str = field(default_factory=lambda: uuid4().hex)


__all__ = ["HostBindings", "RunContext", "SessionContext", "SessionOptions"]
