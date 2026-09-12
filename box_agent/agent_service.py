"""Protocol-neutral Agent Service facade for runtime construction."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .agent import Agent
from .agent_runtime import AgentFactory, build_agent
from .agent_run import AgentRunHandle
from .api import RunRequest
from .run_control import PermissionBroker, RunControl


class AgentService:
    """Construct Agent instances from adapter-resolved capabilities.

    The service is intentionally narrow during migration: adapters continue
    to resolve prompts, tools, permissions, and host metadata, while this
    facade provides one stable construction boundary and preserves injectable
    ``Agent`` factories used by tests and downstream hosts.
    """

    def __init__(self, *, agent_factory: AgentFactory = Agent) -> None:
        self._agent_factory = agent_factory

    def create_agent(self, **kwargs: Any) -> Agent:
        """Create one Agent using the shared constructor forwarding helper."""

        try:
            return build_agent(agent_factory=self._agent_factory, **kwargs)
        except BaseException:
            session_log = kwargs.get("session_log")
            if session_log is not None:
                session_log.close()
            raise

    @staticmethod
    def resolve_skill_loader(tools: list[Any]) -> Any:
        """Use the same built-in reader binding as the public Agent constructor."""
        from .plugins.defaults import skill_loader_from_catalog

        return skill_loader_from_catalog({tool.name: tool for tool in tools})

    async def start(
        self,
        request: RunRequest,
        *,
        session: Any,
        options: Any | None = None,
    ) -> AgentRunHandle:
        """Start one protocol-neutral run over an existing AgentSession.

        The first migration keeps AgentSession as the execution owner. The
        returned handle supplies the stable control, event, and result boundary
        that later adapters can use without importing Kernel internals.
        """

        if not isinstance(request, RunRequest):
            raise TypeError("request must be a RunRequest")
        if getattr(session, "_closed", False) or getattr(session, "_closing", False):
            raise RuntimeError("agent session is closed")
        current_handle = getattr(session, "_run_handle", None)
        if (getattr(current_handle, "is_active", False)
                or getattr(session, "_active_run_task", None) is not None):
            raise RuntimeError("session already has an active run")
        control = RunControl()
        if options is None:
            options = session.build_run_options(
                session_id=request.session_id,
                turn_id=request.run_id,
                current_turn_text=request.user_message,
            )
        if hasattr(options, "run_control"):
            options = replace(options, run_control=control)

        permission_broker = (
            getattr(options, "permission_negotiator", None)
            if options is not None
            else None
        )
        if isinstance(permission_broker, PermissionBroker):
            permission_broker.bind_run(
                run_id=request.run_id,
                grant_store=getattr(session, "grant_store", None),
            )

        handle = AgentRunHandle.for_run(
            state=session,
            run_id=request.run_id,
            events_factory=lambda: session.run_events(options=options),
            control=control,
            permission_broker=(
                permission_broker
                if isinstance(permission_broker, PermissionBroker)
                else None
            ),
        )
        if isinstance(permission_broker, PermissionBroker):
            permission_broker.set_event_sink(handle.publish)
        # No await between the ownership check and runner reservation: another
        # start cannot append history or replace this handle in that interval.
        if request.user_message is not None:
            session.agent.add_user_message(request.user_message)
            grant_store = getattr(session, "grant_store", None)
            if grant_store is not None:
                grant_store.clear_prompt_grants()
        # ACP can own a wider prompt spanning preparation and continuations.
        # Its cancellation must survive starting an inner protocol run.
        if not getattr(session, "turn_active", False):
            session.cancelled = False
        session._run_handle = handle
        handle._start()
        return handle


__all__ = ["AgentService"]
