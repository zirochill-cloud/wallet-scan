# 03 — MVP Scope

The MVP answers one question: **can this system identify wallets whose future early entries are
better than a random wallet's?** Everything not required to answer that is deferred, however
appealing it is to build.

## 1. In scope

| # | Capability | Why it is in the MVP |
|---|---|---|
| 1 | Forward trade collector on new Solana pools | The only component whose value decays if delayed |
| 2 | Point-in-time price / market cap derivation from pool reserves | Every early-entry metric depends on it; highest-risk assumption |
| 3 | Fee-attributed P&L with explicit realized/unrealized split | Profitability is meaningless without it |
| 4 | Full trade-history reconstruction per candidate wallet | Required to avoid selection on the dependent variable |
| 5 | Eight sub-scores + separate confidence score | The core deliverable |
| 6 | Evidence records and deterministic explanations | Requirement: explain why a wallet scored as it did |
| 7 | Cohort benchmarking | Makes wallets from different weeks comparable |
| 8 | Validation harness: point-in-time replay, holdout, baselines | Proves or disproves the premise |
| 9 | Markdown/HTML report generation | The interface a human actually reviews |
| 10 | Template-based narration | Report must work with no LLM installed |

## 2. Out of scope for the MVP — deliberately

Each deferral has a reason beyond "keep it small".

| Deferred | Reason |
|---|---|
| Web dashboard | Highest build cost, lowest evidence value. Markdown reports carry the same information. |
| Live alerting | **Alerts on unvalidated scores are actively harmful** — they create the impression of a working signal before one is demonstrated. Monitor by hand until doc 09 produces a result. |
| Wallet-graph clustering beyond funder/co-buy flags | Needs the graph infrastructure and a false-positive study before it can be trusted. Flags go in; clustering does not. |
| ML ranking model | With a few hundred wallets and one collection window, a learned ranker will comfortably overfit. Transparent weights first; ML only if the transparent model demonstrates signal and there is more data. |
| Multi-chain abstraction | Free to design for, expensive to build and test. One chain proving the methodology is worth more than two chains proving nothing. |
| Copy-trading / execution | Requires custody of a private key and turns an analytics bug into a financial loss. Entirely out of scope. |
| Deep historical backfill | Unaffordable (doc 00 §2). Front collection only, plus a narrow backfill for the initial candidate set. |
| Real-time (<1 s) detection | The collector is a monitoring tool, not a trading system. Copyability is *measured* (doc 09 §6), not assumed away. |
| Mobile or push notifications | Add after there is a validated signal to be notified about. |

## 3. Phased build with acceptance criteria

Each phase ends in something runnable and testable. Nothing is "done" until its criterion is met on
real data, not on a fixture.

### Phase 0 — Skeleton (2–3 days)
Thin CLI, config loading, Postgres schema applied, Parquet/raw writers, credit ledger, structured
logging, checkpoint store.
**Acceptance:** `solscope --help` works; a credit ledger row is written by a dummy job; schema applies
cleanly from `schema/001_init.sql`.

### Phase 1 — Forward collector (1 week)
Watch DexScreener new pairs; persist raw payloads; open a Helius WebSocket subscription for the
shortlisted pools; archive every delivered message verbatim.
**Acceptance:** one hour of collection produces non-empty raw archives for ≥10 new pools; restarting
mid-run resumes without gaps or duplicate events; recorded credit spend matches provider-reported
usage.

### Phase 2 — Derivation, and the go/no-go gate (1–2 weeks)
Decode swap instructions (Anchor IDL where available, hand-written discriminators for pump.fun /
Raydium launchpad otherwise). Compute reserves → price, liquidity, mcap, token age, fees.
**Acceptance (this is the project gate):** for 20 live pairs, locally derived price agrees with a
contemporaneous DexScreener snapshot within tolerance, and derived liquidity is within tolerance of
the snapshot's liquidity.
**If this fails after honest effort, stop and reassess.** No other phase depends on anything except
this, and without it the early-entry objective is unreachable at this budget.

