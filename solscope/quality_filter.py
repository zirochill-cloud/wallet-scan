"""Phase 1b wallet quality filter — verdict and reason codes.

**Reason codes and output contract only. No rule logic is implemented yet.**

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
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "AnchorSetViolation",
    "WalletVerdict",
    "RuleId",
    "GENESIS_BURST_MIN_ANCHORS",
    "GENESIS_BURST_MAX_SECONDS",
    "GENESIS_BURST_DEFAULTS_VALIDATED",
    "NOT_PORTED_HEURISTICS",
    "FilterVerdict",
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

    Not implemented yet: this is the shape the filter must produce (S4).
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
