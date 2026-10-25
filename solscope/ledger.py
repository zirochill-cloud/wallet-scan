"""Credit ledger: record provider spend and enforce a hard monthly cap.

The single most likely way to lose provider access is a runaway loop spending
the monthly budget in an hour (doc 08). This module makes that impossible to
do quietly: every spend is pre-flight checked against the configured cap, and
a job that would exceed it **fails loudly** rather than proceeding at reduced
service or silently burning the account.

Two invariants, both enforced here rather than left to callers:

1. **The cap is a ceiling, not a target.** ``charge()`` refuses before recording
   when the projected spend would cross the cap. It raises ``BudgetExceeded``,
   which callers must not catch and continue past.
2. **Nothing is charged twice.** A ``request_key`` is a normalised fingerprint
   of the request; re-charging the same key is a no-op that returns the
   original row's cost. This makes retries after a crash safe.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Protocol

from .config import BudgetConfig, ProviderBudget


class BudgetExceeded(RuntimeError):
    """A spend would push recorded usage over the provider's monthly cap.

    Deliberately a ``RuntimeError`` subclass with no retry semantics: the
    correct response is to stop the job, not to try again.
    """


class LedgerUnavailable(RuntimeError):
    """The ledger could not be written. See config fail_closed_on_ledger_error."""


@dataclass(frozen=True)
class LedgerRow:
    id: int
    provider: str
    job: str
    operation: str
    units: int
    credits: int
    period_month: _dt.date


class _Connection(Protocol):
    """Minimal DB surface the ledger needs. Keeps this unit-testable."""

    def execute(self, sql: str, params: tuple): ...  # pragma: no cover
    def commit(self) -> None: ...  # pragma: no cover
    def rollback(self) -> None: ...  # pragma: no cover


def period_month_for(when: _dt.datetime) -> _dt.date:
    """First day of the billing month containing ``when``."""
    return _dt.date(when.year, when.month, 1)


class CreditLedger:
    """Append-only spend ledger backed by the ``credit_ledger`` table.

    A ``conn`` exposing ``.execute/.commit/.rollback`` (psycopg, or any DB-API
    connection) is required. Passing ``check_only=True`` turns every operation
    into a dry run for pre-flight cost checks without writing.
    """

    def __init__(self, conn: _Connection, budget: BudgetConfig, *,
                 clock=None, check_only: bool = False) -> None:
        self._conn = conn
        self._budget = budget
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        self._check_only = check_only
        self._warned: set[str] = set()

    # -- reads ---------------------------------------------------------

    def spent(self, provider: str, period: _dt.date) -> int:
        """Credits already recorded for ``provider`` in the billing month."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(credits), 0) FROM credit_ledger "
            "WHERE provider = %s AND period_month = %s",
            (provider, period),
        ).fetchone()
        return int(row[0]) if row else 0

    def remaining(self, provider: str, period: _dt.date | None = None) -> int:
        """Credits still available, floored at 0."""
        budget = self._budget.provider(provider)
        period = period or period_month_for(self._clock())
        return max(0, budget.monthly_cap - self.spent(provider, period))

    def would_exceed(self, provider: str, credits: int,
                     period: _dt.date | None = None) -> bool:
        """True if charging ``credits`` now would cross the monthly cap."""
        budget = self._budget.provider(provider)
        period = period or period_month_for(self._clock())
        return self.spent(provider, period) + credits > budget.monthly_cap

    # -- writes --------------------------------------------------------

    def charge(self, *, provider: str, job: str, operation: str, units: int,
               request_key: str | None = None, notes: str | None = None,
               credits: int | None = None) -> LedgerRow:
        """Record a spend, refusing it if it would cross the cap.

        ``credits`` defaults to ``units × the provider's per-operation cost``.
        Returns the ledger row (existing one if ``request_key`` was already
        charged — this is what makes retries safe).
        """
        budget = self._budget.provider(provider)
        if units < 0:
            raise ValueError(f"units must be non-negative, got {units}")
        cost_per_unit = budget.cost_of(operation)
        total = credits if credits is not None else units * cost_per_unit
        if total < 0:
            raise ValueError(f"credits must be non-negative, got {total}")

        period = period_month_for(self._clock())

        # Idempotency: a retry of an already-charged request must not double-bill.
        if request_key is not None:
            existing = self._conn.execute(
                "SELECT id, provider, job, operation, units, credits, period_month "
                "FROM credit_ledger WHERE provider = %s AND request_key = %s",
                (provider, request_key),
            ).fetchone()
            if existing:
                return LedgerRow(*existing)

        if self._check_only:
            if self.would_exceed(provider, total, period):
                raise BudgetExceeded(
                    f"{provider}: charging {total} credits for {job}/{operation} "
                    f"would exceed the monthly cap of {budget.monthly_cap} "
                    f"(already spent {self.spent(provider, period)})"
                )
            return LedgerRow(-1, provider, job, operation, units, total, period)

        # Pre-flight against the cap. Check BEFORE insert so a refusal leaves
        # no partial row behind.
        already = self.spent(provider, period)
        if already + total > budget.monthly_cap:
            raise BudgetExceeded(
                f"{provider}: {job}/{operation} needs {total} credits but only "
                f"{budget.monthly_cap - already} remain this month "
                f"({already} of {budget.monthly_cap} already spent). "
                f"Refusing to proceed — reduce the funnel or wait for the next "
                f"billing period."
            )

        try:
            row = self._conn.execute(
                "INSERT INTO credit_ledger "
                "(provider, job, operation, units, credits, period_month, request_key, notes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, provider, job, operation, units, credits, period_month",
                (provider, job, operation, units, total, period, request_key, notes),
            ).fetchone()
            self._conn.commit()
        except Exception as exc:
            self._conn.rollback()
            raise LedgerUnavailable(
                f"failed to record {total} credits for {provider}/{job}: {exc}"
            ) from exc

        self._maybe_warn(budget, already + total, period)
        return LedgerRow(*row)

    def preflight(self, *, provider: str, job: str, operations: list[tuple[str, int]],
                  period: _dt.date | None = None) -> int:
        """Total cost of a planned batch, raising if it cannot be afforded.

        ``operations`` is a list of ``(operation_name, units)``. Callers use
        this to reject a job *before* spending anything, rather than dying
        halfway through after burning credits for half the work.
        """
        budget = self._budget.provider(provider)
        period = period or period_month_for(self._clock())
        total = sum(budget.cost_of(op) * units for op, units in operations)
        already = self.spent(provider, period)
        if already + total > budget.monthly_cap:
            raise BudgetExceeded(
                f"{provider}: planned {job} needs {total} credits but only "
                f"{budget.monthly_cap - already} remain this month"
            )
        return total

    def _maybe_warn(self, budget: ProviderBudget, projected: int, period: _dt.date) -> None:
        """Emit a one-time warning when spend crosses the warn_ratio threshold."""
        if budget.monthly_cap <= 0:
            return
        ratio = projected / budget.monthly_cap
        if ratio >= budget.warn_ratio and budget.name not in self._warned:
            self._warned.add(budget.name)
            print(
                f"WARNING: {budget.name} spend has reached {ratio:.0%} of the "
                f"{budget.monthly_cap}-credit monthly cap "
                f"({projected} credits in {period.isoformat()})",
                flush=True,
            )
