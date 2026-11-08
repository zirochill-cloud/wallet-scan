"""Scoring consumer tests: hand-computed numbers, then a real run.

Layer 1 checks the unit mappings, re-normalisation and shrinkage against
arithmetic written out here, so a change in the consumer has to argue with the
fixture rather than with itself.

Layer 2 runs the consumer against real PostgreSQL with four wallets:

* **A** - all three early-entry metrics computed, no positions.
* **B** - one metric ``not_rankable``, so the other two must be re-normalised.
* **C** - every metric ``not_rankable``, so no ``scores`` row may be emitted.
* **D** - like A, plus closed positions and cohort benchmarks, so
  ``token_selection`` executes end to end (launch-week cohort, an ``all_tokens``
  fallback, and a position whose launch time is unknown).

Hand-computed expectations (see the comments beside each check):

* A: credits 4/(1+4)=0.80, 1-0.02=0.98, 0.50 at weights 0.40/0.30/0.30
  -> 76.40 raw; 3 anchors measured on 3 metrics, k=5, prior=50
  -> 0.375*76.40 + 0.625*50 = 59.90.  (The sample is 3, not 9: one anchor
  measured on three metrics is one observation.  Factor 3/(3+5) = 0.375.)
* B: buyer rank dropped, weights re-normalised to 0.5714285714/0.4285714286
  -> 100*(0.5714285714*0.80 + 0.4285714286*0.50) = 67.1428571; 3 anchors
  -> 0.375*67.1428571 + 0.625*50 = 56.4285714 (56.43 stored).
* D: excesses ln2, 0, ln2 -> mean 2ln2/3 = 0.4620981
  -> 100/(1+exp(-0.4620981/0.5)) = 71.5896; 3 anchors
  -> 0.375*71.5896 + 0.625*50 = 58.0961 (58.10 stored).

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

from solscope.confidence import (  # noqa: E402
    QUERIES,
    confidence_adjusted_score,
    evaluate_gate,
)
from solscope.config import load_scoring  # noqa: E402
from solscope.consumer import (  # noqa: E402
    AnchorCounts,
    ConsumerError,
    PositionExcess,
    StoredAggregate,
    build_wallet_score,
    cohort_excess_component,
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

    print("-- confidence composite and gate (sec 12) ----------------------------")

    class FactorConn:
        """No-database connection answering exactly this module's factor queries."""

        def __init__(self, *, computed=0, total=0, trusted=0, entries=0, last=None,
                     resolved=0, measured=0):
            self.rows = {
                QUERIES["coverage"]: (computed, total),
                QUERIES["sample"]: (computed, total),
                QUERIES["price_provenance"]: (trusted, entries),
                QUERIES["recency"]: (last,),
                QUERIES["metadata"]: (resolved, measured),
                QUERIES["provenance_sell_only_pct"]: (0, entries),
            }

        def execute(self, sql, params=()):
            row = self.rows.get(sql)

            class _Result:
                def fetchone(self):
                    return row

            return _Result()

    # The design sec 6 2x2 factors: rich = 0.95/0.90/1.00/0.9223/0.95,
    # thin = 0.50/0.75/0.40/0.8507/0.50.
    rich_conn = FactorConn(computed=19, total=20, trusted=18, entries=20,
                           last=AS_OF - dt.timedelta(days=7), resolved=19, measured=20)
    thin_conn = FactorConn(computed=2, total=4, trusted=3, entries=4,
                           last=AS_OF - dt.timedelta(days=14), resolved=2, measured=4)

    rich = evaluate_gate(rich_conn, "GoodRich", as_of=AS_OF, config=CONFIG)
    check("rich wallet composite is 94.33, exactly the design's arithmetic",
          rich.composite_confidence == Decimal("94.33"), f"{rich.composite_confidence}")
    check("rich wallet passes both conjuncts",
          rich.gate_passed is True and rich.gate_reason == "all thresholds met",
          rich.gate_reason)
    check("19 >= 3, so the anchor conjunct is not the binding one",
          rich.distinct_anchors == 19, f"{rich.distinct_anchors}")

    thin = evaluate_gate(thin_conn, "GoodThin", as_of=AS_OF, config=CONFIG)
    check("thin wallet composite is 59.51 (design sec 6 says 60.51 - see the report)",
          thin.composite_confidence == Decimal("59.51"), f"{thin.composite_confidence}")
    check("thin wallet fails on the anchor conjunct, not on confidence",
          thin.gate_passed is False
          and thin.gate_reason == "distinct_anchors below minimum (2 < 3)",
          thin.gate_reason)
    check("the thin composite is well above the 40 floor, so only anchors can stop it",
          thin.composite_confidence > CONFIG["confidence_gate"]["confidence_min"])

    # The design's original sec 6 column was computed with this form. It is kept
    # for the day scores.band_basis flips to overall_score (D21/D23), and it is
    # explicitly NOT the phase-1b band basis - see the sec 6b block below.
    check("full-model pull reproduces the design's original 80.19 / 29.25",
          (confidence_adjusted_score(Decimal("82"), rich.composite_confidence),
           confidence_adjusted_score(Decimal("28"), rich.composite_confidence))
          == (Decimal("80.19"), Decimal("29.25")),
          f"{confidence_adjusted_score(Decimal('82'), rich.composite_confidence)} / "
          f"{confidence_adjusted_score(Decimal('28'), rich.composite_confidence)}")
    check("thin pair: the same pull gives 66.66 / 34.53 at 59.51",
          (confidence_adjusted_score(Decimal("78"), thin.composite_confidence),
           confidence_adjusted_score(Decimal("24"), thin.composite_confidence))
          == (Decimal("66.66"), Decimal("34.53")))

    import copy

    strict = copy.deepcopy(CONFIG)
    strict["confidence_gate"]["confidence_min"] = 95
    check("confidence_min is read from config (95 rejects the 94.33 wallet)",
          evaluate_gate(rich_conn, "W", as_of=AS_OF, config=strict).gate_passed is False)
    strict2 = copy.deepcopy(CONFIG)
    strict2["confidence_gate"]["min_distinct_anchors"] = 20
    gate_strict = evaluate_gate(rich_conn, "W", as_of=AS_OF, config=strict2)
    check("min_distinct_anchors is read from config (20 rejects 19 anchors)",
          gate_strict.gate_passed is False
          and gate_strict.gate_reason == "distinct_anchors below minimum (19 < 20)",
          gate_strict.gate_reason)
    five_conn = FactorConn(computed=5, total=5)
    strict3 = copy.deepcopy(CONFIG)
    strict3["confidence"]["sample_full_credit_at"] = 10
    default_five = evaluate_gate(five_conn, "W", as_of=AS_OF, config=CONFIG)
    softer = evaluate_gate(five_conn, "W", as_of=AS_OF, config=strict3)
    check("sample_full_credit_at is read from config (5 anchors: 1.00 at 5, 0.50 at 10)",
          (default_five.components["sample"], softer.components["sample"])
          == (Decimal("1.0000"), Decimal("0.5000")),
          f"{default_five.components['sample']} / {softer.components['sample']}")
    check("changing the divisor moves the composite by its weight (50.00 -> 40.00)",
          (default_five.composite_confidence, softer.composite_confidence)
          == (Decimal("50.00"), Decimal("40.00")),
          f"{default_five.composite_confidence} / {softer.composite_confidence}")

    expect(ValueError,
           lambda: evaluate_gate(rich_conn, "W", as_of=AS_OF, config={}),
           "a config without confidence_gate is rejected, not defaulted")
    no_weights = copy.deepcopy(CONFIG)
    no_weights["confidence"].pop("weight_recency")
    expect(ValueError,
           lambda: evaluate_gate(rich_conn, "W", as_of=AS_OF, config=no_weights),
           "a config missing a weight is rejected, not guessed")

    # The gate must not even be consulted for a wallet that gets no scores row.
    import solscope.consumer as consumer_mod

    gate_calls: list = []
    real_gate = consumer_mod.evaluate_gate

    def _spy(*args, **kwargs):
        gate_calls.append(args)
        return real_gate(*args, **kwargs)

    consumer_mod.evaluate_gate = _spy
    try:
        build_wallet_score(
            FactorConn(computed=0, total=3), "WalletC", wallet_c(), [], CONFIG,
            AnchorCounts(0, 3), as_of=AS_OF)
    finally:
        consumer_mod.evaluate_gate = real_gate
    check("a not_rankable wallet never reaches the gate", gate_calls == [], f"{gate_calls}")

    print("-- band derivation (sec 5) ----------------------------------------")
    from solscope.bands import derive_band
    from solscope.confidence import GateResult

    def gate_result(passed=True, reason="all thresholds met"):
        return GateResult(wallet="W", composite_confidence=Decimal("70"),
                          distinct_anchors=5, gate_passed=passed,
                          gate_reason=reason, components={}, weights={}, thresholds={})

    passed = gate_result()
    no_anchors = gate_result(False, "distinct_anchors below minimum (2 < 3)")
    no_evidence = gate_result(False, "confidence below minimum (35.00 < 40.00)")

    # D22: the configured thresholds are now the design's (70/60/40), and the
    # adjusted numbers are the ones the pipeline actually produces - shrink at k=5
    # then the D21 pull, not the retired pull-to-50 column. 19 anchors -> 19/24,
    # 2 anchors -> 2/7.
    matrix = {
        "good+rich": derive_band(Decimal("71.06"), Decimal("82"),
                                 Decimal("94.33"), passed, CONFIG),
        "bad+rich": derive_band(Decimal("30.74"), Decimal("28"),
                                Decimal("94.33"), passed, CONFIG),
        "good+thin": derive_band(Decimal("34.52"), Decimal("78"),
                                 Decimal("59.51"), no_anchors, CONFIG),
        "bad+thin": derive_band(Decimal("25.33"), Decimal("24"),
                                Decimal("59.51"), no_anchors, CONFIG),
    }
    check("good+rich is track (71.06 >= 70, confidence 94.33 >= 60)",
          matrix["good+rich"].band == "track", matrix["good+rich"].band)
    check("bad+rich is exclude, because its raw 28 is below every threshold",
          matrix["bad+rich"].band == "exclude"
          and matrix["bad+rich"].band_reason == "raw_below_investigate",
          matrix["bad+rich"].band_reason)
    check("good+thin is promising_thin, not exclude",
          matrix["good+thin"].band == "promising_thin"
          and matrix["good+thin"].band_reason == "promising_but_unconfirmed",
          f"{matrix['good+thin'].band}/{matrix['good+thin'].band_reason}")
    check("bad+thin shares the composite but not the band: raw 24 fails the floor",
          matrix["bad+thin"].band == "exclude"
          and "promising_thin floor not met" in matrix["bad+thin"].band_reason,
          matrix["bad+thin"].band_reason)
    check("the pair identity holds on band_basis_score",
          matrix["good+rich"].band_basis_score == Decimal("71.06")
          and matrix["bad+rich"].band_basis_score == Decimal("30.74"))

    check("watch is reachable: adjusted 65, confidence 70, gate passed",
          derive_band(Decimal("65"), Decimal("70"), Decimal("70"), passed,
                      CONFIG).band == "watch")
    check("investigate is reachable: adjusted 48, confidence 70, gate passed",
          derive_band(Decimal("48"), Decimal("50"), Decimal("70"), passed,
                      CONFIG).band == "investigate")
    check("a promising-looking wallet at confidence 25 is excluded, not surfaced",
          derive_band(Decimal("40"), Decimal("80"), Decimal("25"), no_anchors,
                      CONFIG).band == "exclude")
    check("a gate failure on confidence is never promising_thin",
          derive_band(Decimal("30"), Decimal("80"), Decimal("35"), no_evidence,
                      CONFIG).band == "exclude")
    low_low = derive_band(Decimal("30"), Decimal("20"), Decimal("20"), passed, CONFIG)
    check("a passed-gate wallet below investigate says why it is excluded",
          low_low.band == "exclude"
          and "AND confidence below watch threshold" in low_low.band_reason,
          low_low.band_reason)
    check("configured band thresholds are the design's after D22",
          (CONFIG["bands"]["track"], CONFIG["bands"]["watch"],
           CONFIG["bands"]["investigate"], CONFIG["bands"]["deprioritize"])
          == (70, 60, 40, 0),
          f"{CONFIG['bands']}")
    check("the track confidence conjunct is a config key, not a hidden default",
          CONFIG["bands"]["track_min_confidence"] == 60,
          f"{CONFIG['bands']}")

    print("-- sec 6b: real shrinkage, and the 70/75 boundary ----------------")
    from solscope.confidence import shrink_for_confidence
    from solscope.consumer import shrink as shrink_observation

    # Sec 6b: the pipeline's own shrinkage, not hand-supplied adjusted scores.
    real = {}
    for name, raw, anchors, confidence, gate in (
            ("good+rich", "82", 19, "94.33", passed),
            ("bad+rich", "28", 19, "94.33", passed),
            ("good+thin", "78", 2, "59.51", no_anchors),
            ("bad+thin", "24", 2, "59.51", no_anchors)):
        shrunk, _factor = shrink_observation(Decimal(raw), anchors, k=Decimal(5),
                                             prior=Decimal(50))
        adjusted = shrink_for_confidence(shrunk, Decimal(confidence))
        real[name] = (shrunk, adjusted, derive_band(adjusted, Decimal(raw),
                                                    Decimal(confidence), gate, CONFIG))
    check("19 anchors shrink 82 -> 75.33 and 28 -> 32.58",
          (round(real["good+rich"][0], 2), round(real["bad+rich"][0], 2))
          == (Decimal("75.33"), Decimal("32.58")),
          f"{real['good+rich'][0]} / {real['bad+rich'][0]}")
    check("2 anchors shrink 78 -> 58.00 and 24 -> 42.57",
          (round(real["good+thin"][0], 2), round(real["bad+thin"][0], 2))
          == (Decimal("58.00"), Decimal("42.57")),
          f"{real['good+thin'][0]} / {real['bad+thin'][0]}")
    check("the pipeline produces 71.06 / 30.74 / 34.52 / 25.33",
          tuple(real[k][1] for k in
                ("good+rich", "bad+rich", "good+thin", "bad+thin"))
          == (Decimal("71.06"), Decimal("30.74"), Decimal("34.52"),
              Decimal("25.33")),
          str({k: str(v[1]) for k, v in real.items()}))
    check("D23: the matrix scores are the produced ones, not the old pull-to-50 column",
          all(matrix[k].band_basis_score == real[k][1] for k in matrix),
          str({k: (str(matrix[k].band_basis_score), str(real[k][1]))
               for k in matrix}))

    import copy
    before = copy.deepcopy(CONFIG)
    before["bands"].update({"track": 75, "investigate": 45})
    at_75 = derive_band(real["good+rich"][1], Decimal("82"), Decimal("94.33"),
                        passed, before)
    check("sec 6b: the same wallet is watch under the old 75 floor",
          at_75.band == "watch", at_75.band)
    check("sec 6b: and track under the configured 70 - 71.06 is the boundary",
          real["good+rich"][2].band == "track"
          and Decimal("70") <= real["good+rich"][1] < Decimal("75"),
          f"{real['good+rich'][2].band} at {real['good+rich'][1]}")
    bad_at_75 = derive_band(real["bad+rich"][1], Decimal("28"), Decimal("94.33"),
                            passed, before)
    check("sec 6b: bad+rich stays exclude under both thresholds, on raw_below_investigate",
          (real["bad+rich"][2].band, bad_at_75.band) == ("exclude", "exclude")
          and real["bad+rich"][2].band_reason == "raw_below_investigate"
          and real["bad+rich"][1] < Decimal("40"),
          f"{real['bad+rich'][2].band}/{real['bad+rich'][2].band_reason} "
          f"at {real['bad+rich'][1]}")
    check("sec 6b: the thin pair is decided by the gate, so D22 does not move it",
          (real["good+thin"][2].band, real["bad+thin"][2].band)
          == ("promising_thin", "exclude"),
          f"{real['good+thin'][2].band} / {real['bad+thin'][2].band}")

    print("-- wallet assembly ------------------------------------------------")
    scored = build_wallet_score(
        FactorConn(computed=3, total=3), "WalletA", wallet_a(), [], CONFIG,
        AnchorCounts(3, 3), as_of=AS_OF)
    check("A is scored", scored is not None)
    if scored is not None:
        check("overall is the neutral 50.00 (five dimensions unbuilt)",
              scored.overall_score == Decimal("50.00"), f"{scored.overall_score}")
        # 0.30*1.00 coverage + 0.20*0.60 sample = 0.42; provenance, recency and
        # metadata have no rows to read, and are 0.0 rather than dropped.
        check("A's confidence is the composite 42.00, not a blend of dimensions",
              scored.confidence_score == Decimal("42.00"), f"{scored.confidence_score}")
        check("A clears the confidence floor on coverage and sample alone",
              scored.gate.gate_passed is True, scored.gate.gate_reason)
        # D21: the pull applies to the shrunk early_entry, not to the placeholder 50.
        check("adjusted is 25.16 = shrunk early_entry 59.90 x 42/100",
              scored.confidence_adjusted_score == Decimal("25.16"),
              f"{scored.confidence_adjusted_score}")
        check("and it is not overall_score (the placeholder would give 50.00)",
              scored.confidence_adjusted_score != Decimal("50.00"))
        check("the row records its band basis",
              scored.band_basis == "shrunk_early_entry", scored.band_basis)
        check("the row is excluded on the adjusted score, with the reason kept",
              (scored.band, scored.recommendation) == ("exclude", "exclude")
              and "raw_below_investigate" in scored.gated_by,
              f"{scored.band}/{scored.recommendation}/{scored.gated_by}")
        check("gated_by names the missing dimensions",
              all(dim in scored.gated_by for dim in ("profitability", "integrity")),
              scored.gated_by)
        check("a passing gate adds no confidence_gate reason",
              "confidence_gate" not in scored.gated_by, scored.gated_by)
    check("C is not scored at all",
          build_wallet_score(
              FactorConn(computed=0, total=3), "WalletC", wallet_c(), [], CONFIG,
              AnchorCounts(0, 3), as_of=AS_OF) is None)
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


