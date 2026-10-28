"""Phase 1b early-entry metric definitions (docs/PHASE1A_RECOVERY.md §5).

**Signatures and contracts only. The bodies are not implemented yet** — this module
fixes the definitions so that the scorer, the reports and the tests can be written
against a stable interface, and so that "we agreed the definition" is a fact in the
repo rather than a claim in a chat message.

Scope: the three *weighted* inputs of the Phase 1b early-entry score, plus the two
diagnostics that the scoring config retains at weight 0.00.

===========================  ====================================================
Metric                       Status
===========================  ====================================================
``max_subsequent_return``    weighted 0.40 — forward appreciation (owned here, not
                             by ``token_selection``; see the double-count
                             resolution in docs/PHASE1A_RECOVERY.md §5)
``buyer_rank_at_entry``      weighted 0.30 — replaces ``token_age_at_entry``
``pct_entries_before_volume_spike``
                             weighted 0.30
``mint_age_at_entry``        diagnostic (0.00) — see the pool-age warning below
``mc_at_entry``              diagnostic (0.00) — deferred, no supply source
===========================  ====================================================

Every threshold here mirrors a key in ``config/scoring.yaml`` under
``early_entry``. Nothing in this module may hardcode a value a human is expected to
tune (module contract of ``solscope/config.py``).

Two traps this module exists to prevent:

1. **Mint age is not pool age.** ``swaps.token_age_seconds`` is
   ``event_time - pool.created_at``. Any age used here is measured from the mint's
   own creation transaction. See docs/PHASE1A_RECOVERY.md §2.
2. **A rate over a sampled set is a property of the sample.** ``buyer_rank_at_entry``
   refuses to rank an incomplete buyer set rather than ranking a sample and calling
   the result a rank. See docs/PHASE1B_SCOPING.md Q2 invariant 2.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

__all__ = [
    "INSUFFICIENT_WINDOW",
    "PriceObservation",
    "BuyerFirstSeen",
    "HourlyVolume",
    "WindowReturn",
    "MaxSubsequentReturn",
    "BuyerRank",
    "VolumeSpikeResult",
    "max_subsequent_return",
    "buyer_rank_at_entry",
    "pct_entries_before_volume_spike",
]

#: Marker reported for a window the entry is not yet old enough to fill.
#: Mirrors ``early_entry.insufficient_window_marker`` in config/scoring.yaml.
#: A window is NEVER imputed or shortened: a truncated window silently compares
#: different horizons across wallets.
INSUFFICIENT_WINDOW = "INSUFFICIENT_WINDOW"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceObservation:
    """One SWAP-implied price point for the mint after the wallet's entry.

    ``price_quote`` is a price the swap itself implies (quote per whole base unit),
    the same quantity ``swaps.exec_price_quote`` holds — never a reserve ratio and
    never a provider candle. Provenance must be ``swap_derived`` for the point to
    count; see docs/04-data-model.md §"Prices carry provenance".
    """

    at: _dt.datetime
    price_quote: Decimal
    source: str = "swap_derived"


@dataclass(frozen=True)
class BuyerFirstSeen:
    """A unique buyer of the mint and the timestamp of its first buy.

    The unit is the *buyer*, not the trade: several buys by one wallet do not make
    it several buyers. Ranking over trades instead of buyers would let a wallet
    improve its own rank by trading more.
    """

    buyer: str
    first_buy_at: _dt.datetime


@dataclass(frozen=True)
class HourlyVolume:
    """Mint volume for one clock hour, in quote units (or USD; be consistent)."""

    hour_start: _dt.datetime
    volume: Decimal


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowReturn:
    """The outcome of one observation window.

    ``multiple`` is ``None`` exactly when the entry is younger than the window, in
    which case ``status`` is ``INSUFFICIENT_WINDOW``. Zero observations inside a
    *mature* window is not the same thing and must be reported as such — it is a
    fact about the mint (nothing traded), not about the data.
    """

    window_days: int
    status: str                      # "OK" | INSUFFICIENT_WINDOW
    multiple: Decimal | None = None  # peak price / entry price
    peak_day: int | None = None      # integer day-of-peak measured from entry


@dataclass(frozen=True)
class MaxSubsequentReturn:
    """All reported windows, plus which one is the headline."""

    headline_days: int
    windows: tuple[WindowReturn, ...] = ()

    def headline(self) -> WindowReturn | None:
        """The window the prose quotes; ``None`` if that window is not usable."""
        for w in self.windows:
            if w.window_days == self.headline_days and w.status == "OK":
                return w
        return None


@dataclass(frozen=True)
class BuyerRank:
    """A wallet's entry position among the mint's buyers.

    ``percentile`` is what the score consumes; ``rank`` is the diagnostic. They are
    reported together because rank without its denominator is uninterpretable:
    rank 3 of 10 and rank 3 of 2,000 are not comparable achievements.
    """

    computed: bool
    rank: int | None = None                 # ordinal, 1 = first buyer
    total_unique_buyers: int | None = None
    percentile: Decimal | None = None       # rank / total_unique_buyers
    reason: str | None = None               # why not computed, when computed is False


@dataclass(frozen=True)
class VolumeSpikeResult:
    """Whether a volume spike occurred, and the wallet's position relative to it."""

    spike_at: _dt.datetime | None
    spike_ratio: Decimal | None
    entries_total: int = 0
    entries_before_spike: int = 0
    fraction_before_spike: Decimal | None = None
    no_spike_observed: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def max_subsequent_return(
    *,
    entry_price_quote: Decimal,
    entry_time: _dt.datetime,
    observations: Sequence[PriceObservation],
    as_of: _dt.datetime,
    windows_days: Sequence[int] = (1, 7, 30),
    headline_days: int = 30,
) -> MaxSubsequentReturn:
    """Peak SWAP-implied price after entry, as a multiple of the entry price.

    **Definition.** For each ``N`` in ``windows_days``, the multiple is::

        peak(price_quote for observations with entry_time < at <= entry_time + N days)
        ---------------------------------------------------------------------------
                                entry_price_quote

    over observations whose ``source`` is ``swap_derived``. ``peak_day`` is
    ``floor((at_of_peak - entry_time) / 1 day)``, i.e. an integer day count from
    entry, reported so a report can say *when* the peak happened rather than only
    how large it was.

    **Point-in-time.** Only observations with ``at <= as_of`` are eligible, so a
    score computed on date X cannot see a peak that occurred after X. This is the
    same constraint the schema's two clocks exist to enforce.

    **Insufficient windows.** If ``as_of - entry_time < N days``, that window is
    reported as ``INSUFFICIENT_WINDOW`` with ``multiple=None``. The window is never
    imputed, never shortened, and never compared against a full window from another
    wallet — doing so would rank wallets on different observation horizons without
    saying so. The list of windows is returned in full so the caller can see which
    ones were usable.

    **Not implemented yet.** Contract only. When implemented, this must read prices
    derived from the swap itself (docs/PHASE1A_RECOVERY.md §2) and must not fall
    back to a provider candle to fill a gap.

    Args:
        entry_price_quote: quote-per-whole-base-unit paid at entry.
        entry_time: the wallet's first buy of the mint (block time, not ingest).
        observations: swap-implied price points for the mint, any order.
        as_of: the point-in-time cutoff.
        windows_days: observation windows in days.
        headline_days: which window the prose quotes; must be in ``windows_days``.

    Returns:
        A :class:`MaxSubsequentReturn` carrying every requested window.

    Raises:
        NotImplementedError: always, until the logic lands (S5).
    """
    raise NotImplementedError(
        "max_subsequent_return is defined but not implemented (PHASE1B_SCOPING S5). "
        "Definition: docs/PHASE1A_RECOVERY.md section 5; config keys under "
        "early_entry.max_subsequent_return_*."
    )


