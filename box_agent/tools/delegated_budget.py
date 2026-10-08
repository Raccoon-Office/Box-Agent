"""Run-local shared execution quotas propagated only within child scopes."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(eq=False)
class DelegatedBudget:
    limit: int
    used: int = 0


_current: ContextVar[tuple[DelegatedBudget, ...]] = ContextVar("delegated_budgets", default=())


def current_budgets() -> tuple[DelegatedBudget, ...]:
    return _current.get()


@contextmanager
def bind_budgets(budgets: tuple[DelegatedBudget, ...]) -> Iterator[None]:
    token = _current.set(budgets)
    try:
        yield
    finally:
        _current.reset(token)


@dataclass
class BudgetCharge:
    """One logical invocation; reservation and entry contain no await."""

    ledgers: tuple[DelegatedBudget, ...] = ()
    charged: bool = False

    def reserve(self, ledgers: tuple[DelegatedBudget, ...]) -> bool:
        if self.charged:
            return True
        unique = tuple(dict.fromkeys(ledgers))
        if any(ledger.used >= ledger.limit for ledger in unique):
            return False
        for ledger in unique:
            ledger.used += 1
        self.ledgers = unique
        self.charged = True
        return True

    def release_unexecuted(self) -> None:
        if self.charged:
            for ledger in self.ledgers:
                ledger.used -= 1
            self.charged = False
