"""Aggregation tests: hand-computed fixtures plus real-PostgreSQL idempotency.

Two layers, deliberately:

1. **Pure logic** — every fixture from the recovered specification
   (docs/RECOVERED_FROM_TRANSCRIPT.md §7) is hand-computed here, so the reductions
   are checked against arithmetic rather than against the implementation's own
   output. This layer needs no database and runs under system Python.
2. **Database** — ``write_aggregate`` against real PostgreSQL: the same aggregate
   written twice leaves one row, a rule-version bump leaves two, ``not_rankable``
   stores no value, and a failed write leaves the connection usable.

What the fixtures pin down, and why each one is here:

* ``[4,4,4] -> 4.0``: 5x on every anchor compounds to 5x, not 15x.
* ``[99,0,0] -> ~3.64``: one 99x and two duds is worth less than three 5x, which is
  the whole claim behind using a geometric mean.
* ``[1000,4,4] == [100,4,4]``: winsorisation actually caps.
* ``[-1,4,4] -> 0``: a total loss zeroes the product; the recovered spec records the
  result as 0, and that is what the code returns.
* ``[4] -> not_rankable``: one anchor is an anecdote, not a ranking.
* ``[4,4,NC,NC] -> 4.0 with coverage 0.5``: excluded anchors lower coverage without
  changing the number, so "0.5 of this wallet was measurable" is visible.

Run:  python tests/test_aggregation.py
Requires (DB half only): psycopg[binary] and either $SOLSCOPE_TEST_PGDSN or pgserver.
"""
from __future__ import annotations

import os
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.aggregation import (  # noqa: E402
    AggregateError,
    AggregateResult,
    AnchorMetricRow,
    aggregate_buyer_rank_at_entry,
    aggregate_max_subsequent_return,
    aggregate_pct_entries_before_volume_spike,
)

try:
    import psycopg
    import psycopg.errors as E
except ImportError:  # pragma: no cover
    psycopg = None
    E = None

_failures: list[str] = []

MR = "max_subsequent_return"
BR = "buyer_rank_at_entry"
PV = "pct_entries_before_volume_spike"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def close(actual: Decimal | None, expected: str, tol: str = "1e-9") -> bool:
    if actual is None:
        return False
    return abs(actual - Decimal(expected)) <= Decimal(tol)


def rows(values, metric: str = MR, *, window_days: int | None = None) -> list[AnchorMetricRow]:
    """Build one row per value; ``None`` becomes an excluded (NOT_COMPUTABLE) anchor."""
    out: list[AnchorMetricRow] = []
    for i, value in enumerate(values, start=1):
        if value is None:
            out.append(AnchorMetricRow(anchor_id=i, metric_name=metric, status="NOT_COMPUTABLE"))
        else:
            out.append(
                AnchorMetricRow(
                    anchor_id=i,
                    metric_name=metric,
                    status="computed",
                    value=Decimal(str(value)),
                    window_days=window_days,
                )
            )
    return out


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


