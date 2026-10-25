# 07 — Development Roadmap

Ten phases, each ending in something runnable and testable. Nothing is "done" until its exit
criterion has been met on **real collected data**, not on fixtures.

Effort estimates assume one developer working part-time. They are planning aids, not commitments.

## The cold-start problem, and why Phase 1 starts on day one

Historical data cannot be bought at this budget (doc 00 §2). The scoring model therefore needs its own
collection window before it can say anything meaningful, and **that window cannot be retroactively
extended.** Every week the collector is not running is a week of data that is permanently unavailable.

This produces one ordering rule that overrides convenience:

> **The collector is built first, before any analytics, and runs continuously from the day it works.**

Realistic timeline: Phase 1 live by week 1. Meaningful scoring needs roughly **6–12 weeks of
collection**. Phase 9 validation therefore lands around month 3–4, and cannot land sooner no matter how
fast the code is written. This is a property of the data budget, not of effort.

## Phase overview

| Phase | Deliverable | Effort | Hard dependency |
|---|---|---|---|
| 0 | Skeleton, schema applied, credit ledger | 2–3 days | — |
| 1 | **Forward collector running continuously** | 1 week | 0 |
| 2 | **Price derivation + GO/NO-GO gate** — *passed 2026-10-25, see doc 11* | 1–2 weeks | 1 |
| 3 | Position reconstruction and P&L | 1–2 weeks | 2 |
| 4 | Candidate wallet discovery | 1 week | 2 |
| 5 | Metrics and cohort benchmarks | 1–2 weeks | 3, 4 |
| 6 | Scoring engine | 1 week | 5 |
| 7 | Reports (templates) | 1 week | 6 |
| 8 | AI narration + claim verifier | 1 week | 7 |
| 9 | **Validation — the real deliverable** | 2 weeks | 6 (+ 6–12 weeks of data) |
| 10 | Monitoring and alerts *(conditional)* | 1–2 weeks | 9 succeeding |

Phases 3 and 4 are independent and can be parallelised or reordered.

---

## Phase 0 — Skeleton (2–3 days)

Thin CLI (`solscope discover|ingest|derive|score|report|watch|validate`), config loading from
`config/`, Postgres schema applied, Parquet writers, **credit ledger with a hard monthly cap**, job
checkpoints, structured logging.

**Exit criteria**
- `solscope --help` works.
- `python tests/test_schema_smoke.py` passes against a fresh database.
- `python tests/test_scoring_config.py` passes.
- A dummy job writes a `credit_ledger` row, and exceeding the configured cap **fails the job loudly**
  instead of degrading the provider account.

## Phase 1 — Forward collector (1 week) ← *start on day one*

Poll DexScreener's keyless endpoints for new Solana pairs; filter to tokens with a minimum liquidity
and more than one hour of activity; open a Helius WebSocket subscription
(`LaserStream WSS standard methods` is included on the free plan) for the shortlist; archive every
delivered message verbatim to `data/raw/`.

**Exit criteria**
- One hour of collection yields non-empty raw archives for ≥10 new pools.
- A process kill mid-run recovers without gaps or duplicate events (checkpoint + overlap window).
- Recorded credit spend matches provider-reported usage within a small margin.
- Aggregate disk growth measured, so storage can be projected rather than guessed.

**Design constraints to respect now:** `getTransfersByAddress` **errors on the free plan** — the
adapter layer must never route through it. Credit spend per job is capped, because a runaway loop is
the most likely way to lose provider access.

## Phase 2 — Derivation and the project gate (1–2 weeks)

Decode swap instructions — Anchor IDL where available, hand-written instruction discriminators and
log-keyword matching for pump.fun and the Raydium launchpad programs. Compute
`exec_price = |Δquote| / |Δbase|` from the swap's own balance deltas, effective execution price
from the swap amounts, liquidity, market cap, token age, and the fee components (priority fee,
Jito tip, platform fee, implied LP fee). **Do not** derive price from `quote_reserve / base_reserve`
using a naive "largest balance = vault" assumption — doc 10 measured that approach off by 4–5
orders of magnitude; reserve-derived prices require decoding the pool's real state.

