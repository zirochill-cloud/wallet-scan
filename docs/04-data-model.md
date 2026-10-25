# 04 — Data Model

The executable schema is [`schema/001_init.sql`](../schema/001_init.sql): **41 tables, 484 columns,
106 indexes, 53 foreign keys, 22 explicit CHECK constraints, 5 enums**, organised into 12 sections.
It has been applied to a real PostgreSQL and exercised with inserts, not merely reviewed.

Counts above are measured against a live database, not estimated. Re-measured 2026-10-26 on
PostgreSQL 16.6: tables 41, columns 484, indexes 106, foreign keys 53 — all confirmed.

> **Correction.** This file previously claimed *306 check constraints*. That figure was
> `information_schema.table_constraints` counting **307** rows of `constraint_type='CHECK'`, of which
> **285 are PostgreSQL's `_not_null` pseudo-constraints** — one per NOT NULL column. The schema has
> **22** real CHECK constraints (`pg_constraint where contype='c'` also returns 22). The earlier number
> was a double-count of NOT NULL columns, not a miscount of checks.

## Verification status

```
$ python tests/test_schema_smoke.py
schema applied; 41 tables, 39908 bytes of DDL
  PASS  REGRESSION duplicate swap with NULL inner_index rejected
  PASS  REGRESSION duplicate transfer with NULL inner_index rejected
  PASS  REGRESSION second OPEN position for same wallet+token rejected
  PASS  a CLOSED position may coexist with an open one
  PASS  a new position may open after the previous one closed
  PASS  swap side must be buy|sell
  PASS  unknown inventory_origin rejected
  PASS  unknown owning_subscore rejected
  PASS  FK rejects swap for unknown wallet
  PASS  invalid subscription_kind rejected
ALL SCHEMA CHECKS PASSED

Tested on PostgreSQL 16.2 (pgserver). Schema requires PostgreSQL 15+ — see below.
```

### Two real bugs this test caught

Both were invisible on inspection and would have corrupted production data silently.

**1. Uniqueness silently disabled by NULLs.** The first draft deduplicated decoded swaps with
`UNIQUE (signature, instruction_index, inner_index)`. But `inner_index` is **NULL for every
top-level instruction** — which is most of them — and Postgres treats NULLs as distinct in a unique
constraint. The constraint therefore admitted unlimited duplicates of the most common row type.
Confirmed by inserting the same swap twice: the statement succeeded.

Fix: `UNIQUE NULLS NOT DISTINCT (...)`, applied to `swaps`, `token_transfers` and
`wallet_funding_links`. All three have nullable components that are NULL in the normal case
(`inner_index`, and `first_funding_sig` when a funding path is observed without a known signature).

**2. Concurrent open positions permitted.** `UNIQUE (address, mint, opened_at)` allowed a wallet to
hold *two simultaneously open positions* in one token — because the rows differ by `opened_at`. That
would double-count inventory and corrupt every downstream P&L figure.

Fix: a partial unique index, which is the correct primitive:

```sql
CREATE UNIQUE INDEX positions_one_open_per_wallet_token
  ON positions (address, mint) WHERE NOT is_closed;
```

Any number of *closed* position episodes may accumulate per wallet+token; at most one may be open.
This also keeps P&L per episode clean, which matters because a wallet that exits and re-enters has
two distinct holding periods with different entry conditions.

The lesson generalises: **a schema that parses is not a schema that works.** Both defects survived a
careful read and died on the first insert.

## Design invariants

These are the rules that make point-in-time analysis possible and prevent the accounting errors the
requirements specifically warn about.

### 1. Never store a token amount as a float

Every amount is `NUMERIC(38,0)` raw integer units plus a `SMALLINT decimals` column beside it.
SOL has 9 decimals, USDC 6, most SPL tokens 6 or 9 — mixing them silently destroys P&L. Floats also
lose precision on large integer supplies. Prices are `NUMERIC(38,18)`; USD aggregates
`NUMERIC(38,2)`.

### 2. Two clocks on every event

`event_time` (block/slot time) and `ingest_time` (when this system learned it). A query filtered on
`event_time` alone can admit data that did not exist at the evaluation moment — a look-ahead bug that
is invisible without the second column. Filtering requires both.

A consequence worth knowing: rows with `NULL event_time` are **excluded** from point-in-time queries
by `event_time <= now()`, which is the desired default. Provider responses occasionally omit
`blockTime`; such rows must be backfilled from a neighbouring slot before they can participate in
scoring, and the pipeline should treat NULL as "not yet usable" rather than "now".

### 3. Transfers are not purchases

`token_transfers` is a separate table from `swaps`, and `positions` carries
`inventory_origin ∈ {swap_only, includes_transfer_in, includes_airdrop}` plus
`transfer_in_qty_raw`.

This is the single most common way naive wallet trackers manufacture fake geniuses: a wallet receives
tokens by transfer (or airdrop) at no cost, sells them, and a tool that only understands "tokens in,
SOL out" reports an infinite return. `lot_matches` only ever links a `buy_swap_id` to a
`sell_swap_id` — **there is no schema path by which a transfer can acquire a cost basis**, so the
error is structurally impossible rather than merely discouraged.

### 4. Prices carry provenance

Every derived price is tagged `swap_derived` / `pool_ohlcv` / `snapshot_api` / `provider_parsed` /
`manual`. `swap_derived` is the highest trust because it comes from the same transaction as the
trade. `pool_snapshots` is a separate table precisely because a snapshot is only meaningful at
`fetched_at` and must never be mistaken for history.

### 5. Scores are immutable and versioned

