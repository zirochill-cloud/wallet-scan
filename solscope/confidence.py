"""Phase 1b §12a — the five data-confidence factors.

Each factor answers one question about *evidence*, never about performance:

===========  ==========================================================
coverage     what share of this wallet's anchors was actually measured?
provenance   what share of its entries were priced from the swap itself?
sample       how many anchors back the numbers (against full credit at 5)?
recency      how long since the wallet was last seen trading?
metadata     what share of its anchor tokens have resolved mint metadata?
===========  ==========================================================

The structural claim of `docs/PHASE1B_SECTION12.md` §1 lives in this module:
**no factor may read a performance column.** Nothing here touches
``sub_scores.value``, ``realized_pnl``, ``roi``, ``positions.realized_capture`` or
any other outcome measure, so a good wallet and a bad wallet with identical data
receive identical confidence. ``tests/test_confidence.py`` enforces that
mechanically against the queries this module actually executes, so a later edit
cannot quietly introduce a performance input.

Conventions, all from the design:

* Every factor is computed for **every** scored wallet and returns a ``Decimal``
  in ``[0, 1]``, quantised to 4 dp — the same precision as
  ``wallet_aggregates.coverage`` (``NUMERIC(6,4)``) and as the design's fixtures.
* A missing input scores ``ZERO``. It is never ``None``, never imputed to a
  neutral value, and never re-normalised away: an unmeasured wallet must not
  receive the benefit of a measurement it does not have. The per-factor *reason*
  is attached in S12b, where the composite is explained and stored.
* ``ZERO`` is a legitimate answer ("no evidence"), not an error.

The composite (§3) and the gate (§4) also live here: ``composite_confidence``
weights the five factors, ``evaluate_gate`` decides rankability, and
``confidence_adjusted_score`` applies the confidence pull. Band derivation (§5) is
S12c and is deliberately absent — a band rule applied before the composite is
stored would look correct in tests and mean nothing.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP
from typing import Any, Mapping

ZERO = Decimal("0")
ONE = Decimal("1")
_TWO_DP = Decimal("0.01")
_FOUR_DP = Decimal("0.0001")
_PLACES = _FOUR_DP

#: Distinct computed anchors at which more anchors stop adding confidence (D5).
DEFAULT_SAMPLE_FULL_CREDIT_AT = 5
#: Days for recency to halve (existing `confidence.recency_half_life_days`).
DEFAULT_RECENCY_HALF_LIFE_DAYS = 60

#: `swaps.price_provenance` value that counts as a self-evidencing entry price.
TRUSTED_PROVENANCE = "swap_derived"


# ---------------------------------------------------------------------------
# queries — named, so that a test can audit exactly what each factor reads
# ---------------------------------------------------------------------------

COVERAGE_SQL = """
    SELECT count(DISTINCT anchor_id) FILTER (WHERE status = 'computed'),
           count(DISTINCT anchor_id)
    FROM anchor_wallet_metrics
    WHERE wallet = %s
"""

# The same denominator as coverage, deliberately: D1 established that the
# observable unit is a distinct anchor, not a metric row. Counting rows would
# give a wallet three observations of one token and call it sample.
SAMPLE_SQL = COVERAGE_SQL

# One row per position, joined to the buy that opened it. LATERAL + LIMIT 1
# because a signature is not unique in `swaps` (one transaction can carry several
# instructions); the earliest buy instruction is the entry. LEFT, because a
# position with no entry signature must count against the denominator rather than
# vanish from it.
PROVENANCE_SQL = """
    SELECT count(*) FILTER (WHERE s.price_provenance = 'swap_derived'),
           count(*)
    FROM positions p
    LEFT JOIN LATERAL (
        SELECT price_provenance
        FROM swaps
        WHERE signature = p.entry_swap_signature
          AND trader = p.address
          AND base_mint = p.mint
          AND side = 'buy'
        ORDER BY slot, instruction_index, id
        LIMIT 1
    ) s ON true
    WHERE p.address = %s
"""

RECENCY_SQL = """
    SELECT max(event_time) FROM swaps WHERE trader = %s
"""

# Anchors the wallet was measured on, LEFT JOINed to token metadata so an anchor
# that never got a `tokens` row counts as unresolved instead of disappearing.
# tokens.mint is the primary key, so the join cannot fan out.
METADATA_SQL = """
    SELECT count(DISTINCT a.anchor_id) FILTER (
               WHERE t.mint IS NOT NULL
                 AND t.metadata_json IS NOT NULL
                 AND t.metadata_completeness <> 'unknown'),
           count(DISTINCT a.anchor_id)
    FROM anchors a
    JOIN anchor_wallet_metrics m
      ON m.anchor_id = a.anchor_id AND m.wallet = %s
    LEFT JOIN tokens t ON t.mint = a.mint
