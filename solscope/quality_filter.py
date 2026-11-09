"""Phase 1b wallet quality filter — four rules, verdicts, reason codes.

**All four rules are implemented**: ``genesis_burst`` (cross-anchor), ``sandwich``,
``throughput`` and ``router_only``, plus :func:`screen_wallet` which applies them in
that order and returns one verdict. They are calibrated against synthetic
bundled/clean sets (``tests/test_phase1b_filter.py``); their thresholds are **not
validated** — see ``SCREEN_DEFAULTS_VALIDATED``.

Why the filter exists at all, and why it is now the precondition for anchor
overlap: ranking needs ≥3 anchors per wallet, which means anchor sets must overlap
in their buyer sets. Overlap alone floods ``track`` with bot wallets that are early
in everything because they are automated. The filter is what makes an overlapping
anchor set produce a ranking of traders rather than of bots.

Built fresh: no filter code survived from the prior session
(docs/PHASE1A_RECOVERY.md §3). What survives is one measurement — **44 of 45 wallets
in a known-bundled set were admitted** — and the failure mode behind it.

## Input domain (this resolves the module's most ambiguous point)

Despite the name, this is **not** a filter over wallets in isolation. It reads the
**anchor→wallet→swaps chain**:

============================  ==========================================
Reads                         Why
============================  ==========================================
``swaps`` (first buy per      ``positions.entry_token_age_seconds`` gives
wallet, per anchor)           the delta from genesis but loses the
                              *anchor identity*, so a wallet cannot be
                              compared **across** anchors.
anchor genesis + deployer     The genesis-burst rule counts anchors
(new registry)                entered within N seconds of genesis.
anchor→wallet edges           ">= N anchors" spans several anchors by
                              one wallet.
============================  ==========================================

**Consequence:** the unit of decision is the **(wallet, anchor) pair**, and a verdict
is relative to a *specific anchor set and rule version*. The same wallet can be
accepted against one anchor set and rejected against another. A verdict recorded
without its anchor set and rule version is therefore meaningless — see
docs/PHASE1B_SCOPING.md Q2 invariant 3 (verdicts are immutable and versioned).

## The reason the codes exist at all

The prior filter admitted 44 of 45 bundled wallets and left **no record of why**.
That is what made the failure undiagnosable after the fact: there was no way to ask
which rule had admitted them, on what measured values, under which thresholds. The
codes below exist so that question is always answerable, and so that a *rejection
rate* is measurable on both a known-bundled set and a known-clean one.

## The three per-wallet heuristics

``sandwich``, ``throughput`` and ``router_only`` were the prior session's untested
heuristics. They are implemented here for the first time, with their own reason codes
and their own calibration, and they are deliberately *not* trusted as
bundle detectors: sandwiching and high-frequency routing describe automation, and
automation is not the same thing as a coordinated cluster. They fire on evidence a
human can recheck — round-trip duration, swaps per day, aggregator share — and their
thresholds are guesses (``SCREEN_DEFAULTS_VALIDATED`` is False).

## Precedence

``screen_wallet`` applies the rules in a fixed order and stops at the first hit:

    genesis_burst  ->  [swap sufficiency]  ->  sandwich  ->  throughput  ->  router_only

A *rejection* always outranks *not computable*: proven bundle evidence must not be
masked by a different rule lacking data. Swap-derived rules come after the
sufficiency check because "fewer than three swaps" cannot clear or trip a
throughput rule — that wallet is ``NOT_COMPUTABLE_INSUFFICIENT_SWAPS``, never
``ACCEPTED``.
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from solscope.metrics import BuyerFirstSeen

__all__ = [
    "RULE_VERSION",
    "AnchorObservation",
    "WalletAnchorEntry",
    "AnchorSetViolation",
    "WalletVerdict",
    "RuleId",
    "GENESIS_BURST_MIN_ANCHORS",
    "GENESIS_BURST_MAX_SECONDS",
    "GENESIS_BURST_DEFAULTS_VALIDATED",
    "MIN_SWAPS_TO_SCREEN",
    "SANDWICH_MAX_SECONDS",
    "SANDWICH_MIN_ROUND_TRIPS",
    "SANDWICH_MIN_SHARE",
    "SANDWICH_MIN_WINDOW_SWAPS",
    "SANDWICH_RULE_VERSION",
    "SANDWICH_WINDOW_DAYS",
    "THROUGHPUT_MAX_SWAPS_PER_DAY",
    "ROUTER_ONLY_MIN_AGGREGATOR_FRACTION",
    "SCREEN_DEFAULTS_VALIDATED",
    "SCREEN_RULE_VERSION",
    "DEFAULT_SCREEN_CONFIG",
    "ROUND_TRIP",
    "RoundTrip",
    "SwapObservation",
    "FilterVerdict",
    "genesis_burst",
    "recent_window",
    "sandwich",
    "throughput",
    "router_only",
    "screen_wallet",
    "evaluate_anchor_set",
    "evaluate_wallet",
]


# ---------------------------------------------------------------------------
# Genesis-burst rule parameters — UNVALIDATED
# ---------------------------------------------------------------------------

#: Number of anchors a wallet must be genesis-adjacent in to be rejected.
#: **A guess, not a tuned value.** See NOTES.md section QUALITY_FILTER_PENDING.
GENESIS_BURST_MIN_ANCHORS: int = 2

#: Seconds after an anchor's genesis within which a first buy counts as "in burst".
#: **A guess, not a tuned value.** See NOTES.md section QUALITY_FILTER_PENDING.
GENESIS_BURST_MAX_SECONDS: int = 60

#: Explicitly false so that no caller can mistake these numbers for validated
#: thresholds. Flip only after the rejection rate has been measured on a
#: known-bundled set AND a false-positive rate on a known-clean set.
GENESIS_BURST_DEFAULTS_VALIDATED: bool = False

#: Heuristics from the prior session that are still not ported. Empty since
#: 2026-11-08, when all three were implemented; the name is kept so that removing it
#: is a deliberate edit rather than a silent regression in coverage.
NOT_PORTED_HEURISTICS: tuple[str, ...] = ()

# --- the three per-wallet heuristics --------------------------------------

#: Round trips shorter than this are a sandwich signature, not a position.
#: **A guess** — see NOTES.md § QUALITY_FILTER_IMPLEMENTED.
SANDWICH_MAX_SECONDS: int = 90

#: How many such round trips **inside the recent window** make a sandwich wallet.
SANDWICH_MIN_ROUND_TRIPS: int = 3

#: How far back "recent" reaches, in days. Measured, not guessed: the real-data
#: calibration found a 417-day retail history carrying 30 sub-90 s round trips —
#: roughly two a month — while a confirmed bot carried hundreds an hour. A lifetime
#: count cannot tell those apart; a 30-day window can.
SANDWICH_WINDOW_DAYS: int = 30

#: Share of the recent window's swaps that must be quick round trips. A dedicated
#: sandwich wallet spends most of its activity flipping; a human with one opportunistic
#: scalp among fifty trades sits near 2%.
#: **A designed default, not a fitted one** — see the calibration for measured shares.
SANDWICH_MIN_SHARE: float = 0.20

#: Fewer recent swaps than this and a *share* is not a meaningful statistic. The
#: wallet is not judged a sandwich — a sparse trader is not a sandwich bot — which is
#: deliberately different from ``MIN_SWAPS_TO_SCREEN``: insufficient *lifetime* data
#: is NOT_COMPUTABLE, but a thin *window* on an otherwise screened wallet is a pass.
SANDWICH_MIN_WINDOW_SWAPS: int = 10

#: Version of the sandwich rule's semantics. v1 counted quick round trips over the
#: wallet's entire history with no rate; v2 measures their share of recent activity.
#: Recorded because a v1 verdict and a v2 verdict on the same wallet mean different
#: things, and the stored rows carry only ``rule_version``.
SANDWICH_RULE_VERSION: str = "sandwich/v2"

#: Swaps per day above which a wallet is treated as infrastructure rather than a
#: participant. **A guess.**
THROUGHPUT_MAX_SWAPS_PER_DAY: int = 200

#: Share of swaps that must be aggregator-routed (Jupiter, OKX, dFlow) before
#: ``router_only`` can fire. **A guess.**
ROUTER_ONLY_MIN_AGGREGATOR_FRACTION: float = 0.95

#: Fewer swaps than this and the swap-derived rules cannot decide anything: the
#: wallet is ``NOT_COMPUTABLE_INSUFFICIENT_SWAPS``, not accepted.
MIN_SWAPS_TO_SCREEN: int = 3

#: Explicitly false, and for a reason: the calibration sets are synthetic. Flipping
#: this requires real bundled AND real clean wallets.
SCREEN_DEFAULTS_VALIDATED: bool = False

#: A round trip at or below this duration is a *quick* one for ``sandwich``.
ROUND_TRIP = "round_trip"

#: Default thresholds in one mapping, so a caller can override one key without
#: restating the rest. ``screen_wallet(config=...)`` merges over this.
DEFAULT_SCREEN_CONFIG: dict[str, object] = {
    "genesis_burst_min_anchors": GENESIS_BURST_MIN_ANCHORS,
    "genesis_burst_max_seconds": GENESIS_BURST_MAX_SECONDS,
    "sandwich_max_seconds": SANDWICH_MAX_SECONDS,
    "sandwich_min_round_trips": SANDWICH_MIN_ROUND_TRIPS,
    "sandwich_window_days": SANDWICH_WINDOW_DAYS,
    "sandwich_min_share": SANDWICH_MIN_SHARE,
    "sandwich_min_window_swaps": SANDWICH_MIN_WINDOW_SWAPS,
    "throughput_max_swaps_per_day": THROUGHPUT_MAX_SWAPS_PER_DAY,
    "router_only_min_aggregator_fraction": ROUTER_ONLY_MIN_AGGREGATOR_FRACTION,
    "min_swaps_to_screen": MIN_SWAPS_TO_SCREEN,
}


# ---------------------------------------------------------------------------
# Anchor-set preconditions (docs/PHASE1A_RECOVERY.md §4)
# ---------------------------------------------------------------------------


class AnchorSetViolation(str, Enum):
    """Why an **anchor set** is unusable, independent of any wallet.

    These are preconditions, not wallet verdicts. A wallet verdict computed against a
    violated anchor set is not evidence about the wallet, so these codes must be
    resolved *before* wallet filtering — otherwise a non-independent anchor set
    manufactures exactly the repeatable-looking performance
    docs/PHASE1A_RECOVERY.md §4 warns about.
    """

    #: Two anchors share a deployer. A cluster behaving as one entity is one data
    #: point, not many.
    DEPLOYER_SHARED = "anchor_set.deployer_shared"

    #: An anchor's deployer appears among another anchor's first-20 buyers.
    DEPLOYER_IS_FIRST20_BUYER = "anchor_set.deployer_is_first20_buyer"

    #: Two anchors are within 24 hours of each other, so one market regime supplies
    #: the whole sample.
    TEMPORAL_CLUSTER = "anchor_set.temporal_cluster"

    #: No anchor has first-20 buyers spanning more than 5 minutes. Without spread,
    #: buyer ordering has no observable signal and rank-at-entry degenerates to a
    #: tie among near-simultaneous transactions.
    NO_TIMESTAMP_SPREAD = "anchor_set.no_timestamp_spread"


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


class RuleId(str, Enum):
    """Identifiers for rules that fired. Carried on every verdict so it is auditable.

    Only implemented rules appear here, which since 2026-11-08 means all four.
    """

    #: Wallet was within N seconds of genesis for at least N anchors.
    GENESIS_BURST = "rule.genesis_burst"

    #: Quick round trips are a large enough share of recent swaps (``sandwich/v2``).
    SANDWICH = "rule.sandwich"

    #: Mean swap rate above ``THROUGHPUT_MAX_SWAPS_PER_DAY``.
    THROUGHPUT = "rule.throughput"

    #: More than ``ROUTER_ONLY_MIN_AGGREGATOR_FRACTION`` of swaps aggregator-routed
    #: with no direct pool interaction.
    ROUTER_ONLY = "rule.router_only"


# ---------------------------------------------------------------------------
# Wallet verdicts
# ---------------------------------------------------------------------------


class WalletVerdict(str, Enum):
    """Outcome for one (wallet, anchor) pair.

    Three-way, not two-way. "Not computable" is deliberately distinct from both
    accepted and rejected: a wallet that could not be evaluated has *not* passed, and
    recording it as accepted is how a false-negative rate becomes unmeasurable — the
    exact failure mode this module exists to fix.
    """

    #: Passed every rule that could be evaluated, with the evidence complete enough
    #: to evaluate them.
    ACCEPTED = "wallet.accepted"

    #: First buy fell within ``GENESIS_BURST_MAX_SECONDS`` of genesis for at least
    #: ``GENESIS_BURST_MIN_ANCHORS`` anchors. Targets the measured same-deployer,
    #: same-second failure mode with a *cross-anchor* test, which no per-wallet
    #: property can express.
    REJECT_GENESIS_BURST = "wallet.reject_genesis_burst"

    #: Fewer than ``GENESIS_BURST_MIN_ANCHORS`` anchors were observed, so the burst
    #: rule can neither fire nor be cleared. Must not be reported as ACCEPTED.
    NOT_COMPUTABLE_INSUFFICIENT_ANCHORS = "wallet.not_computable_insufficient_anchors"

    #: The mint's first-N buyer set is not provably complete, so rank and burst
    #: position cannot be computed. Ranking a prefix of history produces
    #: early-looking wallets that are artefacts of the sampling
    #: (docs/PHASE1B_SCOPING.md Q2 invariant 2).
    NOT_COMPUTABLE_INCOMPLETE_BUYER_SET = "wallet.not_computable_incomplete_buyer_set"

    #: The anchor's genesis time is unknown, so the burst delta is undefined.
    NOT_COMPUTABLE_NO_GENESIS = "wallet.not_computable_no_genesis"

    #: Fewer than ``MIN_SWAPS_TO_SCREEN`` swaps observed: the swap-derived rules can
    #: neither fire nor clear, so the wallet is unscreened rather than clean.
    NOT_COMPUTABLE_INSUFFICIENT_SWAPS = "wallet.not_computable_insufficient_swaps"

    #: Two or more ≤90s round trips across different mints (sandwich signature).
    REJECT_SANDWICH = "wallet.reject_sandwich"

    #: Swap rate above the throughput ceiling — infrastructure, not a trader.
    REJECT_THROUGHPUT = "wallet.reject_throughput"

    #: Aggregator-routed only, with no direct pool interaction (router/bot traffic).
    REJECT_ROUTER_ONLY = "wallet.reject_router_only"


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterVerdict:
    """One (wallet, anchor) decision, with the evidence that produced it.

    Every field is load-bearing for later auditing:

    * ``rule_id`` + ``rule_version`` — which rule decided, and under which thresholds.
      Without them a threshold change silently rewrites the meaning of old verdicts.
    * ``measured`` — the numbers that tripped the rule, not a prose summary. The
      44/45 failure was undiagnosable precisely because the values were not kept.
    * ``anchor_set_id`` — the verdict is relative to this set. A verdict without it
      cannot be re-measured or compared.

    Produced by :func:`evaluate_wallet` — one row per (wallet, anchor) pair,
    all carrying the same cross-anchor decision.
    """

    wallet: str
    anchor_mint: str
    verdict: WalletVerdict
    anchor_set_id: str
    rule_version: str
    rule_id: RuleId | None = None
    measured: dict[str, object] = field(default_factory=dict)
    decided_at: _dt.datetime | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def is_rejection(self) -> bool:
        """True only for a rule-driven rejection — not for "not computable"."""
        return self.verdict.value.startswith("wallet.reject")

    def is_computable(self) -> bool:
        """False when the evidence was insufficient to decide either way."""
        return not self.verdict.value.startswith("wallet.not_computable")


# ---------------------------------------------------------------------------
# Inputs to the rules
# ---------------------------------------------------------------------------

#: Version carried on every verdict. Bump whenever a rule's logic or its
#: thresholds change meaning: verdicts from different versions are not
#: comparable, and a verdict recorded without its version means nothing.
RULE_VERSION = "genesis_burst/v1"


@dataclass(frozen=True)
class AnchorObservation:
    """One anchor as the filter needs it: when it launched, who deployed it, who bought first.

    ``genesis_at`` is the **mint's creation transaction block time** (the §2
    route) — not pool creation, not ingest time. ``None`` means genesis is
    unknown, which makes the wallet rule *not computable* rather than wrong.

    ``first20`` feeds only the anchor-set preconditions of §4 (deployer-is-buyer,
    timestamp spread); the wallet rule never reads it.
    """

    mint: str
    genesis_at: _dt.datetime | None
    deployer: str | None = None
    first20: tuple[BuyerFirstSeen, ...] = ()


@dataclass(frozen=True)
class WalletAnchorEntry:
    """One anchor the wallet entered, and when it first bought there."""

    anchor_mint: str
    first_buy_at: _dt.datetime


# ---------------------------------------------------------------------------
# Checking the anchor-set preconditions (§4) — the three independence rules
# ---------------------------------------------------------------------------


def evaluate_anchor_set(
    anchors: Sequence[AnchorObservation],
) -> tuple[AnchorSetViolation, ...]:
    """Check an anchor set against the §4 independence requirements.

    Returns each distinct violation at most once, in a stable order — an empty
    tuple means the set satisfies all three requirements. Anchors with unknown
    genesis are skipped by the temporal check and anchors with unknown deployer
    by the two deployer checks: an uncheckable requirement is reported as
    uncheckable elsewhere, never silently as satisfied here.

    The spread requirement is "at least one anchor whose first-20 buyers span
    more than 5 minutes". An anchor with fewer than two first-20 buyers has a
    span of zero, so a set with no observed spread trips this — buyer ordering
    carries no signal when there is no ordering, which is §4's reason for the
    requirement in the first place.
    """
    found: list[AnchorSetViolation] = []

    def add(v: AnchorSetViolation) -> None:
        if v not in found:
            found.append(v)

    # 1. deployer diversity — no two anchors share a deployer.
    by_deployer: dict[str, str] = {}
    for a in anchors:
        if a.deployer is None:
            continue
        if a.deployer in by_deployer and by_deployer[a.deployer] != a.mint:
            add(AnchorSetViolation.DEPLOYER_SHARED)
        by_deployer[a.deployer] = a.mint

    # 2. no deployer is among another anchor's first-20 buyers.
    first20 = {a.mint: frozenset(b.buyer for b in a.first20) for a in anchors}
    for a in anchors:
        if a.deployer is None:
            continue
        if any(a.mint != b.mint and a.deployer in first20[b.mint] for b in anchors):
            add(AnchorSetViolation.DEPLOYER_IS_FIRST20_BUYER)

    # 3. temporal diversity — no two anchors within 24 hours.
    dated = [a for a in anchors if a.genesis_at is not None]
    if any(
        abs((x.genesis_at - y.genesis_at).total_seconds()) < 24 * 3600
        for i, x in enumerate(dated)
        for y in dated[i + 1:]
    ):
        add(AnchorSetViolation.TEMPORAL_CLUSTER)

    # 4. timestamp spread — >= 1 anchor whose first-20 buyers span > 5 minutes.
    def span(a: AnchorObservation) -> float:
        if len(a.first20) < 2:
            return 0.0
        times = [b.first_buy_at for b in a.first20]
        return (max(times) - min(times)).total_seconds()

    if not any(span(a) > 300 for a in anchors):
        add(AnchorSetViolation.NO_TIMESTAMP_SPREAD)

    return tuple(found)


# ---------------------------------------------------------------------------
# The genesis-burst rule
# ---------------------------------------------------------------------------


def evaluate_wallet(
    *,
    wallet: str,
    entries: Sequence[WalletAnchorEntry],
    anchors: Mapping[str, AnchorObservation],
    buyer_set_complete: bool,
    anchor_set_id: str = "",
    rule_version: str = RULE_VERSION,
    min_anchors: int = GENESIS_BURST_MIN_ANCHORS,
    max_seconds: int = GENESIS_BURST_MAX_SECONDS,
) -> tuple[FilterVerdict, ...]:
    """Apply the genesis-burst rule to one wallet's anchor history.

    **Rule:** reject a wallet whose first buy for at least ``min_anchors``
    anchors falls within ``max_seconds`` of each anchor's genesis
    (docs/PHASE1A_RECOVERY.md §3).

    Returns **one verdict per entered anchor** — the (wallet, anchor) unit the
    module documents — all carrying the same cross-anchor decision plus that
    anchor's own delta. A wallet that entered no anchors still gets one row
    (``anchor_mint=""``) so it cannot silently vanish from a run. Duplicate
    anchors collapse to their earliest first buy: "first buy" is unique per
    (wallet, anchor) by definition.

    Precedence, first match wins:

    1. ``buyer_set_complete`` false → ``NOT_COMPUTABLE_INCOMPLETE_BUYER_SET``.
       A first buy read from a prefix of history is an artefact of sampling —
       it looks earlier than it was.
    2. ``burst_hits >= min_anchors`` → ``REJECT_GENESIS_BURST``. Evidence to
       reject is checked *before* the not-computable paths so that a different
       anchor's missing genesis cannot mask a rejection that is already proven.
    3. an entered anchor with unknown genesis → ``NOT_COMPUTABLE_NO_GENESIS``.
    4. fewer than ``min_anchors`` anchors with known genesis →
       ``NOT_COMPUTABLE_INSUFFICIENT_ANCHORS`` (including zero entries).
    5. otherwise → ``ACCEPTED``: evaluated against at least ``min_anchors``
       anchors, and the rule did not fire.

    ``measured`` carries the numbers rather than a prose summary — per-anchor
    deltas in seconds, the burst hits, and the thresholds in force. That is
    the record whose absence made the 44/45 failure undiagnosable.

    ``GENESIS_BURST_DEFAULTS_VALIDATED`` is False for these thresholds: a
    verdict here applies an *unvalidated* rule to *synthetic* calibration
    data, which is why ``rule_version`` travels with every row.
    """
    # First buy per anchor is unique; collapse duplicates, earliest wins.
    per_anchor: dict[str, _dt.datetime] = {}
    for e in entries:
        if e.anchor_mint not in per_anchor or e.first_buy_at < per_anchor[e.anchor_mint]:
            per_anchor[e.anchor_mint] = e.first_buy_at

    deltas: dict[str, float] = {}
    burst: list[str] = []
    without_genesis = 0

    for mint, bought_at in per_anchor.items():
        a = anchors.get(mint)
        if a is None or a.genesis_at is None:
            without_genesis += 1
            continue
        delta = (bought_at - a.genesis_at).total_seconds()
        deltas[mint] = delta
        if 0.0 <= delta <= max_seconds:
            burst.append(mint)

    if not buyer_set_complete:
        verdict = WalletVerdict.NOT_COMPUTABLE_INCOMPLETE_BUYER_SET
    elif len(burst) >= min_anchors:
        verdict = WalletVerdict.REJECT_GENESIS_BURST
    elif without_genesis:
        verdict = WalletVerdict.NOT_COMPUTABLE_NO_GENESIS
    elif len(deltas) < min_anchors:
        verdict = WalletVerdict.NOT_COMPUTABLE_INSUFFICIENT_ANCHORS
    else:
        verdict = WalletVerdict.ACCEPTED

    measured: dict[str, object] = {
        "min_anchors": min_anchors,
        "max_seconds": max_seconds,
        "entries": len(per_anchor),
        "anchors_with_genesis": len(deltas),
        "anchors_without_genesis": without_genesis,
        "burst_hits": len(burst),
        "burst_anchors": tuple(burst),
        "deltas_seconds": dict(deltas),
    }

    if not per_anchor:
        return (
            FilterVerdict(
                wallet=wallet,
                anchor_mint="",
                verdict=verdict,
                anchor_set_id=anchor_set_id,
                rule_version=rule_version,
                rule_id=RuleId.GENESIS_BURST,
                measured=measured,
                notes=("wallet entered no anchors",),
            ),
        )

    return tuple(
        FilterVerdict(
            wallet=wallet,
            anchor_mint=mint,
            verdict=verdict,
            anchor_set_id=anchor_set_id,
            rule_version=rule_version,
            rule_id=RuleId.GENESIS_BURST,
            measured={**measured, "anchor_delta_seconds": deltas.get(mint)},
        )
        for mint in per_anchor
    )


# ---------------------------------------------------------------------------
# Inputs to the per-wallet heuristics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundTrip:
    """One position episode reduced to what ``sandwich`` needs.

    ``positions.Episode`` is the real source; this is its projection, so the rule
    does not depend on the reconstruction module and can be tested without one. A
    still-open episode has no ``closed_at`` and is not a round trip: a sandwich
    exists only when the wallet got out again.
    """

    mint: str
    opened_at: _dt.datetime | None
    closed_at: _dt.datetime | None

    @property
    def hold_seconds(self) -> float | None:
        """Seconds held, or None for an open / undated episode."""
        if self.opened_at is None or self.closed_at is None:
            return None
        return (self.closed_at - self.opened_at).total_seconds()


@dataclass(frozen=True)
class SwapObservation:
    """One swap reduced to routing evidence.

    ``aggregator`` names the router that carried it (``jupiter``, ``okx``,
    ``dflow``, ...). ``pool`` names the pool traded against directly. ``direct_pool``
    is explicit for callers that know the swap hit a pool without naming it; when
    omitted it is inferred as "a pool is named and no aggregator carried it".
    """

    at: _dt.datetime
    aggregator: str | None = None
    pool: str | None = None
    direct_pool: bool | None = None

    @property
    def hit_pool_directly(self) -> bool:
        if self.direct_pool is not None:
            return bool(self.direct_pool)
        return self.aggregator is None and self.pool is not None


def round_trips(positions: Sequence[object]) -> list[RoundTrip]:
    """Project position episodes (or already-built ``RoundTrip``s) for the rules.

    Accepts anything with ``mint`` / ``opened_at`` / ``closed_at`` attributes, which
    includes ``positions.Episode`` without importing it.
    """
    out: list[RoundTrip] = []
    for p in positions:
        if isinstance(p, RoundTrip):
            out.append(p)
            continue
        out.append(
            RoundTrip(
                mint=getattr(p, "mint", ""),
                opened_at=getattr(p, "opened_at", None),
                closed_at=getattr(p, "closed_at", None),
            )
        )
    return out


# ---------------------------------------------------------------------------
# The three per-wallet heuristics
# ---------------------------------------------------------------------------


def recent_window(
    swaps: Sequence[SwapObservation],
    window_days: int,
    *,
    as_of: _dt.datetime | None = None,
) -> list[SwapObservation]:
    """Swaps inside the last ``window_days``, ending at ``as_of``.

    ``as_of`` defaults to the wallet's **own latest swap**, not wall-clock now. A
    screening run over a historical sample would otherwise see an empty window for
    every wallet that stopped trading a week ago and pass it by default; anchoring to
    the last observed swap asks "was this wallet a sandwich bot while it was trading",
    which is the question the rule is for. Callers that want a fixed cutoff pass it.
    """
    if not swaps:
        return []
    end = as_of or max(s.at for s in swaps)
    start = end - _dt.timedelta(days=window_days)
    return [s for s in swaps if start <= s.at <= end]


def sandwich(
    wallet: str,
    positions: Sequence[object],
    swaps: Sequence[SwapObservation] = (),
    *,
    min_round_trips: int = SANDWICH_MIN_ROUND_TRIPS,
    max_seconds: int = SANDWICH_MAX_SECONDS,
    window_days: int = SANDWICH_WINDOW_DAYS,
    min_share: float = SANDWICH_MIN_SHARE,
    min_window_swaps: int = SANDWICH_MIN_WINDOW_SWAPS,
    as_of: _dt.datetime | None = None,
) -> bool:
    """Concentration of quick round trips in recent activity (``sandwich/v2``).

    Fires when, inside the last ``window_days`` of the wallet's own activity:

    * at least ``min_round_trips`` round trips lasted at most ``max_seconds``,
    * in at least two different mints (three fast flips in one token is scalping one
      launch, not a sandwich operation), and
    * those round trips are at least ``min_share`` of the window's swaps.

    **Why a share and not a count.** The v1 rule counted quick round trips over the
    entire observed history, which is not the same question. Real-data calibration on
    the 100 anchor buyers found a two-year retail wallet (0.5 swaps/day, active in all
    24 hours) rejected because 30 of its 207 lifetime swaps were sub-90 s flips — about
    two a month — while a 32,488 swaps/day bot received the same verdict. Counting
    cannot separate a habit from a strategy; proportion can.

    A window with fewer than ``min_window_swaps`` swaps returns False rather than
    "not computable": the wallet *has* been screened by the other rules, and a sparse
    trader is simply not a sandwich bot. Insufficient lifetime data remains
    ``NOT_COMPUTABLE_INSUFFICIENT_SWAPS`` at the ``screen_wallet`` level — the two
    conditions are different questions and keep different answers.

    ``max_seconds`` is the one v1 threshold this rule keeps: calibration showed the
    30-180 s sweep to be completely flat, so the window's duration and the share carry
    the decision, and inventing precision here would be false confidence.
    """
    if len(swaps) < min_window_swaps:
        return False
    window = recent_window(swaps, window_days, as_of=as_of)
    if len(window) < min_window_swaps:
        return False
    start = (as_of or max(s.at for s in swaps)) - _dt.timedelta(days=window_days)
    quick = [
        t for t in round_trips(positions)
        if t.hold_seconds is not None
        and t.hold_seconds <= max_seconds
        and t.opened_at is not None
        and t.opened_at >= start
    ]
    if len({t.mint for t in quick}) < 2:
        return False
    return len(quick) >= min_round_trips and len(quick) / len(window) >= min_share


def throughput(
    wallet: str,
    swaps: Sequence[SwapObservation],
    *,
    max_swaps_per_day: int = THROUGHPUT_MAX_SWAPS_PER_DAY,
    min_swaps: int = MIN_SWAPS_TO_SCREEN,
) -> bool:
    """Mean swap rate above ``max_swaps_per_day`` over the observed window.

    The window is the span between the wallet's first and last swap, floored at one
    minute: dividing a burst of swaps by a zero-second window would report an
    infinite rate for a wallet that simply traded twice in the same slot.

    Fewer than ``min_swaps`` observations returns False — the unscreened case is
    represented by the verdict, not by this predicate, so caller code cannot mistake
    "not enough data" for "clean".
    """
    if len(swaps) < min_swaps:
        return False
    times = sorted(s.at for s in swaps)
    span_seconds = max(60.0, (times[-1] - times[0]).total_seconds())
    return (len(swaps) / span_seconds) * 86400 > max_swaps_per_day


def router_only(
    wallet: str,
    swaps: Sequence[SwapObservation],
    *,
    min_fraction: float = ROUTER_ONLY_MIN_AGGREGATOR_FRACTION,
    min_swaps: int = MIN_SWAPS_TO_SCREEN,
) -> bool:
    """More than ``min_fraction`` of swaps aggregator-routed, none hitting a pool directly.

    Routing everything through Jupiter/OKX/dFlow and never touching a pool directly
    is the shape of an integration or a bot, not of a wallet choosing trades. Both
    halves are required: a high aggregator share with even one direct pool swap is
    ordinary retail behaviour in 2026.
    """
    if len(swaps) < min_swaps:
        return False
    routed = sum(1 for s in swaps if s.aggregator)
    direct = sum(1 for s in swaps if s.hit_pool_directly)
    if direct:
        return False
    return routed / len(swaps) > min_fraction


def genesis_burst(
    wallet: str,
    anchors: Mapping[str, AnchorObservation],
    *,
    entries: Sequence[WalletAnchorEntry],
    buyer_set_complete: bool = True,
    min_anchors: int = GENESIS_BURST_MIN_ANCHORS,
    max_seconds: int = GENESIS_BURST_MAX_SECONDS,
) -> bool:
    """True when the burst rule fires — a thin wrapper over :func:`evaluate_wallet`.

    The rule itself lives in ``evaluate_wallet`` because it produces per-anchor
    verdicts; this exists so ``screen_wallet`` can ask the question in one line and
    so the three heuristics share a call shape. ``buyer_set_complete=False`` yields
    False: an incomplete buyer set is not computable, not a rejection.
    """
    verdicts = evaluate_wallet(
        wallet=wallet,
        entries=entries,
        anchors=anchors,
        buyer_set_complete=buyer_set_complete,
        min_anchors=min_anchors,
        max_seconds=max_seconds,
    )
    return bool(verdicts) and verdicts[0].verdict is WalletVerdict.REJECT_GENESIS_BURST


# ---------------------------------------------------------------------------
# screen_wallet — the four rules, in order, one verdict
# ---------------------------------------------------------------------------

#: Version carried by :func:`screen_wallet` verdicts. Separate from ``RULE_VERSION``
#: (the genesis-burst-only version) so that adding three rules did not relabel every
#: existing burst verdict as having been screened by four.
SCREEN_RULE_VERSION = "quality_screen/v2"


def screen_wallet(
    wallet: str,
    anchors: Mapping[str, AnchorObservation],
    positions: Sequence[object] = (),
    swaps: Sequence[SwapObservation] = (),
    config: Mapping[str, object] | None = None,
    *,
    entries: Sequence[WalletAnchorEntry] | None = None,
    buyer_set_complete: bool = True,
    anchor_set_id: str = "",
    rule_version: str = SCREEN_RULE_VERSION,
) -> FilterVerdict:
    """Apply the four rules to one wallet and return a single verdict.

    Order, first hit wins (see the module docstring): ``genesis_burst``, swap
    sufficiency, ``sandwich``, ``throughput``, ``router_only``.

    ``entries`` is the wallet's anchor history; when it is omitted it is derived from
    ``positions`` — each episode's mint and open time — which is exactly the
    anchor→wallet edge the burst rule compares against each anchor's genesis.

    Outcomes are three-way, and the difference is load-bearing:

    * ``ACCEPTED`` — every rule that could be evaluated was, and none fired.
    * ``REJECT_*`` — a named rule fired, with the numbers in ``measured``.
    * ``NOT_COMPUTABLE_*`` — the evidence needed was absent. This is **not** a clean
      bill: with fewer than ``min_swaps_to_screen`` swaps the wallet has not been
      screened at all.

    ``config`` merges over ``DEFAULT_SCREEN_CONFIG``; keys name the rule they tune.
    ``SCREEN_DEFAULTS_VALIDATED`` is False, so a verdict here applies unvalidated
    thresholds — which is why ``rule_version`` travels on every row.
    """
    cfg: dict[str, object] = {**DEFAULT_SCREEN_CONFIG, **dict(config or {})}
    trips = round_trips(positions)
    if entries is None:
        entries = [
            WalletAnchorEntry(anchor_mint=t.mint, first_buy_at=t.opened_at)
            for t in trips
            if t.opened_at is not None
        ]

    measured: dict[str, object] = {
        "config": dict(cfg),
        "thresholds_validated": SCREEN_DEFAULTS_VALIDATED,
        "anchors_observed": len(entries),
        "swaps_observed": len(swaps),
        "positions_observed": len(trips),
    }

    # 1. genesis burst — cross-anchor, so it is the one rule that needs the anchors.
    burst_verdicts = evaluate_wallet(
        wallet=wallet,
        entries=entries,
        anchors=anchors,
        buyer_set_complete=buyer_set_complete,
        anchor_set_id=anchor_set_id,
        rule_version=rule_version,
        min_anchors=int(cfg["genesis_burst_min_anchors"]),
        max_seconds=int(cfg["genesis_burst_max_seconds"]),
    )
    if burst_verdicts and burst_verdicts[0].verdict is WalletVerdict.REJECT_GENESIS_BURST:
        return FilterVerdict(
            wallet=wallet,
            anchor_mint=burst_verdicts[0].anchor_mint,
            verdict=WalletVerdict.REJECT_GENESIS_BURST,
            anchor_set_id=anchor_set_id,
            rule_version=rule_version,
            rule_id=RuleId.GENESIS_BURST,
            measured={**measured, **burst_verdicts[0].measured},
            notes=("rejected before the swap rules ran",),
        )

    # 2. sufficiency — a wallet we cannot screen is not a wallet we cleared.
    min_swaps = int(cfg["min_swaps_to_screen"])
    if len(swaps) < min_swaps:
        return FilterVerdict(
            wallet=wallet,
            anchor_mint="",
            verdict=WalletVerdict.NOT_COMPUTABLE_INSUFFICIENT_SWAPS,
            anchor_set_id=anchor_set_id,
            rule_version=rule_version,
            measured={**measured, "min_swaps_to_screen": min_swaps},
            notes=(
                f"{len(swaps)} swap(s) observed; a throughput or router rule cannot "
                f"clear or trip on fewer than {min_swaps}",
            ),
        )

    # 3-5. the per-wallet heuristics, in order.
    window = recent_window(swaps, int(cfg["sandwich_window_days"]))
    window_quick = [
        t for t in trips
        if t.hold_seconds is not None
        and t.hold_seconds <= int(cfg["sandwich_max_seconds"])
        and t.opened_at is not None
        and t.opened_at >= max(s.at for s in swaps) - _dt.timedelta(
            days=int(cfg["sandwich_window_days"]))
    ]
    if sandwich(
        wallet,
        positions,
        swaps,
        min_round_trips=int(cfg["sandwich_min_round_trips"]),
        max_seconds=int(cfg["sandwich_max_seconds"]),
        window_days=int(cfg["sandwich_window_days"]),
        min_share=float(cfg["sandwich_min_share"]),
        min_window_swaps=int(cfg["sandwich_min_window_swaps"]),
    ):
        return FilterVerdict(
            wallet=wallet,
            anchor_mint="",
            verdict=WalletVerdict.REJECT_SANDWICH,
            anchor_set_id=anchor_set_id,
            rule_version=rule_version,
            rule_id=RuleId.SANDWICH,
            measured={
                **measured,
                "quick_round_trips": len(window_quick),
                "window_swaps": len(window),
                "quick_share": (round(len(window_quick) / len(window), 4)
                                if window else 0.0),
                "quick_mints": len({t.mint for t in window_quick}),
                "min_round_trips": cfg["sandwich_min_round_trips"],
                "min_share": cfg["sandwich_min_share"],
                "window_days": cfg["sandwich_window_days"],
                "max_seconds": cfg["sandwich_max_seconds"],
            },
            notes=("concentration rule: quick round trips are a share of recent swaps",),
        )

    if throughput(
        wallet,
        swaps,
        max_swaps_per_day=int(cfg["throughput_max_swaps_per_day"]),
        min_swaps=min_swaps,
    ):
        times = sorted(s.at for s in swaps)
        span = max(60.0, (times[-1] - times[0]).total_seconds())
        return FilterVerdict(
            wallet=wallet,
            anchor_mint="",
            verdict=WalletVerdict.REJECT_THROUGHPUT,
            anchor_set_id=anchor_set_id,
            rule_version=rule_version,
            rule_id=RuleId.THROUGHPUT,
            measured={
                **measured,
                "swaps_per_day": round((len(swaps) / span) * 86400, 2),
                "span_days": round(span / 86400, 4),
                "max_swaps_per_day": cfg["throughput_max_swaps_per_day"],
            },
        )

    routed = sum(1 for s in swaps if s.aggregator)
    direct = sum(1 for s in swaps if s.hit_pool_directly)
    if router_only(
        wallet,
        swaps,
        min_fraction=float(cfg["router_only_min_aggregator_fraction"]),
        min_swaps=min_swaps,
    ):
        return FilterVerdict(
            wallet=wallet,
            anchor_mint="",
            verdict=WalletVerdict.REJECT_ROUTER_ONLY,
            anchor_set_id=anchor_set_id,
            rule_version=rule_version,
            rule_id=RuleId.ROUTER_ONLY,
            measured={
                **measured,
                "aggregator_fraction": round(routed / len(swaps), 4),
                "direct_pool_swaps": direct,
                "min_fraction": cfg["router_only_min_aggregator_fraction"],
            },
        )

    return FilterVerdict(
        wallet=wallet,
        anchor_mint="",
        verdict=WalletVerdict.ACCEPTED,
        anchor_set_id=anchor_set_id,
        rule_version=rule_version,
        measured={
            **measured,
            "aggregator_fraction": round(routed / len(swaps), 4),
            "direct_pool_swaps": direct,
            "quick_round_trips": len(window_quick),
            "window_swaps": len(window),
            "quick_share": (round(len(window_quick) / len(window), 4) if window else 0.0),
        },
        notes=("all four rules evaluated; none fired",),
    )
