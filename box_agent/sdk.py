"""Python SDK over the protocol-neutral run boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agent_run import AgentRunHandle
from .agent_service import AgentService
from .api import RunDeliveryOptions, RunRequest, RunResult

if TYPE_CHECKING:
    from .agent import AgentRunOptions
    from .agent_session import AgentSession


class AgentClient:
    """Run work in a caller-owned Session without depending on CLI or ACP.

    The caller retains Session creation and cleanup. ``start`` exposes streaming
    and control through a handle; ``run`` consumes events and returns the result.
    Use ``RunResult.termination_kind`` to distinguish budgets and interruption
    from normal turn completion. Legacy ``COMPLETED`` is not task verification.
    """

    def __init__(
        self, session: AgentSession, *, service: AgentService | None = None,
    ) -> None:
        self._session = session
        self._service = service if service is not None else AgentService()

    async def start(
        self, request: RunRequest, *, options: AgentRunOptions | None = None,
        delivery_options: RunDeliveryOptions | None = None,
    ) -> AgentRunHandle:
        """Start one run and return its event/control/result handle."""

        return await self._service.start(
            request, session=self._session, options=options, delivery_options=delivery_options,
        )

    async def run(
        self, request: RunRequest, *, options: AgentRunOptions | None = None,
        delivery_options: RunDeliveryOptions | None = None,
    ) -> RunResult:
        """Use result-only delivery, including the host permission callback."""

        handle = await self.start(request, options=options, delivery_options=delivery_options)
        async with handle:
            return await handle.result()


__all__ = ["AgentClient"]
