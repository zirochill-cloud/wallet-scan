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
    RoundTrip,
    SwapObservation,
    WalletAnchorEntry,
    WalletVerdict,
    MIN_SWAPS_TO_SCREEN,
    SANDWICH_MIN_ROUND_TRIPS,
    SANDWICH_MIN_SHARE,
    SANDWICH_MIN_WINDOW_SWAPS,
    SANDWICH_WINDOW_DAYS,
    evaluate_anchor_set,
    evaluate_wallet,
    router_only,
    sandwich,
    screen_wallet,
    throughput,
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


def wallet_series(*, swaps: int, span_days: float, quick_trips: int,
                  quick_mints: int = 3, window_days: int = 30):
    """A (swaps, round trips) pair with a controlled recent-window composition.

    Swaps are spread evenly over ``span_days`` ending now; every quick round trip sits
    inside the last ``window_days`` and lasts 30 s, so ``share`` is exactly
    ``quick_trips / swaps-in-window``. This is how the real wallets are described in
    docs/PHASE1B_CALIBRATION.md, so the fixtures and the field data speak the same
    language.
    """
    end = T0 + dt.timedelta(days=span_days)
    step = dt.timedelta(days=span_days) / swaps if swaps else dt.timedelta(0)
    sw = [SwapObservation(at=end - step * (swaps - 1 - i), aggregator=None, pool="pump-amm")
          for i in range(swaps)]
    window_seconds = window_days * 86400
    trips = []
    for j in range(quick_trips):
        offset = window_seconds * (j + 1) / (quick_trips + 1)
        at = end - dt.timedelta(seconds=window_seconds - offset)
        trips.append(RoundTrip(mint=f"qm{j % quick_mints}", opened_at=at,
                               closed_at=at + dt.timedelta(seconds=30)))
    return sw, trips


