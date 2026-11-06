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
import re
import sys
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.confidence import (  # noqa: E402
    QUERIES, coverage_factor, metadata_factor, provenance_factor, recency_factor,
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

        blindness_checks(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# performance blindness (S12a-3)
# ---------------------------------------------------------------------------

#: Name fragments that mark a performance column. A confidence factor that reads
#: any of them is no longer orthogonal to wallet quality, whatever its docstring
#: says.
BANNED = ("pnl", "roi", "return", "value", "score", "adjusted")

#: The four tables the factors are allowed to read.
READABLE_TABLES = ("positions", "anchor_wallet_metrics", "swaps", "tokens")

#: Derived/performance tables no factor may touch at all.
FORBIDDEN_TABLES = ("scores", "sub_scores", "wallet_aggregates", "wallet_metrics")


def schema_columns() -> dict[str, set[str]]:
    """Column names per table, parsed from the schema files themselves."""
    text = "".join(
        (ROOT / "schema" / name).read_text(encoding="utf-8")
        for name in ("001_init.sql", "002_anchor_registry.sql"))
    tables: dict[str, set[str]] = {}
    for block in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\);", text, re.S):
        table, body = block.group(1), block.group(2)
        columns = set()
        for line in body.splitlines():
            line = line.strip()
            # Column lines start with the name and a type (TEXT, NUMERIC(38,0), ...).
            match = re.match(r"^([a-z_][a-z0-9_]*)\s+[A-Z]", line)
            if match:
                columns.add(match.group(1))
        tables[table] = columns
    return tables


def _identifiers(sql: str) -> set[str]:
    """Names in a statement, with string literals removed (they are values)."""
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                          re.sub(r"'[^']*'", " ", sql)))


def _banned_hits(sql: str) -> set[str]:
    return {name for name in _identifiers(sql)
            if any(bad in name.lower() for bad in BANNED)}


class RecordingConn:
    """Delegates to the real connection while remembering every statement run."""

    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return self._conn.execute(sql, params)


def blindness_checks(conn) -> None:
    """The five factors must be unable to read a performance number.

    Three independent checks, because each one alone can be fooled: the declared
    queries, the statements actually executed, and the schema those statements
    are checked against. If a later edit adds `positions.realized_pnl_lamports`
    to any factor, this fails rather than quietly making confidence track skill.
    """
    print("\n[performance blindness: factors read no outcome column]")
    tables = schema_columns()
    check("schema parsed for all four readable tables",
          all(t in tables and tables[t] for t in READABLE_TABLES),
          str(sorted(tables)))

    banned_columns = sorted(
        f"{table}.{col}"
        for table in READABLE_TABLES for col in tables.get(table, ())
        if any(bad in col.lower() for bad in BANNED))
    # Non-vacuity: the check must be looking at a schema that really does carry
    # performance columns, or it proves nothing.
    check("the schema really does carry performance columns to avoid",
          len(banned_columns) >= 5, str(banned_columns))
    check("outcome columns really match the banned fragments",
          {"pnl", "roi", "value"} <= {bad for bad in BANNED
                                        for col in banned_columns
                                        if bad in col.lower()},
          str(banned_columns))

    print(f"  note  {len(banned_columns)} performance column(s) in the readable "
          f"tables, e.g. {', '.join(banned_columns[:4])}")

    check("no declared query mentions a banned identifier",
          all(not _banned_hits(sql) for sql in QUERIES.values()),
          str({name: sorted(_banned_hits(sql)) for name, sql in QUERIES.items()
               if _banned_hits(sql)}))
    for column in banned_columns:
        bare = column.split(".")[1]
        check(f"no declared query references {column}",
              all(not re.search(rf"\b{bare}\b", sql) for sql in QUERIES.values()))

    check("no declared query touches a derived/performance table",
          all(not re.search(rf"\b({'|'.join(FORBIDDEN_TABLES)})\b", sql)
              for sql in QUERIES.values()))

    # The statements the functions actually run, not just the constants they are
    # supposed to use.
    recorder = RecordingConn(conn)
    coverage_factor(recorder, "ConfRich")
    provenance_factor(recorder, "ConfRich")
    sample_factor(recorder, "ConfRich", {"confidence": {}})
    recency_factor(recorder, "ConfRich", as_of=AS_OF, config={"confidence": {}})
    metadata_factor(recorder, "ConfRich")

    check("all five factors executed a statement",
          len(recorder.statements) == 5, str(len(recorder.statements)))
    check("no executed statement reads a banned identifier",
          all(not _banned_hits(sql) for sql in recorder.statements),
          str([sorted(_banned_hits(sql)) for sql in recorder.statements
               if _banned_hits(sql)]))
    check("no executed statement reads a forbidden table",
          all(not re.search(rf"\b({'|'.join(FORBIDDEN_TABLES)})\b", sql)
              for sql in recorder.statements))
    check("each executed statement was the factor's declared query",
          all(sql in QUERIES.values() for sql in recorder.statements),
          str([sql.strip().splitlines()[0] for sql in recorder.statements
               if sql not in QUERIES.values()]))


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
