"""Translate a billable invocation into the host's standard ACP permission request."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

from acp.agent.connection import AgentSideConnection
from acp.schema import AllowedOutcome, PermissionOption, ToolCallUpdate

from box_agent.run_control import RunControl


class BillingConfirmation:
    def __init__(
        self,
        connection: AgentSideConnection,
        session_id: str,
        turn_id: str,
        is_active: Callable[[], bool],
        *,
        control: RunControl,
    ) -> None:
        self.connection = connection
        self.session_id = session_id
        self.turn_id = turn_id
        self.is_active = is_active
        self.control = control
        self.lock = asyncio.Lock()

    async def __call__(self, capability: str) -> bool:
        async with self.lock:
            if not self.is_active():
                return False
            return await self.control.run_until_cancelled(
                lambda: self._request_approval(capability), cancelled_result=False,
            )

    async def _request_approval(self, capability: str) -> bool:
        invocation_id = uuid4().hex
        try:
            response = await asyncio.wait_for(
                self.connection.request_permission(
                    session_id=self.session_id,
                    tool_call=ToolCallUpdate(
                        tool_call_id=invocation_id,
                        raw_input={
                            "scope": "billing",
                            "requested_scope": capability,
                            "turn_id": self.turn_id,
                            "invocation_id": invocation_id,
                        },
                    ),
                    options=[
                        PermissionOption(option_id="approve", name="Confirm", kind="allow_once"),
                        PermissionOption(
                            option_id="approve_session",
                            name="Allow for this conversation",
                            kind="allow_always",
                        ),
                        PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
                    ],
                ),
                timeout=120,
            )
        except Exception:
            return False
        return (
            self.is_active()
            and isinstance(response.outcome, AllowedOutcome)
            and response.outcome.option_id in {"approve", "approve_session"}
        )