def test_sandwich_v2_calibration_ground_truth() -> None:
    """sandwich/v2 against the profiles measured on the 100 real wallets.

    Every expectation here is the *measured* outcome from
    docs/PHASE1B_CALIBRATION.md, including the two cases the redesign does not get
    right. Encoding the honest answer is the point: a regression test that asserts a
    hoped-for verdict would pass while the filter stayed broken.
    """
    print("\n[sandwich v2 — ground truth from the 100-wallet calibration]")

    # 8a6JShiMBw, the anchor mismatch under v1: a retail trader carrying 30 sub-90s
    # round trips across 417 days, ~2 of them in the recent window.
    sw, trips = wallet_series(swaps=207, span_days=417, quick_trips=2)
    check("retail wallet (2 quick trips in 30 days) is ACCEPTED",
          not sandwich("C50", trips, sw), "v1 rejected this wallet")

    # The same wallet as measured: 198 swaps in its last 30 days, 27 quick trips = 13.6%.
    sw, trips = wallet_series(swaps=198, span_days=30, quick_trips=27)
    check("retail wallet at 13.6% quick share is ACCEPTED",
          not sandwich("C51", trips, sw))

    # Two human wallets: sparse, few or no round trips.
    sw, trips = wallet_series(swaps=3, span_days=51, quick_trips=0)
    check("3-swap human in 51 days is ACCEPTED", not sandwich("C52", trips, sw))
    sw, trips = wallet_series(swaps=8, span_days=40, quick_trips=1)
    check("8-swap human with one 30s flip is ACCEPTED", not sandwich("C53", trips, sw))

    # Unambiguous bots: the lost-bot signatures v2 must still catch.
    sw, trips = wallet_series(swaps=1389, span_days=1, quick_trips=1189)
    check("1,389-swap/day bot at 86% quick share is REJECTED", sandwich("C54", trips, sw))
    sw, trips = wallet_series(swaps=1339, span_days=24, quick_trips=621)
    check("55-swaps/day bot at 46% quick share is REJECTED", sandwich("C55", trips, sw))

    # Known limitation, asserted as measured rather than as hoped.
    sw, trips = wallet_series(swaps=1260, span_days=23, quick_trips=215)
    check("bot-like wallet at 17.1% share is ACCEPTED (just under the 20% floor)",
          not sandwich("C56", trips, sw), "a near-miss the redesign does not fix")

    # 52NUhyYcpw: 1,462 swaps but only 23 matched round trips — volume, not sandwiching.
    sw, trips = wallet_series(swaps=1462, span_days=0.132, quick_trips=22)
    check("volume wallet (22 trips in 1,462 swaps) is NOT a sandwich",
          not sandwich("C57", trips, sw))
    check("...and throughput catches it instead", throughput("C57", sw))

    # The share floor and count floor, stated directly.
    sw, trips = wallet_series(swaps=100, span_days=30, quick_trips=40)
    check("100 window swaps, 40 quick trips, share 0.40 is REJECTED",
          sandwich("C58", trips, sw))
    sw, trips = wallet_series(swaps=100, span_days=30, quick_trips=15)
    check("100 window swaps, 15 quick trips, share 0.15 is ACCEPTED",
          not sandwich("C59", trips, sw))
    sw, trips = wallet_series(swaps=100, span_days=30, quick_trips=2)
    check("2 quick trips in 30 days is ACCEPTED (below the count floor)",
          not sandwich("C60", trips, sw))

    # A thin window is a pass, not "not computable" — the two are different questions.
    sw, trips = wallet_series(swaps=4, span_days=20, quick_trips=2)
    check("4 swaps in the window is a pass, not NOT_COMPUTABLE",
          not sandwich("C61", trips, sw) and len(sw) >= MIN_SWAPS_TO_SCREEN)

    # End to end through screen_wallet, on the four wallets that must not move.
    cases = {
        "retail-anchor": (207, 417, 2, WalletVerdict.ACCEPTED),
        "bot-fast": (1389, 1, 1189, WalletVerdict.REJECT_SANDWICH),
        "human-sparse": (8, 40, 1, WalletVerdict.ACCEPTED),
    }
    for name, (n, span, q, expected) in cases.items():
        sw, trips = wallet_series(swaps=n, span_days=span, quick_trips=q)
        verdict = screen_wallet(
            f"C-{name}", ANCHORS, trips, sw,
            entries=[WalletAnchorEntry("A4", ANCHORS["A4"].genesis_at + dt.timedelta(days=1))],
        ).verdict
        check(f"screen_wallet: {name} -> {expected.value}",
              verdict is expected, f"got {verdict.value}")

    print(f"  note  v2 floors in force: min_round_trips {SANDWICH_MIN_ROUND_TRIPS}, "
          f"min_share {SANDWICH_MIN_SHARE:.0%}, window {SANDWICH_WINDOW_DAYS}d, "
          f"min_window_swaps {SANDWICH_MIN_WINDOW_SWAPS}")


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

    # --- screen_wallet: all four rules over the same labelled sets --------
    print("\n[calibration — screen_wallet (four rules)]")

    def organic_swaps(i: int, count: int = 40) -> list[SwapObservation]:
        """`count` swaps over 30 days, 60% aggregator-routed and 40% direct.

        Deliberately below both swap thresholds: ~1.3 swaps/day against a 200/day
        ceiling, and 0.60 aggregator share against a 0.95 floor.
        """
        base = T0 + dt.timedelta(hours=i)
        step = dt.timedelta(days=30) / count
        return [
            SwapObservation(
                at=base + step * k,
                aggregator="jupiter" if k % 5 < 3 else None,
                pool=None if k % 5 < 3 else "pump-amm",
            )
            for k in range(count)
        ]

    def organic_positions(i: int) -> list[RoundTrip]:
        """Two multi-day holds — positions, not sandwiches."""
        start = T0 + dt.timedelta(days=i % 7)
        return [
            RoundTrip("A4", start, start + dt.timedelta(days=2)),
            RoundTrip("A7", start + dt.timedelta(days=3), start + dt.timedelta(days=6)),
        ]

    def screen_rates(mints: dict[str, list[str]], delay_fn, label: str,
                     *, swaps_fn=organic_swaps, positions_fn=organic_positions):
        rejected = accepted = not_computable = 0
        for wallet, mints_w in mints.items():
            i = int(wallet[1:])
            row = screen_wallet(
                wallet,
                ANCHORS,
                positions_fn(i),
                swaps_fn(i),
                entries=entries_for(wallet, mints_w, delay_fn) if mints_w else [],
                anchor_set_id="synthetic-2026-10-28",
            )
            if row.is_rejection():
                rejected += 1
            elif row.verdict is WalletVerdict.ACCEPTED:
                accepted += 1
            else:
                not_computable += 1
        total = rejected + accepted + not_computable
        print(
            f"  {label}: {total} wallets — rejected {rejected} "
            f"({rejected/total:.0%}), accepted {accepted}, "
            f"not_computable {not_computable}"
        )
        return {"rejected": rejected, "accepted": accepted,
                "not_computable": not_computable, "total": total}

    sb = screen_rates(BUNDLED_MINTS, burst_delay, "bundled")
    check("screen_wallet bundled rejection >= 90%",
          sb["rejected"] / sb["total"] >= 0.90,
          f"got {sb['rejected']/sb['total']:.0%}")

    sc = screen_rates(CLEAN_MINTS, late_delay, "clean  ")
    check("screen_wallet clean false-positive <= 10%",
          sc["rejected"] / sc["total"] <= 0.10,
          f"got {sc['rejected']/sc['total']:.0%}")
    check("screen_wallet clean set is accepted, not merely un-rejected",
          sc["accepted"] == sc["total"], f"got {sc['accepted']} of {sc['total']}")

    # --- each heuristic fires on its own signature -----------------------
    print("\n[the three heuristics]")

    no_swaps = screen_wallet("B00", ANCHORS, [], [],
                             entries=entries_for("B00", ["A1", "A2", "A3"], burst_delay))
    check("genesis burst outranks missing swap data",
          no_swaps.verdict is WalletVerdict.REJECT_GENESIS_BURST,
          f"got {no_swaps.verdict.value}")

    quick = [RoundTrip(m, T0, T0 + dt.timedelta(seconds=30))
             for m in ("A1", "A2", "A3")] * 4  # 12 quick trips, 3 mints
    row = screen_wallet("C00", ANCHORS, quick, organic_swaps(0),
                        entries=entries_for("C00", ["A4"], late_delay))
    check("sandwich fires on 12 quick round trips carrying 30% of a 40-swap window",
          row.verdict is WalletVerdict.REJECT_SANDWICH, f"got {row.verdict.value}")
    check("sandwich evidence carries the quick trips and their share",
          row.measured.get("quick_round_trips") == 12
          and row.measured.get("window_swaps") == 40
          and abs(row.measured.get("quick_share", 0) - 0.3) < 1e-6, f"got {row.measured}")
    check("sandwich needs different mints, not just quick trips",
          not sandwich("C00", [RoundTrip("A1", T0, T0 + dt.timedelta(seconds=30))] * 12,
                       organic_swaps(0)))

    fast = [SwapObservation(at=T0 + dt.timedelta(minutes=3 * k),
                            aggregator=None, pool="pump-amm") for k in range(500)]
    row = screen_wallet("C01", ANCHORS, organic_positions(1), fast,
                        entries=entries_for("C01", ["A4"], late_delay))
    check("throughput fires above 200 swaps/day",
          row.verdict is WalletVerdict.REJECT_THROUGHPUT, f"got {row.verdict.value}")
    check("throughput evidence carries the measured rate",
          isinstance(row.measured.get("swaps_per_day"), float)
          and row.measured["swaps_per_day"] > 200, f"got {row.measured}")

    routed_only = [SwapObservation(at=T0 + dt.timedelta(hours=k), aggregator="jupiter")
                   for k in range(40)]
    row = screen_wallet("C02", ANCHORS, organic_positions(2), routed_only,
                        entries=entries_for("C02", ["A4"], late_delay))
    check("router_only fires on 100% aggregator with no pool interaction",
          row.verdict is WalletVerdict.REJECT_ROUTER_ONLY, f"got {row.verdict.value}")
    check("one direct pool swap defeats router_only",
          not router_only("C02", routed_only[:39]
                          + [SwapObservation(at=T0, aggregator=None, pool="pump-amm")]))

    thin = [SwapObservation(at=T0, aggregator="jupiter"),
            SwapObservation(at=T0 + H, aggregator="jupiter")]
    row = screen_wallet("C03", ANCHORS, [], thin,
                        entries=entries_for("C03", ["A4"], late_delay))
    check("two swaps is not_computable, not accepted and not rejected",
          row.verdict is WalletVerdict.NOT_COMPUTABLE_INSUFFICIENT_SWAPS,
          f"got {row.verdict.value}")
    check("...and it is not counted as a rejection",
          not row.is_rejection() and not row.is_computable())

    both = screen_wallet("C04", ANCHORS, quick, routed_only,
                         entries=entries_for("C04", ["A4"], late_delay))
    check("rule order: sandwich is reported ahead of router_only",
          both.verdict is WalletVerdict.REJECT_SANDWICH, f"got {both.verdict.value}")

    # --- sandwich v2 ground truth (real-data regression) ------------------
    test_sandwich_v2_calibration_ground_truth()

    # --- summary ----------------------------------------------------------
    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("ALL QUALITY-FILTER CALIBRATION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
