"""Phase 1b scoring consumer — stored wallet aggregates become scores.

The aggregator (`solscope.aggregation`) answers "what is this wallet's value for
this metric".  This module answers the next question: "what does that buy the
wallet in the score", and writes the answer into the existing scoring tables
(``scores``, ``sub_scores``, ``score_evidence``) through ``score_runs`` and
``score_config_versions``.

What it reads
-------------
* ``wallet_aggregates`` — the three Phase 1b early-entry metrics, at one
  ``rule_version``.
* ``positions`` + ``tokens`` + ``cohort_benchmarks`` — the wallet's closed
  positions and the launch-week benchmark that makes their multiples comparable
  (docs/05-scoring-methodology.md §9).  ``cohort_benchmarks.available_at`` gates
  the lookup: a benchmark over a full 30-day window is not readable on day one of
  that window.

What it writes
--------------
* one ``scores`` row per (run, wallet), keyed by ``rule_version`` through the run;
* one ``sub_scores`` row per emitted dimension (``early_entry``,
  ``token_selection``, and a ``confidence`` row that carries the two new factors
  in its ``explanation``);
* ``score_evidence`` rows for every component, so a sub-score can be rebuilt from
  the database alone.

Weights and re-normalisation
----------------------------
Weights come from ``config/scoring.yaml``; nothing is hardcoded here.  A metric
that is ``not_rankable``/``not_computable``, absent, or configured at weight 0 is
dropped and the remaining weights are re-scaled to sum to 1.0 **within the
dimension**.  A missing metric must never be scored as 0: that would turn a
coverage gap into a behavioural verdict.  If no metric in a dimension survives,
the dimension has no value and the wallet is not scored this phase.

Unit mapping (the one modelling decision this module makes)
-----------------------------------------------------------
The three aggregates have different units, so each becomes a bounded credit in
[0, 1] before weighting:

===========================  ===========================================
``max_subsequent_return``    ``r / (1 + r)`` — 1x -> 0.50, 4x -> 0.80,
                             99x -> 0.99.  A bounded, monotone reading of an
                             unbounded multiple that needs no new tunable
                             constant, so ``scoring.yaml`` stays untouched.
``buyer_rank_at_entry``      ``1 - percentile`` — 0.001 (first buyer) -> 0.999.
``pct_entries_before_volume_spike``  the fraction itself.
===========================  ===========================================

Composite = ``100 x sum(weight_used x credit)``, then shrunk toward the neutral
prior exactly as ``config/scoring.yaml`` specifies
(``shrunk = n/(n+k) * raw + k/(n+k) * prior``).  Shrinkage is applied because the
whole point of this phase is that anchor counts are small.

Anchor counts, not metric rows
------------------------------
``anchor_sample_size`` counts **distinct anchors**::

    SELECT count(DISTINCT anchor_id) FROM anchor_wallet_metrics
    WHERE wallet = %s AND status = 'computed'

Summing ``anchor_count_computed`` over the three metrics would count each anchor up
to three times (one row per metric) and credit a 3-anchor wallet with 9 independent
observations.  ``anchor_coverage`` uses the same denominator: distinct computed
anchors / distinct anchors.  Neither is available from ``wallet_aggregates``, which
stores per-metric counts only.

Confidence: the §12 composite, then the gate
-------------------------------------------
``scores.confidence_score`` is now the five-factor composite from
``solscope.confidence`` (doc §12, S12b):

    confidence = 100 x (0.30*coverage + 0.25*price_provenance + 0.20*sample
                        + 0.15*recency + 0.10*metadata)

All five factors are computed for every scored wallet and stored as evidence; a
missing input contributes 0.0 and the other weights are **not** re-normalised.
The gate is ``composite >= confidence_gate.confidence_min`` AND
``distinct_computed_anchors >= confidence_gate.min_distinct_anchors`` (both from
``scoring.yaml``).  Option 3's ``blend_confidence`` — which gated on a
``confidence`` that was always 0.00 and therefore excluded every wallet — is
retired, along with its three config keys.

Band derivation is S12c. Until then every row keeps ``band='exclude'`` with a
``gated_by`` that names the reason, so no wallet is ranked on a partial score.

Why ``overall_score`` is neutral
--------------------------------
Five of the seven dimensions are not built yet (S1–S3).  An "overall" built from
the two that exist would read as a seven-dimension score to every later consumer,
so the row carries the neutral 50.00, ``band='exclude'`` and a ``gated_by`` that
names the missing dimensions.  The real output of this phase is the sub-scores.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Mapping, Sequence

from .confidence import (
    GateResult,
    confidence_adjusted_score,
    evaluate_gate,
    sell_only_pct,
    shrink_for_confidence,
)
from .config import CONFIG_DIR, ConfigError, load_scoring

__all__ = [
    "EARLY_ENTRY_METRICS",
    "PIPELINE_VERSION",
    "AnchorCounts",
    "ConsumerError",
    "ConsumerReport",
    "DimensionScore",
    "StoredAggregate",
    "WalletScore",
    "build_wallet_score",
    "cohort_excess_component",
    "credit_for",
    "early_entry_dimension",
    "excess_for",
    "launch_week_key",
    "load_anchor_counts",
    "load_position_excesses",
    "renormalise",
    "run_consumer",
    "shrink",
    "token_selection_dimension",
    "write_wallet_score",
]

_LOG = logging.getLogger(__name__)

#: Bumped when the consumer's own behaviour changes in a way that changes numbers.
PIPELINE_VERSION = "phase1b-consumer/1"

#: The three Phase 1b early-entry metrics, in reporting order.
EARLY_ENTRY_METRICS: tuple[str, ...] = (
    "max_subsequent_return",
    "buyer_rank_at_entry",
    "pct_entries_before_volume_spike",
)

#: Dimension names exactly as ``sub_scores.dimension`` spells them.
DIM_EARLY_ENTRY = "early_entry"
DIM_TOKEN_SELECTION = "token_selection"
DIM_CONFIDENCE = "confidence"

#: Dimensions this phase cannot compute. Named on the scores row so a reader of
#: the row cannot mistake a partial score for a full one.
UNBUILT_DIMENSIONS: tuple[str, ...] = (
    "profitability",
    "repeatability",
    "risk",
    "exit_effectiveness",
    "integrity",
)

#: ``overall_score`` when the seven dimensions are not all available. Neutral, by
#: the same logic as ``confidence_adjusted_score``: absent evidence neither
#: rewards nor punishes.
PARTIAL_OVERALL_SCORE = Decimal("50.00")

#: Benchmark metric key and kind used for token selection (§9 of doc 05).
COHORT_METRIC_KEY = "max_multiple_30d"

_ONE = Decimal(1)
_HUNDRED = Decimal(100)
_TWO_DP = Decimal("0.01")
_FOUR_DP = Decimal("0.0001")
_TEN_DP = Decimal("0.0000000001")


class ConsumerError(ValueError):
    """The consumer was asked to produce something it cannot defend."""


def _dec(value: Any) -> Decimal:
    """Config numbers and DB NUMERICs both arrive as something convertible."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _clamp01(value: Decimal) -> Decimal:
    return max(Decimal(0), min(value, _ONE))


