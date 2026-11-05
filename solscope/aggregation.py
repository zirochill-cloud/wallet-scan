"""Aggregate per-anchor metrics into one wallet-level number (Phase 1b, S5).

One wallet is observed at N anchors. Each anchor contributes a metric — how far a
token ran after this wallet entered, how early it ranked among buyers, whether it
entered before the volume spike. Aggregation reduces those N values to the one
number the scoring consumer needs, and states how much of the wallet it is based on.

The design rule that shapes everything here: **a missing number is never a zero.**
A wallet whose anchors could not be measured gets ``not_computable``; a wallet with
one usable anchor out of four gets ``not_rankable`` and a coverage of 0.25. Writing
0.0 in either case would make "we do not know" indistinguishable from "measured and
bad", which is the mistake that turns a data-coverage problem into a false
behavioural signal (the same reasoning as ``anchor_writer.write_metric``).

Why each reduction is the one it is:

* **max_subsequent_return — geometric mean of (1 + r), minus 1.** The wallet's
  outcome across anchors compounds: capital committed to five entries does not get
  the arithmetic average of their multiples, it gets the product. An arithmetic mean
  would let one 500x anchor award a high score to a wallet that was wrong on every
  other one. Returns are winsorised at ``winsorize_at`` (default 100) because beyond
  that the sample is a lottery ticket rather than a repeatable entry edge, and one
  such row would otherwise dominate every comparison it appears in.
* **buyer_rank_at_entry — median percentile.** Rank is an ordering, not a quantity;
  a single 0.001 (first buyer on one token) should not drag the centre of the
  distribution away from the wallet's ordinary behaviour.
* **pct_entries_before_volume_spike — mean fraction.** The input is already a
  bounded per-anchor fraction in [0, 1], so the mean is the honest centre and no
  winsorisation is needed.

Shared contract (:class:`AggregateResult`): a number or a reason, the anchor counts
both ways, the coverage those imply, and a per-anchor trace. The trace is not
decoration — without it, a surprising score cannot be traced back to the anchors
that produced it.

Thresholds: ``min_anchors`` defaults to 2. One anchor is an anecdote, and the point
of aggregating is to say something that survives a second observation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable, Literal, Sequence

__all__ = [
    "AGGREGATE_STATUSES",
    "MIN_ANCHORS",
    "WINSORIZE_AT",
    "AggregateError",
    "AggregateResult",
    "AnchorMetricRow",
    "aggregate_buyer_rank_at_entry",
    "aggregate_max_subsequent_return",
    "aggregate_pct_entries_before_volume_spike",
]

#: Aggregate statuses. ``computed`` is the only one that carries a value.
AGGREGATE_STATUSES: frozenset[str] = frozenset(
    {"computed", "not_rankable", "not_computable"}
)

#: One anchor is an anecdote; two is the smallest number that can agree.
MIN_ANCHORS: int = 2

#: Cap on the per-anchor multiple. Above this the sample is a lottery ticket.
WINSORIZE_AT: Decimal = Decimal("100")

#: A return cannot be worse than total loss.
_MIN_RETURN: Decimal = Decimal("-1")

#: Statuses ``anchor_writer.write_metric`` may have written for an anchor.
_ANCHOR_STATUSES: frozenset[str] = frozenset(
    {"computed", "INSUFFICIENT_WINDOW", "NO_OBSERVATIONS", "NOT_COMPUTABLE"}
)

_METRIC_MAX_RETURN = "max_subsequent_return"
_METRIC_BUYER_RANK = "buyer_rank_at_entry"
_METRIC_PREVOLUME = "pct_entries_before_volume_spike"


class AggregateError(ValueError):
    """An aggregate input was rejected before it could produce a misleading number."""


@dataclass(frozen=True)
class AnchorMetricRow:
    """One anchor's contribution, as stored in ``anchor_wallet_metrics``.

    ``status`` is carried rather than inferred from ``value``: ``INSUFFICIENT_WINDOW``
    (the entry is younger than the window), ``NO_OBSERVATIONS`` (the window matured
    but held no usable price) and ``NOT_COMPUTABLE`` all explain a missing value, and
    they are distinguishable only if the status is preserved. Any status other than
    ``computed`` counts as excluded from the aggregate.
    """

    anchor_id: int
    metric_name: str
    status: str
    value: Decimal | None = None
    window_days: int | None = None
    peak_day: int | None = None

    def __post_init__(self) -> None:
        if self.metric_name not in {_METRIC_MAX_RETURN, _METRIC_BUYER_RANK, _METRIC_PREVOLUME}:
            raise AggregateError(f"unknown metric_name {self.metric_name!r}")
        if self.status not in _ANCHOR_STATUSES:
            raise AggregateError(f"unknown anchor status {self.status!r}")
        if (self.status == "computed") != (self.value is not None):
            raise AggregateError(
                f"anchor {self.anchor_id}: status {self.status!r} with value {self.value!r} — "
                "computed requires a value, every other status forbids one"
            )
        if self.value is not None and not isinstance(self.value, Decimal):
            raise AggregateError(
                f"anchor {self.anchor_id}: value must be Decimal, got {type(self.value).__name__}"
            )


@dataclass
class AggregateResult:
    """The aggregate plus everything needed to audit it."""

    metric_name: str
    value: Decimal | None
    status: Literal["computed", "not_rankable", "not_computable"]
    anchor_count_computed: int
    anchor_count_excluded: int
    coverage: float
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_computed(self) -> bool:
        return self.status == "computed"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view: Decimals become strings, so nothing is lost to float."""
        return {
            "metric_name": self.metric_name,
            "value": None if self.value is None else str(self.value),
            "status": self.status,
            "anchor_count_computed": self.anchor_count_computed,
            "anchor_count_excluded": self.anchor_count_excluded,
            "coverage": self.coverage,
            "evidence": list(self.evidence),
        }