"""

#: Diagnostic (D17): how many of the wallet's positions opened on a sell, i.e. on
#: inventory acquired before the observed window. Provenance counts those as zero
#: credit — correctly — but the number alone cannot tell a reviewer whether the
#: wallet routes badly or simply predates observation. This is that distinction.
SELL_ONLY_SQL = """
    SELECT count(*) FILTER (WHERE entry_swap_signature IS NULL), count(*)
    FROM positions
    WHERE address = %s
"""

#: Every statement this module executes, keyed by factor name.
QUERIES: dict[str, str] = {
    "coverage": COVERAGE_SQL,
    "price_provenance": PROVENANCE_SQL,
    "sample": SAMPLE_SQL,
    "recency": RECENCY_SQL,
    "metadata": METADATA_SQL,
    "provenance_sell_only_pct": SELL_ONLY_SQL,
}

#: Factor name -> the ``confidence:`` key that weights it.
WEIGHT_KEYS: dict[str, str] = {
    "coverage": "weight_coverage",
    "price_provenance": "weight_price_provenance",
    "sample": "weight_sample",
    "recency": "weight_recency",
    "metadata": "weight_metadata",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ratio(numerator: int, denominator: int) -> Decimal:
    """Share, 0.0 when there is nothing to divide — absence is not coverage."""
    if denominator <= 0:
        return ZERO
    return (Decimal(int(numerator)) / Decimal(int(denominator))).quantize(
        _PLACES, rounding=ROUND_HALF_UP)


def _confidence_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    section = (config or {}).get("confidence")
    return section if isinstance(section, Mapping) else {}


def _counts(conn, sql: str, wallet: str) -> tuple[int, int]:
    row = conn.execute(sql, (wallet,)).fetchone()
    if row is None:
        return 0, 0
    return int(row[0] or 0), int(row[1] or 0)


# ---------------------------------------------------------------------------
# the five factors
# ---------------------------------------------------------------------------

def coverage_factor(conn, wallet: str) -> Decimal:
    """Share of the wallet's anchors whose metrics were computed — 0..1.

    Input: ``anchor_wallet_metrics`` distinct anchors, computed / total. This is
    D1's denominator, so it agrees with ``wallet_aggregates.coverage`` by
    construction.

    Fails low when the aggregator excluded most anchors (insufficient forward
    window, no genesis time, unparseable pools): the score then describes a
    minority of the set. A wallet with no anchor rows at all scores 0.0, which
    reads correctly as "no coverage", not "full coverage".
    """
    computed, total = _counts(conn, COVERAGE_SQL, wallet)
    return _ratio(computed, total)


def provenance_factor(conn, wallet: str) -> Decimal:
    """Share of entries priced from the swap itself — 0..1.

    Input: ``swaps.price_provenance`` over the buy named by
    ``positions.entry_swap_signature`` (D10): ``swap_derived`` counts 1,
    everything else 0 (``pool_ohlcv`` candles, ``snapshot_api``, ``provider_parsed``,
    ``manual``).

    Fails low when entry prices were interpolated or snapshot: entry timing and
    valuation are exactly what the early-entry dimensions are about. A position
    with no entry signature (sell-opened episode) counts 0, not skipped.
    """
    trusted, entries = _counts(conn, PROVENANCE_SQL, wallet)
    return _ratio(trusted, entries)


def sample_factor(conn, wallet: str, config: Mapping[str, Any] | None) -> Decimal:
    """``min(1, distinct_computed_anchors / sample_full_credit_at)`` — 0..1.

    ``sample_full_credit_at`` defaults to 5 (D5) and is read from
    ``confidence.sample_full_credit_at``. It answers "how many anchors is
    plenty"; the gate's three-anchor floor is a separate key (§4) and is
    deliberately not derived from it, so calibrating this divisor cannot move
    the rankability floor.

    Two anchors score 0.40, five or more score 1.00. Zero anchors score 0.0.
    """
    divisor = int(_confidence_config(config).get(
        "sample_full_credit_at", DEFAULT_SAMPLE_FULL_CREDIT_AT))
    if divisor <= 0:
        raise ValueError(
            f"confidence.sample_full_credit_at must be > 0, got {divisor}")
    computed, _total = _counts(conn, SAMPLE_SQL, wallet)
    if computed <= 0:
        return ZERO
    return min(ONE, (Decimal(computed) / Decimal(divisor)).quantize(
        _PLACES, rounding=ROUND_HALF_UP))


def recency_factor(conn, wallet: str, *, as_of: _dt.datetime,
                   config: Mapping[str, Any] | None) -> Decimal:
    """``0.5 ** (days_since_last_trade / recency_half_life_days)`` — (0, 1].

    Input: ``max(swaps.event_time)`` for the wallet, measured against ``as_of``.
    Half-life, not e-folding (D14): the key is ``recency_half_life_days``, so 7
    days at 60 is 0.9223 and 60 days is 0.5000.

    A wallet with no observed trade scores 0.0 with the reason recorded — it is
    not imputed to 1.0 ("probably still active") or to the median. A last trade
    after ``as_of`` clamps to 0 days (1.0) rather than producing a factor > 1.
    """
    half_life = float(_confidence_config(config).get(
        "recency_half_life_days", DEFAULT_RECENCY_HALF_LIFE_DAYS))
    if half_life <= 0:
        raise ValueError(
            f"confidence.recency_half_life_days must be > 0, got {half_life}")
    row = conn.execute(RECENCY_SQL, (wallet,)).fetchone()
    last = row[0] if row else None
    if last is None:
        return ZERO
    days = max(0.0, (as_of - last).total_seconds() / 86_400.0)
    return Decimal(str(0.5 ** (days / half_life))).quantize(
        _PLACES, rounding=ROUND_HALF_UP)


def metadata_factor(conn, wallet: str) -> Decimal:
    """Share of anchor tokens with resolved mint metadata — 0..1.

    Input: over the anchors the wallet was measured on,
    ``tokens.metadata_json IS NOT NULL AND tokens.metadata_completeness <> 'unknown'``,
    with ``LEFT JOIN`` so an anchor that has no ``tokens`` row yet (a seed that
    precedes ingestion) counts as unresolved.

    Metadata enters no dimension's arithmetic; it caps how much of the token
    population can be characterised, which is why its weight is smallest.
    """
    resolved, anchors = _counts(conn, METADATA_SQL, wallet)
    return _ratio(resolved, anchors)


def sell_only_pct(conn, wallet: str) -> Decimal:
    """Share of the wallet's positions that opened on a sell — 0..100, 2 dp (D17).

    Not a factor. A sell-opened position has no entry buy, so provenance gives it
    no credit; the percentage says whether that was the wallet routing badly or
    its history simply starting before our observation window. Zero when the
    wallet has no positions (then the provenance factor is 0 for a different,
    already-recorded reason: no entries at all).
    """
    row = conn.execute(SELL_ONLY_SQL, (wallet,)).fetchone()
    if row is None or int(row[1] or 0) <= 0:
        return ZERO.quantize(_TWO_DP)
    return (Decimal(int(row[0] or 0)) * Decimal(100) / Decimal(int(row[1]))).quantize(
        _TWO_DP, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# the composite and the gate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateResult:
    """What the confidence gate decided for one wallet, with everything it used.

    ``components`` holds the five factor values, ``weights`` the configured
    weight of each, and ``thresholds`` the two gate keys actually applied — so
    the evidence table can be rebuilt from this object without re-reading config.
    ``gate_reason`` names the first failing conjunct in words; on success it is
    ``"all thresholds met"``.
    """

    wallet: str
    composite_confidence: Decimal
    distinct_anchors: int
    gate_passed: bool
    gate_reason: str
    components: Mapping[str, Decimal]
    weights: Mapping[str, Decimal]
    thresholds: Mapping[str, Decimal]


def factor_values(conn, wallet: str, *, as_of: _dt.datetime,
                  config: Mapping[str, Any] | None) -> dict[str, Decimal]:
    """All five factors for one wallet, in the design's reporting order."""
    return {
        "coverage": coverage_factor(conn, wallet),
        "price_provenance": provenance_factor(conn, wallet),
        "sample": sample_factor(conn, wallet, config),
        "recency": recency_factor(conn, wallet, as_of=as_of, config=config),
        "metadata": metadata_factor(conn, wallet),
    }


