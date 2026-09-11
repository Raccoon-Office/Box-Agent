"""Protocol-neutral Agent Service facade for runtime construction."""

from __future__ import annotations

from typing import Any

from .agent import Agent
from .agent_runtime import AgentFactory, build_agent


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


__all__ = ["AgentService"]
