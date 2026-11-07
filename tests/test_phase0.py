"""Phase 0 exit-criteria tests (doc 07).

Covers the skeleton's real behaviour without needing a live database:

  * ``solscope --help`` works and every scheduled subcommand is registered
  * ``config/budget.yaml`` loads and validates
  * the credit ledger **fails loudly** when a spend would exceed the cap,
    rather than proceeding and degrading the provider account
  * a repeated ``request_key`` is not charged twice (crash-safe retries)
  * an unknown operation is a hard error, not a silent zero-cost charge

The ledger is tested against a fake connection implementing the small DB
surface it needs, so these run with no Postgres present. Run:
    python tests/test_phase0.py
"""
from __future__ import annotations

import datetime as _dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.config import ConfigError, load_budget  # noqa: E402
from solscope.ledger import (  # noqa: E402
    BudgetExceeded,
    CreditLedger,
    period_month_for,
)

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


# --- fake connection ------------------------------------------------------

class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    """Records SQL and serves canned responses for the ledger's queries."""

    def __init__(self):
        self.inserted: list[tuple] = []
        self.request_keys_seen: set[str] = set()
        self.commits = 0
        self.rollbacks = 0
        self.fail_insert = False

    def execute(self, sql: str, params: tuple = ()):  # noqa: D102
        norm = " ".join(sql.split())
        if norm.startswith("SELECT COALESCE(SUM(credits)"):
            provider, period = params
            # inserted tuple: (provider, job, operation, units, credits,
            #                  period_month, request_key, notes)
            total = sum(
                r[4] for r in self.inserted
                if r[0] == provider and r[5] == period
            )
            return FakeCursor([(total,)])
        if norm.startswith("SELECT id, provider, job, operation, units, credits, period_month "
                           "FROM credit_ledger WHERE provider"):
            provider, request_key = params
            for i, r in enumerate(self.inserted):
                if r[0] == provider and len(r) > 7 and r[6] == request_key:
                    return FakeCursor([(i, r[0], r[1], r[2], r[3], r[4], r[6])])
            return FakeCursor([])
        if norm.startswith("INSERT INTO credit_ledger"):
            if self.fail_insert:
                raise RuntimeError("simulated db failure")
            self.inserted.append(params)
            idx = len(self.inserted) - 1
            return FakeCursor([(idx, params[0], params[1], params[2],
                                params[3], params[4], params[5])])
        raise AssertionError(f"unexpected SQL: {norm[:80]}")

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


# --- tests -----------------------------------------------------------------

def test_cli_parser():
    print("\n[CLI]")
    from solscope.cli import STUB_COMMANDS, build_parser

    parser = build_parser()
    # --help must not raise SystemExit(2); argparse raises SystemExit(0) on
    # --help, which is the success path we want to confirm exists.
    try:
        parser.parse_args(["--help"])
        help_ok = False  # unreachable: parse_args exits
    except SystemExit as e:
        help_ok = e.code == 0
    check("solscope --help exits 0", help_ok)

    # Live commands plus every roadmap stub. 'ingest' graduated from stub to
    # live command in Phase 1; the surface itself must stay complete.
    # Live handlers by phase: 0 (migrate/doctor/budget), 1 (ingest), 2 (derive),
    # 3 (positions), 1b (consumer/scores).
    expected = {"migrate", "doctor", "budget", "ingest", "derive",
                "positions", "consumer", "scores"} | set(STUB_COMMANDS)
    actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
    registered = set(actions[0].choices) if actions else set()
    check("every documented subcommand registered", registered == expected,
          f"missing={expected - registered} extra={registered - expected}")

    # Every stub must be callable and return a distinct 'not built' code.
    args = parser.parse_args(["discover"])
    check("stub subcommand parses", args.command == "discover")