def buyer_rank_at_entry(
    *,
    wallet: str,
    wallet_first_buy_at: _dt.datetime,
    buyers: Sequence[BuyerFirstSeen],
    buyer_set_complete: bool,
) -> BuyerRank:
    """The wallet's entry position by first-buy time, among unique buyers.

    **Definition.** ``rank`` is the ordinal position (1 = first buyer) of ``wallet``
    when all unique buyers of the mint are ordered by ``first_buy_at``. Ties are
    broken by the buyer address so the ordering is deterministic and reproducible —
    a rank that shifts between runs is not evidence. ``total_unique_buyers`` is the
    count of *ever-observed* unique buyers for the mint, and
    ``percentile = rank / total_unique_buyers``.

    The **score consumes ``percentile``**, not ``rank``: rank 3 on a mint with 2,000
    buyers is a stronger signal than rank 3 on a mint with 10, and the ordinal alone
    cannot express that.

    **Completeness is required.** If ``buyer_set_complete`` is false, this returns
    ``computed=False`` with a reason and no rank — it does not rank the sample it was
    given. A rank over a partial buyer set measures the sampling, not the wallet, and
    an early-looking rank is exactly what sampling a prefix of history produces.
    Callers must surface ``computed=False`` as *not computable*, which is distinct
    from both a good rank and a bad one.

    **Prerequisite.** Mint genesis must be known and stored point-in-time before this
    can be trusted at all: buyer ordering is only meaningful relative to genesis, and
    the genesis-burst rule (``solscope/quality_filter.py``) compares the same
    timestamps.

    **Not implemented yet.** Contract only (S5).

    Args:
        wallet: the buyer address being ranked.
        wallet_first_buy_at: that wallet's first buy of the mint.
        buyers: every observed unique buyer with its first-buy time.
        buyer_set_complete: whether the set is provably complete for the mint.

    Returns:
        A :class:`BuyerRank`; ``computed=False`` when the set is incomplete or the
        wallet is absent from it.

    Raises:
        NotImplementedError: always, until the logic lands (S5).
    """
    raise NotImplementedError(
        "buyer_rank_at_entry is defined but not implemented (PHASE1B_SCOPING S5). "
        "Definition: docs/PHASE1A_RECOVERY.md section 5; config keys under "
        "early_entry.buyer_rank_*."
    )


