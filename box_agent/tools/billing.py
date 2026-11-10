"""Run-scoped host consent for billable capabilities, inherited by child tasks."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar

BillingConfirmation = Callable[[str], Awaitable[bool]]
confirmation: ContextVar[BillingConfirmation | None] = ContextVar("billing_confirmation", default=None)


async def confirm_billing(capability: str) -> bool:
    callback = confirmation.get()
    # CLI and standalone tool consumers retain their own billing policy.
    return await callback(capability) if callback is not None else True
