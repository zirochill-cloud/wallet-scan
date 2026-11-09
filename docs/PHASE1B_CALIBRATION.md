# Phase 1b — real-data calibration of the quality filter

**Date:** 2026-11-09
**Sample:** the 100 first-20 buyers already seeded across the five verified anchors
(20 per anchor, `anchor_wallet_verdicts`). Read-only run; nothing was reseeded.
**Rules under test:** `genesis_burst`, `sandwich`, `throughput`, `router_only` via
`screen_wallet`, at the shipped defaults (`SCREEN_DEFAULTS_VALIDATED = False`).
**Harness:** `spikes/calibrate_filter.py`; raw output in `spikes/filter_calibration.json`.

This is the first calibration on on-chain data. It is still **not** validation: the
sample is 100 wallets with no external human/bot labels, drawn from five new
memecoins' early buyers — a population heavily selected for snipers.

## Method, and the sampling correction that matters

The database holds per-wallet metrics, not swap histories (`swaps` and `positions`
are empty for these wallets). Each wallet's history was fetched over Helius RPC
(`getTransactionsForAddress`, full transactions) and reduced to `SwapObservation`s,
`RoundTrip`s derived from per-mint buy→sell episodes, and a behavioural profile.

**The first pass sampled oldest-first and was wrong.** Truncating at 1,500
transactions from the *beginning* caught one wallet's quiet early months and scored
it as 1 swap — while its ten most recent transactions were all swaps. It was
correctly rejected but incorrectly bucketed as `insufficient_swaps`. The sample was
re-fetched newest-first; every number below is from that second pass. Truncation now
drops the distant past, and each observation records `sample_order` and `truncated`.

Two further limitations, stated rather than buried:

- **Router detection is an allowlist** (Jupiter v6/v4, OKX router; nine pool
  programs). A token-moving transaction with no allowlisted venue is counted as a
  transfer, not a swap. That can only undercount swaps, which makes `router_only`
  fire *less* often than it should — never more.
- **25 wallets hit the 1,500-transaction cap**, so their rates describe the recent
  window, not a lifetime.

## Step 1 — rejection rate and verdict distribution

| Outcome | Wallets | Share |
|---|---|---|
| `wallet.reject_sandwich` | 95 | 95% |
| `wallet.not_computable_insufficient_swaps` | 3 | 3% |
| `wallet.accepted` | 2 | 2% |
| **total** | **100** | |

Each rule evaluated alone, default thresholds:

| Rule | Wallets it fires on | Share of sample |
|---|---|---|
| `genesis_burst` | 0 | 0% |
| `sandwich` | 95 | 95% |
| `throughput` | 32 | 32% |
| `router_only` | 0 | 0% |

`genesis_burst` had 40 computable genesis windows and fired on none. It **cannot**
fire on this sample: every wallet is listed under exactly one anchor, so `N = 2` is
unreachable. The zero is a property of the sample, not evidence about the rule.

## Rule overlap

| Pair | Wallets where both fire |
|---|---|
| sandwich + throughput | 32 |
| sandwich + router_only | 0 |
| throughput + router_only | 0 |

32 wallets have ≥2 heuristics firing, and **all of them are the sandwich ∩ throughput
pair** — that is, throughput only ever fires on wallets sandwich already rejects, so
on this sample throughput adds no decisions, only redundancy. `router_only` never
co-fires because it never fires at all.

## The `not_computable` subset — 3 wallets, deferred

| Wallet | Swaps | Reading |
|---|---|---|
| `5brv79eFZ2rGprXNvqgVJBkBptkkw8GJX1XydJyZLyAr` | 2 (both aggregator) | Genuinely thin: one mint, 50-day span |
| `Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt` | 0 | Does not sign the transactions it appears in — passive or spam-targeted address |
| `ardinRsN1mNYVeoJWTBsWeYeXvuR9UUDGMsCDKpb6AT` | 0 | Same |

No disposition is recorded for these; they are unscreened, not cleared.

## Step 2 — threshold sensitivity (each variant changed in isolation)

### `throughput_max_swaps_per_day` — LOAD-BEARING

| Threshold | Wallets it fires on |
|---|---|
| 50 | 58 |
| 100 | 51 |
| **200** (shipped) | **32** |
| 500 | 14 |
| 1000 | 9 |

Monotone and steep: 49 wallets separate the extremes. This threshold changes the
answer materially and is worth fitting.

### `sandwich_max_seconds` — INERT

| Window | Wallets it fires on |
|---|---|
| 30 s | 95 |
| 60 s | 95 |
| **90 s** (shipped) | **95** |
| 180 s | 95 |

**Completely flat.** The rule does not fire on "90-second flips"; the quick round
trips behind these verdicts are seconds long. Widening the window six-fold changes
nothing, and it cannot be narrowed enough to matter without falling below real
execution time. The threshold is not the problem — the rule's shape is (see below).

