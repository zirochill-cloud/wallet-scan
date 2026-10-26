# INVENTORY — status of every document and artifact

Purpose: so the next session does not contradict this one. Each entry says what the file claims, how
much of it has been **verified against something real** (a running database, a live API, a test), and
what is known to be inconsistent.

Status key: **verified** (re-checked against a live system or test this session) · **carried**
(verified in an earlier session, evidence not in this repo) · **draft** (design intent, not yet
built or tested) · **stale** (out of date; superseded).

Verification date for everything marked *verified*: **2026-10-26**, PostgreSQL 16.6, Helius mainnet.

---

## `docs/`

| File | Status | What it claims | Notes |
|---|---|---|---|
| `00-feasibility.md` | carried | The project is feasible at ~$0/month; free tiers are retreating | Free-tier facts dated at writing (Oct 2026). Re-check anything load-bearing before relying on it. |
| `01-data-sources.md` | carried | Which sources are genuinely free, with limits and licence traps | Birdeye/GeckoTerminal retention explicitly marked *unverified* in-doc — correctly, still unverified. |
| `02-architecture.md` | verified, with a known gap | Components, two-tier storage, data flows | Naive reserve formula removed after doc 10. **Gap:** component `C derive` is described generically as "pool reserves → price …", but the implementation prices **PumpSwap only** (see inconsistencies #1). |
| `03-mvp-scope.md` | verified | Phase acceptance criteria; Phase 2 gate | Status line added with measured results. |
| `04-data-model.md` | verified — one figure corrected | The schema and why it is shaped that way | 41/484/106/53 re-measured and confirmed. **"306 check constraints" was wrong** — it counted 285 NOT NULL pseudo-checks + 22 real checks. Corrected to 22 (inconsistency #2). |
| `05-scoring-methodology.md` | verified as config, **unimplemented** | 8 sub-scores, weights, shrinkage, gates, bands | `config/scoring.yaml` validates (`test_scoring_config` green), but `solscope score` is still a stub. Design ≠ behaviour until Phase 6. |
| `06-ai-pipeline.md` | draft | Fact/LLM separation, claim verifier | Phase 8 not started; nothing to verify against. |
| `07-roadmap.md` | verified for Phases 0–2 | Ten phases with exit criteria | Status block added under Phase 2 recording that **criteria 2 and 3 were not evaluated as written** (inconsistency #3). Later phases remain planning. |
| `08-risks.md` | draft | Risk register and cost traps | Not revisited this session. |
| `09-validation.md` | draft | How the ranking is proven useful or not | Phase 9 cannot run until 6–12 weeks of data exist and DEX coverage is complete. |
| `10-phase2-spike.md` | carried — method superseded | First live price-derivation test, 3 runs, 37 swaps | Historically accurate, including its *correction* of the naive formula. Its method (one sampled trade vs `priceNative`) is superseded by doc 11 (reserves vs reserves). Cross-linked. |
| `11-phase2-gate.md` | **verified — current authority** | The go/no-go gate: criterion, method, results, scope, blocker | Three passes recorded: 0.39% (run A), **0.36% grade run**, 0.95% (three-number run). Carries the PumpSwap scope qualifier and the Phase 1b blocker. |
| `INVENTORY.md` | current | This file | — |

---

## `artifacts/`

| File | Status | Contents | Do not confuse with |
|---|---|---|---|
| `phase2_gate.json` | **verified — the grade run** | Fresh pull, shipped criterion, 2026-10-26: 34 inspected / **24 measured** / 5 excluded / 5 unpriced; mean **0.36%**, median 0.007%, p90 0.71%, max 5.42%, 96% inside 5%; liquidity 0.04%. Verdict **PASS**. | `phase2_gate_exec.json` — a *different* sample. |
| `phase2_gate_run.txt` | verified | The grade run's full stdout, committed so the pass can be audited against code that did not change during it. | — |
| `phase2_gate_exec.json` | verified | Three-number run (`--sigs 8`): 23 measured, mean 0.95%, 91% inside 5%, liquidity 0.09%, **PASS**. Carries the `exec_price_quote` field the grade run predates. | Its numbers differ from the grade run **because the sample differs**, not because results regressed. |
| `phase2_gate_exec_run.txt` | verified | Its stdout. | — |
| `triangulation.json` | verified | Two rounds 30s apart: chain reserves, provider reserves, `priceNative`, executed price. Chain ≡ provider to 4 s.f.; `priceNative` tracks execution. | — |

**Schema drift between the two gate artifacts (inconsistency #4):** `phase2_gate.json` was written
before `exec_price_quote` existed, so its rows lack that key. Both are correct for the code that
produced them. Re-running the grade command reproduces the newer schema.

---

## `spikes/` (cited by docs 10 and 11)

| File | Status | Contents |
|---|---|---|
| `phase2_spike.py` | carried | The original stdlib-only live test (doc 10). |
| `phase2_reconciliation.json` | carried | Its Run 3 output — documented in doc 10. |
| `phase2_spike_result.json`, `pumpswap_derived.json` | carried | Earlier spike outputs. |
| `triangulate.py` | **verified** | The two-round DexScreener triangulation; reproduces `artifacts/triangulation.json`. |

---

## `schema/` — all 41 tables with purpose and current row count

**Where the counts come from.** There is **no production database on this machine.** Three
databases exist under the local PostgreSQL 16.6 (`port 5433`): `postgres` (empty),
`solscope_smoke` and `solscope_ledger_live` — the last two are scratch databases created by the
test suites. Counts below are from `solscope_ledger_live`, which holds the complete 41-table
schema. `solscope_smoke` additionally holds **test fixture rows only** (`chains=1, pools=1,
positions=3, swaps=2, token_transfers=1, tokens=1, transactions=2, wallets=2`), all inserted
by `tests/test_schema_smoke.py`'s assertions. The migration runner adds a 42nd table,
`schema_migrations` (1 row: its own applied-migration record) — it is bookkeeping, not part of
the 41 in the DDL.

**Every production table is at 0 rows.** The only non-zero rows in `solscope_ledger_live` are
`credit_ledger=1` and `schema_migrations=1`, both written by `tests/test_ledger_live.py`.

| # | Table | Purpose (from doc 04) | Rows |
|---:|---|---|---:|
| 1 | `chains` | Chain registry; TEXT PK so new chains need no enum migration | 0 |
| 2 | `tokens` | Mint registry: symbol, decimals, launchpad, `created_at` (token age) | 0 |
| 3 | `token_supply_events` | Supply changes as events — market cap depends on supply | 0 |
| 4 | `pools` | Trading pools per DEX, with vault-layout metadata | 0 |
| 5 | `pool_reserve_snapshots` | Point-in-time reserve reads → historical price (the Phase 2 method) | 0 |
| 6 | `pool_snapshots` | Provider current-state snapshot; meaningful only at `fetched_at` | 0 |
| 7 | `wallets` | Address registry + `history_completeness`; not an identity store | 0 |
| 8 | `wallet_labels` | Third-party labels, weak evidence by construction | 0 |
| 9 | `wallet_clusters` | Behavioural clusters (`common_funder`, `co_purchase`, `jito_bundle`…) | 0 |
| 10 | `wallet_cluster_members` | Cluster membership rows; re-clusterable without deleting history | 0 |
| 11 | `wallet_funding_links` | Funding paths; `funder_is_exchange` excludes custodial false positives | 0 |
| 12 | `raw_payloads` | Append-only raw archive; every derived value recomputable from here | 0 |
| 13 | `transactions` | Decoded transactions (Phase 2 writer) | 0 |
| 14 | `swaps` | Decoded swaps; `exec_price` vs `pool_price` gap *is* the slippage | 0 |
| 15 | `token_transfers` | Non-swap transfers — structurally cannot acquire cost basis | 0 |
| 16 | `positions` | FIFO position episodes, `inventory_origin`, one open per wallet+token | 0 |
| 17 | `lot_matches` | buy↔sell lot matching → realized P&L | 0 |
| 18 | `equity_snapshots` | Mark-to-market equity, so never-selling cannot fake low drawdown | 0 |
| 19 | `metric_definitions` | Metric registry; `owning_subscore` enforces no double-counting | 0 |
| 20 | `wallet_metrics` | Computed metric values with `as_of` (point-in-time) | 0 |
| 21 | `cohort_benchmarks` | Benchmarks with `available_at` — no look-ahead join | 0 |
| 22 | `score_config_versions` | Frozen weights + `config_sha256` | 0 |
| 23 | `score_runs` | A scoring run, `as_of` cutoff | 0 |
| 24 | `scores` | Overall / confidence / confidence-adjusted score per wallet | 0 |
| 25 | `sub_scores` | The 8 sub-scores; `explanation` NOT NULL | 0 |
| 26 | `score_evidence` | Score → metric/position/swap/benchmark evidence links | 0 |
| 27 | `score_sensitivity` | Counterfactuals: what would move the wallet's band | 0 |
| 28 | `data_quality_flags` | Per-metric missingness + reason; NULL always explainable | 0 |
| 29 | `watchlists` | User watchlists (Phase 7 shape, ships unused) | 0 |
| 30 | `watchlist_members` | Wallet membership in watchlists | 0 |
| 31 | `alert_rules` | Rules; `is_active` defaults false until scores are validated | 0 |
| 32 | `alerts` | Fired alerts, deduped on `suppression_key` | 0 |
| 33 | `alert_deliveries` | Delivery attempts and outcomes | 0 |
| 34 | `credit_ledger` | Provider credit accounting, hard monthly cap | **1** (test row) |
| 35 | `job_runs` | Job execution records (restart/kill forensics) | 0 |
| 36 | `job_checkpoints` | Restart safety for interrupted jobs | 0 |
| 37 | `ingestion_queue` | Queued ingestion work | 0 |
| 38 | `stream_subscriptions` | WSS subscription state (collector) | 0 |
| 39 | `validation_experiments` | Backtest/out-of-sample experiment definitions (Phase 9) | 0 |
| 40 | `validation_results` | Their results | 0 |
| 41 | `alert_outcomes` | Whether alerts proved useful — false-positive tracking | 0 |

### Why 41 is more than Phases 0–2 should produce

The schema was written **once, complete, in the planning phase** — tables exist because a later
phase will write them, not because anything did. Phase 0–2 code touches only **16** of the 41:

- **Phase 0 (ops):** `credit_ledger`, `job_runs`, `job_checkpoints`, `ingestion_queue`,
  `stream_subscriptions` (5)
- **Phase 1 (forward collection, implemented in this repo):** `chains`, `tokens`, `token_supply_events`,
  `pools`, `pool_reserve_snapshots`, `pool_snapshots`, `raw_payloads` (7)
  
  > **Do not misread this as "Phase 1a was implemented here."** Per the audit in `NOTES.md`
  > § `PHASE1A_AUDIT`, the prior sessions' Phase 1a architecture is **absent from this repo in every
  > form** — its tables do not exist in the DDL, and `solscope/` is a rewrite. The label above is the
  > *current* schema's forward-collection writer set, which is the only Phase 1 that exists here.
- **Phase 2 (decode):** `transactions`, `swaps`, `token_transfers`, plus reads of `pools`/`tokens`
  (3 writers)
- **Shared:** `wallets` (registered by both 1b discovery and Phase 2 decode)

The other **25 are forward schema with no writer yet**: positions/equity (3, Phase 3), wallet
intel (4, Phase 1b/4), metrics/benchmarks (3, Phase 5), the six `score_*` tables (Phase 6),
`data_quality_flags` (Phases 3/5), monitoring (5, Phase 7), validation (3, Phase 9). That is the
expected shape, not scope creep — but it means "41 tables" never implied 41 tables of data.

### Phase 1a data verdict: **absent, not present-but-empty**

The closeout question named six tables. Result of checking schema **and** all three live
databases:

| Table | In `schema/001_init.sql`? | In any DB on this machine? | Rows |
|---|---|---|---|
| `swap_events` | **absent** | **absent** | n/a |
| `wallet_token_positions` | **absent** | **absent** | n/a |
| `swap_hops` | **absent** | **absent** | n/a |
| `pool_cache` | **absent** | **absent** | n/a |
| `pool_candidates` | **absent** (the *Python* `PoolCandidate` dataclass exists in `solscope/adapters/`) | **absent** | n/a |
| `mint_pools` | **absent** | **absent** | n/a |

A repo-wide search for those six identifiers returns **zero matches** — no code, no DDL, no doc
mentions them. Their functional equivalents in the current schema are `swaps`, `positions` and
`pools`; all three exist and are **empty in every database**.

**Verdict:** Phase 1a data did not merely get lost with the database — the table names it is
remembered by belong to a schema revision that is **also gone** (or to a naming convention that
never entered `001_init.sql`). Therefore:

- The **"Phase 1a complete" claim from prior sessions applies to code, not data.** The Phase 0/1
  code did survive as files (`solscope/collector.py`, `archive.py`, `checkpoint.py`,
  `decode.py` — 17 modules), but **zero Phase 1a rows survived in any form**.
- The ~1,510 `swap_events` and ~497 `wallet_token_positions` figures are **carried from the
  closeout message only** — no artifact in this repo records them, and the carried lost-DB table
  in `NOTES.md` § `DATA_LOSS_2026_10` says `swaps=0` at loss time. The two records cannot be
  reconciled from anything on disk. Both are marked carried.
- **Re-profiling cost** (per the carried estimate): ~300 credits, ~1 hour — into the *current*
  schema (`swaps`/`positions`), not the lost one. **Not started**: no phase currently reads those
  tables (per the closeout instruction, the collector stays off).

1. **Scope wording in `02-architecture.md`.** The `derive` component is described as if reserves →
   price applies to any DEX; the shipped code covers PumpSwap only. Doc 11 §8 and README carry the
   qualifier; doc 02 does not. *Open, cosmetic, but misleading to a reader who starts here.*
2. **`04-data-model.md` check-constraint count was wrong** (306 → 22, a NOT NULL double-count).
   *Fixed this session.*
3. **Two of doc 07's three Phase 2 exit criteria were not evaluated as written** — the same-tx
   slippage-sign check (its reference was measured 27% wrong) and the deepest-pool market-cap rule
   (market cap is Phase 5). *Annotated this session rather than left claiming a clean sweep.*
4. **The two gate artifacts have different row schemas** (see above). *Expected; documented.*
5. **`NOTES.md` did not exist** at session start — the prior session's copy did not survive the
   workspace move. It is recreated; its anchor and data-loss sections are marked *carried / not
   re-verified* because no artifact supporting them survived either.
6. **`docs/10` vs `docs/11` method.** Doc 10 reconciles a sampled trade against `priceNative`; doc 11
   reconciles reserves against reserves. Doc 10 is historical and cross-linked, not wrong.
7. **`solscope/` is a rewrite, and the Phase 1a architecture is absent.** The prior sessions'
   `swap_events` / `wallet_token_positions` / `swap_hops` / `pool_cache` / `pool_candidates` /
   `mint_pools` exist in **no** DDL, code, test, or live DB — and `wallet_quality_filter` appears
   nowhere at all. Therefore the earlier "Phase 1a complete" claim applies to **code that is not
   here**, and Phase 1b has no anchor pipeline or quality filter to build on. Full evidence:
   `NOTES.md` § `PHASE1A_AUDIT`. *Open — this supersedes any reading of this file that implies
   Phase 1a artifacts are merely empty.*

## What is *not* covered by anything in this repo yet

- Any wallet, score, position, metric, alert, or backtest — **zero rows ever existed** (`NOTES.md`
  § `DATA_LOSS_2026_10`).
- Any DEX other than PumpSwap at the reserve/pricing layer.
- Phases 3–10 code: `discover`, `score`, `report`, `watch`, `validate` are stubs returning exit 2.