def _weights(config: Mapping[str, Any] | None) -> dict[str, Decimal]:
    """Read the five weights; a missing or non-unit set is an error, not a guess."""
    section = _confidence_config(config)
    weights: dict[str, Decimal] = {}
    for name, key in WEIGHT_KEYS.items():
        if key not in section:
            raise ValueError(f"scoring.yaml: missing confidence.{key}")
        weights[name] = Decimal(str(section[key]))
    total = sum(weights.values(), ZERO)
    if total != ONE:
        raise ValueError(
            f"scoring.yaml: confidence weights sum to {total}, expected 1.00")
    return weights


def _composite(factors: Mapping[str, Decimal],
               weights: Mapping[str, Decimal]) -> Decimal:
    """``100 x sum(weight x factor)``, clamped to [0, 100], stored at 2 dp.

    No re-normalisation: a zero factor costs exactly its weight. Rescaling the
    others would let a wallet with unmeasured provenance end up *more* confident
    than one with measured-and-poor provenance.
    """
    total = sum((weights[name] * factors[name] for name in WEIGHT_KEYS), ZERO)
    value = (Decimal(100) * total).quantize(_TWO_DP, rounding=ROUND_HALF_UP)
    return max(ZERO, min(Decimal(100), value))


def composite_confidence(conn, wallet: str, *, as_of: _dt.datetime,
                         config: Mapping[str, Any] | None) -> Decimal:
    """The §12 confidence score for one wallet — 0..100 at 2 dp.

    Always computed, for every scored wallet: all five factors are read even when
    their inputs are missing, and a missing input contributes 0.0 rather than
    dropping the term. ``0.00`` is a valid, reportable value meaning "no
    evidence"; it is never NULL and never an error.
    """
    factors = factor_values(conn, wallet, as_of=as_of, config=config)
    return _composite(factors, _weights(config))


