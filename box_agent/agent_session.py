"""Protocol-independent Agent session state and configuration boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agent import Agent, AgentRunOptions
from .agent_run import AgentRunHandle
from .agent_runtime import AgentFactory, _UNSET
from .agent_service import AgentService
from .config import Config
from .events import AgentEvent, DoneEvent, StopReason
from .execution_profile import ExecutionProfile

if TYPE_CHECKING:
    from .plugins.runtime import PluginRuntime, PluginSession
    from .session_context import HostBindings, SessionOptions
    from .env_context import EnvContext
    from .llm import SessionBoundLLM
    from .session_log import SessionLog
    from .tools.permissions import GrantStore, PermissionEngine
    from .tools.runtime import SkillRuntimeContext
    from .tools.skill_preload import SkillPreloadAttribution
    from .tools.skill_scratch import SkillScratchDirectory


@dataclass
class AgentSession:
    """Own live Agent state without depending on a host protocol.

    ``create`` requires the existing Config explicitly. Keep its reference and
    the existing read timing: constructor settings are resolved once, while
    turn-level policy reads use this session's config. ``None`` is supported
    only for compatibility with callers wrapping an already-created Agent.
    Config is not included in repr; durable facts still belong to SessionLog.
    """

    agent: Agent
    config: Config | None = field(
        default=None, kw_only=True, repr=False, compare=False,
    )
    session_llm: SessionBoundLLM | None = None
    summary_llm: SessionBoundLLM | None = None
    cancelled: bool = False
    skill_scratch_dir: SkillScratchDirectory | None = None
    permission_engine: PermissionEngine | None = None
    grant_store: GrantStore | None = None
    memory_extractor: Any | None = None
    inject_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    turn_active: bool = False
    memory_block: str | None = None
    thinking_enabled: bool = False
    execution_profile: ExecutionProfile = "standard"
    explicitly_allowed_skill_names: set[str] = field(default_factory=set)
    env_context: EnvContext | None = None
    skill_runtime_context: SkillRuntimeContext | None = None
    skill_loader: Any | None = None
    skill_selector: Any | None = None
    force_plan_start: bool = False
    require_plan_approval: bool = False
    pending_plan_approval: dict[str, Any] | None = None
    preloaded_skill_names: list[str] = field(default_factory=list)
    preloaded_skill_hashes: dict[str, str] = field(default_factory=dict)
    preloaded_skill_attributions: dict[str, SkillPreloadAttribution] = field(
        default_factory=dict,
    )
    waiting_for_user_input: bool = False
    turn_counter: int = 0
    continuation_applied: bool = False
    current_turn_id: str = ""
    source_text: str = ""
    last_error: str | None = None
    last_error_code: int | str | None = None
    last_error_category: str | None = None
    last_error_details: dict[str, Any] | None = None
    mcp_fallback_tools: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._run_handle = AgentRunHandle(self)
        self.plugin_session: PluginSession | None = None
        self._owns_plugin_runtime = False
        self._closed = False
        self._closing = False
        self._close_lock = asyncio.Lock()
        self._active_run_task: asyncio.Task | None = None
        self._active_run_finished: asyncio.Future | None = None
        self._run_driving = False
        self._external_run_cleanup: asyncio.Future | None = None
        self._active_events: Any = None
        self._active_activation: Any = None

    @property
    def run_handle(self) -> AgentRunHandle:
        return self._run_handle

    @classmethod
    def create(
        cls,
        *,
        config: Config,
        llm_client: Any,
        system_prompt: str,
        tools: list[Any],
        workspace_dir: str | Path | None = None,
        token_limit: int | None = None,
        hooks: list[Any] | None = None,
        plugins: tuple[Any, ...] = (),
        session_log: SessionLog | None = None,
        session_id: str | None = None,
        skill_runtime: Any = _UNSET,
        utility: bool = False,
        allowed_connector_ids_provider: Callable[[], frozenset[str]] | None = None,
        agent_factory: AgentFactory = Agent,
        **state: Any,
    ) -> AgentSession:
        """Resolve session configuration through the existing Agent factory.

        Hosts supply prepared capabilities and resolved workspace/model
        overrides. Protocol-specific metadata belongs to their subclass.
        """

        settings = config.agent
        capabilities = {}
        if skill_runtime is not _UNSET:
            capabilities["skill_runtime"] = skill_runtime
        if session_id is not None:
            capabilities["session_id"] = session_id
        agent = AgentService(agent_factory=agent_factory).create_agent(
            llm_client=llm_client,
            system_prompt=system_prompt,
            tools=tools,
            hooks=hooks,
            max_steps=settings.max_steps,
            tool_limits=config.tool_limits,
            workspace_dir=str(
                workspace_dir if workspace_dir is not None else settings.workspace_dir
            ),
            token_limit=(
                config.llm.context_token_limit if token_limit is None else token_limit
            ),
            thinking_enabled=state.get("thinking_enabled", False),
            max_parallel_tools=settings.max_parallel_tools,
            parallel_tool_timeout_seconds=settings.parallel_tool_timeout_seconds,
            provider_stale_seconds=settings.provider_stale_seconds,
            memory_promotion_enabled=settings.memory_promotion_proposal_enabled,
            memory_promotion_hit_threshold=settings.memory_promotion_hit_threshold,
            memory_promotion_cooldown_days=settings.memory_promotion_cooldown_days,
            truncation_continuation_enabled=settings.retry_on_suspected_truncation,
            max_truncation_continuations=settings.max_truncation_continuations,
            max_truncated_tool_call_retries=settings.max_truncated_tool_call_retries,
            truncated_tool_call_boost_cap=settings.truncated_tool_call_boost_cap,
            context_resource_dedup_enabled=settings.context_resource_dedup_enabled,
            allowed_connector_ids_provider=allowed_connector_ids_provider,
            deferred_mcp_loading_enabled=(
                not utility
                and config.tools.enable_mcp
                and config.tools.mcp.deferred_loading_enabled
            ),
            session_log=session_log,
            **({"enable_builtin_tools": False} if utility else {}),
            **({"plugins": plugins} if plugins else {}),
            **capabilities,
        )
        return cls(agent=agent, config=config, **state)

    @classmethod
    async def open(
        cls,
        *,
        config: Config,
        options: SessionOptions | None = None,
        host: HostBindings | None = None,
        runtime: PluginRuntime | None = None,
        agent_factory: AgentFactory = Agent,
        **state: Any,
    ) -> AgentSession:
        """Prepare configured plugins, then construct the Agent and this class.

        ``create`` remains the synchronous prepared-resource API. Supplied host
        resources are borrowed; only resources created by plugins are disposed.
        """
        from .composition import _attach_cleanup_error
        from .plugins.runtime import PluginRuntime
        from .session_context import HostBindings, SessionContext, SessionOptions

        options = options or SessionOptions()
        if state:
            options = replace(options, state={**options.state, **state})
        context = SessionContext(config, options, host or HostBindings())
        owns_runtime = runtime is None
        runtime = runtime if runtime is not None else PluginRuntime()
        opening = runtime.begin_open()
        plugins = None
        primary_error = None
        try:
            plugins = await runtime.open_session(context)
            resources = plugins.resources
            session = cls.create(
                config=config,
                agent_factory=agent_factory,
                llm_client=resources.llm_client,
                system_prompt=resources.system_prompt,
                tools=resources.tools,
                workspace_dir=context.workspace,
                token_limit=options.token_limit,
                hooks=resources.hooks,
                session_log=context.host.session_log,
                utility=options.utility,
                **resources.state,
            )
            session.plugin_session = plugins
            session._owns_plugin_runtime = owns_runtime
            plugins.owner = session
            from .session_assembly import finish_session

            await finish_session(session, resources)
            if runtime._closing:
                raise RuntimeError("plugin runtime is closing")
            return session
        except BaseException as error:
            primary_error = error
            if plugins is not None:
                try:
                    await plugins.aclose()
                except BaseException as cleanup_error:
                    _attach_cleanup_error(error, cleanup_error)
            raise
        finally:
            runtime.finish_open(opening)
            if primary_error is not None and owns_runtime:
                try:
                    await runtime.aclose()
                except BaseException as cleanup_error:
                    _attach_cleanup_error(primary_error, cleanup_error)

    def build_run_options(self, **overrides: Any) -> AgentRunOptions:
        """Bind session state before applying explicit host turn overrides."""

        values = {
            "summary_llm": self.summary_llm,
            "is_cancelled": lambda: self.cancelled,
            "inject_queue": self.inject_queue,
        }
        if self.memory_extractor is not None:
            values["memory_extractor"] = self.memory_extractor
        values.update(overrides)
        return replace(self.agent.default_run_options(), **values)

    def request_cancel(self) -> None:
        """Request cooperative cancellation through this session's options."""

        self.cancelled = True

    async def run_events(
        self,
        *,
        options: AgentRunOptions | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run through the public Agent API and close its stream on exit."""

        if self._closed or self._closing:
            raise RuntimeError("agent session is closed")
        if self._active_run_task is not None:
            raise RuntimeError("session already has an active run")
        if (self._run_handle.is_active
                and self._run_handle._runner_task is not asyncio.current_task()):
            raise RuntimeError("session already has an active run")
        self._active_run_task = asyncio.current_task()
        finished = asyncio.get_running_loop().create_future()
        self._active_run_finished = finished
        self._run_driving = True
        self._external_run_cleanup = None
        enclosing_turn_active = self.turn_active
        self.turn_active = True
        self.agent.last_stop_reason = None
        events = None
        activation = None
        primary_error = None
        try:
            effective_options = options if options is not None else self.build_run_options()
            if self.plugin_session is not None:
                from .kernel.ports import KernelServices
                from .session_context import RunContext

                if effective_options.kernel_services is not None:
                    raise ValueError("managed Session owns kernel service binding")
                activation = await self.plugin_session.open_run(RunContext(
                    self.plugin_session.context, self.agent, effective_options,
                ))
                self._active_activation = activation
                effective_options = replace(
                    effective_options,
                    kernel_services=activation.registry.require(KernelServices),
                )
            events = self.agent.run_events(
                options=effective_options,
            )
            self._active_events = events
            async for event in events:
                if isinstance(event, DoneEvent):
                    self.agent.last_stop_reason = event.stop_reason.value
                    self.waiting_for_user_input = (
                        event.stop_reason == StopReason.WAITING_FOR_USER
                    )
                self._run_driving = False
                try:
                    yield event
                finally:
                    # anext()/aclose() may be driven by a different task on
                    # each resume (for example asyncio.wait_for).
                    self._active_run_task = asyncio.current_task()
                    self._run_driving = True
                if self._closing:
                    return
        except GeneratorExit:
            raise
        except BaseException as error:
            primary_error = error
            raise
        finally:
            from .composition import _attach_cleanup_error, _combined_cleanup_error

            errors = []
            try:
                if self._external_run_cleanup is not None:
                    error = await asyncio.shield(self._external_run_cleanup)
                    if error is not None:
                        errors.append(error)
                else:
                    try:
                        await self._close_run_stream(events, activation)
                    except BaseException as error:
                        errors.append(error)
            finally:
                self._active_events = None
                self._active_activation = None
                self._active_run_task = None
                self._active_run_finished = None
                self._run_driving = False
                if not finished.done():
                    finished.set_result(None)
                # ACP can own a wider prompt spanning several continuation runs.
                self.turn_active = enclosing_turn_active
            if errors:
                cleanup_error = _combined_cleanup_error(errors)
                if primary_error is None:
                    raise cleanup_error
                _attach_cleanup_error(primary_error, cleanup_error)

    async def _close_run_stream(self, events: Any, activation: Any) -> None:
        from .composition import _combined_cleanup_error

        errors = []
        if events is not None:
            try:
                await events.aclose()
            except BaseException as error:
                errors.append(error)
        if activation is not None:
            try:
                await self.plugin_session.close_run(activation)
            except BaseException as error:
                errors.append(error)
        if errors:
            raise _combined_cleanup_error(errors)

    async def aclose(self) -> None:
        """Finish the active run, then release only this session's resources."""
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            if self._active_run_task is None and self._run_handle.is_active:
                await self._run_handle.aclose()
            task = self._active_run_task
            if task is not None:
                self.request_cancel()
                if (self._run_driving and task is not asyncio.current_task()
                        and not task.done()):
                    finished = self._active_run_finished
                    task.cancel()
                    # Wait for our generator, not the consumer's entire task:
                    # its finally may itself call Session/runtime.aclose().
                    if finished is not None:
                        await asyncio.shield(finished)
                else:
                    # The consumer may close a stream while suspended at yield.
                    # A resuming consumer joins this cleanup instead of closing
                    # the same asynchronous generator concurrently.
                    cleanup = asyncio.get_running_loop().create_future()
                    self._external_run_cleanup = cleanup
                    cleanup_error = None
                    try:
                        await self._close_run_stream(
                            self._active_events, self._active_activation,
                        )
                    except BaseException as error:
                        cleanup_error = error
                    finally:
                        cleanup.set_result(cleanup_error)
                    self._active_events = None
                    self._active_activation = None
                    self._active_run_task = None
                    self._run_driving = False
                    if self._active_run_finished is not None:
                        if not self._active_run_finished.done():
                            self._active_run_finished.set_result(None)
                        self._active_run_finished = None
                    if cleanup_error is not None:
                        raise cleanup_error
            if self.plugin_session is not None:
                await self.plugin_session.aclose()
                runtime = self.plugin_session.runtime
                if self._owns_plugin_runtime and not runtime._closing:
                    await runtime.aclose()
            self._closed = True
            self.turn_active = False


__all__ = ["AgentSession"]