def _q2(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_TWO_DP, rounding=ROUND_HALF_EVEN)


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_FOUR_DP, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StoredAggregate:
    """One row of ``wallet_aggregates``, as the consumer sees it.

    ``status`` is carried rather than inferred from ``value`` for the same reason
    the aggregator carries it: ``not_rankable`` (measured, too few anchors) and
    ``not_computable`` (nothing measured) both mean "no number", and a consumer
    that flattened them into ``None`` could not tell a thin wallet from an empty
    one when re-normalising weights.
    """

    metric_name: str
    value: Decimal | None
    status: str
    anchor_count_computed: int = 0
    anchor_count_excluded: int = 0
    coverage: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.metric_name not in EARLY_ENTRY_METRICS:
            raise ConsumerError(f"unknown metric {self.metric_name!r}")
        if self.status not in {"computed", "not_rankable", "not_computable"}:
            raise ConsumerError(f"unknown aggregate status {self.status!r}")
        if (self.status == "computed") != (self.value is not None):
            raise ConsumerError(
                f"{self.metric_name}: status {self.status!r} with value {self.value!r}"
            )


@dataclass(frozen=True)
class AnchorCounts:
    """Distinct anchors behind one wallet's aggregates — anchors, not metric rows.

    ``computed`` counts anchors with at least one ``status='computed'`` metric row;
    ``total`` counts every anchor with any row at all.  A 3-anchor wallet whose three
    metrics all computed therefore has ``AnchorCounts(3, 3)``, not 9: counting metric
    rows would triple-count the same three observations.
    """

    computed: int
    total: int


@dataclass(frozen=True)
class Component:
    """One metric's contribution to a dimension, before and after re-weighting."""

    metric: str
    weight: Decimal          # configured weight
    weight_used: Decimal     # after re-normalisation within the dimension
    raw: Decimal             # the stored aggregate value
    credit: Decimal          # 0..1 after the metric's own unit mapping


@dataclass(frozen=True)
class DimensionScore:
    """A dimension's value plus everything needed to explain it."""

    dimension: str
    status: str                       # computed | not_rankable | not_computable
    raw_value: Decimal | None         # 0..100, before shrinkage
    shrunk_value: Decimal | None      # 0..100, after shrinkage
    weight: Decimal                   # top-level dimension weight
    sample_size: int
    shrinkage_applied: Decimal
    components: tuple[Component, ...]
    explanation: str
    anchor_count_computed: int = 0
    anchor_count_excluded: int = 0
    #: The §12 composite, carried by the ``confidence`` dimension row only.
    #: ``None`` for dimensions whose confidence is not a separate number.
    confidence: Decimal | None = None

    @property
    def is_computed(self) -> bool:
        return self.status == "computed"

    @property
    def weights_used(self) -> dict[str, Decimal]:
        return {component.metric: component.weight_used for component in self.components}


@dataclass(frozen=True)
class WalletScore:
    """One wallet's scores row plus its sub-scores, ready to write."""

    wallet: str
    early_entry: DimensionScore
    token_selection: DimensionScore
    anchor_sample_size: Decimal
    anchor_coverage: Decimal
    overall_score: Decimal
    confidence_score: Decimal
    confidence_adjusted_score: Decimal
    band: str
    recommendation: str
    gated_by: str
    gate: GateResult
    #: What the confidence pull was applied to; D21 makes this early-entry-specific
    #: until the other five dimensions exist.
    band_basis: str = "shrunk_early_entry"


@dataclass
class ConsumerReport:
    """What one consumer run did."""

    rule_version: str
    config_version: str
    as_of: _dt.datetime
    window_start: _dt.datetime
    window_end: _dt.datetime
    score_run_id: int | None = None
    wallets_seen: int = 0
    wallets_scored: int = 0
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def wallets_skipped(self) -> int:
        return len(self.skipped)