def _select(per_anchor: Iterable[AnchorMetricRow], metric_name: str) -> list[AnchorMetricRow]:
    """Rows for this metric, in input order.

    A mixed list is filtered rather than rejected: the three aggregates are usually
    called on one wallet's full metric set, and silently using a row that describes
    a different metric would be the bug worth catching — a filter cannot do that.
    """
    rows: list[AnchorMetricRow] = []
    for row in per_anchor:
        if not isinstance(row, AnchorMetricRow):
            raise AggregateError(
                f"per_anchor must contain AnchorMetricRow, got {type(row).__name__}"
            )
        if row.metric_name == metric_name:
            rows.append(row)
    return rows


def _empty(metric_name: str, rows: Sequence[AnchorMetricRow], computed: int) -> AggregateResult:
    """The result for a set of rows that produced no number at all."""
    return AggregateResult(
        metric_name=metric_name,
        value=None,
        status="not_computable",
        anchor_count_computed=computed,
        anchor_count_excluded=len(rows) - computed,
        coverage=(computed / len(rows)) if rows else 0.0,
        evidence=[_trace(row) for row in rows],
    )


def _trace(row: AnchorMetricRow, **extra: Any) -> dict[str, Any]:
    """One anchor's line in the evidence list."""
    entry: dict[str, Any] = {
        "anchor_id": row.anchor_id,
        "anchor_status": row.status,
        "raw_value": None if row.value is None else str(row.value),
        "included": row.status == "computed",
    }
    if row.window_days is not None:
        entry["window_days"] = row.window_days
    if row.peak_day is not None:
        entry["peak_day"] = row.peak_day
    entry.update(extra)
    return entry


def _guarded(
    metric_name: str,
    rows: list[AnchorMetricRow],
    values: Sequence[Decimal],
    *,
    min_anchors: int,
    reducer,
    traces: list[dict[str, Any]],
) -> AggregateResult:
    """Apply the shared preconditions, then the metric's own reduction.

    Preconditions, in order:

    * no rows, or no *computed* rows → ``not_computable``. Nothing was measured, so
      there is no number to defend.
    * fewer than ``min_anchors`` computed → ``not_rankable``. Something was measured,
      just not enough to compare this wallet against another.
    * otherwise ``computed``.
    """
    if min_anchors < 1:
        raise AggregateError(f"min_anchors must be >= 1, got {min_anchors}")

    computed = len(values)
    if not rows or computed == 0:
        return _empty(metric_name, rows, computed)

    coverage = computed / len(rows)
    if computed < min_anchors:
        return AggregateResult(
            metric_name=metric_name,
            value=None,
            status="not_rankable",
            anchor_count_computed=computed,
            anchor_count_excluded=len(rows) - computed,
            coverage=coverage,
            evidence=traces,
        )

    return AggregateResult(
        metric_name=metric_name,
        value=reducer(values),
        status="computed",
        anchor_count_computed=computed,
        anchor_count_excluded=len(rows) - computed,
        coverage=coverage,
        evidence=traces,
    )