def _gate_thresholds(config: Mapping[str, Any] | None) -> dict[str, Decimal]:
    """The two gate keys. Never fall back to a hardcoded default (D5/D12)."""
    section = config.get("confidence_gate") if isinstance(config, Mapping) else None
    if not isinstance(section, Mapping):
        raise ValueError("scoring.yaml: missing 'confidence_gate' thresholds")
    thresholds: dict[str, Decimal] = {}
    for key in ("confidence_min", "min_distinct_anchors"):
        if key not in section:
            raise ValueError(f"scoring.yaml: missing confidence_gate.{key}")
        thresholds[key] = Decimal(str(section[key]))
    return thresholds


def evaluate_gate(conn, wallet: str, *, as_of: _dt.datetime,
                  config: Mapping[str, Any] | None) -> GateResult:
    """Decide whether a wallet may be ranked — §4's two conjuncts.

    ``composite >= confidence_gate.confidence_min`` **and**
    ``distinct_computed_anchors >= confidence_gate.min_distinct_anchors``.

    The anchor conjunct is checked first because it is the validity rule: a
    wallet with two anchors cannot be ranked whatever its confidence says, and
    naming the anchors (rather than a confidence that may be well above 40) is
    the honest explanation. Both numbers are config keys; neither is derived
    from the other.
    """
    factors = factor_values(conn, wallet, as_of=as_of, config=config)
    weights = _weights(config)
    composite = _composite(factors, weights)
    anchors, _total = _counts(conn, COVERAGE_SQL, wallet)
    thresholds = _gate_thresholds(config)

    minimum_anchors = int(thresholds["min_distinct_anchors"])
    minimum_confidence = thresholds["confidence_min"]
    if anchors < minimum_anchors:
        passed, reason = False, (f"distinct_anchors below minimum "
                                 f"({anchors} < {minimum_anchors})")
    elif composite < minimum_confidence:
        passed, reason = False, (f"confidence below minimum "
                                 f"({composite} < {minimum_confidence.quantize(_TWO_DP)})")
    else:
        passed, reason = True, "all thresholds met"

    return GateResult(
        wallet=wallet,
        composite_confidence=composite,
        distinct_anchors=anchors,
        gate_passed=passed,
        gate_reason=reason,
        components=factors,
        weights=weights,
        thresholds=thresholds,
    )


def confidence_adjusted_score(overall: Decimal, confidence: Decimal) -> Decimal:
    """``50 + (overall - 50) * confidence/100`` — doc 05 §12's pull to neutral.

    Low confidence neither rewards nor punishes: it declines to make the claim,
    so a 90 at 40 confidence becomes 66 and a 20 at 40 becomes 38.
    """
    overall = Decimal(overall)
    confidence = Decimal(confidence)
    return (Decimal(50) + (overall - Decimal(50)) * (confidence / Decimal(100))
            ).quantize(_TWO_DP, rounding=ROUND_HALF_EVEN)