# ---------------------------------------------------------------------------
# Pure scoring logic
# ---------------------------------------------------------------------------
def credit_for(metric: str, value: Decimal) -> Decimal:
    """Map one aggregate onto a bounded credit in [0, 1].

    See the module docstring for why each metric maps the way it does. Clamping is
    not cosmetic: ``buyer_rank_at_entry`` and the pre-volume fraction are stored in
    [0, 1], and a value outside that range would silently over-weight one metric
    inside the weighted sum.
    """
    if metric == "max_subsequent_return":
        if value < 0:
            # The aggregator already floors its own result; a negative value here
            # would have to come from a hand-written row. Credit is 0, not negative.
            return Decimal(0)
        return _clamp01(value / (_ONE + value))
    if metric == "buyer_rank_at_entry":
        return _clamp01(_ONE - value)
    if metric == "pct_entries_before_volume_spike":
        return _clamp01(value)
    raise ConsumerError(f"unknown metric {metric!r}")


def renormalise(weights: Sequence[tuple[str, Decimal]]) -> dict[str, Decimal]:
    """Scale the surviving weights so they sum to exactly 1.0.

    The last weight absorbs the rounding remainder, so the stored weights always
    add to 1.0 as written rather than to 0.9999999 — a sub-score whose weights do
    not sum to 1 is the kind of thing that gets noticed a year later.
    """
    kept = [(name, weight) for name, weight in weights if weight > 0]
    if not kept:
        raise ConsumerError("no positive weights left to re-normalise")
    total = sum((weight for _, weight in kept), Decimal(0))
    scaled: dict[str, Decimal] = {}
    running = Decimal(0)
    for index, (name, weight) in enumerate(kept):
        if index == len(kept) - 1:
            scaled[name] = _ONE - running
        else:
            share = (weight / total).quantize(_TEN_DP, rounding=ROUND_HALF_EVEN)
            scaled[name] = share
            running += share
    return scaled


def shrink(
    raw: Decimal, sample_size: int, *, k: Decimal, prior: Decimal
) -> tuple[Decimal, Decimal]:
    """``n/(n+k) * raw + k/(n+k) * prior``. Returns (shrunk, applied factor)."""
    if k < 0:
        raise ConsumerError(f"shrinkage k must be >= 0, got {k}")
    if sample_size <= 0:
        return prior, Decimal(0)
    n = Decimal(sample_size)
    factor = n / (n + k) if (n + k) else Decimal(1)
    shrunk = factor * raw + (_ONE - factor) * prior
    return shrunk, factor


def _dimension_weights(section: Mapping[str, Any], metrics: Sequence[str]) -> dict[str, Decimal]:
    """Read ``weight_<metric>`` keys out of a scoring.yaml dimension block."""
    weights: dict[str, Decimal] = {}
    for metric in metrics:
        key = f"weight_{metric}"
        if key not in section:
            raise ConsumerError(f"scoring.yaml: missing {key!r}")
        weights[metric] = _dec(section[key])
    return weights


def _explain(
    dimension: str,
    status: str,
    composite: Decimal | None,
    components: Sequence[Component],
    sample_size: int,
    factor: Decimal,
    k: Decimal,
    prior: Decimal,
    extra: str = "",
) -> str:
    """Deterministic prose for ``sub_scores.explanation`` (NOT NULL in the schema)."""
    if composite is None or not components:
        return f"{dimension} {status}: no metric could be scored{('; ' + extra) if extra else ''}"
    parts = ", ".join(
        f"{c.metric} {c.raw:.4f} -> {c.credit * _HUNDRED:.2f} (w {c.weight_used:.4f})"
        for c in components
    )
    text = (
        f"{dimension} {_q2(composite)} ({status}) from {len(components)} metric(s): {parts}; "
        f"shrinkage {factor:.4f} (n={sample_size}, k={k}, prior={prior})"
    )
    return f"{text}; {extra}" if extra else text