def test_budget_config():
    print("\n[budget.yaml]")
    budget = load_budget()
    check("loads without error", budget is not None)
    check("helius present", "helius" in budget.providers)
    check("helius cap is 1,000,000",
          budget.providers["helius"].monthly_cap == 1_000_000)
    check("get_transaction costs 1 credit",
          budget.providers["helius"].cost_of("get_transaction") == 1)
    check("fail_closed defaults true", budget.fail_closed_on_ledger_error is True)

    # getTransfersByAddress must NOT be routable — it errors on the free plan.
    try:
        budget.providers["helius"].cost_of("get_transfers_by_address")
        transfers_blocked = False
    except ConfigError:
        transfers_blocked = True
    check("getTransfersByAddress is not a routable operation", transfers_blocked)

    # Unknown operation must be a hard error, never a silent zero.
    try:
        budget.providers["helius"].cost_of("some_new_operation")
        unknown_is_error = False
    except ConfigError:
        unknown_is_error = True
    check("unknown operation raises ConfigError", unknown_is_error)


def _budget_config(cap=1000, warn_ratio=0.8, costs=None):
    from solscope.config import BudgetConfig, ProviderBudget
    return BudgetConfig(providers={
        "helius": ProviderBudget(
            name="helius",
            monthly_cap=cap,
            warn_ratio=warn_ratio,
            operation_costs=costs or {"get_transaction": 1, "get_signatures_for_address": 1},
        )
    })


def test_ledger_cap_fails_loudly():
    print("\n[ledger: hard cap]")
    conn = FakeConn()
    budget = _budget_config(cap=1000)
    clock = lambda: _dt.datetime(2026, 10, 15, tzinfo=_dt.timezone.utc)
    ledger = CreditLedger(conn, budget, clock=clock)

    # Spend up to the cap exactly.
    ledger.charge(provider="helius", job="j", operation="get_transaction", units=1000)
    check("a spend up to the cap succeeds", True)
    check("ledger row recorded", len(conn.inserted) == 1)

    # One more credit must be refused, loudly, with no partial row.
    try:
        ledger.charge(provider="helius", job="j", operation="get_transaction", units=1)
        raised = None
    except BudgetExceeded as e:
        raised = e
    check("spend past the cap raises BudgetExceeded", raised is not None)
    check("refusal names the remaining budget",
          raised is not None and "remain this month" in str(raised))
    check("refusal writes no ledger row", len(conn.inserted) == 1)
    check("refusal does not commit", conn.commits == 1)

    check("remaining() floors at 0",
          ledger.remaining("helius") == 0)
    check("would_exceed() predicts correctly", ledger.would_exceed("helius", 1) is True)
    check("would_exceed(0) is False at cap", ledger.would_exceed("helius", 0) is False)


def test_ledger_idempotency():
    print("\n[ledger: crash-safe retries]")
    conn = FakeConn()
    budget = _budget_config(cap=1000)
    clock = lambda: _dt.datetime(2026, 10, 15, tzinfo=_dt.timezone.utc)
    ledger = CreditLedger(conn, budget, clock=clock)

    first = ledger.charge(provider="helius", job="j", operation="get_transaction",
                          units=5, request_key="abc")
    second = ledger.charge(provider="helius", job="j", operation="get_transaction",
                           units=5, request_key="abc")
    check("same request_key returns same row id", first.id == second.id)
    check("same request_key charges only once", len(conn.inserted) == 1)
    check("only one commit for a retried charge", conn.commits == 1)


def test_ledger_preflight():
    print("\n[ledger: preflight]")
    conn = FakeConn()
    budget = _budget_config(cap=100)
    clock = lambda: _dt.datetime(2026, 10, 15, tzinfo=_dt.timezone.utc)
    ledger = CreditLedger(conn, budget, clock=clock)

    total = ledger.preflight(provider="helius", job="backfill",
                             operations=[("get_transaction", 60),
                                         ("get_signatures_for_address", 20)])
    check("preflight totals across operations", total == 80)

    try:
        ledger.preflight(provider="helius", job="too-big",
                         operations=[("get_transaction", 200)])
        raised = None
    except BudgetExceeded as e:
        raised = e
    check("preflight over cap raises before any spend", raised is not None)
    check("preflight spent nothing", len(conn.inserted) == 0)


