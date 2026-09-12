"""Per-run control gates shared by hosts and the execution kernel."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from uuid import uuid4

from .api import ControlCommand
from .events import PermissionRequestEvent
from .tools.permissions import GrantStore


class RunControl:
    """Coordinate cooperative pause, resume, and cancellation for one run."""

    def __init__(self) -> None:
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()
        self._state = "running"
        self._cancelled = False

    @property
    def state(self) -> str:
        return self._state

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def request_pause(self) -> None:
        if not self._cancelled and self._state in {"running", "resuming"}:
            self._state = "pausing"
            self._resume_gate.clear()

    def request_resume(self) -> None:
        if not self._cancelled:
            self._state = "resuming"
            self._resume_gate.set()

    def request_cancel(self) -> None:
        self._cancelled = True
        self._state = "cancelled"
        self._resume_gate.set()

    async def checkpoint(self) -> bool:
        """Wait until this run may make its next externally visible action."""

        if self._cancelled:
            return False
        while not self._resume_gate.is_set():
            self._state = "paused"
            await self._resume_gate.wait()
        if self._cancelled:
            return False
        self._state = "running"
        return True


class PermissionBroker:
    """Match host permission responses to one pending request in one run."""

    def __init__(
        self,
        *,
        run_id: str,
        on_request: Callable[[dict[str, object]], Awaitable[None] | None],
        grant_store: GrantStore | None = None,
    ) -> None:
        self._run_id = run_id
        self._on_request = on_request
        self._event_sink: Callable[[PermissionRequestEvent], Awaitable[None] | None] | None = None
        self._pending: dict[str, tuple[dict[str, object], asyncio.Future[str | None]]] = {}
        self._cancelled = False
        self._grant_store = grant_store

    def bind_run(self, *, run_id: str, grant_store: GrantStore | None) -> None:
        """Bind once to the owning run and its existing permission store."""
        if (run_id != self._run_id or self._event_sink is not None
                or self._cancelled or self._pending):
            raise ValueError("permission broker belongs to a different or finished run")
        if self._grant_store is not None and self._grant_store is not grant_store:
            raise ValueError("permission broker belongs to a different grant store")
        self._grant_store = grant_store

    def set_event_sink(
        self,
        sink: Callable[[PermissionRequestEvent], Awaitable[None] | None],
    ) -> None:
        self._event_sink = sink

    async def negotiate(self, permission_request: Mapping[str, object]) -> bool:
        if self._cancelled:
            return False
        request_id = str(permission_request.get("request_id") or uuid4().hex)
        if request_id in self._pending:
            raise ValueError("permission request_id is already pending")
        future = asyncio.get_running_loop().create_future()
        request = {
            **dict(permission_request),
            "run_id": self._run_id,
            "request_id": request_id,
        }
        self._pending[request_id] = (request, future)
        try:
            if self._event_sink is not None:
                notification = self._event_sink(PermissionRequestEvent(
                    tool_call_id=str(request.get("tool_call_id") or ""),
                    scope=str(request.get("scope") or ""),
                    requested_scope=str(request.get("requested_scope") or ""),
                    reason=str(request.get("reason") or ""),
                    path=str(request.get("path") or ""),
                    temporary_supported=bool(request.get("temporary_supported", True)),
                    persistent_supported=bool(request.get("persistent_supported", True)),
                    persistent_label=str(request.get("persistent_label") or ""),
                    command=str(request.get("command") or ""),
                    risk=str(request.get("risk") or ""),
                    request_id=request_id,
                ))
            else:
                notification = self._on_request(request)
            if notification is not None:
                await notification
            grant_scope = await future
            if grant_scope is None or self._cancelled:
                return False
            if self._grant_store is not None:
                return self._grant_store.apply_permission_grant(request, grant_scope)
            # Safety retries need a one-shot decision, not a durable grant.
            return request.get("scope") == "safety"
        except asyncio.CancelledError:
            raise
        finally:
            self._pending.pop(request_id, None)

    async def respond(self, command: ControlCommand) -> bool:
        """Apply one permission command; return false for stale/unknown IDs."""

        request_id = command.request_id
        if not request_id or request_id not in self._pending:
            return False
        option_id = command.payload.get("option_id", command.payload.get("optionId"))
        approved = command.payload.get("approved")
        choices = {
            "approve": "prompt", "approve_session": "session",
            "reject": None, "deny": None, "denied": None,
        }
        if "approved" in command.payload and not isinstance(approved, bool):
            raise ValueError("approved must be a boolean")
        if option_id is not None and (
            not isinstance(option_id, str) or option_id not in choices
        ):
            raise ValueError("unknown permission response option")
        if option_id is None and approved is None:
            raise ValueError("permission_response requires option_id or approved")
        if (option_id is not None and approved is not None
                and approved != (choices[option_id] is not None)):
            raise ValueError("conflicting permission response")
        grant_scope = (
            choices[option_id] if option_id is not None
            else ("prompt" if approved else None)
        )
        request, future = self._pending[request_id]
        if future.done():
            return False
        if grant_scope is not None:
            support_key = "temporary_supported" if grant_scope == "prompt" else "persistent_supported"
            if request.get(support_key, True) is False:
                raise ValueError("permission response option is not supported by this request")
        future.set_result(grant_scope)
        return True

    def cancel(self) -> None:
        self._cancelled = True
        for _request, future in self._pending.values():
            if not future.done():
                future.set_result(None)


__all__ = ["PermissionBroker", "RunControl"]