**Exit criteria — this is the go/no-go gate for the whole project**
- For **20 live pairs**, locally derived price agrees with a *contemporaneous* DexScreener snapshot
  within tolerance, and derived liquidity is within tolerance of the snapshot's liquidity.
- `exec_price` and `pool_price` differ by a slippage that is plausible and correctly signed; an
  implausible value means the reserve decoding is wrong and must be fixed here.
- Multi-pool tokens are handled: market cap is computed from the deepest pool or a liquidity-weighted
  aggregate, and the choice is recorded with the price.

**If this fails after honest effort, stop.** Every downstream capability depends on it, and without it
the early-entry objective is unreachable at this budget. The honest outcome is to publish the finding
and reassess scope, not to substitute candle-based prices and pretend the precision is unchanged.

## Phase 3 — Position reconstruction (1–2 weeks)

FIFO lot ledger; position episodes; realized/unrealized split with mark-to-market equity; fee
attribution; **transfer-in and airdrop exclusion**; per-metric data-quality flags.

**Exit criteria**
- Hand-verified against **3 wallets' transactions on an independent block explorer**: trade count,
  entry/exit prices and realized P&L reconcile exactly, or every discrepancy is explained by a
  recorded completeness flag.
- **A wallet that received tokens by transfer and dumped them is not shown as profitable.** This is
  the specific failure mode the schema is designed to make impossible; it must be tested explicitly.
- A wallet that exits and re-enters the same token produces two distinct position episodes with
  separate entry conditions.
- Metrics that cannot be computed are `null` **with a reason**, never `0`.

## Phase 4 — Candidate discovery (1 week)

Extract early buyers for shortlisted tokens (`getSignaturesForAddress` + `getTransaction`, 1 credit
each); build the candidate wallet pool; add wallet-graph flags for common funder (excluding exchange
funders), co-purchase, and same-slot/co-bundle activity.

**Exit criteria**
- Candidate pool ≥150 wallets, no duplicates.
- **The pool contains counterparts of losers, not only of winners.** A pool made entirely of
  successful wallets means the §4.4 rule (full-history evaluation, not just the tokens they were found
  through) is not implemented — this is the single most important check in this phase.
- Cluster flags set, with exchange-funder exclusion demonstrably working.

## Phase 5 — Metrics and benchmarks (1–2 weeks)

Every metric in the doc 05 ownership table; Kish effective sample size; cohort benchmark tables for
`launch_week` with `available_at`; shrinkage inputs.

**Exit criteria**
- Every metric recomputes **identically** from stored inputs on a second run (determinism test).
- A `cohort_benchmarks` row exists for every token in the candidate pool, or the fallback to
  `all_tokens` is recorded.
- No metric is consumed by more than one sub-score — asserted against
  `metric_definitions.owning_subscore`.

## Phase 6 — Scoring (1 week)

Eight sub-scores, confidence, weights from `config/scoring.yaml`, gates, bands, evidence rows,
sensitivity rows.

**Exit criteria**
- **`frozen: true` committed before any validation run.** Weight changes after seeing validation
  results invalidate the validation; this must be a version-control fact, not a convention.
- Changing a weight in the config visibly changes the ranking — proving the config is genuinely wired.
- Every sub-score has a non-empty `explanation` and at least one `score_evidence` row.
- A hand-checked wallet's score matches a manual computation from its published metrics.
- A wallet with a fabricated 100% win rate over 3 trades lands near the neutral prior, not near 100
  (the shrinkage regression test).

## Phase 7 — Reports (1 week)

`FactBundle` assembly; deterministic template renderer producing all ten required sections in Markdown
and HTML.

**Exit criteria**
- Report renders with **no LLM installed**.
- Every figure in the output appears in the fact bundle.
- Material `quality_flags` appear in §7 of the report.

## Phase 8 — AI narration (1 week)

Ollama adapter, JSON-Schema-constrained output, the six-step claim verifier, repair-then-fallback.

**Exit criteria**
- The full adversarial test suite in doc 06 §7 passes, including the fabricated-figure rejection and
  the prompt-injection-in-token-name case.