def test_ledger_period():
    print("\n[ledger: billing period]")
    check("mid-month maps to month start",
          period_month_for(_dt.datetime(2026, 10, 17, tzinfo=_dt.timezone.utc))
          == _dt.date(2026, 10, 1))
    check("year boundary",
          period_month_for(_dt.datetime(2026, 12, 31, tzinfo=_dt.timezone.utc))
          == _dt.date(2026, 12, 1))


def test_migrations_discoverable():
    print("\n[migrations]")
    from solscope.db import discover_migrations

    migs = discover_migrations()
    check("at least one migration found", len(migs) >= 1)
    check("migrations are ordered", [m.order for m in migs]
          == sorted(m.order for m in migs))
    check("001_init.sql present", any(m.path.name == "001_init.sql" for m in migs))


def test_consumer_cli():
    print("\n[consumer CLI]")
    import argparse as _argparse
    import contextlib
    import io
    import unittest.mock as mock

    from solscope import cli as cli_mod
    from solscope.cli import Ctx, _HANDLERS, build_parser
    from solscope.consumer import ConsumerReport

    parser = build_parser()
    args = parser.parse_args(["consumer", "--rule-version", "v1",
                              "--as-of", "2026-11-07T00:00:00+00:00", "--dry-run"])
    check("consumer parses --rule-version/--as-of/--dry-run",
          (args.command, args.rule_version, args.dry_run) == ("consumer", "v1", True),
          repr(args))
    check("consumer is a live handler, not a roadmap stub",
          _HANDLERS["consumer"] is cli_mod._cmd_consumer,
          getattr(_HANDLERS["consumer"], "__name__", "?"))

    class _AggConn:
        """Just enough connection for the rule-version default lookup."""

        def __init__(self):
            self.closed = False

        def execute(self, sql, params=()):
            if "wallet_aggregates" not in sql:
                raise AssertionError(f"unexpected SQL: {' '.join(sql.split())[:60]}")
            return FakeCursor([("v1",)])

        def close(self):
            self.closed = True

    conn = _AggConn()
    seen: dict[str, object] = {}
    instant = _dt.datetime(2026, 11, 7, tzinfo=_dt.timezone.utc)

    def fake_run(connection, *, rule_version, as_of=None, dry_run=False, **_kw):
        seen.update(rule_version=rule_version, dry_run=dry_run, as_of=as_of)
        return ConsumerReport(
            rule_version=rule_version,
            config_version="cfg-test",
            as_of=as_of,
            window_start=as_of,
            window_end=as_of,
            wallets_seen=3,
            wallets_scored=2,
            skipped={"W3": "early_entry_not_rankable"},
            bands={"watch": 1, "exclude": 1},
        )

    ctx = Ctx(budget=load_budget(), dsn="host=example", verbose=False)
    out = io.StringIO()
    with mock.patch.object(cli_mod, "connect", lambda _dsn: conn), \
         mock.patch("solscope.consumer.run_consumer", fake_run), \
         contextlib.redirect_stdout(out):
        rc = cli_mod._cmd_consumer(ctx, _argparse.Namespace(
            rule_version=None, as_of=instant, dry_run=True))
    text = out.getvalue()

    check("consumer --dry-run exits 0", rc == 0, text)
    check("it defaults the rule version from wallet_aggregates",
          seen.get("rule_version") == "v1", repr(seen))
    check("--dry-run is passed through, not swallowed",
          seen.get("dry_run") is True, repr(seen))
    check("the connection is closed, not leaked", conn.closed)
    check("the summary prints counts, gated-out reasons and the band split",
          all(token in text for token in
              ("wallets seen   3", "scored         2", "gated out      1",
               "early_entry_not_rankable 1", "watch 1", "exclude 1")),
          text)
    check("every band is listed, so a zero is visible",
          all(name in text for name in ("track", "investigate", "promising_thin")),
          text)


def main() -> int:
    test_cli_parser()
    test_consumer_cli()
    test_budget_config()
    test_ledger_cap_fails_loudly()
    test_ledger_idempotency()
    test_ledger_preflight()
    test_ledger_period()
    test_migrations_discoverable()

    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL PHASE 0 CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