def pct_entries_before_volume_spike(
    *,
    entry_times: Sequence[_dt.datetime],
    hourly_volume: Sequence[HourlyVolume],
    spike_multiple: Decimal = Decimal("3.0"),
    rolling_median_hours: int = 24,
    exclude_first_hours: int = 1,
) -> VolumeSpikeResult:
    """Fraction of the wallet's entries that preceded the mint's volume spike.

    **Definition of "spike".** The first hour whose volume is at least
    ``spike_multiple`` times the **rolling median** hourly volume over the preceding
    ``rolling_median_hours`` hours, **excluding the first ``exclude_first_hours``
    hours** from the comparison window.

    Both exclusions matter. A median (not a mean) stops one outsized hour from
    raising the bar that later hours are judged against. Excluding the opening hours
    stops a mint's own launch burst — which is not a *spike* relative to a baseline,
    it is the baseline not existing yet — from being detected as one.

    **No spike observed.** If no hour qualifies, ``no_spike_observed`` is true and
    ``fraction_before_spike`` is 1.0: every entry preceded the spike because there
    was none, and nothing is post-spike by definition. The alternative — treating a
    quiet mint as a failed measurement — penalises wallets for the market's
    behaviour rather than their own.

    **Inputs are computed, not fetched.** Both the entries and the hourly volume come
    from swap deltas and timestamps already decoded, so this metric needs no external
    volume feed and no additional credits.

    **Not implemented yet.** Contract only (S5).

    Args:
        entry_times: the wallet's entry timestamps for this mint.
        hourly_volume: mint volume per clock hour, ascending, contiguous.
        spike_multiple: threshold multiple of the rolling median.
        rolling_median_hours: baseline window length in hours.
        exclude_first_hours: opening hours omitted from the baseline.

    Returns:
        A :class:`VolumeSpikeResult`; ``fraction_before_spike`` is ``None`` only when
        there are no entries to measure.

    Raises:
        NotImplementedError: always, until the logic lands (S5).
    """
    raise NotImplementedError(
        "pct_entries_before_volume_spike is defined but not implemented "
        "(PHASE1B_SCOPING S5). Definition: docs/PHASE1A_RECOVERY.md section 5; "
        "config keys under early_entry.spike_*."
    )
