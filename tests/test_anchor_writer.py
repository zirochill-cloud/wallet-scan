"""Anchor registry writer test: idempotency and constraint behaviour on real PostgreSQL.

What this proves, against a real database rather than a fake connection:

1. ``schema/002_anchor_registry.sql`` applies cleanly after ``001_init.sql`` and the
   migration runner records both.
2. Writing the same anchor / verdict / metric twice leaves **one** row. The
   retrospective pipeline is re-runnable by design, so a second run that doubled the
   evidence would silently weight one run twice in every aggregate.
3. A metric with a NULL ``window_days`` still collides on re-write. This is the case
   a literal four-column PRIMARY KEY could not express (NULs are not equal in a
   unique index), so it is enforced with ``UNIQUE NULLS NOT DISTINCT`` — the assertion
   fails loudly if that ever regresses to plain UNIQUE.
4. A different ``rule_version`` adds a row instead of overwriting history: verdicts
   under different thresholds are not comparable.
5. Bad input fails cleanly — a Python error naming the field for contradictory
   status/value, a foreign-key violation for an unknown anchor, and a CHECK violation
   for a hand-written row that breaks the same invariant at the schema level.

Run:  python tests/test_anchor_writer.py
Requires: psycopg[binary] and pgserver, OR a reachable Postgres via
$SOLSCOPE_TEST_PGDSN. Skips cleanly if neither is available.
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

try:
    import psycopg
    import psycopg.errors as E
except ImportError:  # pragma: no cover
    print("SKIP: psycopg not installed (pip install 'psycopg[binary]')")
    sys.exit(0)

from solscope.anchor_writer import (  # noqa: E402
    AnchorInput,
    AnchorWriterError,
    write_anchor,
    write_metric,
    write_verdict,
)
from solscope.db import migrate  # noqa: E402

_failures: list[str] = []

GENESIS = _dt.datetime(2026, 9, 1, 12, 0, tzinfo=_dt.timezone.utc)
MINT = "MintAnchor1111111111111111111111111111111"
WALLET = "WalletBundled11111111111111111111111111111"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def expect(exc_type, fn, name: str) -> None:
    try:
        fn()
    except exc_type:
        print(f"  PASS  {name}")
        return
    except Exception as other:  # noqa: BLE001
        _failures.append(name)
        print(f"  FAIL  {name}: expected {exc_type.__name__}, got "
              f"{type(other).__name__}: {str(other)[:120]}")
        return
    _failures.append(name)
    print(f"  FAIL  {name}: expected {exc_type.__name__}, but it succeeded")


def expect_sql(conn, exc_type, fn, name: str) -> None:
    """expect(), but for hand-written SQL that aborts the transaction on failure.

    A constraint violation leaves the connection in ``InFailedSqlTransaction``, so
    every later statement would fail for the wrong reason. Rolling back keeps the
    next assertion honest. (The writer helpers roll back their own failures; this is
    only for the raw statements that probe the schema directly.)
    """
    expect(exc_type, fn, name)
    conn.rollback()


def connect():
    """Return conninfo for a scratch database, mirroring the other live tests."""
    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if dsn:
        return {"conninfo": dsn}, "solscope_anchor_writer"

    try:
        import pgserver
    except ImportError:
        print("SKIP: neither $SOLSCOPE_TEST_PGDSN nor pgserver available")
        sys.exit(0)

    pgserver.get_server("/tmp/pgdata-solscope")
    return {"host": "/tmp/pgdata-solscope", "user": "postgres"}, "solscope_anchor_writer"


def count(conn, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")
    row = conn.execute(sql, params).fetchone()
    return int(row[0])


def main() -> int:
    conninfo, dbname = connect()

    admin = dict(conninfo)
    with psycopg.connect(**admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
        cur.execute(f"CREATE DATABASE {dbname}")

    # Split admin from target so every assertion runs inside the scratch database.
    target = dict(conninfo)
    dsn_text = target.pop("conninfo", None)
    if dsn_text:
        from psycopg.conninfo import conninfo_to_dict
        target = conninfo_to_dict(dsn_text) or {}
    target["dbname"] = dbname

    with psycopg.connect(**target) as conn:
        # --- 1. migrations -------------------------------------------------
        applied = migrate(conn)
        names = [m.path.name for m in applied]
        check("001_init.sql applied", "001_init.sql" in names, f"got {names}")
        check("002_anchor_registry.sql applied", "002_anchor_registry.sql" in names, f"got {names}")
        check("migrate is idempotent", migrate(conn) == [])

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        for t in ("anchors", "anchor_wallet_verdicts", "anchor_wallet_metrics"):
            check(f"table {t} exists", t in tables)

        # --- 2. anchors ----------------------------------------------------
        anchor = AnchorInput(
            mint=MINT,
            source="synthetic",
            created_by_rule_version="calibration/v1",
            genesis_time=GENESIS,
            genesis_slot=100,
        )
        first_id = write_anchor(conn, anchor)
        second_id = write_anchor(conn, anchor)
        check("anchors: same mint yields the same anchor_id", first_id == second_id,
              f"{first_id} != {second_id}")
        check("anchors: writing twice leaves one row", count(conn, "anchors") == 1)

        # A later pass that knows MORE fills the gaps.
        write_anchor(conn, AnchorInput(
            mint=MINT, source="synthetic", created_by_rule_version="calibration/v1",
            deployer="DeployerABC",
        ))
        row = conn.execute(
            "SELECT deployer, genesis_time, genesis_slot FROM anchors WHERE mint=%s", (MINT,)
        ).fetchone()
        check("anchors: later write fills a missing deployer", row[0] == "DeployerABC",
              f"got {row[0]!r}")
        check("anchors: later write keeps the known genesis time", row[1] == GENESIS,
              f"got {row[1]!r}")

        # A later pass that knows LESS must not blank what is established.
        write_anchor(conn, AnchorInput(
            mint=MINT, source="dex_screener", created_by_rule_version="calibration/v1",
        ))
        row = conn.execute(
            "SELECT deployer, genesis_time, source FROM anchors WHERE mint=%s", (MINT,)
        ).fetchone()
        check("anchors: a less-informed rewrite does not erase the deployer",
              row[0] == "DeployerABC", f"got {row[0]!r}")
        check("anchors: a less-informed rewrite does not erase genesis",
              row[1] == GENESIS, f"got {row[1]!r}")
        check("anchors: source records the first admission, not the rewrite",
              row[2] == "synthetic", f"got {row[2]!r}")

        expect(AnchorWriterError,
               lambda: AnchorInput(mint=MINT, source="twitter", created_by_rule_version="v1"),
               "anchors: unknown source rejected before the write")

        # --- 3. verdicts ---------------------------------------------------
        evidence = {"anchor_mint": MINT, "genesis_delta_s": 12, "threshold_s": 60}
        write_verdict(
            conn, anchor_id=first_id, wallet=WALLET,
            verdict="wallet.reject_genesis_burst", rule_version="genesis_burst/v1",
            rule_id="rule.genesis_burst", evidence=evidence,
        )
        write_verdict(
            conn, anchor_id=first_id, wallet=WALLET,
            verdict="wallet.reject_genesis_burst", rule_version="genesis_burst/v1",
            rule_id="rule.genesis_burst", evidence={**evidence, "burst_hits": 2},
        )
        check("verdicts: writing twice leaves one row",
              count(conn, "anchor_wallet_verdicts") == 1)
        ev = conn.execute(
            "SELECT evidence FROM anchor_wallet_verdicts WHERE anchor_id=%s AND wallet=%s",
            (first_id, WALLET),
        ).fetchone()[0]
        check("verdicts: the re-run's evidence wins (one answer per version)",
              ev.get("burst_hits") == 2, f"got {ev}")

        check("verdicts: the wallet stub row was created",
              count(conn, "wallets", "address=%s", (WALLET,)) == 1)

        # A new rule version is new history, not a rewrite.
        write_verdict(
            conn, anchor_id=first_id, wallet=WALLET,
            verdict="wallet.accepted", rule_version="genesis_burst/v2",
        )
        check("verdicts: a new rule_version adds a row",
              count(conn, "anchor_wallet_verdicts") == 2)

        expect(AnchorWriterError,
               lambda: write_verdict(
                   conn, anchor_id=first_id, wallet=WALLET,
                   verdict="wallet.accepted", rule_version="genesis_burst/v1",
                   rule_id="rule.genesis_burst"),
               "verdicts: a named rule on a non-rejection is refused")
        expect(AnchorWriterError,
               lambda: write_verdict(
                   conn, anchor_id=first_id, wallet=WALLET,
                   verdict="wallet.reject_genesis_burst", rule_version="genesis_burst/v1"),
               "verdicts: a rejection without its rule is refused")
        expect(AnchorWriterError,
               lambda: write_verdict(
                   conn, anchor_id=first_id, wallet=WALLET,
                   verdict="wallet.maybe", rule_version="genesis_burst/v1"),
               "verdicts: an unknown verdict is refused")

        # Schema-level invariant, written by hand: a rejection must name a rule.
        expect_sql(conn, E.CheckViolation,
                   lambda: conn.execute(
                       "INSERT INTO anchor_wallet_verdicts "
                       "(anchor_id, wallet, verdict, rule_version) "
                       "VALUES (%s, %s, 'wallet.reject_genesis_burst', 'handwritten/v1')",
                       (first_id, WALLET),
                   ),
                   "verdicts: CHECK refuses a rejection with no rule_id")

        # --- 4. metrics ----------------------------------------------------
        write_metric(
            conn, anchor_id=first_id, wallet=WALLET,
            metric_name="max_subsequent_return", window_days=30,
            status="computed", value="4.2500000000", peak_day=3,
        )
        write_metric(
            conn, anchor_id=first_id, wallet=WALLET,
            metric_name="max_subsequent_return", window_days=30,
            status="computed", value="5.0000000000", peak_day=4,
        )
        check("metrics: same window written twice leaves one row",
              count(conn, "anchor_wallet_metrics") == 1)
        val, peak = conn.execute(
            "SELECT value, peak_day FROM anchor_wallet_metrics "
            "WHERE anchor_id=%s AND window_days=30", (first_id,)
        ).fetchone()
        check("metrics: the re-run's value wins", float(val) == 5.0 and peak == 4,
              f"got {val}, {peak}")

        # Distinct windows coexist.
        write_metric(
            conn, anchor_id=first_id, wallet=WALLET,
            metric_name="max_subsequent_return", window_days=7,
            status="INSUFFICIENT_WINDOW",
        )
        check("metrics: a different window is a different row",
              count(conn, "anchor_wallet_metrics") == 2)

        # The NULL-window case: this is what a literal 4-column PK could not key.
        write_metric(
            conn, anchor_id=first_id, wallet=WALLET,
            metric_name="buyer_rank_at_entry", status="computed", value="0.0034",
        )
        write_metric(
            conn, anchor_id=first_id, wallet=WALLET,
            metric_name="buyer_rank_at_entry", status="computed", value="0.0035",
        )
        check("metrics: NULL window_days still collides on re-write",
              count(conn, "anchor_wallet_metrics",
                    "metric_name='buyer_rank_at_entry'") == 1)

        expect(AnchorWriterError,
               lambda: write_metric(
                   conn, anchor_id=first_id, wallet=WALLET,
                   metric_name="pct_entries_before_volume_spike",
                   status="computed"),
               "metrics: computed without a value is refused")
        expect(AnchorWriterError,
               lambda: write_metric(
                   conn, anchor_id=first_id, wallet=WALLET,
                   metric_name="pct_entries_before_volume_spike",
                   status="NO_OBSERVATIONS", value="0.5"),
               "metrics: a non-computed status with a value is refused")
        expect(AnchorWriterError,
               lambda: write_metric(
                   conn, anchor_id=first_id, wallet=WALLET,
                   metric_name="buyer_rank_at_entry", status="computed",
                   value="0.1", window_days=30),
               "metrics: window_days on an unwindowed metric is refused")
        expect(AnchorWriterError,
               lambda: write_metric(
                   conn, anchor_id=first_id, wallet=WALLET,
                   metric_name="max_subsequent_return", status="computed", value="2.0"),
               "metrics: a windowed metric without window_days is refused")

        # Schema-level: computed must carry a value (the same invariant, in SQL).
        expect_sql(conn, E.CheckViolation,
                   lambda: conn.execute(
                       "INSERT INTO anchor_wallet_metrics "
                       "(anchor_id, wallet, metric_name, status) "
                       "VALUES (%s, %s, 'pct_entries_before_volume_spike', 'computed')",
                       (first_id, WALLET),
                   ),
                   "metrics: CHECK refuses computed with a NULL value")

        # --- 5. foreign keys ----------------------------------------------
        # Both writers roll back their own failures, so the connection stays usable
        # and the *next* assertion is not a masked InFailedSqlTransaction.
        expect(E.ForeignKeyViolation,
               lambda: write_metric(
                   conn, anchor_id=999_999, wallet=WALLET,
                   metric_name="buyer_rank_at_entry", status="NOT_COMPUTABLE"),
               "FK: an unknown anchor_id raises ForeignKeyViolation cleanly")

        def insert_verdict_for_unknown_wallet():
            conn.execute(
                "INSERT INTO anchors (mint, source, created_by_rule_version) "
                "VALUES ('MintSecond', 'synthetic', 'v1')"
            )
            conn.execute(
                "INSERT INTO anchor_wallet_verdicts "
                "(anchor_id, wallet, verdict, rule_version) "
                "SELECT anchor_id, 'NoSuchWallet', 'wallet.accepted', 'x/v1' "
                "FROM anchors WHERE mint='MintSecond'"
            )

        expect_sql(conn, E.ForeignKeyViolation, insert_verdict_for_unknown_wallet,
                   "FK: an unknown wallet raises ForeignKeyViolation cleanly")

        # The connection must still work after two rolled-back violations.
        check("the connection is still usable after failed writes",
              count(conn, "anchors") == 1, "MintSecond should have been rolled back")

    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL ANCHOR WRITER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