def early_entry_dimension(
    aggregates: Mapping[str, StoredAggregate],
    config: Mapping[str, Any],
    anchors: AnchorCounts,
) -> DimensionScore:
    """The ``early_entry`` composite for one wallet.

    Drops every metric that is absent, non-computed, or configured at weight 0,
    then re-normalises the rest. When nothing survives, the dimension reports
    ``not_rankable`` if any anchor was measured anywhere (the wallet exists in the
    data but cannot be ranked) and ``not_computable`` otherwise.

    ``anchors`` must count distinct anchors, not metric rows: it is the ``n`` in the
    shrinkage formula, and counting one anchor three times would overstate the
    evidence behind the score by a factor of three.
    """
    section = config["early_entry"]
    weights = _dimension_weights(section, EARLY_ENTRY_METRICS)
    dimension_weight = _dec(config["weights"]["early_entry"])
    k = _dec(config["shrinkage"]["k"]["early_entry"])
    prior = _dec(config["shrinkage"]["prior"])

    survivors: list[tuple[str, Decimal, Decimal]] = []  # (metric, weight, raw)
    dropped: list[str] = []
    any_measured = False
    computed_total = 0
    excluded_total = 0

    for metric in EARLY_ENTRY_METRICS:
        stored = aggregates.get(metric)
        if stored is not None:
            computed_total += stored.anchor_count_computed
            excluded_total += stored.anchor_count_excluded
            any_measured = any_measured or stored.anchor_count_computed > 0
        if stored is None:
            dropped.append(f"{metric}=absent")
        elif stored.status != "computed" or stored.value is None:
            dropped.append(f"{metric}={stored.status}")
        elif weights[metric] <= 0:
            dropped.append(f"{metric}=weight 0")
        else:
            survivors.append((metric, weights[metric], stored.value))

    if not survivors:
        status = "not_rankable" if any_measured else "not_computable"
        return DimensionScore(
            dimension=DIM_EARLY_ENTRY,
            status=status,
            raw_value=None,
            shrunk_value=None,
            weight=dimension_weight,
            sample_size=0,
            shrinkage_applied=Decimal(0),
            components=(),
            explanation=_explain(
                DIM_EARLY_ENTRY,
                status,
                None,
                (),
                0,
                Decimal(0),
                k,
                prior,
                extra="dropped: " + ", ".join(dropped),
            ),
            anchor_count_computed=computed_total,
            anchor_count_excluded=excluded_total,
        )

    used = renormalise([(metric, weight) for metric, weight, _ in survivors])
    components = tuple(
        Component(
            metric=metric,
            weight=weight,
            weight_used=used[metric],
            raw=raw,
            credit=credit_for(metric, raw),
        )
        for metric, weight, raw in survivors
    )
    composite = _HUNDRED * sum(
        (component.weight_used * component.credit for component in components), Decimal(0)
    )
    sample_size = anchors.computed
    shrunk, factor = shrink(composite, sample_size, k=k, prior=prior)

    extra = f"metrics_used={len(components)}/{len(EARLY_ENTRY_METRICS)}"
    if dropped:
        extra += "; dropped: " + ", ".join(dropped)
    return DimensionScore(
        dimension=DIM_EARLY_ENTRY,
        status="computed",
        raw_value=composite,
        shrunk_value=shrunk,
        weight=dimension_weight,
        sample_size=sample_size,
        shrinkage_applied=_q4(factor),
        components=components,
        explanation=_explain(
            DIM_EARLY_ENTRY, "computed", composite, components, sample_size, factor, k, prior, extra
        ),
        anchor_count_computed=computed_total,
        anchor_count_excluded=excluded_total,
    )


def launch_week_key(when: _dt.datetime) -> str:
    """``launch_week:YYYY-Www`` for a launch instant, in ISO weeks.

    ISO rather than naive week-of-year because the two disagree across a year
    boundary: 2025-12-29 belongs to ISO week 2026-W01, and a benchmark keyed to the
    wrong week would silently compare wallets against the wrong cohort.
    """
    iso = when.isocalendar()
    return f"launch_week:{iso.year}-W{iso.week:02d}"


def excess_for(multiple: Decimal, cohort_median: Decimal) -> Decimal:
    """``log(1 + multiple) - log(1 + cohort_median)`` — doc 05 §9.

    Log-ratios rather than differences because multiples are multiplicative: the
    step from 2x to 4x should count the same as 10x to 20x.
    """
    if multiple <= -_ONE or cohort_median <= -_ONE:
        raise ConsumerError("multiples must be greater than -1 to take a log")
    return (_ONE + multiple).ln() - (_ONE + cohort_median).ln()


def cohort_excess_component(mean_excess: Decimal, scale: Decimal) -> Decimal:
    """``100 / (1 + exp(-mean_excess / scale))`` on 0..100, per doc 05 §9."""
    if scale <= 0:
        raise ConsumerError(f"cohort_excess_logistic_scale must be > 0, got {scale}")
    return _HUNDRED / (_ONE + (-(mean_excess / scale)).exp())


@dataclass(frozen=True)
class PositionExcess:
    """One closed position's cohort-relative outcome, or why it has none."""

    position_id: int
    multiple: Decimal
    cohort_key: str | None = None
    cohort_kind: str | None = None
    cohort_median: Decimal | None = None
    excess: Decimal | None = None
    reason: str | None = None


def token_selection_dimension(
    excesses: Sequence[PositionExcess], config: Mapping[str, Any]
) -> DimensionScore:
    """The ``token_selection`` composite: cohort excess, shrunk.

    ``weight_hit_rate`` is 0.00 by the double-count resolution, so it is dropped as
    a component rather than contributing a zero — a diagnostic metric must not be
    able to hold the dimension hostage when it cannot be computed.
    """
    section = config["token_selection"]
    weights = _dimension_weights(section, ("cohort_excess", "hit_rate"))
    dimension_weight = _dec(config["weights"]["token_selection"])
    scale = _dec(section["cohort_excess_logistic_scale"])
    k = _dec(config["shrinkage"]["k"]["token_selection"])
    prior = _dec(config["shrinkage"]["prior"])

    included = [item for item in excesses if item.excess is not None]

    if not included:
        reason = "no closed position with a benchmark"
        if excesses:
            reasons = sorted({item.reason for item in excesses if item.reason})
            reason = "; ".join(reasons) or reason
        status = "not_rankable" if excesses else "not_computable"
        return DimensionScore(
            dimension=DIM_TOKEN_SELECTION,
            status=status,
            raw_value=None,
            shrunk_value=None,
            weight=dimension_weight,
            sample_size=0,
            shrinkage_applied=Decimal(0),
            components=(),
            explanation=_explain(
                DIM_TOKEN_SELECTION,
                status,
                None,
                (),
                0,
                Decimal(0),
                k,
                prior,
                extra=f"{len(excesses)} position(s) excluded: {reason}",
            ),
        )

    mean_excess = sum((item.excess for item in included), Decimal(0)) / Decimal(len(included))
    composite = cohort_excess_component(mean_excess, scale)

    used = renormalise([(name, weight) for name, weight in weights.items()])
    components = tuple(
        Component(
            metric=name,
            weight=weights[name],
            weight_used=used[name],
            raw=mean_excess,
            credit=_clamp01(composite / _HUNDRED),
        )
        for name in used
    )
    sample_size = len(included)
    shrunk, factor = shrink(composite, sample_size, k=k, prior=prior)

    excluded = len(excesses) - len(included)
    extra = (
        f"mean_excess={mean_excess:.4f} over {sample_size} position(s), "
        f"scale={scale}, {excluded} excluded"
    )
    return DimensionScore(
        dimension=DIM_TOKEN_SELECTION,
        status="computed",
        raw_value=composite,
        shrunk_value=shrunk,
        weight=dimension_weight,
        sample_size=sample_size,
        shrinkage_applied=_q4(factor),
        components=components,
        explanation=_explain(
            DIM_TOKEN_SELECTION, "computed", composite, components, sample_size, factor, k, prior, extra
        ),
    )


