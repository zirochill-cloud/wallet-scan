"""Scoring consumer tests: hand-computed numbers, then a real run.

Layer 1 checks the unit mappings, re-normalisation and shrinkage against
arithmetic written out here, so a change in the consumer has to argue with the
fixture rather than with itself.

Layer 2 runs the consumer against real PostgreSQL with four wallets:

* **A** — all three early-entry metrics computed, no positions.
* **B** — one metric ``not_rankable``, so the other two must be re-normalised.
* **C** — every metric ``not_rankable``, so no ``scores`` row may be emitted.
* **D** — like A, plus closed positions and cohort benchmarks, so
  ``token_selection`` executes end to end (launch-week cohort, an ``all_tokens``
  fallback, and a position whose launch time is unknown).

Hand-computed expectations (see the comments beside each check):

* A: credits 4/(1+4)=0.80, 1-0.02=0.98, 0.50 at weights 0.40/0.30/0.30
  -> 76.40 raw; n=9 anchors, k=15, prior=50 -> 0.375*76.40 + 0.625*50 = 59.90.
* B: buyer rank dropped, weights re-normalised to 0.5714285714/0.4285714286
  -> 100*(0.5714285714*0.80 + 0.4285714286*0.50) = 67.1428571; n=6
  -> 0.2857142857*67.1428571 + 0.7142857143*50 = 54.89796.
* D: excesses ln2, 0, ln2 -> mean 2ln2/3 = 0.4620981
  -> 100/(1+exp(-0.4620981/0.5)) = 71.5896; n=3 -> 53.5983.

Run:  python tests/test_scoring_consumer.py
Requires (DB half only): psycopg[binary] and either $SOLSCOPE_TEST_PGDSN or pgserver.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.config import load_scoring  # noqa: E402
from solscope.consumer import (  # noqa: E402
    AnchorCounts,
    ConsumerError,
    PositionExcess,
    StoredAggregate,
    blend_confidence,
    build_wallet_score,
    cohort_excess_component,
    confidence_factors,
    credit_for,
    early_entry_dimension,
    excess_for,
    launch_week_key,
    renormalise,
    run_consumer,
    shrink,
    token_selection_dimension,
)

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

_failures: list[str] = []
CONFIG = load_scoring()

MR = "max_subsequent_return"
BR = "buyer_rank_at_entry"
PV = "pct_entries_before_volume_spike"
AS_OF = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
WINDOW_START = AS_OF - dt.timedelta(days=30)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        _failures.append(name)
        print(f"  FAIL  {name}{': ' + detail if detail else ''}")


def close(actual, expected: str, tol: str = "1e-6") -> bool:
    if actual is None:
        return False
    return abs(Decimal(actual) - Decimal(expected)) <= Decimal(tol)


def expect(exc_type, fn, name: str) -> None:
    try:
        fn()
    except exc_type:
        print(f"  PASS  {name}")
        return
    except Exception as other:  # noqa: BLE001
        _failures.append(name)
        print(f"  FAIL  {name}: expected {exc_type.__name__}, got {type(other).__name__}: {other}")
        return
    _failures.append(name)
    print(f"  FAIL  {name}: expected {exc_type.__name__}, but it succeeded")


def agg(metric: str, value, status: str = "computed", computed: int = 3, excluded: int = 0):
    return StoredAggregate(
        metric_name=metric,
        value=None if value is None else Decimal(str(value)),
        status=status,
        anchor_count_computed=computed,
        anchor_count_excluded=excluded,
        coverage=Decimal(computed) / Decimal(computed + excluded) if computed + excluded else Decimal(0),
    )


def wallet_a() -> dict[str, StoredAggregate]:
    return {MR: agg(MR, 4), BR: agg(BR, "0.02"), PV: agg(PV, "0.5")}


def wallet_b() -> dict[str, StoredAggregate]:
    return {
        MR: agg(MR, 4),
        BR: agg(BR, None, status="not_rankable", computed=1),
        PV: agg(PV, "0.5"),
    }


def wallet_c() -> dict[str, StoredAggregate]:
    return {
        MR: agg(MR, None, status="not_rankable", computed=1),
        BR: agg(BR, None, status="not_rankable", computed=1),
        PV: agg(PV, None, status="not_rankable", computed=1),
    }


# --------------------------------------------------------------------------
# 1. pure logic
# --------------------------------------------------------------------------
def logic_checks() -> None:
    print("-- unit mappings --------------------------------------------------")
    check("4x -> 0.80 credit", close(credit_for(MR, Decimal("4")), "0.8"))
    check("1x -> 0.50 credit", close(credit_for(MR, Decimal("1")), "0.5"))
    check("99x -> 0.99 credit", close(credit_for(MR, Decimal("99")), "0.99"))
    check("a negative multiple earns 0, not a negative credit",
          close(credit_for(MR, Decimal("-1")), "0"))
    check("percentile 0.02 -> 0.98 credit", close(credit_for(BR, Decimal("0.02")), "0.98"))
    check("percentile 0.001 -> 0.999 credit", close(credit_for(BR, Decimal("0.001")), "0.999"))
    check("pre-volume fraction 0.5 -> 0.5 credit", close(credit_for(PV, Decimal("0.5")), "0.5"))
    check("an out-of-range fraction is clamped, not amplified",
          close(credit_for(PV, Decimal("1.5")), "1"))

    print("-- re-normalisation ------------------------------------------------")
    full = renormalise([(MR, Decimal("0.40")), (BR, Decimal("0.30")), (PV, Decimal("0.30"))])
    check("full weights are unchanged and sum to 1.0",
          all(close(full[m], w) for m, w in
              ((MR, "0.40"), (BR, "0.30"), (PV, "0.30"))),
          f"{full}")
    partial = renormalise([(MR, Decimal("0.40")), (PV, Decimal("0.30"))])
    check("0.40/0.30 re-normalises to 0.5714285714/0.4285714286",
          close(partial[MR], "0.5714285714") and close(partial[PV], "0.4285714286"),
          f"{partial}")
    check("re-normalised weights sum to exactly 1.0", sum(partial.values()) == Decimal(1),
          f"{sum(partial.values())}")
    single = renormalise([(MR, Decimal("0.40"))])
    check("a single survivor takes the whole weight", single == {MR: Decimal(1)})
    dropped = renormalise([(MR, Decimal("0.40")), ("hit_rate", Decimal("0"))])
    check("a zero-weight diagnostic is dropped, not zero-weighted", dropped == {MR: Decimal(1)},
          f"{dropped}")
    expect(ConsumerError, lambda: renormalise([("hit_rate", Decimal("0"))]),
           "re-normalising only zero weights is an error")

    print("-- shrinkage ------------------------------------------------------")
    k_early = Decimal(str(CONFIG["shrinkage"]["k"]["early_entry"]))
    check("config sets early_entry k = 5 (half weight at 5 anchors)", k_early == Decimal(5),
          f"k={k_early}")
    shrunk, factor = shrink(Decimal("76.4"), 3, k=k_early, prior=Decimal(50))
    check("factor at n=3 with the configured k is 3/(3+5) = 0.375",
          close(factor, "0.375", "1e-9"), f"factor={factor}")
    check("shrunk = 0.375*76.4 + 0.625*50 = 59.90", close(shrunk, "59.9"), f"{shrunk}")
    _, heavy = shrink(Decimal("76.4"), 3, k=Decimal(15), prior=Decimal(50))
    check("the function still honours a larger k (n=3, k=15 -> 0.1667)",
          close(heavy, "0.1666666", "1e-6"), f"factor={heavy}")
    shrunk, factor = shrink(Decimal("76.4"), 0, k=k_early, prior=Decimal(50))
    check("zero sample shrinks fully to the prior",
          close(shrunk, "50") and factor == 0, f"{shrunk}/{factor}")

    print("-- early_entry: all metrics computed (wallet A) --------------------")
    early_a = early_entry_dimension(wallet_a(), CONFIG, AnchorCounts(3, 3))
    check("composite is 76.40", early_a.status == "computed" and close(early_a.raw_value, "76.4"),
          f"status={early_a.status} raw={early_a.raw_value}")
    check("weights used sum to 1.0", sum(early_a.weights_used.values()) == Decimal(1),
          f"{early_a.weights_used}")
    check("sample size counts anchors, not metric rows: 3 metrics on 3 anchors -> 3 (not 9)",
          early_a.sample_size == 3, f"{early_a.sample_size}")
    check("shrunk value is 59.90 (n=3 -> factor 3/8 = 0.375)",
          close(early_a.shrunk_value, "59.9"), f"{early_a.shrunk_value}")
    check("explanation names the components and the shrinkage",
          "max_subsequent_return 4.0000 -> 80.00" in early_a.explanation
          and "shrinkage 0.3750" in early_a.explanation
          and "n=3" in early_a.explanation,
          early_a.explanation)
    check("confidence factors for A are 0.3 and 1.0 (3 anchors, not 9 metric rows)",
          confidence_factors(AnchorCounts(3, 3)) == (Decimal("0.3"), Decimal("1")),
          f"{confidence_factors(AnchorCounts(3, 3))}")

    print("-- early_entry: one metric dropped (wallet B) ----------------------")
    early_b = early_entry_dimension(wallet_b(), CONFIG, AnchorCounts(3, 3))
    check("composite re-normalised to 67.1428571", close(early_b.raw_value, "67.1428571"),
          f"{early_b.raw_value}")
    check("only two weights survive and they sum to 1.0",
          len(early_b.weights_used) == 2 and sum(early_b.weights_used.values()) == Decimal(1),
          f"{early_b.weights_used}")
    check("the dropped metric is named in the explanation",
          "buyer_rank_at_entry=not_rankable" in early_b.explanation, early_b.explanation)
    check("shrunk value is 56.42857 (n=3 -> factor 0.375)",
          close(early_b.shrunk_value, "56.4285714", "1e-5"), f"{early_b.shrunk_value}")
    check("A and B stay distinguishable after re-normalisation",
          early_a.shrunk_value > early_b.shrunk_value,
          f"{early_a.shrunk_value} vs {early_b.shrunk_value}")

    print("-- early_entry: nothing rankable (wallet C) ------------------------")
    early_c = early_entry_dimension(wallet_c(), CONFIG, AnchorCounts(0, 3))
    check("C is not_rankable", early_c.status == "not_rankable" and early_c.raw_value is None,
          f"status={early_c.status} raw={early_c.raw_value}")
    check("C keeps its measured-anchor count group", early_c.anchor_count_computed == 3,
          f"{early_c.anchor_count_computed}")
    empty_c = early_entry_dimension({}, CONFIG, AnchorCounts(0, 0))
    check("no aggregates at all -> not_computable", empty_c.status == "not_computable",
          f"{empty_c.status}")
    check("no anchor rows -> coverage 0.0, not 1.0",
          confidence_factors(AnchorCounts(0, 0)) == (Decimal(0), Decimal(0)),
          f"{confidence_factors(AnchorCounts(0, 0))}")
    check("anchors with no computed metric -> coverage 0.0",
          confidence_factors(AnchorCounts(0, 3)) == (Decimal(0), Decimal(0)),
          f"{confidence_factors(AnchorCounts(0, 3))}")
    check("a not_computable aggregate is dropped like a not_rankable one",
          early_entry_dimension(
              {MR: agg(MR, None, status="not_computable", computed=0), PV: agg(PV, "0.5")},
              CONFIG,
              AnchorCounts(3, 3),
          ).status == "computed",
          "one survivor must still score")

    print("-- token_selection ------------------------------------------------")
    check("2026-04-01 is ISO week 2026-W14",
          launch_week_key(dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc))
          == "launch_week:2026-W14",
          launch_week_key(dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)))
    check("2025-12-29 belongs to ISO year 2026 (the year-boundary trap)",
          launch_week_key(dt.datetime(2025, 12, 29, tzinfo=dt.timezone.utc))
          == "launch_week:2026-W01",
          launch_week_key(dt.datetime(2025, 12, 29, tzinfo=dt.timezone.utc)))
    check("excess(3x vs 1x median) is ln 2",
          close(excess_for(Decimal("3"), Decimal("1")), "0.6931471806", "1e-9"))
    check("component(ln 2, scale 0.5) is 80.0",
          close(cohort_excess_component(Decimal("0.6931471806"), Decimal("0.5")), "80.0"),
          f"{cohort_excess_component(Decimal('0.6931471806'), Decimal('0.5'))}")
    check("a coin-flip multiple against its cohort scores 50",
          close(cohort_excess_component(Decimal(0), Decimal("0.5")), "50"))

    excesses = [
        PositionExcess(position_id=1, multiple=Decimal("3"), excess=Decimal("0.6931471806")),
        PositionExcess(position_id=2, multiple=Decimal("3"), excess=Decimal("0.6931471806")),
        PositionExcess(position_id=3, multiple=Decimal("1"), excess=Decimal(0)),
    ]
    token = token_selection_dimension(excesses, CONFIG)
    check("three positions, mean excess 2ln2/3 -> 71.5896",
          close(token.raw_value, "71.5896", "1e-4"), f"{token.raw_value}")
    check("token_selection shrunk with k=5 over 3 positions -> 58.10",
          close(token.shrunk_value, "58.0961", "1e-4"), f"{token.shrunk_value}")
    check("hit_rate stays a diagnostic: only cohort_excess carries weight",
          list(token.weights_used) == ["cohort_excess"]
          and token.weights_used["cohort_excess"] == Decimal(1),
          f"{token.weights_used}")
    no_positions = token_selection_dimension([], CONFIG)
    check("no positions -> token_selection not_computable",
          no_positions.status == "not_computable", f"{no_positions.status}")
    unbenchmarked = token_selection_dimension(
        [PositionExcess(position_id=9, multiple=Decimal("3"), reason="no benchmark")], CONFIG
    )
    check("positions but no benchmark -> not_rankable, with the reason kept",
          unbenchmarked.status == "not_rankable" and "no benchmark" in unbenchmarked.explanation,
          unbenchmarked.explanation)

    print("-- confidence gate (Option 3) --------------------------------------")
    gate_a = blend_confidence(Decimal(0), Decimal("0.9"), Decimal("1.0"), CONFIG)
    check("today every wallet fails on existing_confidence = 0",
          gate_a.confidence == Decimal(0) and gate_a.gate_passed is False, f"{gate_a}")
    check("the note names the failing conjunct",
          "existing_confidence below minimum" in gate_a.note, gate_a.note)
    check("the note quotes both numbers", "(0.00 < 25.00)" in gate_a.note, gate_a.note)
    check("components record all three inputs and all three thresholds",
          set(gate_a.components) == {
              "existing_confidence", "anchor_sample_size", "anchor_coverage",
              "existing_confidence_min", "anchor_sample_size_min", "anchor_coverage_min",
          },
          f"{sorted(gate_a.components)}")

    gate_pass = blend_confidence(Decimal(70), Decimal("0.9"), Decimal("1.0"), CONFIG)
    check("A with existing confidence 70 passes every conjunct",
          gate_pass.gate_passed is True and gate_pass.confidence == Decimal(70),
          f"{gate_pass}")
    check("a passing gate says so in words", gate_pass.note == "all thresholds met",
          gate_pass.note)

    gate_b = blend_confidence(Decimal(70), Decimal("0.2"), Decimal("0.6667"), CONFIG)
    check("B fails on the anchor sample, not on confidence",
          gate_b.gate_passed is False and "anchor_sample_size below minimum" in gate_b.note,
          gate_b.note)
    check("the sample failure quotes 0.20 < 0.30", "(0.20 < 0.30)" in gate_b.note, gate_b.note)

    gate_cov = blend_confidence(Decimal(70), Decimal("0.9"), Decimal("0.4"), CONFIG)
    check("low coverage fails even with a good sample",
          gate_cov.gate_passed is False and "anchor_coverage below minimum" in gate_cov.note,
          gate_cov.note)

    check("the gate never modifies the confidence it was given",
          (gate_a.confidence, gate_b.confidence, gate_cov.confidence)
          == (Decimal(0), Decimal(70), Decimal(70)),
          "gate must not blend")

    expect(ConsumerError,
           lambda: blend_confidence(Decimal(0), Decimal(0), Decimal(0), {}),
           "a config without confidence_gate is rejected, not defaulted")

    # The gate must not even be consulted for a wallet that gets no scores row.
    import solscope.consumer as consumer_mod

    gate_calls: list = []
    real_blend = consumer_mod.blend_confidence

    def _spy(*args, **kwargs):
        gate_calls.append(args)
        return real_blend(*args, **kwargs)

    consumer_mod.blend_confidence = _spy
    try:
        build_wallet_score("WalletC", wallet_c(), [], CONFIG, AnchorCounts(0, 3))
    finally:
        consumer_mod.blend_confidence = real_blend
    check("a not_rankable wallet never reaches the gate", gate_calls == [], f"{gate_calls}")

    print("-- wallet assembly ------------------------------------------------")
    scored = build_wallet_score("WalletA", wallet_a(), [], CONFIG, AnchorCounts(3, 3))
    check("A is scored", scored is not None)
    if scored is not None:
        check("overall is the neutral 50.00 (five dimensions unbuilt)",
              scored.overall_score == Decimal("50.00"), f"{scored.overall_score}")
        check("confidence is 0.00 because the blend is open",
              scored.confidence_score == Decimal(0), f"{scored.confidence_score}")
        check("the row is excluded from ranking",
              (scored.band, scored.recommendation) == ("exclude", "exclude"),
              f"{scored.band}/{scored.recommendation}")
        check("gated_by names the missing dimensions",
              all(dim in scored.gated_by for dim in ("profitability", "integrity")),
              scored.gated_by)
        check("gated_by records the confidence gate's reason",
              "confidence_gate: existing_confidence below minimum" in scored.gated_by,
              scored.gated_by)
    check("C is not scored at all",
          build_wallet_score("WalletC", wallet_c(), [], CONFIG, AnchorCounts(0, 3)) is None)
    expect(ConsumerError,
           lambda: early_entry_dimension(wallet_a(), {"early_entry": {}}, AnchorCounts(3, 3)),
           "a scoring config missing weights is rejected, not guessed")


# --------------------------------------------------------------------------
# 2. database
# --------------------------------------------------------------------------
def db_target():
    dsn = os.environ.get("SOLSCOPE_TEST_PGDSN")
    if dsn:
        return {"conninfo": dsn}, "solscope_consumer"
    try:
        import pgserver
    except ImportError:
        return None
    pgserver.get_server("/tmp/pgdata-solscope")
    return {"host": "/tmp/pgdata-solscope", "user": "postgres"}, "solscope_consumer"


def count(conn, table: str, where: str = "", params: tuple = ()) -> int:
    sql = f"SELECT count(*) FROM {table}" + (f" WHERE {where}" if where else "")
    return int(conn.execute(sql, params).fetchone()[0])


def seed_positions(conn) -> None:
    """Wallet D's positions: two in a benched launch week, one on the fallback."""
    conn.execute(
        "INSERT INTO tokens (mint, chain_id, created_at) VALUES "
        "('MintW14A', 'solana', %s), ('MintW14B', 'solana', %s), "
        "('MintNoWeek', 'solana', %s), ('MintNoLaunch', 'solana', NULL)",
        (
            dt.datetime(2026, 4, 1, 12, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 4, 2, 12, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 10, 12, tzinfo=dt.timezone.utc),
        ),
    )
    conn.execute(
        """
        INSERT INTO cohort_benchmarks (
            cohort_key, cohort_kind, metric_key, window_days, n_samples,
            value_median, available_at
        ) VALUES
            ('launch_week:2026-W14', 'launch_week', 'max_multiple_30d', 30, 42, 1.0, %s),
            ('all_tokens', 'all_tokens', 'max_multiple_30d', 30, 500, 1.0, %s)
        """,
        (
            dt.datetime(2026, 4, 15, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        ),
    )
    # Poison row: same cohort, later available_at, but only 3 samples. If the
    # consumer ignored benchmark_min_cohort_n this median (5.0) would win the
    # "latest available" ordering and move every excess for W14.
    conn.execute(
        """
        INSERT INTO cohort_benchmarks (
            cohort_key, cohort_kind, metric_key, window_days, n_samples,
            value_median, available_at
        ) VALUES ('launch_week:2026-W14', 'launch_week', 'max_multiple_30d', 30, 3, 5.0, %s)
        """,
        (dt.datetime(2026, 4, 20, tzinfo=dt.timezone.utc),),
    )
    conn.execute(
        """
        INSERT INTO positions (
            address, chain_id, mint, opened_at, is_closed, max_multiple_30d
        ) VALUES
            ('WalletD', 'solana', 'MintW14A', %s, true, 3.0),
            ('WalletD', 'solana', 'MintW14B', %s, true, 1.0),
            ('WalletD', 'solana', 'MintNoWeek', %s, true, 3.0),
            ('WalletD', 'solana', 'MintNoLaunch', %s, true, 3.0)
        """,
        tuple(AS_OF - dt.timedelta(days=n) for n in (20, 19, 5, 4)),
    )
    conn.commit()


def db_checks() -> bool:
    if psycopg is None:
        print("\nSKIP: psycopg not installed — database checks skipped")
        return False
    target = db_target()
    if target is None:
        print("\nSKIP: neither $SOLSCOPE_TEST_PGDSN nor pgserver available")
        return False

    from solscope.aggregation import (
        aggregate_buyer_rank_at_entry,
        aggregate_max_subsequent_return,
        aggregate_pct_entries_before_volume_spike,
    )
    from solscope.aggregation import AnchorMetricRow
    from solscope.anchor_writer import write_aggregate, write_metric
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

    RULE = "agg-v1"

    def rows(values, metric, wallet):
        out = []
        for i, value in enumerate(values, start=1):
            out.append(
                AnchorMetricRow(
                    anchor_id=i,
                    metric_name=metric,
                    status="NOT_COMPUTABLE" if value is None else "computed",
                    value=None if value is None else Decimal(str(value)),
                    wallet=wallet,
                )
            )
        return out

    def store(conn, wallet, max_r, rank, pre):
        for metric, aggregate in (
            (MR, aggregate_max_subsequent_return(rows(max_r, MR, wallet))),
            (BR, aggregate_buyer_rank_at_entry(rows(rank, BR, wallet))),
            (PV, aggregate_pct_entries_before_volume_spike(rows(pre, PV, wallet))),
        ):
            write_aggregate(conn, wallet, aggregate, RULE)

    def seed_anchor_metrics(conn, wallet, prefix, total, computed_per_metric):
        """Real ``anchor_wallet_metrics`` rows: the D1 denominator reads these.

        ``computed_per_metric`` says how many of the wallet's ``total`` anchors have
        a computed value for each metric, which is what makes wallet B's buyer rank
        ``not_rankable`` (1 of 3) while its other two metrics are measured on all 3.
        """
        ids = [
            int(
                conn.execute(
                    "INSERT INTO anchors (mint, source, created_by_rule_version) "
                    "VALUES (%s, 'synthetic', 'test-consumer') RETURNING anchor_id",
                    (f"{prefix}-mint-{index}",),
                ).fetchone()[0]
            )
            for index in range(total)
        ]
        for metric, computed in computed_per_metric.items():
            for index, anchor_id in enumerate(ids):
                measured = index < computed
                write_metric(
                    conn,
                    anchor_id=anchor_id,
                    wallet=wallet,
                    metric_name=metric,
                    status="computed" if measured else "NOT_COMPUTABLE",
                    value=Decimal("1") if measured else None,
                    window_days=30 if metric == MR else None,
                )

    print("\n-- run_consumer (real PostgreSQL) --------------------------------")
    with psycopg.connect(**conn_kwargs) as conn:
        applied = [m.path.name for m in migrate(conn)]
        check("migrations applied", "003_wallet_aggregates.sql" in applied, f"{applied}")

        store(conn, "WalletA", [4, 4, 4], [0.02, 0.02, 0.02], [0.5, 0.5, 0.5])
        store(conn, "WalletB", [4, 4, 4], [0.02], [0.5, 0.5, 0.5])
        store(conn, "WalletC", [4], [0.02], [0.5])
        store(conn, "WalletD", [4, 4, 4], [0.02, 0.02, 0.02], [0.5, 0.5, 0.5])
        seed_anchor_metrics(conn, "WalletA", "A", 3, {MR: 3, BR: 3, PV: 3})
        seed_anchor_metrics(conn, "WalletB", "B", 3, {MR: 3, BR: 1, PV: 3})
        seed_anchor_metrics(conn, "WalletC", "C", 1, {MR: 0, BR: 0, PV: 0})
        seed_anchor_metrics(conn, "WalletD", "D", 3, {MR: 3, BR: 3, PV: 3})
        seed_positions(conn)

        report = run_consumer(
            conn, rule_version=RULE, as_of=AS_OF, window_start=WINDOW_START, window_end=AS_OF
        )
        check("four wallets seen, three scored", (report.wallets_seen, report.wallets_scored) == (4, 3),
              f"{report.wallets_seen}/{report.wallets_scored}")
        check("wallet C is skipped with its reason",
              report.skipped.get("WalletC") == "early_entry_not_rankable", f"{report.skipped}")
        check("three scores rows", count(conn, "scores") == 3,
              f"{count(conn, 'scores')}")
        check("nine sub-scores rows (3 per scored wallet)", count(conn, "sub_scores") == 9,
              f"{count(conn, 'sub_scores')}")
        check("the run records the scoring config version",
              report.config_version == str(CONFIG["version"]), report.config_version)
        check("score_runs carries the aggregate rule version as its label",
              conn.execute(
                  "SELECT experiment_label, wallets_scored FROM score_runs WHERE score_run_id=%s",
                  (report.score_run_id,),
              ).fetchone() == (f"aggregate_rule:{RULE}", 3),
              "label/wallets_scored mismatch")
        check("score_config_versions records the weights used",
              count(conn, "score_config_versions", "config_version=%s", (report.config_version,))
              == 1)

        a = conn.execute(
            "SELECT s.id, s.overall_score, s.confidence_score, s.band, s.gated_by "
            "FROM scores s WHERE s.address='WalletA'"
        ).fetchone()
        check("A's row is neutral, excluded and explained",
              close(a[1], "50") and close(a[2], "0") and a[3] == "exclude"
              and "incomplete_score" in a[4],
              f"{a}")

        early_a = conn.execute(
            "SELECT raw_value, normalized_value, shrunk_value, weight, sample_size, "
            "shrinkage_applied, confidence FROM sub_scores WHERE score_id=%s AND dimension='early_entry'",
            (a[0],),
        ).fetchone()
        check("A's early_entry raw is 76.40, shrunk 59.90",
              close(early_a[0], "76.4") and close(early_a[2], "59.9"), f"{early_a}")
        check("A's early_entry weight is the dimension weight 0.25",
              close(early_a[3], "0.25"), f"{early_a[3]}")
        check("a 3-anchor wallet measured on 3 metrics stores sample_size 3, not 9",
              early_a[4] == 3 and close(early_a[5], "0.375", "1e-4"), f"{early_a}")
        check("A's early_entry confidence column is left NULL (blend open)",
              early_a[6] is None, f"{early_a[6]}")

        early_b = conn.execute(
            "SELECT raw_value, shrunk_value FROM sub_scores "
            "WHERE score_id=(SELECT id FROM scores WHERE address='WalletB') "
            "AND dimension='early_entry'"
        ).fetchone()
        # raw_value is NUMERIC(38,10); shrunk_value is NUMERIC(6,2) and rounds.
        check("B's re-normalised early_entry is 67.1428571420 / 56.43",
              close(early_b[0], "67.1428571420", "1e-9") and close(early_b[1], "56.43"),
              f"{early_b}")

        token_d = conn.execute(
            "SELECT raw_value, shrunk_value, sample_size, explanation FROM sub_scores "
            "WHERE score_id=(SELECT id FROM scores WHERE address='WalletD') "
            "AND dimension='token_selection'"
        ).fetchone()
        check("D's token_selection is 71.5896 / 58.10 over 3 of 4 positions",
              close(token_d[0], "71.5896", "1e-4") and close(token_d[1], "58.10")
              and token_d[2] == 3,
              f"{token_d}")
        check("D's token_selection explanation reports the exclusion",
              "1 excluded" in token_d[3], token_d[3])
        b_notes = [
            row[0]
            for row in conn.execute(
                "SELECT note FROM score_evidence WHERE dimension='early_entry' AND score_id="
                "(SELECT id FROM scores WHERE address='WalletB')"
            ).fetchall()
        ]
        b_weights = [Decimal(note.split("weight_used=")[1]) for note in b_notes]
        check("B's stored component weights re-normalise to exactly 1.0",
              len(b_weights) == 2 and sum(b_weights) == Decimal(1), f"{b_weights}")

        check("the thin launch-week benchmark was ignored (median 5.0 would give a different value)",
              close(token_d[0], "71.5896", "1e-4"), f"{token_d[0]}")

        token_a = conn.execute(
            "SELECT normalized_value FROM sub_scores WHERE score_id=%s "
            "AND dimension='token_selection'",
            (a[0],),
        ).fetchone()
        check("A has no positions, so its token_selection stores no value",
              token_a[0] is None, f"{token_a}")

        factors = dict(
            conn.execute(
                "SELECT subject_id, value FROM score_evidence "
                "WHERE score_id=%s AND dimension='confidence'",
                (a[0],),
            ).fetchall()
        )
        check("anchor_sample_size is stored as evidence (0.3 = 3 anchors / 10)",
              close(factors.get("anchor_sample_size"), "0.3"), f"{factors}")
        check("anchor_coverage is stored as evidence (1.0)",
              close(factors.get("anchor_coverage"), "1.0"), f"{factors}")
        check("the confidence sub-score explains that the blend is open",
              "not blended" in conn.execute(
                  "SELECT explanation FROM sub_scores WHERE score_id=%s AND dimension='confidence'",
                  (a[0],),
              ).fetchone()[0])

        counts_before = (
            count(conn, "scores"),
            count(conn, "sub_scores"),
            count(conn, "score_evidence"),
            count(conn, "score_runs"),
        )
        second = run_consumer(
            conn, rule_version=RULE, as_of=AS_OF, window_start=WINDOW_START, window_end=AS_OF
        )
        counts_after = (
            count(conn, "scores"),
            count(conn, "sub_scores"),
            count(conn, "score_evidence"),
            count(conn, "score_runs"),
        )
        check("a second run is idempotent for every table", counts_before == counts_after,
              f"{counts_before} vs {counts_after}")
        check("the second run reuses the same score_run",
              second.score_run_id == report.score_run_id,
              f"{second.score_run_id} vs {report.score_run_id}")
        check("one scores row per (run, wallet) after two runs",
              count(conn, "scores", "address='WalletA'") == 1)

        # A wallet that no longer has aggregates must lose its row, not keep a stale one.
        conn.execute("DELETE FROM wallet_aggregates WHERE wallet='WalletB'")
        conn.commit()
        third = run_consumer(
            conn, rule_version=RULE, as_of=AS_OF, window_start=WINDOW_START, window_end=AS_OF
        )
        b_rows = count(conn, "scores", "address='WalletB'")
        check("a wallet that lost its aggregates is pruned from the run",
              third.wallets_scored == 2 and b_rows == 0,
              f"scored={third.wallets_scored} b_rows={b_rows}")
        check("pruning leaves the other wallets intact",
              count(conn, "scores") == 2, f"{count(conn, 'scores')}")

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
        print("SCORING CONSUMER PURE-LOGIC CHECKS PASSED (database checks skipped)")
        return 0
    print("ALL SCORING CONSUMER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
