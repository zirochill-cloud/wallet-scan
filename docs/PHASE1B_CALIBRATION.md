# Phase 1b — real-data calibration of the quality filter

**Sample:** the 100 first-20 buyers already seeded across the five verified anchors
(20 per anchor, `anchor_wallet_verdicts`). Read-only: nothing reseeded, no thresholds
persisted from measurement.
**Harness:** `spikes/calibrate_filter.py`; raw output `spikes/filter_calibration.json`.

Two runs are recorded here. **v1** measured the rules as shipped and found the
`sandwich` rule too aggressive. **v2** redesigns `sandwich` from a lifetime count to a
concentration of recent activity and re-measures. The v1 record is kept because it is
the evidence that motivated the redesign — deleting it would leave the new thresholds
looking like guesses.

Neither run is *validation*: the sample has no human/bot labels, and it is drawn from
five new memecoins' early buyers — a population heavily selected for snipers.

## Method, and two corrections that matter

The database holds per-wallet metrics, not swap histories, so each wallet's history was
fetched over Helius RPC and reduced to swaps, round trips and a behavioural profile.

**1. Sampling direction.** The first pass sampled oldest-first and truncated at 1,500
transactions, which caught one wallet's quiet early months and reported it as having
1 swap while its ten most recent transactions were all swaps. Every number in both runs
comes from a **newest-first** sample; truncation drops the distant past.

**2. Round-trip reconstruction.** The v1 pass opened an episode only when none was open
and closed it on the next sell — so "buy, buy, buy, sell-all" collapsed into *one* round
trip. A wallet with 1,462 swaps in three hours scored 21 round trips. v2 derives round
trips with **FIFO lot matching**, the same semantics `positions.reconstruct` uses: each
buy lot matched against the sells that consume it. This raised the confirmed bot
`Gfds2Ur4UV` from 273 to **1,189** quick round trips and left `52NUhyYcpw` at 22 —
because that wallet genuinely has few matched round trips (see below).

Limitations: router detection is an allowlist (a swap through an unnamed aggregator is
counted as a transfer, which can only make `router_only` fire *less*); and 25 wallets hit
the 1,500-transaction cap, so their rates describe the recent window, not a lifetime.

---

# v2 — concentration-based sandwich (2026-11-09)

## The rule

`sandwich(wallet, positions, swaps)` fires when, inside the last `window_days` of the
wallet's **own** activity:

- at least `min_round_trips` = **3** round trips lasted at most `max_seconds` = **90**,
- in at least **two different mints**,
- and those round trips are at least `min_share` = **0.20** of the window's swaps,
- with at least `min_window_swaps` = **10** swaps in the window.

Below 10 window swaps the rule returns **False**, not "not computable": a sparse trader
is not a sandwich bot. Insufficient *lifetime* data remains
`NOT_COMPUTABLE_INSUFFICIENT_SWAPS` at the `screen_wallet` level. The two conditions are
different questions and keep different answers.

**One deliberate deviation from the specification:** the window ends at the wallet's own
latest swap, not wall-clock now. A screening run over a historical sample would otherwise
see an empty window for every wallet that stopped trading a week ago and pass it by
default. Anchoring to the last observed swap asks "was this wallet sandwiching while it
was trading", which is the question the rule is for. Callers can pass `as_of` for a fixed
cutoff.

`SCREEN_RULE_VERSION` moved to `quality_screen/v2`, and `SANDWICH_RULE_VERSION` is
`sandwich/v2`, because a v1 and a v2 verdict on the same wallet mean different things and
the stored rows carry only `rule_version`.

## Before and after, same 100 wallets

| Outcome | v1 | v2 |
|---|---|---|
| `wallet.reject_sandwich` | 95 | **67** |
| `wallet.reject_throughput` | 0 | **5** |
| `wallet.accepted` | 2 | **25** |
| `wallet.not_computable_insufficient_swaps` | 3 | 3 |
| **rejection rate** | **95%** | **72%** |