- With the model deliberately broken, a complete report is still produced via templates, and the
  degradation is recorded.
- A report is tagged with its renderer (`template` or `llm:<model>`) so a reader always knows which
  they are reading.

## Phase 9 — Validation (2 weeks) ← *the actual deliverable*

Point-in-time replay; discovery/validation split; random-wallet, all-wallet-median, buy-every-new-token
and market baselines; rank IC with bootstrap confidence intervals; precision@k; **shadow
(copyability-adjusted) portfolio**; survivorship and look-ahead audits; weight-stability check.

**Exit criteria**
- A written result stating whether the ranking beat the baselines **out-of-sample**, with confidence
  intervals — *including a negative result if that is what the data shows.*
- An automated look-ahead test that replays sampled positions and asserts no input has
  `event_time > as_of` and `ingest_time > as_of`.
- A weight-perturbation check: ±25% on each weight, reporting Kendall's τ of the resulting ranking.
  Fragile rankings indicate overfitting regardless of headline performance.

See doc 09 for the full design, including the pre-registered failure criteria.

## Phase 10 — Monitoring (conditional, 1–2 weeks)

Watchlist; trigger rules; co-buy detection with **cluster deduplication so ten wallets of one operator
count as one signal**; Telegram/ntfy dispatch with dedupe and cooldown; monitoring-trigger text in
reports; `alert_outcomes` tracking for false-positive measurement.

**Entry condition — deliberately strict.**
Either Phase 9 demonstrated out-of-sample value, **or** the decision to ship monitoring as a
descriptive tool is made explicitly and the limitation is stated at the top of every alert. A monitor
built on an unvalidated score is a machine for manufacturing false confidence, and doc 03 §4 explains
why this ordering matters more than any other in the plan.

---

## Critical path and sequencing rationale

```
Phase 1 (collector) ──────────────► runs continuously, feeds everything
    │
    └─► Phase 2 (derivation) ──► GATE ──► Phases 3/4/5 ──► Phase 6 ──► 7 ──► 8
                                              │
                                              └─► Phase 9 (needs 6–12 weeks of collected data)
                                                       │
                                                       └─► Phase 10 (only if 9 succeeds)
```

Three ordering choices are load-bearing:

1. **Collector before analytics** — the collection window cannot be bought back.
2. **Derivation before reconstruction** — building P&L on approximate prices means re-validating
   everything downstream when prices are fixed.
3. **Validation before monitoring** — the one most likely to be violated under enthusiasm, and the
   most costly to get wrong.

## What to defer even if it feels urgent

| Tempting | Why to wait |
|---|---|
| Web dashboard | Highest build cost, lowest evidence value. Markdown reports carry the same information. |
| Live alerts | Alerts on an unvalidated score create false confidence. |
| ML ranking model | With a few hundred wallets and one collection window, a learned ranker will overfit comfortably. Transparent weights first. |
| Multi-chain | Free to design for, expensive to build and test. One chain proving the methodology beats two proving nothing. |
| More data sources | Additional providers add licence surface and adapter code without addressing the binding constraint, which is methodology, not coverage. |
| Copy-trading | Requires custody of a private key; turns a modelling bug into a financial loss. Out of scope permanently. |

## Kill criteria

Stated in advance so they are decisions rather than reactions:

| Trigger | Action |
|---|---|
| Phase 2 derivation fails to reconcile with live snapshots | **Stop.** Early-entry scoring is unreachable at this budget. Reduce to descriptive analytics or abandon. |
| Credit spend cannot be kept within 1M/month for a useful funnel | Reduce funnel width; do not upgrade to a paid plan without re-deciding the budget. |
| Phase 9 shows rank IC statistically indistinguishable from zero across two independent windows | **The ranking has no demonstrated value.** Publish the negative result, reduce the tool to descriptive analytics, and stop building on the score. |
| Provider removes the free tier mid-project | Switch adapters; raw archives are retained precisely for this. If no free route to history remains, the project stops at whatever was already collected. |
| Fewer than ~6 weeks of collected data by the planned validation date | Delay validation. Validating on an insufficient window produces a confident wrong answer, which is worse than no answer. |
