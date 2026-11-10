# WATCH AND WAIT — accumulating anchors from the free live feed

Design note, written 2026-11-10. **No pipeline is implemented here**; this
records the only path that reaches the July–September 2026 anchor window without
paying for PRO, and the decision points that would make it worth building.

## The pattern

Anchor discovery needs tokens that launched 30–90 days ago. The free tier's
`/kol/first-touches` feed only reaches ~7 hours (measured: 45 pages, 900 events,
2026-11-10 12:22→19:09 UTC). Walking `/kol/feed?kol=` for 100 KOLs to cover 30
days costs ~19,800 calls, i.e. ~99 days of the 200/day quota. So the window
cannot be reached backwards. It can be reached forwards, by waiting.

Run `spikes/kol_anchor_discovery.py` once a day against the live feed and log
every candidate. After 30 days, the oldest candidates are exactly the
30-day-old launches the pipeline wants. Re-verify each: did it pump ≥5×?

## What exists, and what does not

| Piece | State |
|---|---|
| `spikes/kol_anchor_discovery.py` | Working. Read-only, FREE tier, ~50 calls/run |
| `spikes/verify_anchors.py` | Working. Checks a candidate's pump and buyers |
| `spikes/seed_anchors.py` | Working. Writes anchors → verdicts → metrics → aggregates |
| DB schema + `consumer` + `scores` | Working. Green harness, Path 3 gates at 1 |
| Daily cron | **Not built** — manual invocation today |
| `kol_candidates` queue table | **Not built** — candidates live in dated JSON files |
| Deferred verifier (age ≥ 30 days) | **Not built** |

## What would need building

1. A daily invocation (cron or a scheduled task) of the discovery probe writing
   `data/kol_candidates_YYYY-MM-DD.json` (gitignored, one file per day).
2. A `kol_candidates` table: `mint, symbol, kol_wallets, entry_mcap,
   discovered_at, verified_at, verdict`, keyed by mint so a token discovered on
   several days is one row.
3. A deferred verifier that selects candidates with `discovered_at < now() - 30
   days` and `verified_at IS NULL`, runs `verify_anchors.py`, and records the
   verdict. Everything downstream already exists.

## Cost and yield

- Discovery: **0 credits** — MadeOnSol free tier, ~50 of 200 calls/day.
- Verification: **~70 Helius credits per candidate**. 150 candidates/month ≈
  10,500 credits, ~1% of the 1M monthly cap.
- Yield: 5–10 candidates/day × 30 days = 150–300 tokens. If 10–20% pass the
  ≥5× test, that is **15–60 anchors** — enough for track-band ranking (Path B),
  where a wallet needs ≥3 distinct anchors.

## Timeline

First track-band output 30+ days after accumulation starts. Sooner only if
anchors are supplied directly or PRO unlocks `/kol/coordination/history`.

## Decision point

After two weeks of accumulation, sample 20 candidates and verify them:

- **≥5 of 20 verify** → viable; build the queue table and the deferred verifier.
- **<5 of 20 verify** → the filters are selecting the wrong population. Adjust
  `--min-kols`/`--max-mcap` or the KOL subset before accumulating further.

## Why not just lower the gates harder

Path 3 (`MIN_ANCHORS = 1`, `min_distinct_anchors = 1`) is already in place and
lets single-anchor wallets compute. That is the ceiling of what gate relaxation
buys: with one anchor, confidence on this dataset is 34.00 and the confidence
floor of 40 still excludes every wallet, because coverage/sample cannot rise
without more anchors or more per-wallet data. Relaxing `confidence_min` would
turn the gate into decoration — a validity rule that admits wallets it was
written to reject. More anchors is the honest fix; this note is how to get them.
