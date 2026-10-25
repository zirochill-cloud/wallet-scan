# 02 — Recommended Architecture

## 1. Shape of the system

A single-machine, single-user batch-and-stream pipeline. Deliberately **not** microservices, not
containerised, not cloud-hosted. There is no user concurrency, no uptime requirement, and no reason
to pay the complexity tax of distribution.

```
                    ┌──────────────────────────────────────────────────┐
                    │  A. DISCOVERY            (free, keyless)          │
                    │  DexScreener new pairs  →  candidate tokens       │
                    │  GeckoTerminal new pools                          │
                    │  getProgramAccountsV2   →  launchpad mints (1 cr) │
                    └──────────────────┬───────────────────────────────┘
                                       │  token shortlist
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │  B. INGESTION       (budgeted, credit-ledgered)    │
                    │  Forward: LaserStream WSS standard methods        │
                    │  Backfill: getSignaturesForAddress + getTransaction│
                    │  → raw JSON archived, then decoded to events      │
                    └──────────────────┬───────────────────────────────┘
                                       │  decoded swaps + pool reserves
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │  C. DERIVATION                                    │
                    │  exec_price = |Δquote| / |Δbase| (from swap)      │
                    │  mcap  = price × circulating_supply               │
                    │  token age, liquidity, fee attribution            │
                    └──────────────────┬───────────────────────────────┘
                                       │  point-in-time facts
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │  D. RECONSTRUCTION                                │
                    │  FIFO lots → positions → realized / unrealized    │
                    │  wallet metrics → cohort benchmarks               │
                    └──────────────────┬───────────────────────────────┘
                                       │  metrics
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │  E. SCORING      (deterministic, frozen weights)  │
                    │  8 sub-scores + confidence → 0–100 + evidence     │
                    └──────────────────┬───────────────────────────────┘
                                       │  facts + evidence
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
        ┌───────────────────────────┐   ┌───────────────────────────────┐
        │ F. AI INTERPRETATION      │   │ G. MONITORING                 │
        │  Ollama + claim verify    │   │  watchlist, co-buy detection, │
        │  (optional, degradable)   │   │  Telegram/ntfy alerts         │
        └───────────────┬───────────┘   └───────────────┬───────────────┘
                        └──────────────┬────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────────────┐
                    │  H. VALIDATION HARNESS  (doc 09)                  │
                    │  point-in-time replay · holdout · baselines       │
                    │  shadow portfolio · rank IC                       │
                    └──────────────────────────────────────────────────┘
```

## 2. Component responsibilities and boundaries

Each component is a Python package with a **single narrow interface** and no knowledge of the layers
above it.

| # | Component | Owns | Must not |
|---|---|---|---|
| A | `discovery` | Enumerating candidate tokens and candidate wallets from free sources | Compute metrics or scores |
| B | `ingest` | Fetching, archiving raw responses, rate-limit and credit budgeting, checkpoints | Interpret or decode semantics |
| C | `derive` | Pool reserves → price, market cap, token age, liquidity, fees | Aggregate across wallets |
| D | `reconstruct` | FIFO lots, positions, P&L, wallet metrics, cohort benchmarks | Apply scores or weights |
| E | `score` | Sub-scores, weights, shrinkage, confidence, evidence records | Fetch data or call an LLM |
| F | `narrate` | Turning computed facts into prose, with verification | Compute any number it reports |
| G | `monitor` | Watchlists, trigger evaluation, notification dispatch | Score wallets |
| H | `validate` | Point-in-time replay, holdout evaluation, baselines | Feed back into scores |

The dependency graph is strictly one-directional. **`score` must never import `ingest`**, and
**`narrate` must never import anything that computes a metric** — it receives a fact bundle and
returns prose. Those two prohibitions are what keep the system auditable.

## 3. Storage architecture

Two tiers, for two genuinely different workloads.