`score_config_versions` holds frozen weights and a `config_sha256`. `scores` rows are keyed by
`(score_run_id, address)` and are never updated in place; `score_runs.as_of` records the
point-in-time cutoff. Combined with `wallet_metrics.as_of`, this makes "what did the model say on
date X, using only data available then" answerable — which is the foundation of the validation plan
in doc 09.

### 6. Metric ownership is enforced by the database

`metric_definitions.owning_subscore` is constrained to the eight dimensions, and each metric belongs
to exactly one. This is how "avoid double-counting correlated indicators" stops being a promise and
becomes a constraint: if `win_rate` is owned by `repeatability`, no other sub-score can consume it.
See doc 05 §7 for the full ownership table.

### 7. Confidence is a stored output, not an afterthought

`scores` carries `overall_score`, `confidence_score`, and `confidence_adjusted_score` as three
separate columns, plus `gated_by` to record when a hard exclusion fired.
`sub_scores.confidence` and `sub_scores.shrinkage_applied` record *why* a value moved toward the
population mean. The requirement to distinguish confidence from performance is therefore structural.

### 8. Every score is explainable

`sub_scores.explanation` is `NOT NULL`. `score_evidence` links a score to the specific metrics,
positions, swaps, clusters and benchmarks behind it. `score_sensitivity` stores the counterfactual —
what value a metric would need, and what evidence would be required, to move the wallet to a
different band. Requirement E.10 ("monitoring triggers") and F ("what new evidence would change the
score") are answered by stored rows, not by prose.

## Table map

| Section | Tables | Notes |
|---|---|---|
| Reference | `chains` | `TEXT` PK, so additional chains need no enum migration |
| Tokens | `tokens`, `token_supply_events` | Supply as events, because mcap depends on it and supply can change |
| Pools | `pools`, `pool_reserve_snapshots`, `pool_snapshots` | Reserve snapshots feed point-in-time price; snapshots are current-state only |
| Wallets | `wallets`, `wallet_labels`, `wallet_clusters`, `wallet_cluster_members`, `wallet_funding_links` | Labels are separate and weak by construction |
| Raw archive | `raw_payloads` | Append-only; every derived value is recomputable from here |
| Events | `transactions`, `swaps`, `token_transfers` | The decoded layer |
| Positions | `positions`, `lot_matches`, `equity_snapshots` | FIFO; MTM equity so "never sells" cannot fake low drawdown |
| Metrics | `wallet_metrics`, `metric_definitions`, `cohort_benchmarks` | Benchmarks carry `available_at` for point-in-time safety |
| Scoring | `score_config_versions`, `score_runs`, `scores`, `sub_scores`, `score_evidence`, `score_sensitivity` | Immutable snapshots |
| Quality | `data_quality_flags` | `NULL` + a reason is always available |
| Monitoring | `watchlists`, `watchlist_members`, `alert_rules`, `alerts`, `alert_deliveries` | Alert dedupe on `suppression_key` |
| Operations | `credit_ledger`, `job_runs`, `job_checkpoints`, `ingestion_queue`, `stream_subscriptions` | Credit cap enforcement, restart safety |
| Validation | `validation_experiments`, `validation_results`, `alert_outcomes` | The falsifiability apparatus |

## Modelling decisions worth defending

**Why cluster membership is a table, not a boolean on `wallets`.** A wallet can belong to several
clusters formed by different methods (`common_funder`, `co_purchase`, `same_slot`, `jito_bundle`)
with different confidences, and clustering is re-run as new data arrives.
`wallet_clusters.superseded_by` supports that without deleting history. A boolean column would have
forced a single, permanent, unjustifiable judgement.

**Why `funder_is_exchange` exists.** Exchange and custodial wallets fund huge numbers of unrelated
users. Treating "same funder" as evidence of coordination without excluding them is the most common
false-positive generator in this kind of analysis. The flag makes the exclusion explicit and
queryable rather than a note in someone's head.

**Why `equity_snapshots` stores mark-to-market, not realized only.** A wallet that buys and never
sells has zero realized drawdown. Scoring that as excellent risk management is exactly the failure
the requirements warn about. `marked_value_usd` forces open positions into the drawdown calculation.

**Why `cohort_benchmarks.available_at` exists.** A benchmark computed over a full month is not
available at the start of that month. Without `available_at`, a score dated mid-month could
legitimately join a benchmark built from the whole month — a look-ahead leak. The column makes the
constraint explicit and testable.

**Why `alerts` defaults to `is_active = false` on rules.** Alerting on an unvalidated score
manufactures false confidence (doc 03 §2). Rules exist in the schema from the start so the shape is
right, but they ship disabled.

**Why `swaps` keeps `exec_price` and `pool_price` separately.** The gap between them *is* the
slippage, which is needed both for realistic-return calculations and as a decoder self-check: a
negative or absurd slippage means the reserve decoding is wrong.

## Known limitations of this model

Stated plainly, because the requirements ask for explicit treatment of what cannot be established:

- **`wallets` is an address registry, not an identity store.** Nothing in this schema can establish
  that an address belongs to a person. `wallet_clusters` records *evidence of coordination*, never
  ownership.
- **Completeness is per-wallet, not global.** `wallets.history_completeness` records what we know
  about our own coverage. For most wallets outside the collection window it will be `unknown`, and
  `wallet_metrics.metric_quality` must carry that through to every derived number.
- **`swaps` cannot capture every trade.** Program-derived swaps, aggregator routes and novel
  launchpads require hand-written decoders. `decoder_version` is recorded per row so that a decoder
  improvement can be identified and re-run, and `data_quality_flags` is where unrecognised
  activity is surfaced.
- **The schema does not prevent survivorship bias.** That is a candidate-selection concern handled
  in doc 02 §4.4 and tested in doc 09 — no constraint can catch it.
