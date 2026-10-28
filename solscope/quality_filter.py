"""Phase 1b wallet quality filter — genesis-burst rule, verdicts, reason codes.

**The genesis-burst rule and the anchor-set preconditions are implemented** and
calibrated against synthetic bundled/clean sets (``tests/test_phase1b_filter.py``);
**the three prior heuristics are not** — see ``NOT_PORTED_HEURISTICS``.

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

## What is deliberately NOT here

The prior filter's three heuristics — ``sandwich``, ``throughput``, ``router_only`` —
are **not implemented and have no codes**. They were never tested, and the filter they
belonged to admitted 44 of 45. Giving them enum members would imply they work. They
are listed in ``NOT_PORTED_HEURISTICS`` so the omission is visible rather than
mistaken for an oversight, and so they cannot be silently reintroduced.
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
    "NOT_PORTED_HEURISTICS",
    "FilterVerdict",
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

#: Heuristics from the prior session that are NOT ported. Present so the omission is
#: deliberate and visible; they have no codes and no rules.
NOT_PORTED_HEURISTICS: tuple[str, ...] = ("sandwich", "throughput", "router_only")


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

    Only implemented rules appear here. ``sandwich``/``throughput``/``router_only``
    are absent on purpose — see ``NOT_PORTED_HEURISTICS``.
    """

    #: Wallet was within N seconds of genesis for at least N anchors.
    GENESIS_BURST = "rule.genesis_burst"


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
        return self.verdict is WalletVerdict.REJECT_GENESIS_BURST

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
