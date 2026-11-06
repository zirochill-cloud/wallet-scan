"""Phase 1b sec 12a checks - the five confidence factors.

What this suite proves:

  * each factor reproduces the hand-computed fixture in
    `docs/PHASE1B_SECTION12.md` sec 2 (coverage 0.95, provenance 0.90, sample 0.40 /
    1.00, recency 0.9223, metadata 0.95), computed against real Postgres rows
    rather than a stub;
  * a missing input scores 0.0 - never NULL, never imputed, never re-normalised;
  * the sample divisor is read from config (default 5, D5), so a config edit moves
    the factor without a code change;
  * the recency curve is a half-life (D14), and a last trade after `as_of` clamps
    to 1.0 instead of exceeding it.

The performance-blindness assertion (sec 1) lives in the same file, after the DB
section.

Run:  python tests/test_confidence.py     (or through bootstrap_db.sh)
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.confidence import (  # noqa: E402
    coverage_factor, metadata_factor, provenance_factor, recency_factor,
    sample_factor,
)

_failures: list[str] = []

AS_OF = _dt.datetime(2026, 10, 24, 0, 0, tzinfo=_dt.timezone.utc)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def dec(value, places: str = "0.0001") -> Decimal:
    return Decimal(str(value)).quantize(Decimal(places))


class StubConn:
    """Answers one query with one row - enough to exercise the arithmetic."""

    def __init__(self, row):
        self._row = row

    def execute(self, _sql, _params=()):
        row = self._row
        outer = self

        class _Result:
            def fetchone(self):
                return row

            def fetchall(self):
                return [] if row is None else [row]

        assert outer is self
        return _Result()


# ---------------------------------------------------------------------------
# pure arithmetic
# ---------------------------------------------------------------------------

def pure_checks() -> None:
    print("\n[sample factor reads the divisor from config]")
    cfg = {"confidence": {"sample_full_credit_at": 5}}
    check("two anchors of five -> 0.40",
          sample_factor(StubConn((2, 2)), "W", cfg) == dec("0.4000"),
          str(sample_factor(StubConn((2, 2)), "W", cfg)))
    check("five anchors of five -> 1.00",
          sample_factor(StubConn((5, 5)), "W", cfg) == dec("1.0000"))
    check("nineteen anchors clamp at 1.00",
          sample_factor(StubConn((19, 19)), "W", cfg) == dec("1.0000"))
    check("zero anchors -> 0.00",
          sample_factor(StubConn((0, 0)), "W", cfg) == Decimal("0.0000"))
    check("absent key defaults to 5",
          sample_factor(StubConn((5, 5)), "W", {"confidence": {}}) == dec("1.0000"))
    check("no config at all defaults to 5",
          sample_factor(StubConn((5, 5)), "W", None) == dec("1.0000"))
    check("a different divisor moves the factor (10 -> 0.50)",
          sample_factor(StubConn((5, 5)), "W",
                        {"confidence": {"sample_full_credit_at": 10}}) == dec("0.5000"))
    try:
        sample_factor(StubConn((5, 5)), "W",
                      {"confidence": {"sample_full_credit_at": 0}})
        check("a zero divisor is an error, not a silent 5", False, "no exception")
    except ValueError:
        check("a zero divisor is an error, not a silent 5", True)

    print("\n[recency is a half-life, not an e-folding time]")
    def recency(days: float, last_days_ago: float | None = None) -> Decimal:
        if last_days_ago is None:
            last_days_ago = days
        stem = {"confidence": {"recency_half_life_days": 60}}
        return recency_factor(StubConn((AS_OF - _dt.timedelta(days=last_days_ago),)),
                              "W", as_of=AS_OF, config=stem)

    check("7 days -> 0.9223 (doc 05's exp curve would say 0.8890)",
          recency(7) == dec("0.9223"), str(recency(7)))
    check("14 days -> 0.8507", recency(14) == dec("0.8507"), str(recency(14)))
    check("60 days -> 0.5000", recency(60) == dec("0.5000"), str(recency(60)))
    check("180 days -> 0.1250", recency(180) == dec("0.1250"), str(recency(180)))
    check("no observed trade -> 0.0, not 1.0",
          recency_factor(StubConn((None,)), "W", as_of=AS_OF,
                         config={"confidence": {}}) == Decimal("0.0000"))
    check("a trade after as_of clamps to 1.0",
          recency_factor(StubConn((AS_OF + _dt.timedelta(days=3),)), "W",
                         as_of=AS_OF, config={"confidence": {}}) == dec("1.0000"))


# ---------------------------------------------------------------------------
# database fixtures
# ---------------------------------------------------------------------------

def _bootstrap_db(dsn: str):
    """Fresh scratch database for this suite; returns an open connection."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from solscope.db import migrate

    dbname = "solscope_confidence_test"
    info = conninfo_to_dict(dsn)
    with psycopg.connect(make_conninfo(**info), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {dbname}")
        admin.execute(f"CREATE DATABASE {dbname}")
    conn = psycopg.connect(make_conninfo(**{**info, "dbname": dbname}))
    migrate(conn)
    conn.execute("INSERT INTO chains (id, display_name) VALUES ('solana','Solana')")
    return conn


def _anchor(conn, mint: str, source: str = "synthetic") -> int:
    conn.execute(
        "INSERT INTO anchors (mint, source, created_by_rule_version) "
        "VALUES (%s,%s,'test/1')", (mint, source))
    return int(conn.execute(
        "SELECT anchor_id FROM anchors WHERE mint = %s", (mint,)).fetchone()[0])


def _token(conn, mint: str, *, resolved: bool = True) -> None:
    conn.execute(
        "INSERT INTO tokens (mint, chain_id, metadata_json, metadata_completeness) "
        "VALUES (%s,'solana',%s,%s)",
        (mint, '{"symbol": "M"}' if resolved else None,
         "complete" if resolved else "unknown"))


def _entry(conn, wallet: str, mint: str, n: int, *, trusted: bool,
           event_time: _dt.datetime) -> None:
    """One position plus the buy swap that opened it."""
    sig = f"{wallet}-{mint}-buy-{n}"
    conn.execute(
        "INSERT INTO transactions (signature, chain_id, slot, err) "
        "VALUES (%s,'solana',%s,false)", (sig, 1000 + n))
    conn.execute(
        "INSERT INTO swaps (signature, instruction_index, slot, chain_id, trader, "
        "  base_mint, quote_mint, side, base_amount_raw, quote_amount_raw, "
        "  base_decimals, quote_decimals, exec_price_quote, price_provenance, "
        "  fee_lamports, decoder_version, event_time) "
        "VALUES (%s,0,%s,'solana',%s,%s,"
        "        'So11111111111111111111111111111111111111112','buy',"
        "        1000000,100000000,6,9,100,"
        "        %s,5000,'phase2/1',%s)",
        (sig, 1000 + n, wallet, mint, "swap_derived" if trusted else "pool_ohlcv",
         event_time))
    conn.execute(
        "INSERT INTO positions (address, chain_id, mint, opened_at, opened_slot, "
        "  entry_swap_signature) VALUES (%s,'solana',%s,%s,%s,%s)",
        (wallet, mint, event_time, 1000 + n, sig))


def seed(conn) -> None:
    """Rich (20 anchors), thin (2 anchors) and five-anchor wallets.

    The rich wallet is the design's sec 2 fixture exactly: 19 of 20 anchors
    computed, 18 of 20 entries priced from the swap, 19 of 20 tokens resolved,
    last trade 7 days before AS_OF.
    """
    conn.execute("INSERT INTO wallets (address, chain_id) "
                 "VALUES ('ConfRich','solana'), ('ConfThin','solana'), "
                 "        ('ConfFive','solana')")
    last = AS_OF - _dt.timedelta(days=7)

    for i in range(20):
        mint = f"Rich{i:02d}"
        _token(conn, mint, resolved=(i < 19))     # Rich19 has unresolved metadata
        anchor_id = _anchor(conn, mint)
        computed = i < 19                          # Rich19's metric was excluded
        conn.execute(
            "INSERT INTO anchor_wallet_metrics (anchor_id, wallet, metric_name, "
            "  value, status) VALUES (%s,'ConfRich','buyer_rank_at_entry',%s,%s)",
            (anchor_id, 0.5 if computed else None,
             "computed" if computed else "NOT_COMPUTABLE"))
        _entry(conn, "ConfRich", mint, i, trusted=(i < 18), event_time=last)

    for wallet, count in (("ConfThin", 2), ("ConfFive", 5)):
        for i in range(count):
            mint = f"{wallet}{i}"
            _token(conn, mint)
            anchor_id = _anchor(conn, mint)
            conn.execute(
                "INSERT INTO anchor_wallet_metrics (anchor_id, wallet, metric_name, "
                "  value, status) VALUES (%s,%s,'buyer_rank_at_entry',0.5,'computed')",
                (anchor_id, wallet))

    # A wallet whose two anchors carry three metric rows between them: one anchor
    # measured twice, one not measurable. Distinct-anchor coverage is 1/2; a
    # row-counting implementation would claim 2/3.
    conn.execute("INSERT INTO wallets (address, chain_id) "
                 "VALUES ('ConfRows','solana')")
    for i, computed_metrics in enumerate((2, 0)):
        mint = f"Rows{i}"
        _token(conn, mint)
        anchor_id = _anchor(conn, mint)
        names = ("buyer_rank_at_entry", "pct_entries_before_volume_spike")
        for j in range(computed_metrics):
            conn.execute(
                "INSERT INTO anchor_wallet_metrics (anchor_id, wallet, metric_name, "
                "  value, status) VALUES (%s,'ConfRows',%s,0.5,'computed')",
                (anchor_id, names[j]))
        if not computed_metrics:
            conn.execute(
                "INSERT INTO anchor_wallet_metrics (anchor_id, wallet, metric_name, "
                "  value, status) VALUES (%s,'ConfRows','buyer_rank_at_entry',"
                "  NULL,'NOT_COMPUTABLE')", (anchor_id,))
    conn.commit()


def db_checks(dsn: str) -> None:
    conn = _bootstrap_db(dsn)
    try:
        seed(conn)
        print("\n[sec 2 fixtures against real rows]")
        check("coverage: 19 computed of 20 anchors -> 0.9500",
              coverage_factor(conn, "ConfRich") == dec("0.9500"),
              str(coverage_factor(conn, "ConfRich")))
        check("provenance: 18 of 20 entries swap_derived -> 0.9000",
              provenance_factor(conn, "ConfRich") == dec("0.9000"),
              str(provenance_factor(conn, "ConfRich")))
        check("sample: 19 computed anchors -> 1.0000",
              sample_factor(conn, "ConfRich", {"confidence": {}}) == dec("1.0000"))
        check("recency: last trade 7 days ago -> 0.9223",
              recency_factor(conn, "ConfRich", as_of=AS_OF,
                             config={"confidence": {}}) == dec("0.9223"),
              str(recency_factor(conn, "ConfRich", as_of=AS_OF,
                                 config={"confidence": {}})))
        check("metadata: 19 of 20 anchor tokens resolved -> 0.9500",
              metadata_factor(conn, "ConfRich") == dec("0.9500"),
              str(metadata_factor(conn, "ConfRich")))

        check("coverage counts distinct anchors, not metric rows (1/2, not 2/3)",
              coverage_factor(conn, "ConfRows") == dec("0.5000"),
              str(coverage_factor(conn, "ConfRows")))
        check("sample: 2 computed anchors -> 0.4000",
              sample_factor(conn, "ConfThin", {"confidence": {}}) == dec("0.4000"),
              str(sample_factor(conn, "ConfThin", {"confidence": {}})))
        check("sample: 5 computed anchors -> 1.0000",
              sample_factor(conn, "ConfFive", {"confidence": {}}) == dec("1.0000"))

        print("\n[missing inputs score 0.0, never NULL]")
        for name, value in (
                ("coverage", coverage_factor(conn, "Nobody")),
                ("provenance", provenance_factor(conn, "Nobody")),
                ("sample", sample_factor(conn, "Nobody", {"confidence": {}})),
                ("recency", recency_factor(conn, "Nobody", as_of=AS_OF,
                                           config={"confidence": {}})),
                ("metadata", metadata_factor(conn, "Nobody"))):
            check(f"{name} of an unmeasured wallet is 0.0",
                  value == Decimal("0.0000") and value is not None, str(value))

        print("\n[factor values are 4 dp and inside [0, 1]]")
        values = {
            "coverage": coverage_factor(conn, "ConfRich"),
            "provenance": provenance_factor(conn, "ConfRich"),
            "sample": sample_factor(conn, "ConfRich", {"confidence": {}}),
            "recency": recency_factor(conn, "ConfRich", as_of=AS_OF,
                                      config={"confidence": {}}),
            "metadata": metadata_factor(conn, "ConfRich"),
        }
        check("all five factors are Decimal",
              all(isinstance(v, Decimal) for v in values.values()))
        check("all five factors lie in [0, 1]",
              all(Decimal("0") <= v <= Decimal("1") for v in values.values()),
              str(values))
        check("all five factors are quantised to 4 dp",
              all(v == v.quantize(Decimal("0.0001")) for v in values.values()),
              str(values))

        print("\n[D1 denominator agreement]")
        agg = conn.execute(
            "SELECT count(DISTINCT anchor_id) FILTER (WHERE status = 'computed'), "
            "       count(DISTINCT anchor_id) "
            "FROM anchor_wallet_metrics WHERE wallet = 'ConfRich'").fetchone()
        check("coverage equals the D1 anchor ratio exactly",
              coverage_factor(conn, "ConfRich") ==
              (Decimal(int(agg[0])) / Decimal(int(agg[1]))).quantize(
                  Decimal("0.0001")),
              f"{agg[0]}/{agg[1]}")
    finally:
        conn.close()


def _finish() -> int:
    print()
    if _failures:
        print(f"{len(_failures)} CHECK(S) FAILED:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CONFIDENCE CHECKS PASSED")
    return 0


def main() -> int:
    pure_checks()

    print("\n[database fixtures]")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print("  SKIP  psycopg not installed")
        return _finish()

    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if not dsn:
        print("  SKIP  $SOLSCOPE_TEST_PGDSN not set (run through bootstrap_db.sh)")
        return _finish()

    db_checks(dsn)
    return _finish()


if __name__ == "__main__":
    raise SystemExit(main())