def build_wallet_score(
    conn,
    wallet: str,
    aggregates: Mapping[str, StoredAggregate],
    excesses: Sequence[PositionExcess],
    config: Mapping[str, Any],
    anchors: AnchorCounts,
    *,
    as_of: _dt.datetime,
) -> WalletScore | None:
    """Assemble one wallet's scores row, or ``None`` when it must not be scored.

    A wallet with no computable ``early_entry`` gets no ``scores`` row at all: this
    phase's claim is the early-entry sub-score, and a row carrying only
    ``token_selection`` would put a wallet into the scores table on the strength of
    a dimension that is not this phase's subject — so the confidence gate is never
    consulted for it, and no confidence number is invented for it.

    ``confidence_score`` is the §12 composite (``evaluate_gate``), not a blend of
    the existing dimensions: confidence answers "how much evidence", the score
    answers "how good", and Option 3's attempt to gate on a confidence that did not
    exist yet is retired.

    ``confidence_adjusted_score`` uses the **shrunk early-entry** value, not
    ``overall_score`` (D21). With five of eight dimensions unbuilt, overall is the
    neutral 50.00, so `50 + (50 - 50) * confidence` is 50.00 for every wallet — a
    column of identical values that would read as a ranking. The pull therefore
    applies to the one dimension this phase measures, and bands derived from it
    are early-entry bands. ``WalletScore.band_basis`` records which it was.
    """
    early = early_entry_dimension(aggregates, config, anchors)
    if not early.is_computed:
        return None
    token = token_selection_dimension(excesses, config)
    gate = evaluate_gate(conn, wallet, as_of=as_of, config=config)

    confidence = gate.composite_confidence
    overall = PARTIAL_OVERALL_SCORE
    adjusted = shrink_for_confidence(early.shrunk_value or Decimal(0), confidence)

    gates: list[str] = []
    if not gate.gate_passed:
        gates.append(f"confidence_gate: {gate.gate_reason}")
    gates.append("incomplete_score: missing_dimensions=" + ",".join(UNBUILT_DIMENSIONS))
    return WalletScore(
        wallet=wallet,
        early_entry=early,
        token_selection=token,
        anchor_sample_size=gate.components["sample"],
        anchor_coverage=gate.components["coverage"],
        overall_score=overall,
        confidence_score=confidence,
        confidence_adjusted_score=adjusted,
        band="exclude",
        recommendation="exclude",
        gated_by=";".join(gates),
        gate=gate,
    )


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------
def load_aggregates(conn, rule_version: str) -> dict[str, dict[str, StoredAggregate]]:
    """Every wallet's aggregates at one ``rule_version``, grouped by wallet."""
    if not rule_version:
        raise ConsumerError("rule_version is required")
    rows = conn.execute(
        """
        SELECT wallet, metric_name, value, status, anchor_count_computed,
               anchor_count_excluded, coverage
        FROM wallet_aggregates
        WHERE rule_version = %s
        ORDER BY wallet, metric_name
        """,
        (rule_version,),
    ).fetchall()

    grouped: dict[str, dict[str, StoredAggregate]] = {}
    for wallet, metric, value, status, computed, excluded, coverage in rows:
        grouped.setdefault(wallet, {})[metric] = StoredAggregate(
            metric_name=metric,
            value=value,
            status=status,
            anchor_count_computed=computed,
            anchor_count_excluded=excluded,
            coverage=_dec(coverage),
        )
    return grouped


def load_anchor_counts(conn, wallets: Sequence[str]) -> dict[str, AnchorCounts]:
    """Distinct compute/total anchor counts per wallet.

    This is the D1 query, deliberately at anchor grain::

        SELECT wallet, count(DISTINCT anchor_id) FILTER (WHERE status = 'computed'),
                       count(DISTINCT anchor_id)
        FROM anchor_wallet_metrics
        WHERE wallet = ANY(%s)
        GROUP BY wallet

    ``wallet_aggregates`` cannot answer it: it stores per-metric counts, so summing
    them counts one anchor once per metric it was measured on.
    """
    wallet_list = [w for w in wallets]
    if not wallet_list:
        return {}
    rows = conn.execute(
        """
        SELECT wallet,
               count(DISTINCT anchor_id) FILTER (WHERE status = 'computed'),
               count(DISTINCT anchor_id)
        FROM anchor_wallet_metrics
        WHERE wallet = ANY(%s)
        GROUP BY wallet
        """,
        (wallet_list,),
    ).fetchall()
    return {wallet: AnchorCounts(int(computed), int(total)) for wallet, computed, total in rows}