### `router_only_min_aggregator_fraction` — INERT (and structurally so)

| Threshold | Wallets it fires on |
|---|---|
| 0.80 | 0 |
| 0.90 | 0 |
| **0.95** (shipped) | **0** |
| 0.99 | 0 |

Zero at every setting. The rule also requires **zero** direct pool swaps, and this
sample trades almost entirely directly against pump.fun/PumpSwap/Raydium (e.g. 1,389
of 1,389 swaps direct for the most active wallet). `router_only` is inert here for a
real reason — early memecoin buyers are pool-direct — and would need a sample of
aggregator-only traders to say anything at all.

### `genesis_burst` (N, Y) — structurally inert

| N | Y = 30 s | Y = 60 s | Y = 120 s |
|---|---|---|---|
| 2 | 0 | 0 | 0 |
| 3 | 0 | 0 | 0 |

Every cell is 0 because no wallet is in two anchors' first-20 sets. This sweep is
uninformative by construction, not evidence of inertness.

## Step 3 — spot-check against activity profiles

Solscan's account pages are client-rendered and return only a shell to a text fetch,
so the substitution the user authorised was used: the same RPC transaction data, with
the ten most recent transactions examined per wallet. The "manual assessment" below is
therefore a profile judgement (rate, cadence, spread, amount variance), not a human
reviewer's.

Only **2 accepted wallets exist** in this sample, so the requested 5 rejected + 5
accepted cannot be met; all 2 accepted are included.

| Wallet | Verdict | Swaps | Swaps/day | Median gap | Distinct hours | Mints | Amount CV | Assessment |
|---|---|---|---|---|---|---|---|---|
| `Gfds2Ur4UV…73Pn7` | reject_sandwich | 1,389 | 32,488 | 1 s | 3 | 158 | 1.46 | **Bot** — 1,389 swaps in 62 minutes |
| `52NUhyYcpw…BJgAt` | reject_sandwich | 1,462 | 11,092 | 0 s | 4 | 31 | 5.69 | **Bot** — same-second bursts |
| `AviBqnzP8m…o869N` | reject_sandwich | 1,339 | 55.8 | 7 s | 18 | 626 | 0.76 | **Bot** — 626 mints, 7 s cadence |
| `8GSDbEhMy3…NEM32a` | reject_sandwich | 1,260 | 53.8 | 0 s | 10 | 127 | 1.47 | **Bot-like** — same-second fills |
| `8a6JShiMBw…Ry18k` | reject_sandwich | 207 | 0.5 | 597 s | 24 | 79 | 0.69 | **Discretionary** — 1.7-year history, all hours, 10-min cadence |
| `FwA2rjEzvs…vPdta` | accepted | 3 | 0.06 | 990 s | 24 | 3 | 1.18 | **Human** — 3 swaps in 51 days |
| `GRmwzXEBvD…pk9Tg` | accepted | 8 | 0.2 | 1,309 s | 24 | 3 | 1.48 | **Human** — 8 swaps in 40 days |

**Agreement: 7 of 8.** One mismatch, and it is the informative one:

- `8a6JShiMBw…` — 0.5 swaps/day over 417 days, active in all 24 hours, 10-minute
  median cadence, moderate amount variance: a discretionary trader. It is rejected
  because **30 of its 207 swaps** were round trips under 90 s across 79 mints. Two
  opportunistic scalps per month are enough to condemn a two-year retail history,
  because the rule counts them over the *entire* history with no rate and no
  recency.

The 32,488/day wallet and the 0.5/day wallet receive the same verdict from the same
rule. That is the finding.

## Conclusion

**Filter appears too aggressive on the 100-wallet sample.** 95% rejected, and the
rate is not evidence of 95% bots:

1. `sandwich` produces **every** rejection (95/95) and its window is inert across
   30–180 s, so no threshold choice rescues it.
2. It fires on wallets whose own profiles are plainly discretionary, because it
   counts quick round trips over the whole history without a rate, a recency window,
   or a minimum share.
3. `throughput` is redundant here: it fires only on wallets sandwich already rejects,
   so it changes no outcome — although it is the one **load-bearing** threshold and
   the one worth fitting.
4. `router_only` and `genesis_burst` cannot be measured on this sample at all, for
   structural reasons stated above.

Per instruction, **no threshold was changed and no rule was modified.**
`SCREEN_DEFAULTS_VALIDATED` remains `False`.

### What would change the verdict

- A `sandwich` shape that measures *share* of quick round trips rather than their
  count, or restricts them to a recency window — a logic change, deliberately not
  made here.
- A labelled sample (known-bundled / known-clean) to convert "looks like a bot" into
  an error rate.
- A sample including wallets in ≥2 anchor sets, without which `genesis_burst` has no
  usable evidence.
