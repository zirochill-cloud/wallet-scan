"""Phase 1b §5 — band derivation from the adjusted score, confidence and gate.

A band answers a narrower question than the score does: given how good this wallet
looks **and** how much we can confirm, what should a human do with it?

==========================  ==================================================
``track``                   good and well evidenced: worth following
``watch``                   high on the adjusted score, unproven
``investigate``             middling but measured: worth a manual look
``promising_thin``          looks strong, cannot be ranked (too few anchors)
``exclude``                 not good enough, or not evidenced enough
==========================  ==================================================

Two properties this module exists to preserve:

* **`promising_thin` is not `exclude` with a nicer name.** A wallet that fails the
  anchor conjunct is unrankable, but a high raw score at decent confidence is a
  wallet a human should see. Under the three-band rule it disappeared. The band
  gates on **raw** score, not adjusted: a two-anchor wallet is shrunk hard, and
  asking the shrunken number to clear the bar would demand the wallet prove again
  the thing its missing anchors prevent it from proving.
* **`exclude` keeps its reasons apart.** "We measured it and it is not good
  enough" and "we could not measure enough of it to say" are different facts. The
  distinction lives in ``band_reason`` (and therefore in ``gated_by``), because a
  collapsed band must not collapse the explanation.

The thresholds come from ``config/scoring.yaml`` ``bands:`` — never hardcoded, so
the deviation between the design's 70 and the configured 75 is visible in one
place. The two conjuncts the config has no key for (the confidence floor that
``track`` requires, and the ``promising_thin`` floors) are read from ``bands`` when
present and otherwise default to the design's values; §5 fixes them, and inventing
config keys for them is S12c's decision to record, not this module's to assume.

Band basis (D21): while five of eight dimensions are unbuilt, the adjusted score
is ``shrunk_early_entry x confidence / 100``, so these are **early-entry** bands —
"is this wallet's early-entry ability worth tracking?" — not full-wallet verdicts.
When the other dimensions exist, ``scores.band_basis`` flips to ``overall_score``
and the same rules become full-wallet bands.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any, Mapping

from .confidence import GateResult

#: §5: `track` needs confidence as well as the adjusted score.
DEFAULT_TRACK_MIN_CONFIDENCE = Decimal("60")
#: §5: `promising_thin` speaks about unconfirmed strength, so it reads the raw score.
DEFAULT_PROMISING_MIN_RAW = Decimal("70")
DEFAULT_PROMISING_MIN_CONFIDENCE = Decimal("30")

BAND_TRACK = "track"
BAND_WATCH = "watch"
BAND_INVESTIGATE = "investigate"
BAND_PROMISING_THIN = "promising_thin"
BAND_EXCLUDE = "exclude"

#: §5's defaults, used only when `bands:` omits a key.
DEFAULT_THRESHOLDS = {
    "track": Decimal("70"),
    "watch": Decimal("60"),
    "investigate": Decimal("40"),
}


@dataclasses.dataclass(frozen=True)
class BandResult:
    """One wallet's band plus the reasons a reader needs to trust it.

    ``band_basis_score`` is the number the thresholds were compared against, kept
    separately from ``raw_score`` so a report can show both without recomputing
    anything. ``band_reason`` doubles as the ``gated_by`` fragment: for
    ``promising_thin`` it is the positive ``promising_but_unconfirmed`` note, for a
    gate-passed wallet below the investigate floor it says ``raw_below_investigate``
    (or the fuller statement when confidence is low too), and it is empty for
    bands that are simply a rank.
    """

    band: str
    band_basis_score: Decimal
    raw_score: Decimal
    confidence: Decimal
    gate_passed: bool
    gate_reason: str
    band_reason: str = ""


def _bands_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    section = (config or {}).get("bands")
    return section if isinstance(section, Mapping) else {}


def _threshold(config: Mapping[str, Any] | None, key: str) -> Decimal:
    return Decimal(str(_bands_config(config).get(key, DEFAULT_THRESHOLDS[key])))


def derive_band(adjusted: Decimal, raw_score: Decimal, confidence: Decimal,
                gate: GateResult, config: Mapping[str, Any] | None) -> BandResult:
    """Assign the §5 band from the adjusted score, raw score, confidence and gate.

    Gate outcome is evaluated first, because it decides *which question* the band
    answers. A wallet that fails the anchor conjunct cannot be ranked at all, so
    the adjusted thresholds are not applied to it: it is either
    ``promising_thin`` (strong raw evidence, unconfirmable) or ``exclude``. A
    wallet that fails the confidence conjunct has no evidence budget to be
    promising with, so ``promising_thin`` is deliberately unreachable for it.

    Within a passed gate the order is ``track`` → ``watch`` → ``investigate`` →
    ``exclude``, each threshold read from config. There is no re-derivation of
    values here: the adjusted score arrived already pulled, and bands decide what
    to do with a declined claim rather than changing the number.
    """
    adjusted = Decimal(adjusted)
    raw_score = Decimal(raw_score)
    confidence = Decimal(confidence)

    track_at = _threshold(config, "track")
    watch_at = _threshold(config, "watch")
    investigate_at = _threshold(config, "investigate")
    track_confidence = Decimal(str(_bands_config(config).get(
        "track_min_confidence", DEFAULT_TRACK_MIN_CONFIDENCE)))
    promising_raw = Decimal(str(_bands_config(config).get(
        "promising_thin_raw", DEFAULT_PROMISING_MIN_RAW)))
    promising_confidence = Decimal(str(_bands_config(config).get(
        "promising_thin_confidence", DEFAULT_PROMISING_MIN_CONFIDENCE)))

    def result(band: str, reason: str = "") -> BandResult:
        return BandResult(
            band=band,
            band_basis_score=adjusted,
            raw_score=raw_score,
            confidence=confidence,
            gate_passed=gate.gate_passed,
            gate_reason=gate.gate_reason,
            band_reason=reason,
        )

    if not gate.gate_passed:
        anchor_only = gate.gate_reason.startswith("distinct_anchors")
        if anchor_only and raw_score >= promising_raw and confidence >= promising_confidence:
            return result(BAND_PROMISING_THIN, "promising_but_unconfirmed")
        if anchor_only:
            return result(
                BAND_EXCLUDE,
                f"promising_thin floor not met (raw {raw_score} of {promising_raw}, "
                f"confidence {confidence} of {promising_confidence})")
        # A confidence-gate failure: nothing here is rankable *or* promising.
        return result(BAND_EXCLUDE, "")

    if adjusted >= track_at and confidence >= track_confidence:
        return result(BAND_TRACK)
    if adjusted >= watch_at:
        return result(BAND_WATCH)
    if adjusted >= investigate_at:
        return result(BAND_INVESTIGATE)
    if raw_score < investigate_at and confidence < watch_at:
        # The distinction a future session needs: "we looked and it is not
        # promising" is not the same fact as "we could not evaluate it".
        return result(
            BAND_EXCLUDE,
            "raw score below investigate threshold AND confidence below watch threshold")
    return result(BAND_EXCLUDE, "raw_below_investigate")