def load_position_excesses(
    conn,
    wallet: str,
    as_of: _dt.datetime,
    config: Mapping[str, Any],
) -> list[PositionExcess]:
    """Cohort-relative outcomes for the wallet's closed positions.

    Point-in-time discipline: only benchmarks with ``available_at <= as_of`` are
    candidates. A launch-week cohort is used only when it has at least
    ``benchmark_min_cohort_n`` samples; otherwise the read falls back to
    ``all_tokens``, and a position with no usable benchmark at all is excluded with
    a reason rather than scored against a guess.
    """
    section = config["token_selection"]
    window_days = int(section["benchmark_forward_window_days"])
    min_cohort_n = int(section["benchmark_min_cohort_n"])

    positions = conn.execute(
        f"""
        SELECT p.position_id, p.max_multiple_{window_days}d, t.created_at
        FROM positions p
        JOIN tokens t ON t.mint = p.mint
        WHERE p.address = %s
          AND p.is_closed
          AND p.max_multiple_{window_days}d IS NOT NULL
        ORDER BY p.position_id
        """,
        (wallet,),
    ).fetchall()

    if not positions:
        return []

    keys: dict[str, str | None] = {}
    for _, _, created_at in positions:
        if created_at is None:
            continue
        keys[str(created_at)] = launch_week_key(created_at)

    benchmark_rows = conn.execute(
        """
        SELECT cohort_kind, cohort_key, n_samples, value_median
        FROM cohort_benchmarks
        WHERE metric_key = %s
          AND window_days = %s
          AND available_at <= %s
          AND (cohort_kind = 'all_tokens' OR cohort_key = ANY(%s))
        ORDER BY available_at ASC, id ASC
        """,
        (COHORT_METRIC_KEY, window_days, as_of, list({k for k in keys.values() if k})),
    ).fetchall()

    best: dict[tuple[str, str], tuple[str, Decimal]] = {}
    latest_fallback: tuple[str, Decimal] | None = None
    for kind, cohort_key, n_samples, median in benchmark_rows:
        if median is None:
            continue
        if kind == "launch_week" and n_samples < min_cohort_n:
            continue  # too thin: the all_tokens fallback is the honest comparison
        entry = (kind, _dec(median))
        best[(kind, cohort_key)] = entry
        if kind == "all_tokens":
            latest_fallback = entry  # rows arrive in available_at order

    out: list[PositionExcess] = []
    for position_id, multiple, created_at in positions:
        multiple_dec = _dec(multiple)
        if created_at is None:
            out.append(
                PositionExcess(
                    position_id=position_id,
                    multiple=multiple_dec,
                    reason="unknown launch time (tokens.created_at is NULL)",
                )
            )
            continue
        key = launch_week_key(created_at)
        chosen = best.get(("launch_week", key)) or latest_fallback
        if chosen is None:
            out.append(
                PositionExcess(
                    position_id=position_id,
                    multiple=multiple_dec,
                    cohort_key=key,
                    reason=f"no benchmark for {key} or all_tokens at this as_of",
                )
            )
            continue
        kind, median = chosen
        cohort_key = key if kind == "launch_week" else "all_tokens"
        out.append(
            PositionExcess(
                position_id=position_id,
                multiple=multiple_dec,
                cohort_key=cohort_key,
                cohort_kind=kind,
                cohort_median=median,
                excess=excess_for(multiple_dec, median),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Database writes
# ---------------------------------------------------------------------------
def _config_version_row(conn, config: Mapping[str, Any], config_sha: bytes) -> str:
    """Ensure ``score_config_versions`` carries the weights this run used."""
    version = str(config.get("version") or "")
    if not version:
        raise ConsumerError("scoring config has no version; a run cannot be attributed")
    parameters = {
        key: config[key]
        for key in (
            "early_entry",
            "token_selection",
            "shrinkage",
            "confidence",
            "confidence_gate",
        )
        if key in config
    }
    conn.execute(
        """
        INSERT INTO score_config_versions (
            config_version, weights, parameters, config_sha256, is_frozen, notes
        )
        VALUES (%s, %s::jsonb, %s::jsonb, %s, %s, %s)
        ON CONFLICT (config_version) DO NOTHING
        """,
        (
            version,
            json.dumps(config.get("weights", {}), default=str, sort_keys=True),
            json.dumps(parameters, default=str, sort_keys=True),
            config_sha,
            bool(config.get("frozen", False)),
            "written by solscope.consumer",
        ),
    )
    return version


def _ensure_score_run(
    conn,
    *,
    config_version: str,
    rule_version: str,
    as_of: _dt.datetime,
    window_start: _dt.datetime,
    window_end: _dt.datetime,
    pipeline_version: str,
) -> int:
    """Find or create the run this ``rule_version`` writes into.

    ``experiment_label`` carries the aggregate rule version rather than NULL,
    because NULLs are distinct in a UNIQUE constraint: a NULL label would make every
    re-run a new run and quietly double the scores table.
    """
    label = f"aggregate_rule:{rule_version}"
    row = conn.execute(
        """
        INSERT INTO score_runs (
            config_version, as_of, window_start, window_end, pipeline_version, experiment_label
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (config_version, as_of, window_start, window_end, experiment_label)
        DO UPDATE SET pipeline_version = EXCLUDED.pipeline_version
        RETURNING score_run_id
        """,
        (config_version, as_of, window_start, window_end, pipeline_version, label),
    ).fetchone()
    return int(row[0])


def _write_sub_score(conn, score_id: int, dimension: DimensionScore) -> None:
    contribution = None
    if dimension.shrunk_value is not None:
        contribution = _q2(dimension.weight * dimension.shrunk_value)
    conn.execute(
        """
        INSERT INTO sub_scores (
            score_id, dimension, raw_value, normalized_value, shrunk_value, weight,
            contribution, sample_size, shrinkage_applied, confidence, explanation
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (score_id, dimension) DO UPDATE SET
            raw_value         = EXCLUDED.raw_value,
            normalized_value  = EXCLUDED.normalized_value,
            shrunk_value      = EXCLUDED.shrunk_value,
            weight            = EXCLUDED.weight,
            contribution      = EXCLUDED.contribution,
            sample_size       = EXCLUDED.sample_size,
            shrinkage_applied = EXCLUDED.shrinkage_applied,
            confidence        = EXCLUDED.confidence,
            explanation       = EXCLUDED.explanation
        """,
        (
            score_id,
            dimension.dimension,
            dimension.raw_value,
            _q2(dimension.raw_value),
            _q2(dimension.shrunk_value),
            dimension.weight,
            contribution,
            dimension.sample_size or None,
            dimension.shrinkage_applied,
            dimension.confidence,
            dimension.explanation,
        ),
    )


def _write_evidence(conn, score_id: int, wallet_score: WalletScore) -> None:
    """Replace this score's evidence with what produced it."""
    conn.execute("DELETE FROM score_evidence WHERE score_id = %s", (score_id,))

    for dimension in (wallet_score.early_entry, wallet_score.token_selection):
        for component in dimension.components:
            conn.execute(
                """
                INSERT INTO score_evidence (
                    score_id, dimension, evidence_kind, subject_id, value,
                    benchmark_value, note
                )
                VALUES (%s, %s, 'metric', %s, %s, %s, %s)
                """,
                (
                    score_id,
                    dimension.dimension,
                    component.metric,
                    _q4(component.credit * _HUNDRED),
                    component.raw,
                    f"weight_configured={component.weight}; weight_used={component.weight_used}",
                ),
            )

    # D17: the provenance factor is a number; this says what kind of gap produced
    # it. A sell-opened position has no entry buy, so provenance gives it no credit
    # either way — the percentage separates "this wallet routes badly" from "our
    # history starts after it already held the token".
    conn.execute(
        """
        INSERT INTO score_evidence (
            score_id, dimension, evidence_kind, subject_id, value, note
        )
        VALUES (%s, %s, 'metric', %s, %s, %s)
        """,
        (score_id, DIM_EARLY_ENTRY, "provenance_sell_only_pct",
         sell_only_pct(conn, wallet_score.wallet),
         "share of positions opened by a sell, with no entry buy to price; "
         "provenance scores them 0 by design, so a high value means the history "
         "predates observation rather than bad routing"),
    )

    gate = wallet_score.gate
    for factor, value in gate.components.items():
        conn.execute(
            """
            INSERT INTO score_evidence (
                score_id, dimension, evidence_kind, subject_id, value,
                benchmark_value, note
            )
            VALUES (%s, %s, 'metric', %s, %s, %s, %s)
            """,
            (score_id, DIM_CONFIDENCE, f"factor.{factor}", value,
             gate.weights[factor],
             f"weight={gate.weights[factor]}; evidence-only input (no performance "
             f"column is read)"),
        )
    conn.execute(
        """
        INSERT INTO score_evidence (
            score_id, dimension, evidence_kind, subject_id, value, note
        )
        VALUES (%s, %s, 'metric', %s, %s, %s)
        """,
        (score_id, DIM_CONFIDENCE, "confidence_composite",
         gate.composite_confidence,
         f"100 x sum(weight x factor); distinct_anchors={gate.distinct_anchors}; "
         f"gate {'passed' if gate.gate_passed else 'failed'}: {gate.gate_reason}"),
    )
    for key, value in gate.thresholds.items():
        conn.execute(
            """
            INSERT INTO score_evidence (
                score_id, dimension, evidence_kind, subject_id, value, note
            )
            VALUES (%s, %s, 'metric', %s, %s, %s)
            """,
            (score_id, DIM_CONFIDENCE, f"threshold.{key}", value,
             "confidence_gate threshold applied to this wallet"),
        )


def _confidence_explanation(wallet_score: WalletScore) -> str:
    gate = wallet_score.gate
    factors = ", ".join(f"{name}={value}" for name, value in gate.components.items())
    return (
        f"confidence {gate.composite_confidence} = 100 x sum(weight x factor): "
        f"{factors}; distinct_anchors={gate.distinct_anchors}; "
        f"gate {'passed' if gate.gate_passed else 'failed'}: {gate.gate_reason}"
    )


def write_wallet_score(
    conn,
    score_run_id: int,
    wallet_score: WalletScore,
    *,
    as_of: _dt.datetime,
) -> int:
    """Upsert one wallet's scores row and its sub-rows. Returns ``score_id``."""
    try:
        row = conn.execute(
            """
            INSERT INTO scores (
                score_run_id, address, as_of, overall_score, confidence_score,
                confidence_adjusted_score, band_basis, band, recommendation, gated_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (score_run_id, address) DO UPDATE SET
                as_of                     = EXCLUDED.as_of,
                overall_score             = EXCLUDED.overall_score,
                confidence_score          = EXCLUDED.confidence_score,
                confidence_adjusted_score = EXCLUDED.confidence_adjusted_score,
                band_basis                = EXCLUDED.band_basis,
                band                      = EXCLUDED.band,
                recommendation            = EXCLUDED.recommendation,
                gated_by                  = EXCLUDED.gated_by,
                computed_at               = now()
            RETURNING id
            """,
            (
                score_run_id,
                wallet_score.wallet,
                as_of,
                wallet_score.overall_score,
                wallet_score.confidence_score,
                wallet_score.confidence_adjusted_score,
                wallet_score.band_basis,
                wallet_score.band,
                wallet_score.recommendation,
                wallet_score.gated_by,
            ),
        ).fetchone()
        score_id = int(row[0])

        _write_sub_score(conn, score_id, wallet_score.early_entry)
        _write_sub_score(conn, score_id, wallet_score.token_selection)
        _write_sub_score(
            conn,
            score_id,
            DimensionScore(
                dimension=DIM_CONFIDENCE,
                status="computed",
                raw_value=None,
                shrunk_value=None,
                weight=Decimal(0),
                sample_size=wallet_score.early_entry.sample_size,
                shrinkage_applied=Decimal(0),
                components=(),
                explanation=_confidence_explanation(wallet_score),
                confidence=wallet_score.confidence_score,
            ),
        )
        _write_evidence(conn, score_id, wallet_score)
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - never mask the original failure
            pass
        raise
    return score_id


def _prune_run(conn, score_run_id: int, scored: Sequence[str]) -> int:
    """Remove rows this run no longer produces, so a re-run converges."""
    row = conn.execute(
        """
        DELETE FROM scores
        WHERE score_run_id = %s AND NOT (address = ANY(%s))
        RETURNING id
        """,
        (score_run_id, list(scored)),
    ).fetchall()
    return len(row)


def _config_sha(config: Mapping[str, Any], path: Any = None) -> bytes:
    """SHA-256 of the config file, or of the mapping when no file is involved."""
    if path is None:
        path = CONFIG_DIR / "scoring.yaml"
    try:
        data = path.read_bytes()
    except OSError:
        data = json.dumps(config, default=str, sort_keys=True).encode()
    return hashlib.sha256(data).digest()


def run_consumer(
    conn,
    *,
    rule_version: str,
    as_of: _dt.datetime | None = None,
    window_start: _dt.datetime | None = None,
    window_end: _dt.datetime | None = None,
    scoring: Mapping[str, Any] | None = None,
    config_path: Any = None,
    pipeline_version: str = PIPELINE_VERSION,
    dry_run: bool = False,
) -> ConsumerReport:
    """Score every wallet that has aggregates at ``rule_version``.

    One transaction, committed at the end: a run either lands whole or not at all.
    Re-running with the same arguments is idempotent — the run is found by
    ``(config_version, as_of, window_start, window_end, aggregate_rule:<version>)``,
    scores and sub-scores are upserted, evidence is replaced, and rows for wallets
    that no longer qualify are pruned.
    """
    if not rule_version:
        raise ConsumerError("rule_version is required")
    try:
        config = scoring if scoring is not None else load_scoring(config_path)
    except (ConfigError, OSError) as exc:
        raise ConsumerError(f"scoring config could not be loaded: {exc}") from exc

    as_of = as_of or _dt.datetime.now(_dt.timezone.utc)
    window_end = window_end or as_of
    window_start = window_start or (as_of - _dt.timedelta(days=30))

    report = ConsumerReport(
        rule_version=rule_version,
        config_version=str(config.get("version") or ""),
        as_of=as_of,
        window_start=window_start,
        window_end=window_end,
    )

    aggregates = load_aggregates(conn, rule_version)
    report.wallets_seen = len(aggregates)
    anchor_counts = load_anchor_counts(conn, list(aggregates))

    prepared: list[WalletScore] = []
    for wallet in sorted(aggregates):
        anchors = anchor_counts.get(wallet, AnchorCounts(0, 0))
        excesses = load_position_excesses(conn, wallet, as_of, config)
        wallet_score = build_wallet_score(conn, wallet, aggregates[wallet], excesses,
                                          config, anchors, as_of=as_of)
        if wallet_score is None:
            early = early_entry_dimension(aggregates[wallet], config, anchors)
            report.skipped[wallet] = f"early_entry_{early.status}"
            continue
        prepared.append(wallet_score)

    report.wallets_scored = len(prepared)
    if dry_run:
        return report

    try:
        config_version = _config_version_row(conn, config, _config_sha(config, config_path))
        report.config_version = config_version
        score_run_id = _ensure_score_run(
            conn,
            config_version=config_version,
            rule_version=rule_version,
            as_of=as_of,
            window_start=window_start,
            window_end=window_end,
            pipeline_version=pipeline_version,
        )
        report.score_run_id = score_run_id
        for wallet_score in prepared:
            write_wallet_score(conn, score_run_id, wallet_score, as_of=as_of)
        _prune_run(conn, score_run_id, [ws.wallet for ws in prepared])
        conn.execute(
            "UPDATE score_runs SET finished_at = now(), wallets_scored = %s "
            "WHERE score_run_id = %s",
            (report.wallets_scored, score_run_id),
        )
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001 - never mask the original failure
            pass
        raise

    _LOG.info(
        "consumer run: rule_version=%s scored=%d skipped=%d run_id=%s",
        rule_version,
        report.wallets_scored,
        report.wallets_skipped,
        report.score_run_id,
    )
    return report