def _winsorized(row: AnchorMetricRow, winsorize_at: Decimal) -> Decimal:
    """Cap the upside at ``winsorize_at`` and the downside at total loss."""
    assert row.value is not None
    return max(_MIN_RETURN, min(row.value, winsorize_at))


def _geometric_mean_minus_one(values: Sequence[Decimal]) -> Decimal:
    """``exp(mean(ln(1 + r))) - 1``, floored at 0.

    A single total loss makes one factor exactly 0, the product 0, and the geometric
    mean 0. The recovered specification records that case as ``0`` rather than
    ``-1`` (docs/RECOVERED_FROM_TRANSCRIPT.md §7: "geometric mean collapses to 0"),
    so the result is floored at 0 here. The distinction matters for reading the
    number: this metric answers "what did this wallet's entries reach", and a lost
    position reaching nothing is 0 appreciation, not a negative multiple.
    """
    factors = [Decimal(1) + value for value in values]
    if any(factor <= 0 for factor in factors):
        return Decimal(0)
    mean_log = sum((factor.ln() for factor in factors), Decimal(0)) / Decimal(len(factors))
    result = mean_log.exp() - Decimal(1)
    return result if result > 0 else Decimal(0)


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal(2)


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def aggregate_max_subsequent_return(
    per_anchor: Sequence[AnchorMetricRow],
    *,
    winsorize_at: Decimal = WINSORIZE_AT,
    min_anchors: int = MIN_ANCHORS,
) -> AggregateResult:
    """Compound the wallet's best-case multiples across its anchors.

    Geometric mean of ``1 + winsorised r``, minus 1, floored at 0: a wallet that
    entered five tokens which ran 5x, 5x, 5x, 5x and 5x has a 4.0 aggregate, while
    one that hit 99x once and entered two tokens that went nowhere has ≈3.64 — the
    compound reading rewards consistency over a single lottery ticket, which is the
    property §5 of the scoping document claims this metric has.

    ``winsorize_at`` caps each anchor's multiple; ``min_anchors`` is the smallest
    number of computed anchors that may be called a ranking.
    """
    metric = _METRIC_MAX_RETURN
    rows = _select(per_anchor, metric)
    computed = [row for row in rows if row.status == "computed"]

    traces: list[dict[str, Any]] = []
    values: list[Decimal] = []
    for row in rows:
        if row.status != "computed":
            traces.append(_trace(row))
            continue
        capped = _winsorized(row, winsorize_at)
        values.append(capped)
        traces.append(_trace(row, winsorized_value=str(capped)))

    return _guarded(
        metric,
        rows,
        values,
        min_anchors=min_anchors,
        reducer=_geometric_mean_minus_one,
        traces=traces,
    )


def aggregate_buyer_rank_at_entry(
    per_anchor: Sequence[AnchorMetricRow],
    *,
    min_anchors: int = MIN_ANCHORS,
) -> AggregateResult:
    """Middle of the wallet's entry ranks across anchors.

    ``buyer_rank_at_entry`` is a percentile: 0.001 is first, 1.0 is last. The median
    is used because rank is an ordering, and one early snipe on a single token should
    not move the centre of a wallet's behaviour the way a mean would let it.
    """
    metric = _METRIC_BUYER_RANK
    rows = _select(per_anchor, metric)
    values = [row.value for row in rows if row.status == "computed"]
    return _guarded(
        metric,
        rows,
        [value for value in values if value is not None],
        min_anchors=min_anchors,
        reducer=_median,
        traces=[_trace(row) for row in rows],
    )


def aggregate_pct_entries_before_volume_spike(
    per_anchor: Sequence[AnchorMetricRow],
    *,
    min_anchors: int = MIN_ANCHORS,
) -> AggregateResult:
    """Average share of a wallet's entries that preceded the volume spike.

    Each anchor contributes a fraction in [0, 1] ("did this entry come before the
    token's volume spike"), so the mean across anchors is the fraction of entries
    that were early. Candidacy, not exclusion: an entry after the spike is a 0 in
    the average, not a dropped row — dropping it would answer a different question
    ("of the early entries, how many were early").
    """
    metric = _METRIC_PREVOLUME
    rows = _select(per_anchor, metric)
    values = [row.value for row in rows if row.status == "computed"]
    return _guarded(
        metric,
        rows,
        [value for value in values if value is not None],
        min_anchors=min_anchors,
        reducer=_mean,
        traces=[_trace(row) for row in rows],
    )