**Status: passed 2026-10-25** — 26 live PumpSwap pools, mean price deviation 0.39% (median 0.015%),
all 26 inside 5% (max 3.81%), liquidity mean 0.05% (doc 11). Two definitional corrections came out of it:
reserves must be enumerated per pool owner (a transaction's `meta` omits untouched vaults), and the
reference is DexScreener's *reported reserves*, not `priceNative`, which is a trade-derived price
that sat 15–24% above its own reserves on tokens in a dump.

### Phase 3 — Position reconstruction (1–2 weeks)
FIFO lot ledger; position lifecycle; realized/unrealized split; fee attribution; transfer-in
exclusion; data-quality flags for incomplete histories.
**Acceptance:** hand-verified against 3 wallets' transactions on an independent block explorer —
trade count, entry/exit prices, and realized P&L reconcile to the last decimal, or every discrepancy
is explained by a recorded data-completeness flag. A wallet that received tokens by transfer is
**not** shown as profitable on a subsequent dump.

### Phase 4 — Candidate discovery (1 week)
Early-buyer extraction across the shortlist; token-quality filtering; candidate wallet pool; cluster
flags (common funder, co-purchase, same-slot).
**Acceptance:** candidate pool ≥150 wallets; no wallet appears in the pool twice; the pool contains
both winners' counterparts and losers (a pool containing only successful wallets means the §4.4 rule
is not implemented).

### Phase 5 — Metrics and benchmarks (1–2 weeks)
Wallet metrics per the doc 05 ownership table; cohort benchmark tables (same launch week, same
liquidity band); effective sample size; shrinkage inputs.
**Acceptance:** every metric recomputes identically from stored inputs on a second run; metrics that
cannot be computed are `null` with a reason, never `0`; cohort benchmark exists for every token in
the pool.

### Phase 6 — Scoring (1 week)
Eight sub-scores, confidence, weights loaded from `config/scoring.yaml`, evidence records, band
assignment.
**Acceptance:** weights are frozen in version control **before** any validation run; changing a
weight changes the ranking (config is genuinely wired, not decorative); every sub-score has a
non-empty evidence record; a hand-checked wallet's score matches a manual computation from its
metrics.

### Phase 7 — Reports (1 week)
Fact bundle → template renderer (Markdown and HTML). Every number traceable to a metric id.
**Acceptance:** the report contains all ten required sections; rendering works with no LLM present;
every figure in the output appears in the fact bundle.

### Phase 8 — AI narration (1 week)
Ollama adapter, JSON-schema-constrained output, claim verifier, repair-then-fallback path.
**Acceptance:** with the model deliberately induced to fail, the pipeline falls back to templates and
still produces a complete report; the claim verifier **rejects** a fabricated figure injected into a
test prompt; no output reaches the user unverified.

### Phase 9 — Validation (2 weeks)
Point-in-time replay, discovery/validation split, random-wallet and market baselines, rank IC,
precision@k, shadow (copyability-adjusted) portfolio.
**Acceptance:** a written result stating whether the ranking beat the baselines out-of-sample, with
confidence intervals, **including a negative result if that is what the data shows.**

### Phase 10+ — Monitoring (only after Phase 9 succeeds)
Watchlist, trigger rules, co-buy detection with cluster deduplication, notification dispatch,
monitoring-trigger text in reports.
**Entry condition:** the Phase 9 result shows out-of-sample value, OR the decision is made explicitly
and knowingly to ship monitoring as a descriptive tool with the limitation stated at the top.

## 4. Sequencing rationale

Three ordering choices are load-bearing and worth stating explicitly.

**Collector before analytics.** Every day without a collector is a day of data that cannot be bought
back at this budget. Analytics can be written at any time; the collection window cannot be
retroactively extended.

**Derivation before reconstruction.** Reconstruction depends on prices. Building it against
approximate prices and then fixing the price layer means re-validating everything downstream.

**Scoring before narration.** The LLM's job is to explain computed facts. Building narration first
produces a system that sounds analytical and cannot be verified.

**Validation before monitoring.** This is the one most likely to be violated under enthusiasm.
A monitor built on an unvalidated score is a machine for generating false confidence. The whole point
of doc 09 is to discover whether the score means anything; shipping alerts first inverts the order of
evidence.

## 5. Definition of done for the MVP

All of the following, on real collected data:

1. A ranked list of Solana wallets with 0–100 scores, separate confidence values, and per-dimension
   evidence.
2. Per-wallet reports containing all ten required sections, generated without an LLM.
3. A written validation result comparing high-ranked wallets against random and market baselines on
   a held-out period, with uncertainty stated.
4. An honest account of what could not be measured, per metric, with the reason.
5. Total infrastructure spend: **$0/month.**

Anything not on that list is post-MVP.