def seed_gate_wallet(conn) -> None:
    """Wallet E: three anchors, resolved metadata, priced entries, no recent trade.

    Every factor has an input except recency, which is 0.0 because no swap carries
    an event_time - the "one factor missing" case. Composite 77.00:
    0.30*1.00 coverage + 0.25*1.00 provenance + 0.20*0.60 sample + 0.10*1.00
    metadata, with recency contributing nothing.
    """
    mints = [f"E-mint-{i}" for i in range(3)]
    for mint in mints:
        conn.execute(
            "INSERT INTO tokens (mint, chain_id, metadata_json, metadata_completeness) "
            "VALUES (%s,'solana','{}','complete')", (mint,))

    # One priced entry with no event_time (provenance 1 of 2, recency 0), and one
    # episode opened on a sell (no entry buy exists) so the D17 diagnostic is
    # exercised: provenance is 0.50 and sell-only is 50%.
    conn.execute(
        "INSERT INTO transactions (signature, chain_id, slot, err) "
        "VALUES ('sig-e-0','solana',900,false)")
    conn.execute(
        "INSERT INTO swaps (signature, instruction_index, slot, chain_id, trader, "
        "  base_mint, quote_mint, side, base_amount_raw, quote_amount_raw, "
        "  base_decimals, quote_decimals, exec_price_quote, price_provenance, "
        "  fee_lamports, decoder_version, event_time) "
        "VALUES ('sig-e-0',0,900,'solana','WalletE',%s,"
        "  'So11111111111111111111111111111111111111112','buy',1000000,100000000,"
        "  6,9,100,'swap_derived',1000,'phase2/1',NULL)", (mints[0],))
    conn.execute(
        "INSERT INTO positions (address, chain_id, mint, opened_at, opened_slot, "
        "  entry_swap_signature) VALUES ('WalletE','solana',%s,%s,900,'sig-e-0')",
        (mints[0], AS_OF - dt.timedelta(days=10)))
    conn.execute(
        "INSERT INTO positions (address, chain_id, mint, opened_at, opened_slot) "
        "VALUES ('WalletE','solana',%s,%s,901)",
        (mints[1], AS_OF - dt.timedelta(days=9)))


