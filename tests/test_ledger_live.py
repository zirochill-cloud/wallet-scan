"""Live-Postgres ledger test: the Phase 0 exit criterion that unit tests cannot cover.

Doc 07 Phase 0 requires: *"A dummy job writes a `credit_ledger` row, and exceeding
the configured cap **fails the job loudly** instead of degrading the provider
account."*

tests/test_phase0.py proves the ledger's logic against a fake connection. This
proves the same behaviour against a **real PostgreSQL**, through the real
schema, including that the migration runner applies cleanly first.

Run:  python tests/test_ledger_live.py
Requires: psycopg[binary] and pgserver, OR a reachable Postgres via
$SOLSCOPE_TEST_PGDSN. Skips cleanly if neither is available.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import psycopg
except ImportError:  # pragma: no cover
    print("SKIP: psycopg not installed (pip install 'psycopg[binary]')")
    sys.exit(0)

from solscope.config import BudgetConfig, ProviderBudget  # noqa: E402
from solscope.db import migrate  # noqa: E402
from solscope.ledger import BudgetExceeded, CreditLedger  # noqa: E402

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def connect():
    """Return (conninfo_dict, dbname) for a scratch database."""
    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if dsn:
        return {"conninfo": dsn}, "solscope_ledger_live"

    try:
        import pgserver
    except ImportError:
        print("SKIP: neither $SOLSCOPE_TEST_PGDSN nor pgserver available")
        sys.exit(0)

    pgserver.get_server("/tmp/pgdata-solscope")
    return {"host": "/tmp/pgdata-solscope", "user": "postgres"}, "solscope_ledger_live"


def _budget(cap: int) -> BudgetConfig:
    return BudgetConfig(providers={
        "helius": ProviderBudget(
            name="helius",
            monthly_cap=cap,
            warn_ratio=0.8,
            operation_costs={"get_transaction": 1, "get_signatures_for_address": 1},
        )
    })


def main() -> int:
    conninfo, dbname = connect()

    admin = dict(conninfo)
    with psycopg.connect(**admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
        cur.execute(f"CREATE DATABASE {dbname}")

    # Split admin from target: the DSN names the admin database, but every
    # assertion must run inside the scratch database we just created. With a
    # conninfo DSN the previous code skipped the dbname override entirely and
    # migrated `postgres` instead — found 2026-10-26 on a real server.
    target = dict(conninfo)
    dsn_text = target.pop("conninfo", None)
    if dsn_text:
        from psycopg.conninfo import conninfo_to_dict
        target = conninfo_to_dict(dsn_text) or {}
    target["dbname"] = dbname

    with psycopg.connect(**target) as conn:
        # 1. The migration runner must apply the real schema cleanly.
        applied = migrate(conn)
        check("migration runner applies 001_init.sql",
              any(m.path.name == "001_init.sql" for m in applied))

        # Re-running must be a no-op (idempotent).
        again = migrate(conn)
        check("migrate is idempotent", again == [])

        # 2. A dummy job writes a real credit_ledger row.
        ledger = CreditLedger(conn, _budget(cap=1000))
        row = ledger.charge(
            provider="helius", job="dummy_backfill", operation="get_transaction",
            units=50, request_key="dummy-job-001", notes="phase0 exit criterion",
        )
        check("dummy job wrote a ledger row", row.id > 0, f"got id={row.id}")
        check("row records 50 credits", row.credits == 50, f"got {row.credits}")

        # It must be readable back out of the table, not just the RETURNING clause.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*), COALESCE(SUM(credits),0) FROM credit_ledger "
                "WHERE provider='helius' AND job='dummy_backfill'"
            )
            n, total = cur.fetchone()
        check("row is persisted in credit_ledger", n == 1 and total == 50,
              f"count={n} sum={total}")

        check("remaining reflects the spend", ledger.remaining("helius") == 950)

        # 3. Exceeding the cap must fail loudly, and write nothing.
        try:
            ledger.charge(provider="helius", job="runaway_loop",
                          operation="get_transaction", units=5000)
            raised = None
        except BudgetExceeded as exc:
            raised = exc
        check("exceeding the cap raises BudgetExceeded", raised is not None)
        check("the failure names the budget",
              raised is not None and "1000" in str(raised))

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM credit_ledger WHERE job='runaway_loop'")
            rows_after = cur.fetchone()[0]
        check("refused spend wrote no row", rows_after == 0)

        # The original row must be untouched by the refusal.
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(credits),0) FROM credit_ledger")
            total_after = cur.fetchone()[0]
        check("prior spend is unchanged after refusal", total_after == 50,
              f"got {total_after}")

        # 4. A retried request_key must not double-charge, against the real table.
        retry = ledger.charge(provider="helius", job="dummy_backfill",
                              operation="get_transaction", units=50,
                              request_key="dummy-job-001")
        check("retried request_key returns the original row", retry.id == row.id)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM credit_ledger WHERE job='dummy_backfill'")
            dupes = cur.fetchone()[0]
        check("retry did not create a duplicate row", dupes == 1, f"got {dupes}")

    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL LIVE LEDGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