Per rule evaluated alone: `sandwich` 67, `throughput` 32, `router_only` 0,
`genesis_burst` 0 (40 computable genesis windows, but `N = 2` is unreachable because each
wallet sits in exactly one anchor's first-20 set).

**Overlap:** 27 wallets have ≥2 rules firing, down from 32 — and 5 wallets are now rejected
by throughput **as the primary rule**, wallets sandwich no longer catches. That is the
intended division of labour, not redundancy: sandwich answers "is flipping the strategy",
throughput answers "is the volume infrastructure".

## The seven wallets that had to be checked individually

| Wallet | v1 | v2 | Quick trips / window swaps | Share | Prediction |
|---|---|---|---|---|---|
| `8a6JShiMBw…` (retail, the v1 mismatch) | reject_sandwich | **accepted** | 27 / 198 | 13.6% | **holds** |
| `Gfds2Ur4UV…` (32,488 swaps/day) | reject_sandwich | **reject_sandwich** | 1,189 / 1,389 | 85.6% | **holds** |
| `AviBqnzP8m…` (55.8 swaps/day, 626 mints) | reject_sandwich | **reject_sandwich** | 621 / 1,339 | 46.4% | **holds** |
| `52NUhyYcpw…` (11,226 swaps/day) | reject_sandwich | **reject_throughput** | 22 / 1,462 | 1.5% | **does not hold** |
| `8GSDbEhMy3…` (53.8 swaps/day, bot-like) | reject_sandwich | **accepted** | 215 / 1,260 | 17.1% | **does not hold** |
| `FwA2rjEzvs…` (3 swaps / 51 days) | accepted | **accepted** | 0 / 3 | 0% | **holds** |
| `GRmwzXEBvD…` (8 swaps / 40 days) | accepted | **accepted** | 1 / 7 | 14.3% | **holds** |

Five of seven match the predictions. The two that do not are informative, and neither was
tuned away:

- **`52NUhyYcpw` is not a sandwich wallet.** It makes 1,462 swaps in 3.2 hours but only
  **23** of them are matched round trips — 22 of those under 90 s. It is buying or
  distributing in one direction, not flipping. Its quick-trip share is 1.5%, *below* the
  retail wallet's 13.6%; forcing sandwich to catch it would require a share floor around
  0.01, which would re-capture every retail wallet. It is still rejected — by
  `throughput`, at 11,226 swaps/day, which is the correct rule for what it does.
- **`8GSDbEhMy3` is a near-miss at 17.1%** against a 20% floor. Its profile is bot-like
  (median inter-swap gap 0 s, 10 active hours, 127 mints) and 215 of its 220 round trips
  are sub-90 s, but its swap volume dilutes the share. The gap to the retail wallet is
  13.6% → 17.1%: real but narrow, and a threshold placed between them would be fitted to
  two wallets. It is recorded as a **known false negative**, not as a reason to move the
  floor.

## Threshold sensitivity under v2

| Rule / threshold | Values | Wallets fired | Load-bearing? |
|---|---|---|---|
| `throughput_max_swaps_per_day` | 50 / 100 / **200** / 500 / 1000 | 58 / 51 / **32** / 14 / 9 | **yes** (spread 49) |
| `sandwich_min_share` | 0.10 / 0.15 / **0.20** / 0.30 / 0.40 | 80 / 79 / **67** / 53 / 39 | **yes** (spread 41) |
| `sandwich_window_days` | 7 / 14 / **30** / 60 / 90 | 58 / 60 / **67** / 68 / 72 | **yes** (spread 14) |
| `sandwich_max_seconds` | 30 / 60 / **90** / 180 | 51 / 65 / **67** / 77 | **yes** (spread 26) |
| `router_only_min_aggregator_fraction` | 0.80 / 0.90 / **0.95** / 0.99 | 0 / 0 / **0** / 0 | no (inert) |
| `genesis_burst` (N, Y) | N∈{2,3} × Y∈{30,60,120} | 0 everywhere | unmeasurable here |

The redesign flipped the most important column. Under v1, `sandwich_max_seconds` was
**completely inert** (95 wallets at every window from 30 s to 180 s) because a lifetime
count does not depend on the window. Under v2 every sandwich parameter moves the answer,
which is what a rule that measures concentration should look like — the thresholds now
mean something, which is exactly why they must not be fitted to 100 unlabelled wallets.

`router_only` remains inert for a real reason: this sample trades pool-direct (the most
active wallet: 1,389 of 1,389 swaps direct), so its "no direct pool interaction" conjunct
cannot hold.

---

# v1 — the run that motivated the redesign (2026-11-09)

| Outcome | Wallets |
|---|---|
| `wallet.reject_sandwich` | 95 |
| `wallet.not_computable_insufficient_swaps` | 3 |
| `wallet.accepted` | 2 |

Every rejection came from `sandwich`, and a 32,488-swaps/day bot and a 0.5-swaps/day
retail trader received the same verdict. The cause was the rule's shape, not its
threshold: v1 counted quick round trips over the entire observed history with no rate and
no recency, so 30 sub-90 s flips across 417 days condemned a two-year retail record. The
`sandwich_max_seconds` sweep was flat at 95 wallets for 30/60/90/180 s, confirming the
threshold was not where the decision lived.

Per-rule firing at v1: `sandwich` 95, `throughput` 32, `router_only` 0, `genesis_burst` 0.
Overlap: 32 wallets, all sandwich ∩ throughput — throughput added no decision, only
redundancy. That redundancy is what v2 removed.

---

# Conclusion

**Filter sandwich rule redesigned to concentration-based. Re-calibration: working on the
100-wallet sample, with one known false negative.** Rejection is 72% (inside a plausible
band for a bot-selected population), the human regression guard holds, both unambiguous
bots are still caught by `sandwich`, throughput now makes 5 decisions of its own, and no
human wallet is rejected. The residual failures are `8GSDbEhMy3` (bot-like, 17.1% share,
accepted) and `52NUhyYcpw` (rejected, but by throughput rather than sandwich, because it
is not sandwiching).

`SCREEN_DEFAULTS_VALIDATED` remains **False**. This is a sample-level observation on 100
unlabelled wallets, not population-level validation against external ground truth. The
next validation target is a labelled bundled/clean set from an external source, or a
larger anchor sample (S3 discovery or additional user-supplied anchors).

### What would change the conclusion

- External labels, which would turn "looks like a bot" into an error rate and let the
  13.6%–17.1% gap be located against real classes rather than two wallets.
- A sample containing wallets in ≥2 anchor sets, without which `genesis_burst` has no
  usable evidence.
- A rule that measures round-trip *rate per active hour* rather than share of swaps,
  which would separate `8GSDbEhMy3` without moving the retail wallet.