def db_checks() -> bool:
    if psycopg is None:
        print("\nSKIP: psycopg not installed - database checks skipped")
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
        store(conn, "WalletE", [4, 4, 4], [0.02, 0.02, 0.02], [0.5, 0.5, 0.5])
        seed_anchor_metrics(conn, "WalletE", "E", 3, {MR: 3, BR: 3, PV: 3})

        # WalletF has everything a scored wallet needs — three computed aggregates on
        # three anchors — and one thing it must not survive: a quality-filter
        # rejection. If the filter's decision does not bind here, it does not bind
        # anywhere, and every verdict it records is decoration.
        from solscope.anchor_writer import write_verdict
        from solscope.quality_filter import RuleId, WalletVerdict

        store(conn, "WalletF", [4, 4, 4], [0.02, 0.02, 0.02], [0.5, 0.5, 0.5])
        seed_anchor_metrics(conn, "WalletF", "F", 3, {MR: 3, BR: 3, PV: 3})
        f_anchor = int(
            conn.execute("SELECT anchor_id FROM anchors WHERE mint='F-mint-0'").fetchone()[0]
        )
        write_verdict(
            conn, anchor_id=f_anchor, wallet="WalletF",
            verdict=WalletVerdict.REJECT_SANDWICH, rule_id=RuleId.SANDWICH,
            rule_version="quality_screen/v1",
            evidence={"quick_round_trips": 3, "max_seconds": 90},
        )
        seed_positions(conn)
        seed_gate_wallet(conn)

        report = run_consumer(
            conn, rule_version=RULE, as_of=AS_OF, window_start=WINDOW_START, window_end=AS_OF
        )
        check("six wallets seen, four scored",
              (report.wallets_seen, report.wallets_scored) == (6, 4),
              f"{report.wallets_seen}/{report.wallets_scored}")
        check("the filter-rejected wallet never reaches a score",
              report.skipped.get("WalletF") == "quality_filter:wallet.reject_sandwich",
              f"{report.skipped.get('WalletF')}")
        check("the rejection is reported apart from the measurement gaps",
              report.filter_rejected == {"WalletF": "wallet.reject_sandwich"},
              f"{report.filter_rejected}")
        check("the rejected wallet has no scores row",
              conn.execute("SELECT count(*) FROM scores WHERE address='WalletF'").fetchone()[0] == 0)
        check("the rejected wallet has no sub_scores row",
              conn.execute(
                  "SELECT count(*) FROM sub_scores s JOIN scores sc ON sc.id = s.score_id "
                  "WHERE sc.address = 'WalletF'").fetchone()[0] == 0)
        check("wallet C is skipped with its reason",
              report.skipped.get("WalletC") == "early_entry_not_rankable", f"{report.skipped}")
        check("four scores rows", count(conn, "scores") == 4,
              f"{count(conn, 'scores')}")
        check("twelve sub-scores rows (3 per scored wallet)", count(conn, "sub_scores") == 12,
              f"{count(conn, 'sub_scores')}")
        check("the run records the scoring config version",
              report.config_version == str(CONFIG["version"]), report.config_version)
        check("score_runs carries the aggregate rule version as its label",
              conn.execute(
                  "SELECT experiment_label, wallets_scored FROM score_runs WHERE score_run_id=%s",
                  (report.score_run_id,),
              ).fetchone() == (f"aggregate_rule:{RULE}", 4),
              "label/wallets_scored mismatch")
        check("score_config_versions records the weights used",
              count(conn, "score_config_versions", "config_version=%s", (report.config_version,))
              == 1)

        a = conn.execute(
            "SELECT s.id, s.overall_score, s.confidence_score, s.band, s.gated_by "
            "FROM scores s WHERE s.address='WalletA'"
        ).fetchone()
        check("A's row is neutral, excluded and explained",
              close(a[1], "50") and a[3] == "exclude"
              and "incomplete_score" in a[4],
              f"{a}")
        # 0.30*coverage(3/3) + 0.20*sample(min(1,3/5)) = 0.42; no positions, no
        # swaps, no tokens, so provenance/recency/metadata are 0.0 and are not
        # re-normalised away.
        check("A's stored confidence_score is the composite 42.00, not 0.00",
              close(a[2], "42"), f"{a[2]}")
        a_adjusted = conn.execute(
            "SELECT s.confidence_adjusted_score, s.band_basis, e.shrunk_value "
            "FROM scores s JOIN sub_scores e ON e.score_id = s.id "
            "AND e.dimension = 'early_entry' WHERE s.id = %s", (a[0],)).fetchone()
        check("A's adjusted is shrunk_early_entry x confidence/100 (25.16)",
              close(a_adjusted[0], "25.16"), f"{a_adjusted}")
        check("and the basis is recorded as shrunk_early_entry",
              a_adjusted[1] == "shrunk_early_entry", f"{a_adjusted}")
        a_band = conn.execute(
            "SELECT band, recommendation, gated_by FROM scores WHERE address='WalletA'"
        ).fetchone()
        check("A is banded exclude on the adjusted score",
              a_band[0] == "exclude" and a_band[1] == "exclude", f"{a_band}")
        check("A's gated_by says raw_below_investigate, not a gate failure",
              "raw_below_investigate" in a_band[2]
              and "confidence_gate" not in a_band[2], f"{a_band[2]}")
        check("adjusted is shrunk early_entry 59.90 x 42/100, not the placeholder",
              close(a_adjusted[2], "59.9") and close(a_adjusted[0], "25.16"),
              f"{a_adjusted}")

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
        check("early_entry carries no confidence of its own",
              early_a[6] is None, f"{early_a[6]}")
        check("the confidence dimension row stores the composite 42.00",
              close(conn.execute(
                  "SELECT confidence FROM sub_scores WHERE score_id=%s "
                  "AND dimension='confidence'", (a[0],)).fetchone()[0], "42"),
              "confidence sub-score not stored")

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
                "(SELECT id FROM scores WHERE address='WalletB') "
                "AND subject_id <> 'provenance_sell_only_pct'"
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
        check("all five factors are stored as evidence",
              {"factor.coverage", "factor.price_provenance", "factor.sample",
               "factor.recency", "factor.metadata"} <= set(factors),
              f"{sorted(factors)}")
        check("A's coverage factor is 1.0 and its sample factor 0.60 (3 anchors of 5)",
              close(factors.get("factor.coverage"), "1") and close(factors.get("factor.sample"), "0.6"),
              f"{factors}")
        check("A's missing inputs are stored as 0.0, not dropped",
              close(factors.get("factor.price_provenance"), "0")
              and close(factors.get("factor.recency"), "0")
              and close(factors.get("factor.metadata"), "0"),
              f"{factors}")
        check("the composite and both thresholds are stored as evidence",
              close(factors.get("confidence_composite"), "42")
              and close(factors.get("threshold.confidence_min"), "40")
              and close(factors.get("threshold.min_distinct_anchors"), "3"),
              f"{factors}")
        check("the confidence sub-score explains the composite and the gate",
              "100 x sum(weight x factor)" in conn.execute(
                  "SELECT explanation FROM sub_scores WHERE score_id=%s AND dimension='confidence'",
                  (a[0],),
              ).fetchone()[0])

        e = conn.execute(
            "SELECT id, confidence_score FROM scores WHERE address='WalletE'"
        ).fetchone()
        # 0.30*1.00 + 0.25*0.50 + 0.20*0.60 + 0.15*0.00 + 0.10*1.00 = 0.645
        check("E's composite is 64.50: one factor missing degrades, not disappears",
              close(e[1], "64.5"), f"{e[1]}")
        e_factors = dict(
            conn.execute(
                "SELECT subject_id, value FROM score_evidence "
                "WHERE score_id=%s AND dimension='confidence'", (e[0],),
            ).fetchall()
        )
        check("E's recency is the missing factor and is 0.0",
              close(e_factors.get("factor.recency"), "0"), f"{e_factors}")
        check("E's provenance is 0.50 (one of two entries has an entry buy)",
              close(e_factors.get("factor.price_provenance"), "0.5"), f"{e_factors}")
        e_sell = conn.execute(
            "SELECT subject_id, value, note FROM score_evidence "
            "WHERE score_id=%s AND dimension='early_entry' "
            "AND subject_id='provenance_sell_only_pct'", (e[0],)).fetchone()
        check("D17: sell-only percentage is stored under early_entry",
              e_sell is not None and close(e_sell[1], "50"), f"{e_sell}")
        check("D17: its note distinguishes routing from an unobserved history",
              e_sell is not None and "predates observation" in e_sell[2],
              f"{e_sell}")
        print(f"  note  score_evidence sample: dimension=early_entry "
              f"subject_id={e_sell[0]} value={e_sell[1]}")

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
              third.wallets_scored == 3 and b_rows == 0,
              f"scored={third.wallets_scored} b_rows={b_rows}")
        check("pruning leaves the other wallets intact",
              count(conn, "scores") == 3, f"{count(conn, 'scores')}")

        print("\n-- solscope scores (read command) ---------------------------------")
        import argparse as _argparse
        import contextlib
        import io

        from psycopg.conninfo import make_conninfo

        from solscope.cli import Ctx, _cmd_scores
        from solscope.config import load_budget

        dsn_text = make_conninfo(**conn_kwargs)
        ctx = Ctx(budget=load_budget(), dsn=dsn_text, verbose=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = _cmd_scores(ctx, _argparse.Namespace(
                wallet=None, band=None, limit=50, as_of=None))
        text = out.getvalue()
        print(text)
        check("scores exits 0 against a real database", rc == 0)
        check("it lists every stored wallet",
              all(wallet in text for wallet in ("WalletA", "WalletD", "WalletE")),
              text)
        check("it shows band, confidence, adjusted and the reason",
              all(token in text for token in ("exclude", "42.00", "raw_below_investigate")),
              text)
        check("it states the band basis",
              "basis: shrunk_early_entry" in text, text)

        narrow = io.StringIO()
        with contextlib.redirect_stdout(narrow):
            rc_narrow = _cmd_scores(ctx, _argparse.Namespace(
                wallet="WalletA", band=None, limit=50, as_of=None))
        narrow_text = narrow.getvalue()
        check("--wallet narrows the report",
              rc_narrow == 0 and "WalletA" in narrow_text
              and "WalletD" not in narrow_text, narrow_text)
        filtered = io.StringIO()
        with contextlib.redirect_stdout(filtered):
            _cmd_scores(ctx, _argparse.Namespace(
                wallet=None, band="track", limit=50, as_of=None))
        check("--band with no matches says so instead of printing an empty table",
              "no scores stored" in filtered.getvalue(), filtered.getvalue())

        print("\n-- solscope consumer (run command) --------------------------------")
        from solscope.cli import _cmd_consumer

        scores_before = count(conn, "scores")
        dry_out = io.StringIO()
        with contextlib.redirect_stdout(dry_out):
            rc_dry = _cmd_consumer(ctx, _argparse.Namespace(
                rule_version=None, as_of=AS_OF, dry_run=True))
        dry_text = dry_out.getvalue()
        print(dry_text)
        check("consumer --dry-run exits 0 against a real database", rc_dry == 0, dry_text)
        check("the rule version defaults to what wallet_aggregates holds",
              f"rule_version={RULE}" in dry_text, dry_text)
        check("--dry-run writes no scores rows",
              count(conn, "scores") == scores_before,
              f"{scores_before} -> {count(conn, 'scores')}")
        # By this point in the suite two wallets have been pruned by a later run, so
        # the aggregate set is 5 wallets / 3 scorable — the same counts `scores`
        # just listed above. WalletF is gathered but gated out by the quality filter,
        # so it counts in "seen" and in "gated out", never in "scored".
        check("the summary counts gathered wallets and their bands",
              "wallets seen   5" in dry_text and "scored         3" in dry_text
              and "gated out      2" in dry_text, dry_text)
        check("the summary names the filter separately from the measurement gaps",
              "filter rejects 1" in dry_text
              and "quality_filter:wallet.reject_sandwich 1" in dry_text, dry_text)
        check("it prints a band for every name, including zeros",
              all(band in dry_text for band in
                  ("track", "watch", "investigate", "promising_thin", "exclude")),
              dry_text)

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
