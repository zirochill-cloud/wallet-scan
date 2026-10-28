"""Phase 1b metric bodies — hand-computed expected values (S5).

Every expectation below is arithmetic done by hand in the comments, not
"returned something". If a body changes, these numbers must not move without
the definition in docs/PHASE1A_RECOVERY.md §5 moving first.

Run: python tests/test_phase1b_metrics.py
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from solscope.metrics import (  # noqa: E402
    INSUFFICIENT_WINDOW,
    NO_OBSERVATIONS,
    BuyerFirstSeen,
    HourlyVolume,
    PriceObservation,
    buyer_rank_at_entry,
    max_subsequent_return,
    pct_entries_before_volume_spike,
)

TZ = dt.timezone.utc
T0 = dt.datetime(2026, 1, 1, 0, 0, tzinfo=TZ)

_failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {label}")
    else:
        _failures.append(label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def hours_after(h: float, price: float, source: str = "swap_derived") -> PriceObservation:
    return PriceObservation(
        at=T0 + dt.timedelta(hours=h),
        price_quote=Decimal(str(price)),
        source=source,
    )


# ---------------------------------------------------------------------------
# max_subsequent_return
# ---------------------------------------------------------------------------

def test_max_subsequent_return() -> None:
    print("\n[max_subsequent_return — windows, exclusions, peak day]")
    # Entry price 10 at T0; as_of = T0 + 45 days.
    #   -1h   9999  (BEFORE entry — must never count)
    #   +1h     12
    #  +10h     30  -> peak of the 1d and 7d windows, peak_day 0
    #  +12h   9999  provider_candle (must never count)
    #   +2d     20
    #  +10d     50  -> peak of the 30d window, peak_day 10
    #  +40d    100  (past the 30d window — must not count)
    #  +50d    500  (after as_of — must not count)
    obs = [
        hours_after(-1, 9999),
        hours_after(1, 12),
        hours_after(10, 30),
        hours_after(12, 9999, source="provider_candle"),
        hours_after(2 * 24, 20),
        hours_after(10 * 24, 50),
        hours_after(40 * 24, 100),
        hours_after(50 * 24, 500),
    ]
    r = max_subsequent_return(
        entry_price_quote=Decimal(10), entry_time=T0, observations=obs,
        as_of=T0 + dt.timedelta(days=45),
    )
    w = {x.window_days: x for x in r.windows}
    check("all three windows reported", set(w) == {1, 7, 30}, f"got {sorted(w)}")
    check("1d: peak 30/10 = 3.0, peak_day 0",
          w[1].status == "OK" and w[1].multiple == Decimal("3.0") and w[1].peak_day == 0,
          f"got {w[1]}")
    check("7d: provider candle and pre-entry price excluded, peak still 3.0",
          w[7].status == "OK" and w[7].multiple == Decimal("3.0") and w[7].peak_day == 0,
          f"got {w[7]}")
    check("30d: 40d and post-as_of points excluded, peak 50/10 = 5.0, peak_day 10",
          w[30].status == "OK" and w[30].multiple == Decimal("5.0") and w[30].peak_day == 10,
          f"got {w[30]}")
    check("headline window is 30d and usable",
          r.headline() is not None and r.headline().window_days == 30)

    print("\n[max_subsequent_return — insufficient window is never imputed]")
    r = max_subsequent_return(
        entry_price_quote=Decimal(10), entry_time=T0, observations=obs,
        as_of=T0 + dt.timedelta(days=5),   # 5 days old: only the 1d window fits
    )
    w = {x.window_days: x for x in r.windows}
    check("1d window still computed at as_of=5d",
          w[1].status == "OK" and w[1].multiple == Decimal("3.0"))
    check("7d reported INSUFFICIENT_WINDOW with no multiple",
          w[7].status == INSUFFICIENT_WINDOW and w[7].multiple is None, f"got {w[7]}")
    check("30d reported INSUFFICIENT_WINDOW, not shortened",
          w[30].status == INSUFFICIENT_WINDOW and w[30].multiple is None)
    check("headline() is None when the headline window is unusable",
          r.headline() is None)

    print("\n[max_subsequent_return — mature window with no observations]")
    r = max_subsequent_return(
        entry_price_quote=Decimal(10), entry_time=T0,
        observations=[hours_after(10 * 24, 50)],   # nothing inside 1d or 7d
        as_of=T0 + dt.timedelta(days=45),
    )
    w = {x.window_days: x for x in r.windows}
    check("1d reports NO_OBSERVATIONS (distinct from INSUFFICIENT_WINDOW)",
          w[1].status == NO_OBSERVATIONS and w[1].multiple is None, f"got {w[1]}")
    check("7d reports NO_OBSERVATIONS", w[7].status == NO_OBSERVATIONS)
    check("30d still computes 50/10 = 5.0", w[30].multiple == Decimal("5.0"))

    print("\n[max_subsequent_return — deterministic peak on ties]")
    r = max_subsequent_return(
        entry_price_quote=Decimal(10), entry_time=T0,
        observations=[hours_after(9 * 24, 40), hours_after(3 * 24, 40)],
        as_of=T0 + dt.timedelta(days=45),
    )
    w30 = r.windows[-1]
    check("equal peaks resolve to the EARLIER observation (peak_day 3, not 9)",
          w30.multiple == Decimal("4.0") and w30.peak_day == 3, f"got {w30}")

    print("\n[max_subsequent_return — contract violations]")
    for kwargs, label in [
        (dict(entry_price_quote=Decimal(0), entry_time=T0, observations=[],
              as_of=T0), "zero entry price raises ValueError"),
        (dict(entry_price_quote=Decimal(10), entry_time=T0, observations=[],
              as_of=T0, windows_days=(1, 7), headline_days=30),
         "headline outside windows_days raises ValueError"),
    ]:
        try:
            max_subsequent_return(**kwargs)
            check(label, False, "no exception")
        except ValueError:
            check(label, True)


# ---------------------------------------------------------------------------
# buyer_rank_at_entry
# ---------------------------------------------------------------------------

def test_buyer_rank() -> None:
    print("\n[buyer_rank_at_entry — ordinal, percentile, tie-break]")
    buyers = [
        BuyerFirstSeen("alice", T0 + dt.timedelta(seconds=10)),
        BuyerFirstSeen("bob", T0 + dt.timedelta(seconds=5)),
        BuyerFirstSeen("carol", T0 + dt.timedelta(seconds=30)),
        BuyerFirstSeen("dave", T0 + dt.timedelta(seconds=5)),   # ties with bob
        BuyerFirstSeen("alice", T0 + dt.timedelta(seconds=50)),  # duplicate row
    ]
    # Ordered by (time, address): bob(+5), dave(+5), alice(+10), carol(+30).
    # Duplicate alice collapses to her earliest (+10).
    r = buyer_rank_at_entry(
        wallet="dave", wallet_first_buy_at=T0 + dt.timedelta(seconds=5),
        buyers=buyers, buyer_set_complete=True,
    )
    check("dave ranks 2nd (tie broken by address, duplicate collapsed)",
          r.computed and r.rank == 2, f"got {r}")
    check("total unique buyers is 4, not 5 rows", r.total_unique_buyers == 4, f"got {r}")
    check("percentile = rank/total = 2/4 = 0.5 (the score's input)",
          r.percentile == Decimal("0.5"), f"got {r.percentile}")

    r = buyer_rank_at_entry(
        wallet="alice", wallet_first_buy_at=T0 + dt.timedelta(seconds=10),
        buyers=buyers, buyer_set_complete=True,
    )
    check("alice: rank 3/4 -> percentile 0.75",
          r.rank == 3 and r.percentile == Decimal("0.75"), f"got {r}")

    print("\n[buyer_rank_at_entry — refuses to rank a sample]")
    r = buyer_rank_at_entry(
        wallet="dave", wallet_first_buy_at=T0 + dt.timedelta(seconds=5),
        buyers=buyers, buyer_set_complete=False,
    )
    check("incomplete buyer set -> computed=False, no rank invented",
          not r.computed and r.rank is None and r.reason == "incomplete_buyer_set",
          f"got {r}")

    r = buyer_rank_at_entry(
        wallet="eve", wallet_first_buy_at=T0 + dt.timedelta(seconds=5),
        buyers=buyers, buyer_set_complete=True,
    )
    check("wallet absent from the set -> computed=False with that reason",
          not r.computed and r.reason == "wallet_not_in_buyer_set", f"got {r}")

    r = buyer_rank_at_entry(
        wallet="dave", wallet_first_buy_at=T0 + dt.timedelta(seconds=5),
        buyers=[], buyer_set_complete=True,
    )
    check("empty buyer set -> computed=False (no_buyers)",
          not r.computed and r.reason == "no_buyers", f"got {r}")


# ---------------------------------------------------------------------------
# pct_entries_before_volume_spike
# ---------------------------------------------------------------------------

def hourly(volumes: list[float]) -> list[HourlyVolume]:
    return [
        HourlyVolume(hour_start=T0 + dt.timedelta(hours=i), volume=Decimal(str(v)))
        for i, v in enumerate(volumes)
    ]


def test_pct_before_spike() -> None:
    print("\n[pct_entries_before_volume_spike — hand-computed spike]")
    # volumes by hour: [100, 10, 10, 10, 10, 50]
    #   i=0  opening hour, never a candidate
    #   i=1  baseline [100]          median 100 -> 10/100  = 0.10 < 3
    #   i=2  baseline [100,10]       median  55 -> 10/55   < 3
    #   i=3  baseline [100,10,10]    median  10 -> 10/10   = 1.0 < 3
    #   i=4  baseline [100,10,10,10] median  10 -> 10/10   = 1.0 < 3
    #   i=5  baseline [100,10,10,10,10] median 10 -> 50/10 = 5.0 >= 3  SPIKE
    entries = [
        T0 + dt.timedelta(hours=1),    # before  -> counted
        T0 + dt.timedelta(hours=4),    # before  -> counted
        T0 + dt.timedelta(hours=5),    # IN the spike hour -> not counted
        T0 + dt.timedelta(hours=6),    # after   -> not counted
    ]
    r = pct_entries_before_volume_spike(
        entry_times=entries, hourly_volume=hourly([100, 10, 10, 10, 10, 50]),
    )
    check("spike is hour 5", r.spike_at == T0 + dt.timedelta(hours=5),
          f"got {r.spike_at}")
    check("spike ratio = 50/10 = 5", r.spike_ratio == Decimal(5),
          f"got {r.spike_ratio}")
    check("2 of 4 entries before the spike hour",
          r.entries_total == 4 and r.entries_before_spike == 2, f"got {r}")
    check("fraction_before_spike = 0.5", r.fraction_before_spike == Decimal("0.5"),
          f"got {r.fraction_before_spike}")
    check("no_spike_observed is False", r.no_spike_observed is False)

    print("\n[pct_entries_before_volume_spike — launch hour is not the spike]")
    # [1000, 10, 10, 10]: hour 0 is the launch burst; it has no baseline and
    # is excluded from candidacy, so it is never reported as "the spike".
    r = pct_entries_before_volume_spike(
        entry_times=[T0 + dt.timedelta(minutes=30)],   # genesis buyer
        hourly_volume=hourly([1000, 10, 10, 10]),
    )
    check("quiet launch never spikes", r.no_spike_observed and r.spike_at is None,
          f"got {r}")

    # [1000, 10, 10, 30]: hour 3 vs median(1000,10,10) = 10 -> 3.0 >= 3 SPIKE.
    # The genesis buyer is before it -> fraction 1.0.
    r = pct_entries_before_volume_spike(
        entry_times=[T0 + dt.timedelta(minutes=30)],
        hourly_volume=hourly([1000, 10, 10, 30]),
    )
    check("genesis buyer is before the later spike (hour 3)",
          r.spike_at == T0 + dt.timedelta(hours=3)
          and r.fraction_before_spike == Decimal(1), f"got {r}")

    print("\n[pct_entries_before_volume_spike — no spike counts all as before]")
    r = pct_entries_before_volume_spike(
        entry_times=[T0 + dt.timedelta(hours=1), T0 + dt.timedelta(hours=3)],
        hourly_volume=hourly([10, 10, 10, 10]),
    )
    check("no spike -> no_spike_observed", r.no_spike_observed and r.spike_at is None)
    check("no spike -> all entries count as before-spike (fraction 1.0)",
          r.fraction_before_spike == Decimal(1), f"got {r.fraction_before_spike}")
    check("the reason is recorded, not silent",
          any("spike" in n for n in r.notes), f"notes={r.notes}")

    print("\n[pct_entries_before_volume_spike — edge cases]")
    r = pct_entries_before_volume_spike(
        entry_times=[], hourly_volume=hourly([100, 10, 10, 50]),
    )
    check("no entries -> fraction is None (not 0, not 1)",
          r.entries_total == 0 and r.fraction_before_spike is None, f"got {r}")

    r = pct_entries_before_volume_spike(
        entry_times=[T0 + dt.timedelta(hours=2)], hourly_volume=[],
    )
    check("no hourly volume -> no spike, all entries before it",
          r.no_spike_observed and r.fraction_before_spike == Decimal(1)
          and r.spike_at is None, f"got {r}")


def main() -> int:
    test_max_subsequent_return()
    test_buyer_rank()
    test_pct_before_spike()

    print()
    if _failures:
        print(f"{len(_failures)} FAILURE(S): {_failures}")
        return 1
    print("ALL METRIC CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