# --------------------------------------------------------------------------
# 1. pure logic
# --------------------------------------------------------------------------
def logic_checks() -> None:
    print("-- max_subsequent_return ----------------------------------------")

    r = aggregate_max_subsequent_return(rows([4, 4, 4]))
    check("[4, 4, 4] -> 4.0", r.status == "computed" and close(r.value, "4.0"),
          f"status={r.status} value={r.value}")
    check("[4, 4, 4] counts: 3 computed, 0 excluded, coverage 1.0",
          (r.anchor_count_computed, r.anchor_count_excluded, r.coverage) == (3, 0, 1.0),
          f"{r.anchor_count_computed}/{r.anchor_count_excluded}/{r.coverage}")

    r = aggregate_max_subsequent_return(rows([99, 0, 0]))
    check("[99, 0, 0] -> ~3.64", r.status == "computed" and close(r.value, "3.6416", "0.001"),
          f"value={r.value}")

    r5 = aggregate_max_subsequent_return(rows([4, 4, 4, 4, 4]))
    check("[4, 4, 4, 4, 4] -> 4.0", close(r5.value, "4.0"), f"value={r5.value}")
    check("consistent 5x beats a single 99x", r5.value > r.value, f"{r5.value} vs {r.value}")

    capped = aggregate_max_subsequent_return(rows([1000, 4, 4]))
    atcap = aggregate_max_subsequent_return(rows([100, 4, 4]))
    check("[1000, 4, 4] == [100, 4, 4] (winsorise at 100)",
          capped.value == atcap.value, f"{capped.value} vs {atcap.value}")
    check("[1000, 4, 4] -> ~12.617 (capped at 100)",
          close(capped.value, "12.6172", "0.001"), f"value={capped.value}")
    check("winsorised value is visible in the trace",
          any("winsorized_value" in e and e.get("winsorized_value") == "100"
              for e in capped.evidence),
          f"evidence={capped.evidence}")

    r = aggregate_max_subsequent_return(rows([-1, 4, 4]))
    check("[-1, 4, 4] -> 0 (product collapses)", close(r.value, "0"), f"value={r.value}")

    r = aggregate_max_subsequent_return(rows([4]))
    check("[4] -> not_rankable", r.status == "not_rankable" and r.value is None,
          f"status={r.status} value={r.value}")
    check("[4] not_rankable keeps its counts: 1 computed, coverage 1.0",
          (r.anchor_count_computed, r.anchor_count_excluded, r.coverage) == (1, 0, 1.0),
          f"{r.anchor_count_computed}/{r.anchor_count_excluded}/{r.coverage}")

    r = aggregate_max_subsequent_return(rows([4, 4, None, None]))
    check("[4, 4, NC, NC] -> ~4.0", r.status == "computed" and close(r.value, "4.0"),
          f"status={r.status} value={r.value}")
    check("[4, 4, NC, NC] coverage == 0.5", r.coverage == 0.5, f"coverage={r.coverage}")
    check("[4, 4, NC, NC] counts: 2 computed, 2 excluded",
          (r.anchor_count_computed, r.anchor_count_excluded) == (2, 2),
          f"{r.anchor_count_computed}/{r.anchor_count_excluded}")
    check("excluded anchors appear in the evidence trace",
          len(r.evidence) == 4 and [e["included"] for e in r.evidence] == [True, True, False, False],
          f"evidence={r.evidence}")

    r = aggregate_max_subsequent_return(rows([4, 4]), min_anchors=3)
    check("min_anchors=3 with 2 computed -> not_rankable", r.status == "not_rankable",
          f"status={r.status}")
    r = aggregate_max_subsequent_return(rows([4]), min_anchors=1)
    check("min_anchors=1 allows a single anchor", r.status == "computed" and close(r.value, "4.0"),
          f"status={r.status} value={r.value}")

    print("-- buyer_rank_at_entry ------------------------------------------")
    r = aggregate_buyer_rank_at_entry(rows([0.001, 0.001, 0.5], BR))
    check("[0.001, 0.001, 0.5] -> 0.001", close(r.value, "0.001"), f"value={r.value}")
    r = aggregate_buyer_rank_at_entry(rows([0.01, 0.02, 0.5], BR))
    check("[0.01, 0.02, 0.5] -> 0.02 (median, not mean)", close(r.value, "0.02"), f"value={r.value}")
    r = aggregate_buyer_rank_at_entry(rows([0.5, 0.5, 0.001], BR))
    check("[0.5, 0.5, 0.001] -> 0.5", close(r.value, "0.5"), f"value={r.value}")
    r = aggregate_buyer_rank_at_entry(rows([0.1], BR))
    check("[0.1] -> not_rankable", r.status == "not_rankable" and r.value is None,
          f"status={r.status} value={r.value}")
    r = aggregate_buyer_rank_at_entry(rows([0.2, 0.4], BR))
    check("even count uses the midpoint", close(r.value, "0.3"), f"value={r.value}")

    print("-- pct_entries_before_volume_spike ------------------------------")
    r = aggregate_pct_entries_before_volume_spike(rows([0.5, 0.5, 0.5], PV))
    check("[0.5, 0.5, 0.5] -> 0.5", close(r.value, "0.5"), f"value={r.value}")
    r = aggregate_pct_entries_before_volume_spike(rows([1.0, 0.0], PV))
    check("[1.0, 0.0] -> 0.5", close(r.value, "0.5"), f"value={r.value}")
    r = aggregate_pct_entries_before_volume_spike(rows([1.0], PV))
    check("[1.0] -> not_rankable", r.status == "not_rankable", f"status={r.status}")

    print("-- shared preconditions and input validation --------------------")
    r = aggregate_max_subsequent_return([])
    check("empty per_anchor -> not_computable", r.status == "not_computable" and r.value is None,
          f"status={r.status}")
    check("empty per_anchor -> coverage 0.0, not 1.0", r.coverage == 0.0, f"coverage={r.coverage}")

    r = aggregate_max_subsequent_return(rows([None, None]))
    check("all not-computable -> not_computable", r.status == "not_computable",
          f"status={r.status}")
    check("all not-computable -> 2 excluded, coverage 0.0",
          (r.anchor_count_computed, r.anchor_count_excluded, r.coverage) == (0, 2, 0.0),
          f"{r.anchor_count_computed}/{r.anchor_count_excluded}/{r.coverage}")

    mixed = rows([4, 4, None], MR) + rows([0.5, 0.5], BR)
    r = aggregate_max_subsequent_return(mixed)
    check("mixed-metric input is filtered, not mis-summed",
          r.anchor_count_computed == 2 and len(r.evidence) == 3,
          f"computed={r.anchor_count_computed} evidence={len(r.evidence)}")
    check("every metric name is reported on its own result",
          r.metric_name == MR and aggregate_buyer_rank_at_entry(mixed).metric_name == BR,
          f"{r.metric_name}")

    expect(AggregateError,
           lambda: AnchorMetricRow(anchor_id=1, metric_name=MR, status="computed"),
           "a computed row without a value is rejected")
    expect(AggregateError,
           lambda: AnchorMetricRow(anchor_id=1, metric_name=MR, status="NOT_COMPUTABLE",
                                   value=Decimal("4")),
           "a non-computed row carrying a value is rejected")
    expect(AggregateError,
           lambda: AnchorMetricRow(anchor_id=1, metric_name="sharpe", status="NOT_COMPUTABLE"),
           "an unknown metric name is rejected")
    expect(AggregateError,
           lambda: aggregate_max_subsequent_return(rows([4, 4]), min_anchors=0),
           "min_anchors=0 is rejected")

    payload = aggregate_max_subsequent_return(rows([4, 99])).as_dict()
    check("as_dict() is JSON-safe (Decimal -> string)",
          isinstance(payload["value"], str) and payload["status"] == "computed",
          f"{payload['value']!r}")