**Tier 1 — PostgreSQL (system of record).** Wallets, tokens, positions, trades *(index/metadata)*,
scores, score history, evidence, alerts, watchlists, the credit ledger, and job checkpoints.
Mutable, relational, benefits from constraints and upserts. Small: millions of rows at most.

**Tier 2 — Parquet + DuckDB (analytical bulk).** Raw archived responses, the decoded swap/event
stream, derived price series, and cohort benchmark tables. Append-only, partitionable by date,
scanned in aggregate. Large: tens of millions of rows, comfortably.

Why not simplify to one engine:

- All-in-Postgres makes every backtest a scan over a row store and eventually forces manual
  partitioning to stay responsive.
- All-in-Parquet removes transactions and upserts, which the state layer genuinely needs —
  score history, watchlists and alert dedupe are all read-modify-write.
- All-in-SQLite is a defensible MVP simplification (same schema, one writer) and is the documented
  fallback if running a Postgres daemon is undesirable. The `schema/001_init.sql` is written for
  Postgres but is close enough to port.

**Storage layout:**

```
data/
  raw/            parquet, partitioned {source}/{date}/{hour}   — untouched provider responses
  events/         parquet, partitioned date=YYYY-MM-DD          — decoded swaps, transfers, mints
  prices/         parquet, partitioned date=YYYY-MM-DD          — derived point-in-time prices
  bench/          parquet                                       — cohort benchmark tables
  db/             Postgres data dir (or scope.sqlite)
```

**Invariant: `raw/` is append-only and never rewritten.** Every derived value must be recomputable
from `raw/`. This is the mitigation for provider churn (doc 01 §1.5) — when a free tier disappears,
what was already fetched survives, and re-derivation costs CPU instead of credits.

## 4. Discovery strategy

The requirement is to find valuable wallets rather than accept a manual list. Three complementary
sources, cheapest first.

**4.1 Token-first discovery (primary).**

1. Poll DexScreener's keyless endpoints for new Solana pairs (`/token-profiles/latest`, pair search)
   and GeckoTerminal for new pools. Cheap, keyless, no credits.
2. Filter to tokens that show *traction* — a minimum liquidity, a minimum trade count, and activity
   sustained over more than one hour. This removes the overwhelming majority of instant rugs before
   anything expensive is fetched.
3. For the surviving shortlist, pull the pool's early transactions: `getSignaturesForAddress` on the
   pool address (1 credit), then `getTransaction` on the first N signatures (1 credit each). This
   yields **the actual early buyers**, including the ones who bought and lost — which is the whole
   point (see §4.4).
4. The early-buyer set across many shortlisted tokens becomes the **candidate wallet pool**.

**4.2 Wallet-graph expansion (secondary).** Starting from a seed wallet, expand by:
common first funder, co-purchase in one transaction, same-slot coordinated buys, and repeated
co-occurrence across independent launches. Every expanded wallet inherits a **cluster id** and is
down-weighted as evidence — the requirement is explicit that wallets under common control must not
count as independent signals.

**4.3 Launchpad enumeration (targeted).** `getProgramAccountsV2` is priced at **1 credit**
(unpaginated `getProgramAccounts` is 10), making program-account enumeration strikingly cheap. This
is the efficient route to enumerate launchpad mints and bonding-curve accounts. Note the constraint
that `getTransfersByAddress` is unavailable on the free plan, so transfer-level analysis must be
assembled from `getSignaturesForAddress` + `getTransaction` instead.

**4.4 The critical methodological rule — selection on the dependent variable.**

Discovery cannot avoid knowing which tokens did well; that is how a candidate set is formed. This
creates a serious bias: **a wallet found through its winners will look skilled if you only ever
examine the tokens you found it through.**

The rule that neutralises it:

> **Discovery uses outcomes to *select* wallets. Evaluation uses the wallet's *complete* trade
> history within the collection window to *score* them — including every token they bought that
> went nowhere.**

