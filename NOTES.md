# NOTES — carried state, open blockers, and measured loss

Working notes, not design documentation. Everything here is either measured in this
session or explicitly marked **carried from the prior session's handoff and not
re-verified**.

> **Provenance:** no `NOTES.md` existed on disk at the start of this session
> (`ls NOTES.md` → not found, 2026-10-26). The prior session's copy did not survive
> the workspace move. This file is recreated so the references below have a real
> target. Sections marked *carried* restate what the handoff asserted; they could not
> be re-verified against any artifact in this repo, because no artifact for them
> survived either.

---

## § ANCHOR_SELECTION_CRITERIA

*Status: OPEN BLOCKER. Carried from the prior handoff; not re-verified in this repo.*

The retrospective seed-anchor approach for Phase 1b (candidate wallet discovery) is
blocked on two findings from the prior session:

1. **Non-independent anchors.** The anchors selected as "early discoverers" were not
   independent of one another — a cluster of wallets behaving as one entity counts as
   one data point, not many. Scoring them as separate successes would manufacture
   repeatable-looking performance from a single actor.
2. **The quality filter passed 44/45 bundled wallets.** A filter intended to reject
   coordinated or bundled launches admitted 44 of 45 wallets known to be bundled. As a
   quality filter it therefore has essentially no discriminating power on that sample,
   and any discovery built on it inherits that blindness.

**What unblocks it:** a re-specified anchor selection that (a) de-duplicates anchors by
behavioural cluster before they are counted as evidence, and (b) a quality filter whose
rejection rate on *known-negative* bundled sets is measured before the filter is trusted
on unknown sets. Neither is done.

**Independence:** this blocker belongs to **Phase 1b (discovery)**. It is independent of
the Phase 2 gate — Phase 2 passing says nothing about anchors, and fixing anchors does
not affect the reserve-derivation result. Phase 2 may proceed while this stays open; a
Phase 4 discovery build may not be *trusted* while it is open.

---

## § DATA_LOSS_2026_10

*Status: quantified 2026-10-26. Prior-session row counts are **carried from the
handoff**; no artifact in this repo records them, so they cannot be re-verified here.*

### What the lost database contained

The schema defines 41 tables (`schema/001_init.sql`). Only Phase 0–1 writers existed,
so almost all were empty. As reported in the handoff:

| Table | Rows (reported) | Note |
|---|---|---|
| `pools` | **10** | the shortlist the collector was subscribed to |
| `raw_payloads` | **112,134** rows / 112,134 distinct sha256 | deduplicated across kill/recovery overlap |
| `credit_ledger` | 86 `websocket_data` rows in run 7 alone | bytes 86,115,303 recorded vs 86,370,338 archived |
| `job_runs` / `job_checkpoints` | runs 1–7 | run 2 SIGKILLed, run 7 killed by a DB outage |
| `transactions` | **0** | Phase 2 writes these; Phase 2 was not run until after the loss |
| `swaps` | **0** | ditto |
| `wallets` | **0** | ditto |
| all scoring/metrics/alerts tables | **0** | Phases 5–10 never ran |

Raw archive on disk (not in the DB): **165.8 MB**, growing at **18.98 MB/h** →
projected **455.6 MB/day**, plus 53 gap-recovery archives.

**Which wallets / mints / window:** *none and none.* Zero wallets and zero swaps were
ever persisted, so no analysed wallet, score, or position was lost. The 10 pool
addresses lived only in the lost database and are not recorded in this repo — the
surviving mainnet fixtures (`tests/fixtures/`) contain two of them incidentally
(`GKULyZJr9E…`, plus mints `48YEeDMG6u…`, `9rjHzQPpVL…`, `CsovxRtEM6…`). Observation
window: first collector run **2026-10-15** through at least **2026-10-25**, with
interruptions between runs.

Month-to-date spend when it was lost (also carried): helius **2,399**/1,000,000,
dexscreener 225/300,000, geckoterminal 50/40,000.

### Cost to recover — a footnote, not a crisis

| What | How | Cost |
|---|---|---|
| Equivalent WSS volume going forward | `ceil(bytes/100KB) × 2` credits | 165.8 MB → **≈3,316 credits** (0.33% of monthly cap) |
| Backfill the same transaction history | `getSignaturesForAddress` ≈1 credit/1,000 txs + `getTransaction` 1 credit each | ≈112k payloads → **≈112,200 credits (≈11% of the monthly cap)** |
| Discovery replay | GeckoTerminal / DexScreener | **free** (keyless) |
| Exact raw wire bytes as delivered | cannot be replayed | unrecoverable — but the underlying signed transactions are re-fetchable, which gives equivalent provenance |

**Correction to an earlier claim.** The framing "the collection window cannot be bought
back" was **too strong**. The *raw frames* cannot be replayed, but every analytically
useful artefact — which transactions happened, their instructions and balance deltas —
is on chain and retrievable for roughly 11% of one month's credit budget. What actually
cannot be recovered is the *unexamined* history beyond what we choose to backfill, and
that is a budget decision rather than a wall.

**Verdict: footnote.** Nothing derived was lost, backfill is affordable, and the only
genuinely lost artefact (raw wire bytes) has a signed equivalent.