# --------------------------------------------------------------------------
# 2. database
# --------------------------------------------------------------------------
def db_target():
    """Return (conninfo kwargs, scratch db name), or None when no server is available."""
    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if dsn:
        return {"conninfo": dsn}, "solscope_aggregation"
    try:
        import pgserver
    except ImportError:
        return None
    pgserver.get_server("/tmp/pgdata-solscope")
    return {"host": "/tmp/pgdata-solscope", "user": "postgres"}, "solscope_aggregation"


def count(conn, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")
    return int(conn.execute(sql, params).fetchone()[0])


def db_checks() -> bool:
    """Run the database half. Returns False when it could not run at all."""
    if psycopg is None:
        print("\nSKIP: psycopg not installed — database checks skipped")
        return False
    target = db_target()
    if target is None:
        print("\nSKIP: neither $SOLSCOPE_TEST_PGDSN nor pgserver available")
        return False

    from solscope.anchor_writer import AnchorWriterError, write_aggregate
    from solscope.db import migrate

    conninfo, dbname = target
    admin = dict(conninfo)
    with psycopg.connect(**admin, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
        cur.execute(f"CREATE DATABASE {dbname}")

    conn_kwargs = dict(conninfo)
    dsn_text = conn_kwargs.pop("conninfo", None)
    if dsn_text:
        from psycopg.conninfo import conninfo_to_dict
        conn_kwargs = conninfo_to_dict(dsn_text) or {}
    conn_kwargs["dbname"] = dbname

    print("\n-- write_aggregate (real PostgreSQL) ----------------------------")
    with psycopg.connect(**conn_kwargs) as conn:
        applied = [m.path.name for m in migrate(conn)]
        check("003_wallet_aggregates.sql applied", "003_wallet_aggregates.sql" in applied,
              f"got {applied}")

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            ).fetchall()
        }
        check("table wallet_aggregates exists", "wallet_aggregates" in tables)

        wallet = "WalletAggregate1111111111111111111111111"
        good = aggregate_max_subsequent_return(rows([4, 4, 4], window_days=7))
        write_aggregate(conn, wallet, good, "v1")
        check("first write inserts one row", count(conn, "wallet_aggregates") == 1)

        write_aggregate(conn, wallet, good, "v1")
        check("same aggregate + same rule_version writes once (idempotent)",
              count(conn, "wallet_aggregates") == 1,
              f"rows={count(conn, 'wallet_aggregates')}")

        recomputed = aggregate_max_subsequent_return(rows([4, 4, 4, 4], window_days=7))
        write_aggregate(conn, wallet, recomputed, "v1")
        check("re-run under the same version rewrites, not appends",
              count(conn, "wallet_aggregates") == 1,
              f"rows={count(conn, 'wallet_aggregates')}")
        row = conn.execute(
            "SELECT value, anchor_count_computed FROM wallet_aggregates "
            "WHERE wallet=%s AND rule_version='v1'", (wallet,)
        ).fetchone()
        check("the rewritten row holds the newer counts",
              int(row[1]) == 4 and abs(Decimal(row[0]) - Decimal("4.0")) < Decimal("1e-9"),
              f"{row}")

        write_aggregate(conn, wallet, good, "v2")
        check("a rule_version bump adds a row", count(conn, "wallet_aggregates") == 2,
              f"rows={count(conn, 'wallet_aggregates')}")

        unrankable = aggregate_max_subsequent_return(rows([4], window_days=7))
        write_aggregate(conn, wallet, unrankable, "v1-unrankable")
        stored = conn.execute(
            "SELECT value, status FROM wallet_aggregates WHERE wallet=%s AND rule_version=%s",
            (wallet, "v1-unrankable"),
        ).fetchone()
        check("not_rankable stores a NULL value with its status",
              stored[0] is None and stored[1] == "not_rankable", f"{stored}")

        evidence = conn.execute(
            "SELECT jsonb_array_length(evidence) FROM wallet_aggregates "
            "WHERE wallet=%s AND rule_version='v1'", (wallet,)
        ).fetchone()
        check("the per-anchor trace is stored as a JSON array", int(evidence[0]) == 4,
              f"{evidence}")

        # --- writers reject contradictions before the schema has to -----------
        bogus = AggregateResult(
            metric_name=MR, value=None, status="computed",
            anchor_count_computed=2, anchor_count_excluded=0, coverage=1.0, evidence=[],
        )
        expect(AnchorWriterError,
               lambda: write_aggregate(conn, wallet, bogus, "v3"),
               "computed with no value is rejected by the writer")
        expect(AnchorWriterError,
               lambda: write_aggregate(conn, wallet, good, ""),
               "an empty rule_version is rejected")
        check("the connection survives the rejected writes",
              count(conn, "wallet_aggregates") == 3,
              f"rows={count(conn, 'wallet_aggregates')}")

        # A hand-written row may not break the same invariant at the schema level.
        def insert_bad_row():
            conn.execute(
                "INSERT INTO wallet_aggregates (wallet, metric_name, rule_version, status, "
                "anchor_count_computed, anchor_count_excluded, coverage, value) "
                "VALUES (%s, 'buyer_rank_at_entry', 'v9', 'not_rankable', 1, 0, 1.0, 0.5)",
                (wallet,),
            )
        expect(E.CheckViolation if E else Exception, insert_bad_row,
               "CHECK: not_rankable with a value is refused by the schema")
        conn.rollback()

    return True


def main() -> int:
    logic_checks()
    ran_db = db_checks()

    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for name in _failures:
            print(f"  - {name}")
        return 1
    if not ran_db:
        print("AGGREGATION PURE-LOGIC CHECKS PASSED (database checks skipped)")
        return 0
    print("ALL AGGREGATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