Concretely: once a wallet is a candidate, the system fetches its own full signature history and
reconstructs *all* of its positions in the window. Its score is computed over that full set. A
wallet that bought 40 tokens and had 3 winners scores accordingly. This is implemented as a hard
rule in `reconstruct`, not as a convention, and doc 09 tests it explicitly.

**4.5 Distinguishing "early" from "skilled".** Two separate filters, applied in this order:

1. **Early filter** — did the wallet enter before the token's attention inflection?
2. **Quality filter** — was the token *worth* entering, judged by forward outcomes, and did the
   wallet's entry survive cost accounting?

A wallet passing only the first is a fast buyer (possibly a bot, possibly lucky). A wallet passing
both, repeatedly and across independent tokens, is the thing this tool exists to find. The scoring
model keeps these separate as sub-scores 1 and 5 (doc 05) so the distinction is visible rather than
blended.

## 5. Price and market-cap derivation

The single most important technical component. Design:

**Inputs per trade:** decoded swap instruction (amounts in and out, in raw integer units), the pool's
pre- and post-trade reserve state, the mints' decimals, and the slot/block time.

**Derived per trade:**

| Field | Derivation | Purpose |
|---|---|---|
| `exec_price` | `\|Δquote\| / \|Δbase\|` from the swap's own balance deltas | What the wallet actually paid, slippage included |
| `pool_price_pre` | Decoded pool state before the swap *(requires real vault ID — see below)* | Reference price at entry |
| `pool_price_post` | Decoded pool state after the swap | Reference price at exit/after |
| `slippage_bps` | `(exec_price − pool_price_pre) / pool_price_pre` | Realistic cost; also a self-check on decoding |
| `liquidity_usd` | `quote_reserve × 2 × quote_usd_rate` (constant-product) | Whether the position was exit-able at size |
| `mcap_usd` | `pool_price_post × circulating_supply × quote_usd_rate` | Position within the token's valuation range |

> **Verified constraint (doc 10).** `exec_price` from balance deltas is the primary method and was
> reconciled against live mainnet (37 swaps, mean 2.0–4.2%). **Do not** derive `pool_price_*` by
> inferring the vault as "the account holding the largest balance of that mint" — measured off by
> 4–5 orders of magnitude, because the largest holder is usually a bonding curve or virtual-reserve
> account. Reserve-derived prices require decoding the pool's real state (Anchor IDL or the
> program's account layout). `exec_price` needs no vault identification at all.
| `token_age_s` | `block_time − pool_created_at` | The core early-entry variable |

**Cross-pool aggregation.** A token may trade in several pools simultaneously (bonding curve plus a
migrated AMM, or multiple AMMs). Market cap must be computed from **the deepest pool**, or by a
liquidity-weighted aggregate, and that choice must be recorded with the price. Computing mcap from a
shallow side pool is a common and silent source of wildly wrong valuations.

**Supply tracking.** `circulating_supply` is stored per token with its own timestamp and provenance.
For fixed-supply launchpad tokens it is a constant and the initial mint's supply (read once) is
sufficient; for anything with a mint authority still active, supply changes must be tracked as
events rather than assumed.

**Fees.** Total cost attributed per position must include: priority fee, Jito tip (if any), the
launchpad/platform fee (typically a separate transfer in the same transaction), and the AMM LP fee
(implied by the difference between the pool price before and after the swap). Omitting any of these
systematically overstates profitability, and launchpad fees on memecoins are not negligible.

**Correctness check (Phase 2 gate).** While a DexScreener snapshot is still fresh, compare
locally-derived price and market cap against it. A tolerance of a few percent (allowing for the time
gap and for different supply conventions) is the acceptance criterion. If this fails, the design
stops here and the early-entry capability is declared out of reach at this budget.

## 6. AI pipeline boundary

Full design in doc 06. The architectural rule:

**`narrate` receives an immutable `FactBundle` and returns prose. It cannot query the database.**

