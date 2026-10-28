"""Phase 1b quality filter — genesis-burst calibration on synthetic sets (S4).

The prior filter's failure was not that its parameters were wrong: it was an
**unmeasured** rejection rate. So this test builds two labelled sets, runs the
rule, and reports three numbers that are kept apart on purpose:

    bundled rejection rate    target >= 90%   (the 44/45 failure mode, simulated)
    clean false-positive rate target <= 10%   (late organic entries must survive)
    not_computable fraction   reported SEPARATELY — never folded into either bucket

Targets are asserted as thresholds, not exact numbers: a 60% bundled rejection
would mean the rule is under-powered and must be revised, not tuned around.

WHAT THIS DOES AND DOES NOT PROVE
The sets are synthetic and deterministic (no RNG). They prove the rule fires
when it should and stays silent when it should — i.e. the implementation
matches the specification. They do NOT prove N=2/Y=60 is a good threshold:
that needs real bundled wallets, whose identities were lost (NOTES.md §
QUALITY_FILTER_PENDING; GENESIS_BURST_DEFAULTS_VALIDATED is False).

Set construction (genesis relative to T0, all times UTC)
--------------------------------------------------------
Anchors:  A1 T0+0h (D1), A2 T0+48h (D2), A3 T0+96h (D3)   <- bundled playground
          A4 T0+192h (D4), A7 T0+240h (D7)                <- clean-only, no overlap
          A5 T0+192h (D5), A6 T0+194h (D5)                <- SAME deployer (§4 trip)

bundled (50): 40 wallets enter A1+A2+A3, 6 enter A1+A2, 4 enter A5+A6,
              every first buy 5-25s after that anchor's genesis (inside Y=60).
clean   (50): 35 enter ONE anchor (A4 or A7) at 5-60 min after genesis;
              15 enter BOTH A4 and A7 at 5-60 min (exercises the ACCEPTED path,
              since a single anchor can never be evaluable under N=2).
              Clean wallets touch only A4/A7 — zero anchor overlap with bundled.

Run: python tests/test_phase1b_filter.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.metrics import BuyerFirstSeen  # noqa: E402
from solscope.quality_filter import (  # noqa: E402
    AnchorObservation,
    AnchorSetViolation,
    WalletAnchorEntry,
    WalletVerdict,
    evaluate_anchor_set,
    evaluate_wallet,
)

H = dt.timedelta(hours=1)
S = dt.timedelta(seconds=1)
T0 = dt.datetime(2026, 3, 2, 12, 0, tzinfo=dt.timezone.utc)

ANCHORS: dict[str, AnchorObservation] = {
    "A1": AnchorObservation("A1", T0, deployer="D1"),
    "A2": AnchorObservation("A2", T0 + 48 * H, deployer="D2"),
    "A3": AnchorObservation("A3", T0 + 96 * H, deployer="D3"),
    "A4": AnchorObservation("A4", T0 + 192 * H, deployer="D4"),
    "A7": AnchorObservation("A7", T0 + 240 * H, deployer="D7"),
    "A5": AnchorObservation("A5", T0 + 192 * H, deployer="D5"),
    "A6": AnchorObservation("A6", T0 + 194 * H, deployer="D5"),  # shared deployer
}

BUNDLED_MINTS = {
    **{f"B{i:02d}": ["A1", "A2", "A3"] for i in range(40)},
    **{f"B{i:02d}": ["A1", "A2"] for i in range(40, 46)},
    **{f"B{i:02d}": ["A5", "A6"] for i in range(46, 50)},
}
CLEAN_MINTS = {
    **{f"C{i:02d}": ["A4" if i % 2 == 0 else "A7"] for i in range(35)},
    **{f"C{i:02d}": ["A4", "A7"] for i in range(35, 50)},
}

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        _failures.append(label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def burst_delay(i: int) -> dt.timedelta:
    """5-25 s after genesis: deterministic, always inside Y=60."""
    return (5 + (i * 7) % 21) * S


def late_delay(i: int) -> dt.timedelta:
    """300-3599 s after genesis: 5-60 minutes, always outside Y=60."""
    return dt.timedelta(seconds=300 + (i * 173) % 3300)


def entries_for(wallet: str, mints: list[str], delay_fn) -> list[WalletAnchorEntry]:
    idx = int(wallet[1:])  # B00 -> 0, C35 -> 35
    return [
        WalletAnchorEntry(m, ANCHORS[m].genesis_at + delay_fn(idx + j))
        for j, m in enumerate(mints)
    ]


def run(wallet: str, mints: list[str], delay_fn, *, anchors=ANCHORS, complete=True):
    return evaluate_wallet(
        wallet=wallet,
        entries=entries_for(wallet, mints, delay_fn) if mints else [],
        anchors=anchors,
        buyer_set_complete=complete,
        anchor_set_id="synthetic-2026-10-28",
    )


def bucket(verdict: WalletVerdict) -> str:
    """Three-way classification; not_computable is its own bucket, always."""
    if verdict is WalletVerdict.REJECT_GENESIS_BURST:
        return "rejected"
    if verdict is WalletVerdict.ACCEPTED:
        return "accepted"
    return "not_computable"


def tabulate(mints: dict[str, list[str]], delay_fn, label: str) -> dict[str, int]:
    counts = {"rejected": 0, "accepted": 0, "not_computable": 0}
    inconsistent: list[str] = []
    for wallet, mints_w in mints.items():
        rows = run(wallet, mints_w, delay_fn)
        if len({r.verdict for r in rows}) != 1:
            inconsistent.append(wallet)
        counts[bucket(rows[0].verdict)] += 1
    check(f"{label}: one verdict across a wallet's (wallet, anchor) rows",
          not inconsistent, f"inconsistent: {inconsistent}")
    total = sum(counts.values())
    print(
        f"  {label}: {total} wallets — rejected {counts['rejected']} "
        f"({counts['rejected']/total:.0%}), accepted {counts['accepted']}, "
        f"not_computable {counts['not_computable']}"
    )
    return counts


def main() -> int:
    # --- bundled set: must be rejected -----------------------------------
    print("\n[calibration — bundled set]")
    b = tabulate(BUNDLED_MINTS, burst_delay, "bundled")
    rate = b["rejected"] / sum(b.values())
    check("bundled rejection rate >= 90%", rate >= 0.90, f"got {rate:.0%}")
    check("bundled set has no accepted wallets", b["accepted"] == 0,
          f"got {b['accepted']}")

    # --- clean set: must NOT be rejected ---------------------------------
    print("\n[calibration — clean set]")
    c = tabulate(CLEAN_MINTS, late_delay, "clean  ")
    total_c = sum(c.values())
    fpr = c["rejected"] / total_c
    check("clean false-positive rate <= 10%", fpr <= 0.10, f"got {fpr:.0%}")
    check("not_computable reported as its own bucket (sums to total)",
          c["rejected"] + c["accepted"] + c["not_computable"] == total_c)
    # Construction expectations: 35 single-anchor -> not computable under N=2,
    # 15 two-anchor late entries -> accepted.
    check("clean: 35 single-anchor wallets are not_computable",
          c["not_computable"] == 35, f"got {c['not_computable']}")
    check("clean: 15 two-anchor late wallets are accepted",
          c["accepted"] == 15, f"got {c['accepted']}")

    # --- the evidence is recorded, not summarised ------------------------
    print("\n[evidence carried on the verdict]")
    rows = run("B00", ["A1", "A2", "A3"], burst_delay)
    m = rows[0].measured
    print(f"  sample B00 measured: {m}")
    check("measured carries burst_hits", m.get("burst_hits") == 3, f"got {m.get('burst_hits')}")
    check("measured carries per-anchor deltas in seconds",
          isinstance(m.get("deltas_seconds"), dict) and len(m["deltas_seconds"]) == 3)
    check("measured carries the thresholds in force",
          m.get("min_anchors") == 2 and m.get("max_seconds") == 60)
    check("every row names its anchor (the (wallet, anchor) unit)",
          [r.anchor_mint for r in rows] == ["A1", "A2", "A3"])
    check("rule_id and rule_version travel with the verdict",
          rows[0].rule_id is not None and rows[0].rule_version == "genesis_burst/v1")

    # --- precedence and not-computable paths -----------------------------
    print("\n[precedence and not-computable paths]")

    rows = run("B00", ["A1", "A2"], burst_delay, complete=False)
    check("incomplete buyer set short-circuits everything",
          rows[0].verdict is WalletVerdict.NOT_COMPUTABLE_INCOMPLETE_BUYER_SET)

    anchors_missing = {**ANCHORS, "A7": AnchorObservation("A7", None, deployer="D7")}
    rows = run("B00", ["A1", "A2", "A7"], burst_delay, anchors=anchors_missing)
    check("rejection is NOT masked by another anchor's missing genesis",
          rows[0].verdict is WalletVerdict.REJECT_GENESIS_BURST,
          f"got {rows[0].verdict.value}")

    rows = run("B00", ["A1", "A7"], burst_delay, anchors=anchors_missing)
    check("missing genesis blocks the verdict when rejection is not yet proven",
          rows[0].verdict is WalletVerdict.NOT_COMPUTABLE_NO_GENESIS,
          f"got {rows[0].verdict.value}")

    rows = run("C34", ["A4"], late_delay)
    check("one evaluable anchor is insufficient under N=2",
          rows[0].verdict is WalletVerdict.NOT_COMPUTABLE_INSUFFICIENT_ANCHORS)

    rows = evaluate_wallet(wallet="NOBODY", entries=[], anchors=ANCHORS,
                           buyer_set_complete=True, anchor_set_id="synthetic-2026-10-28")
    check("a wallet with no anchors still returns one row",
          len(rows) == 1 and rows[0].anchor_mint == "")
    check("...and it is not_computable, never ACCEPTED",
          not rows[0].is_computable() and rows[0].verdict is not WalletVerdict.ACCEPTED)

    check("is_rejection() is true only for rule-driven rejections",
          run("B00", ["A1", "A2"], burst_delay)[0].is_rejection()
          and not rows[0].is_rejection())

    # --- anchor-set preconditions (§4) ------------------------------------
    print("\n[anchor-set preconditions — §4]")

    def f20(t0: dt.datetime, offsets: list[int]) -> tuple[BuyerFirstSeen, ...]:
        return tuple(
            BuyerFirstSeen(f"buyer-{t0}-{k}", t0 + dt.timedelta(seconds=o))
            for k, o in enumerate(offsets)
        )

    v = evaluate_anchor_set([ANCHORS["A5"], ANCHORS["A6"]])
    check("shared-deployer anchors trip DEPLOYER_SHARED",
          AnchorSetViolation.DEPLOYER_SHARED in v, f"got {[x.value for x in v]}")

    close = [
        AnchorObservation("T1", T0, deployer="TD1", first20=f20(T0, [0, 400])),
        AnchorObservation("T2", T0 + 12 * H, deployer="TD2",
                          first20=f20(T0 + 12 * H, [0, 400])),
    ]
    check("anchors 12h apart trip TEMPORAL_CLUSTER",
          AnchorSetViolation.TEMPORAL_CLUSTER in evaluate_anchor_set(close))

    buyer_of_other = [
        AnchorObservation("E1", T0, deployer="ED1", first20=f20(T0, [0, 400])),
        AnchorObservation("E2", T0 + 48 * H, deployer="ED2",
                          first20=f20(T0 + 48 * H, [0, 400])
                          + (BuyerFirstSeen("ED1", T0 + 48 * H + 10 * S),)),
    ]
    check("a deployer inside another anchor's first-20 trips the rule",
          AnchorSetViolation.DEPLOYER_IS_FIRST20_BUYER in evaluate_anchor_set(buyer_of_other))

    tight = [
        AnchorObservation("S1", T0, deployer="SD1", first20=f20(T0, [0, 10, 20])),
        AnchorObservation("S2", T0 + 48 * H, deployer="SD2",
                          first20=f20(T0 + 48 * H, [0, 60])),
    ]
    check("a set with no >5min buyer span trips NO_TIMESTAMP_SPREAD",
          AnchorSetViolation.NO_TIMESTAMP_SPREAD in evaluate_anchor_set(tight))

    good = [
        AnchorObservation("G1", T0, deployer="GD1", first20=f20(T0, [0, 120, 400])),
        AnchorObservation("G2", T0 + 48 * H, deployer="GD2",
                          first20=f20(T0 + 48 * H, [0, 60, 360])),
    ]
    v = evaluate_anchor_set(good)
    check("an independent, well-spread set has no violations",
          v == tuple(), f"got {[x.value for x in v]}")

    # --- summary ----------------------------------------------------------
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("ALL QUALITY-FILTER CALIBRATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