```
reconstruct ─┐
derive      ─┼─→ fact bundle (typed, versioned, hashes of inputs)
score       ─┘         │
                       ├─→ deterministic template renderer  ← always available, no LLM
                       └─→ LLM renderer → claim verifier → accept | repair | fall back
```

Consequences: the tool is fully functional with no model installed; the LLM can never introduce a
number that is not in the fact bundle; and swapping models is a config change. The claim verifier
extracts every numeric and named entity from generated prose and checks it against the fact bundle,
rejecting output that references anything absent. That check is what separates "grounded" from
"grounded in principle".

## 7. Provider churn resilience

Given that three providers removed free query access in the 2025–2026 window, provider
replaceability is an architectural requirement, not a nicety.

- Every external source implements a narrow protocol: `PriceSource`, `TradeSource`, `PoolSource`,
  `TokenMetaSource`, `StreamSource`.
- Every adapter records `source`, `fetched_at`, and the raw response payload reference on each row.
- A **credit ledger** table tracks spend per provider per job, with a hard monthly cap enforced in
  code. Exceeding the cap fails the job loudly rather than silently degrading the account.
- Prices and metrics carry a `provenance` enum (`swap_derived` > `pool_ohlcv` > `snapshot_api`) so
  that when a source disappears, affected metrics can be identified and re-derived rather than
  silently mixing qualities.

## 8. Proposed repository layout

```
solscope/
  config/
    scoring.yaml            # all weights, thresholds, parameters (doc 05)
    sources.yaml            # provider endpoints, rate limits, credit costs
    .env.example            # HELIUS_API_KEY, TELEGRAM_BOT_TOKEN, ...
  schema/
    001_init.sql            # Postgres DDL (doc 04)
  solscope/
    discovery/              # A. token + wallet candidates
    ingest/
      adapters/             # helius.py, dexscreener.py, geckoterminal.py
      ledger.py             # credit accounting + monthly cap
      checkpoints.py        # restart-safe high-water marks
    derive/                 # C. reserves → price, mcap, fees
    reconstruct/            # D. FIFO lots, positions, metrics, benchmarks
    score/                  # E. sub-scores, weights, shrinkage, confidence
    narrate/                # F. templates + LLM + claim verifier
    monitor/                # G. watchlist, triggers, notifiers
    validate/               # H. backtest, holdout, baselines, rank IC
    cli.py                  # single entry point: `solscope <command>`
  tests/
    fixtures/               # recorded RPC responses (offline test corpus)
  data/                     # parquet + db (gitignored)
```

Interface decision: **one CLI with subcommands, no web UI in the MVP.**
`solscope discover | ingest | derive | score | report | watch | validate`.

Rationale: a dashboard is the most expensive component to build and the least informative about
whether the methodology works. Markdown and HTML reports generated from the same fact bundles give
90% of the review value at 5% of the cost, and can be replaced later without touching the core.

## 9. Alternatives considered and rejected

| Decision | Alternative | Why rejected |
|---|---|---|
| Postgres + Parquet/DuckDB | Postgres only | Backtests become row-store scans; partitioning required prematurely |
| Postgres + Parquet/DuckDB | Parquet/DuckDB only | No transactions or upserts; score history and alert dedupe are read-modify-write |
| Postgres + Parquet/DuckDB | ClickHouse | Operationally heavier; own guidance wants 32 GB RAM+, warns about <16 GB |
| Python | Rust + Carbon | The hard problem is methodology iteration speed, not throughput; credits cap volume anyway |
| Funnel ingestion | Full historical index | Costs ~10M credits/month and needs a 512 GB node |
| Forward collection first | Historical backfill first | History is unaffordable; forward data is free, complete and unbiased |
| Local price derivation | Paid historical market data | Contradicts the zero-budget constraint; also worse for look-ahead safety |
| One CLI | Web dashboard | Highest build cost, lowest evidence value, premature |
| Template-first narration | LLM-first | Tool must work with no model; LLM output needs a deterministic baseline to be verified against |
