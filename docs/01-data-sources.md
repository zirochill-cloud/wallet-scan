# 01 — Data Sources and Tooling Review

Everything here was checked against provider documentation. Where a limit could not be verified
from an official page, it is marked **unverified** rather than repeated from a secondary source.

A recurring warning: **"has a free account" is not the same as "is free for this use."** Free tiers
differ in three separate ways — an ongoing quota, a limited endpoint set, and a time-limited trial.
Those are not interchangeable, and two of the three are traps for a project that must keep running
indefinitely.

---

## 1. On-chain data providers

### 1.1 Helius — the primary on-chain source ✅ recommended

Verified from [helius.dev/docs/billing/plans](https://www.helius.dev/docs/billing/plans) and
[helius.dev/docs/billing/credits](https://www.helius.dev/docs/billing/credits).

| | Free | Developer |
|---|---|---|
| Price | $0/month | $49/month |
| Monthly credits | 1,000,000 | 10,000,000 |
| RPC rate limit | 10 req/s | 50 req/s |
| DAS API | 2 req/s | 10 req/s |
| Enhanced APIs | 2 req/s | 10 req/s |
| LaserStream WSS (standard Solana methods) | **Included** | Included |
| LaserStream WSS (Helius extensions) | — | Included |
| LaserStream gRPC mainnet | — | — (Devnet only) |
| Wallet API | Included | Included |

Why it is the pick:

- **Genuine archival history within the free quota.** Helius states its historical coverage runs from
  genesis, and `getSignaturesForAddress` + `getTransaction` cost **1 credit each**. This is the only
  documented free route to old wallet transaction history found in this review.
- **A free WebSocket stream.** `LaserStream WSS (standard Solana methods)` is included on Free at
  2 credits per 0.1 MB of uncompressed data, which makes forward collection affordable at $0.
- **Programmatic signup.** An "Agent" plan exists with identical limits, via CLI or MCP server.

Constraints and traps:

- `getTransfersByAddress` is **blocked on Free** — it returns an error. It appears in the credits
  documentation at 10 credits and looks like the obvious tool for wallet history. It is not
  available. Budget for this explicitly.
- Credit costs vary by **three orders of magnitude** between a raw `getTransaction` (1),
  Parsed Events (10), DAS (10) and Enhanced Transactions (100). Accidentally routing history
  through the wrong endpoint silently costs 100× the budget.
- The docs contain at least one internal inconsistency (Parsed Events is described as both 10
  credits/req on all plans and free on paid plans until a stated 2026 date). Verify against the
  live dashboard before pricing anything around a specific endpoint.
- Webhooks: free-plan webhook counts are not stated for current plans; the older **Free V3** plan
  listed **1 webhook**. Webhook events cost 1 credit, but *creating or editing* a webhook costs 100.
  Do not design around webhook fan-out. The WebSocket stream is the better primitive.
- Terms permit use for the customer's own business purposes including integration into their
  products, but restrict reselling or providing the service to third parties. A personal analytics
  tool is fine; becoming an API reseller is not.

### 1.2 Solana public RPC — insufficient alone

Free public endpoints (`api.mainnet-beta.solana.com`) exist and require no key. They are
**rate-limited aggressively, undocumented as to exact limits, and generally retain only recent
history** — they are not archival. Useful for the occasional `getAccountInfo` or `getSlot`; not
usable as the backbone of a backfill. Treat them as a fallback, never as a dependency.

### 1.3 Old Faithful — real free ledger archive, wrong scale for a laptop ⚠️ optional upgrade

[Triton's Old Faithful](https://docs.old-faithful.net/) packages Solana ledger history as
content-addressed CAR archives, with RPC/gRPC access to historical blocks. Its sourcing guide says
the **OF1 archive is free to use**, covering genesis through the epoch before the current one, with
a new epoch published roughly 10–20 hours after each epoch ends.

Reality check: the docs call the ledger "absolutely massive" without publishing a current total
size. Retrieval plus indexes plus local storage is not a laptop-scale commitment, and the project
repo is currently labelled RFC-stage. **Recommendation: do not build on this for the MVP.** Keep it
as the documented escape hatch if a specific, narrow historical slice later proves necessary.

### 1.4 Yellowstone gRPC / Geyser — for building your own index, later ⚠️

[Yellowstone gRPC](https://github.com/rpcpool/yellowstone-grpc) and the
[Agave Geyser interface](https://docs.anza.xyz/validator/geyser) are open source and MIT/Apache
licensed. They stream filtered transactions, blocks and account updates from a node you operate.

This is **live-streaming infrastructure, not a historical warehouse**. Account-update history
captured by a Geyser sink starts when the sink starts; it is not a backfill. Requires a node that, on
Anza's own published guidance, needs 16+ cores and 512 GB RAM. Not viable at this budget. Listed
because it is the correct answer to "how would a funded version of this work" and because the
architecture should not preclude it.

### 1.5 Providers evaluated and rejected

| Provider | Reason |
|---|---|
| **Dune** | Free tier is **view-only**. Solana tables and schema are excellent; query execution is not free. 14-day trial with 2,500 credits. |
| **Bitquery** | 7-day trial, 1,000 points. Personal plan is explicitly **personal-use only, no commercial use**. Solana archive history needs a paid add-on. |
| **Flipside** | Creator Studio shut down; business acquired by SonarX in May 2026. Do not plan around it. |
| **Google BigQuery** | 1 TB/month free query allowance is real, but the legacy Solana transactions table stopped updating 2025-03-31. Any replacement table's freshness must be verified per-table via its `Last modified` metadata and latest-block timestamp before use. |
| **Vybe Network** | 25,000 credits/month and 60 req/min, but historical data on the free plan is **48 hours only**. |
| **Moralis** | **No published free tier.** Lowest tier $149/month. |
| **Solscan Pro** | Free/Lite are **personal or research use only**; commercial application use requires Pro Level 2+. Also prohibits bulk mirroring. |
| **SolanaFM** | Free at 10 RPS per its rate-limit docs, but no stated monthly quota, no stated historical retention guarantee, and no stated commercial-use permission. Usable opportunistically; not a foundation. |
| **Birdeye Data API** | 30,000 CUs/month and 1 RPS appear in secondary sources, but the **official pricing and auth pages defer limits to the account/support**. Which endpoints are enabled on free is **unverified**. Do not architect around it. |

The pattern: **three separate providers gated or removed free query access between 2025 and 2026**
(Dune→view-only, Flipside→shutdown, GitHub Models→retired; Moralis's free tier is gone). This
should be treated as a trend, not a coincidence. See doc 02 §7 for the mitigation.

---

## 2. Market data: prices, liquidity, market cap

### 2.1 DexScreener — free, keyless, current-only ✅ recommended for discovery

Verified from [docs.dexscreener.com/api/reference](https://docs.dexscreener.com/api/reference).

- **No API key.** Rate limits are published per endpoint: **300 req/min** for pair, search, and
  token-pairs endpoints; **60 req/min** for token profiles, boosts, orders and ads.
- Returns current price, current liquidity in USD, `fdv`, `marketCap`, `pairCreatedAt`, rolling
  volume and buy/sell counts across windows, and social/website metadata.
- **There is no historical OHLCV endpoint.** Everything is a current snapshot.

This is the right tool for **discovery** — finding new pairs and filtering by current traction — and
categorically the wrong tool for historical analysis. Rate limits are generous but should be treated
as a budget: the 60/min discovery endpoints are the ones a polling loop will exhaust.

**Licence caution:** the API terms permit commercial and non-commercial use but **prohibit building a
product whose primary purpose competes with DexScreener**, and prohibit resale or passing API access
to third parties. A personal wallet-scoring tool is not a DexScreener competitor; a public token
screener would be. This is a real constraint on how the tool can eventually be distributed.

### 2.2 GeckoTerminal (CoinGecko keyless) — free pool OHLCV ✅ recommended as cross-check

- Keyless public access is documented at roughly **10 calls/min** in CoinGecko's current
  keyless-access docs. An older GeckoTerminal FAQ said 30/min; the conservative figure is used here.
- Provides pool-level **hourly OHLCV**, up to 1,000 records per request, plus pool trades and
  liquidity data.
- **Historical trade-range queries and holder endpoints are not in the free endpoint set** —
  CoinGecko marks those as plan-restricted. The keyed **Basic** plan documents six months of pool
  OHLCV; the keyless tier's exact retention is **unverified**.
- A CoinGecko keyed **Demo** plan exists (100 calls/min, 10,000 calls/month, one year of daily and
  hourly *coin* history) but it covers CoinGecko-listed assets, which most new Solana memecoins are
  not. **Not a substitute for pool-level data.**
- Terms permit charging for a product incorporating the API, but prohibit reselling or redistributing
  the API data as such. Demo plan is labelled "Attribution required".

### 2.3 Jupiter Price API — current price only ⚠️

[developers.jup.ag/docs/price](https://developers.jup.ag/docs/price). Free key: 1 req/s, 60/min.
Keyless: 0.5 req/s, 30/min. Up to 50 mints per request. Returns **current** price with a block ID and
24h change. **No historical series, no OHLCV, no market cap.** Useful for a cheap current-price
sanity check; its licence restricts selling, sharing or sublicensing API content.

### 2.4 The market-cap-at-trade-time problem — and its solution

Stated plainly: **no free API returns a token's market capitalisation at an arbitrary past
timestamp.** Providers offer either a current valuation snapshot or a historical price series, and a
candle close is not the price at a specific slot.

Three candidate resolutions were evaluated:

| Approach | Verdict |
|---|---|
| Snapshot APIs (DexScreener, Jupiter) | Only describe *now*. Useless for history. |
| Historical OHLCV (GeckoTerminal) | Hourly resolution, and produces a *price*, not a *market cap*. Interpolating a trade into a candle introduces error proportional to volatility — worst exactly when it matters, at launch. |
| **Derive from pool reserves in the decoded swap** | **Recommended.** `exec_price = \|Δquote\| / \|Δbase\|`, from the swap's own balance deltas. Exact, free, point-in-time. *(Reserve-ratio pricing requires real pool-state decoding — see doc 10.)* |

Why the third approach is correct, not merely cheaper:

- The swap transaction *contains* the pool state. For a constant-product AMM the post-trade reserves
  are recoverable from the instruction data, and the trade's own input/output amounts give the
  **effective execution price including slippage** — which is the price the wallet actually got.
- Market cap then follows as `price × circulating_supply`. For pump.fun-era memecoins supply is
  fixed and known (~1B, minus burns), so this is stable; supply changes are tracked per token
  rather than re-fetched.
- Because the price comes from the same transaction that the wallet is in, **it is structurally
  impossible to look ahead**. A candle-based approach, by contrast, invites the accidental use of a
  later close, which is precisely the look-ahead bias the requirements warn against.
- Record **two** prices per trade: `exec_price` (from the swap's balance deltas — what the wallet
  paid) and `pool_price_post` (from decoded pool state). Divergence between them *is* the slippage,
  which is needed for the realistic-return calculations in doc 05 §6. Note that inferring the pool
  vault by "largest balance of that mint" is **not** a valid way to get `pool_price_post` — it was
  measured off by 4–5 orders of magnitude (doc 10). Use decoded pool state.

Caveats to encode, not hide: multi-pool tokens (pump.fun bonding curve, then a post-migration AMM)
fragment liquidity, so reserves must be tracked per pool and aggregated when quoting a market cap;
and every amount must be stored as a raw integer with its mint's decimals rather than as a float.

**Validation plan for this assumption:** while DexScreener snapshots are still current, compare
locally-derived price/market cap against the snapshot for the same token within a few minutes. Doc
07 places this in Phase 2, deliberately early, because the whole early-entry objective rests on it.

---

## 3. Decoding and parsing libraries

These solve **decoding**, not data acquisition. All are free and open source.

| Library | Language | Purpose | Notes |
|---|---|---|---|
| [solana-py](https://github.com/michaelhly/solana-py) | Python | RPC + WebSocket client | Its README defers core types to Solders |
| [solders](https://github.com/kevinheavey/solders) | Python (Rust-backed) | Primitives, keys, message/tx serialisation, RPC types | Fast, typed; the foundation for Python-side decoding |
| [anchorpy](https://github.com/kevinheavey/anchorpy) | Python | Anchor IDL client generation | Useful only where an IDL exists |
| [Carbon](https://github.com/sevenlabs-hq/carbon) (SevenLabs) | Rust | Indexer/pipeline framework: Geyser/RPC sources, IDL-generated decoders | MIT. The serious option if the project later needs a real Rust indexer |
| [deBridge solana-tx-parser](https://github.com/debridge-finance/solana-tx-parser-public) | TypeScript | Instruction decoding, CPI flattening, log parsing | Good reference for handling Anchor IDLs |
| [Helius SDK](https://github.com/helius-labs/helius-sdk) | TS | Client SDK | MIT. Client only; provider limits still apply |

**What none of these do:** supply historical data. They decode what you fetch.

Practical note on pump.fun and Raydium: neither is Anchor-based with a stable public IDL for all
instructions. A hybrid approach is required — Anchor IDL where available, plus hand-written
instruction discriminators and log-keyword matching for the launchpad programs. This is real work
and is scheduled as such in doc 07.

---

## 4. Language and runtime choice

**Recommendation: Python for the analytics core, with a narrowly scoped Rust or TypeScript decoder
only if throughput demands it.**

Alternatives considered:

| Option | For | Against |
|---|---|---|
| **Python + solders** | Fastest path to working analytics; DuckDB, pandas/polars, scikit-learn, Ollama client all first-class; solders removes the historical performance objection for parsing | GIL limits stream throughput; fine for a collector that processes thousands, not millions, of tx/min |
| **Rust + Carbon** | The genuinely fast path; same stack as production indexers | Much slower to iterate; the hard problem here is methodology, not throughput |
| **TypeScript** | Good Solana tooling, DexScreener examples | Weaker analytical/data ecosystem; DuckDB and ML tooling are second-class |
| **Go** | Good concurrency | Weakest data-analytics ecosystem of the four |

The binding constraint is analytical iteration speed, not throughput — the credit budget caps
throughput at roughly 500k transactions/month, which is trivially within Python's capability. Choose
the language that makes it fastest to get the methodology wrong and then fix it.

---

## 5. Storage

| Option | Verdict |
|---|---|
| **PostgreSQL 16+ (local)** | **Recommended for system-of-record state.** Constraints, upserts, concurrent readers, native range partitioning, mature Python integration via psycopg 3 / SQLAlchemy. Free, self-hosted, no licence issue. |
| **Parquet + DuckDB** | **Recommended for the bulk event/analytics layer.** Columnar, compressed, partitionable by date; DuckDB queries Parquet directly and is embedded (no server). Excellent for OHLCV, cohort benchmarks and backtests. |
| **TimescaleDB** | Reasonable if continuous aggregates save real work — but note the licence: the **Community Edition is free to self-host under the Tiger Data License, not Apache 2.0**, and `first()/last()` and continuous aggregates live there. Apache 2 Edition has fewer features. |
| **ClickHouse** | Powerful, but its own OSS guidance recommends 32 GB RAM+ and notes machines under 16 GB may need tuning and ingest slowly. More operational weight than this project needs. |
| **SQLite** | Acceptable MVP substitute for Postgres if a daemon is unwanted (one writer at a time per database file). Not the right primary analytical engine. |

**The two-tier rationale.** These are genuinely different workloads. Wallet/position/score/watchlist
state is small, mutable, relational and benefits from constraints and upserts. Trades and prices are
large, append-only and scanned in aggregate. Forcing both into one engine either puts analytics into
a row store (slow) or puts mutable state into a columnar/file store (no transactions, painful
updates). The split is the honest answer, and both halves are free and run happily on 16 GB.

DuckDB caveats that must be respected: it is built for analytical queries, **not many small
concurrent transactions**, and a file-backed database is not generally shared for simultaneous
multi-process writes. Read-only connections are the supported multi-process pattern — which is
exactly how the Parquet layer is used here (one writer, many readers).

Practical footprint guidance: an 8 GB machine is a workable floor for Parquet+DuckDB at this scale
(DuckDB documents a 125 MB/thread minimum and recommends 1–4 GB/thread for best performance);
**16 GB RAM with an SSD is the comfortable target** for tens of millions of rows plus notebooks.
Size disk from a measured sample rather than row count — signatures and addresses dominate the
bytes, not numeric fields.

---

## 6. Local AI

| Option | Free status | Structured output | Verdict |
|---|---|---|---|
| **Ollama** | Local, no quota | Accepts a JSON Schema in `format`; docs describe it as more reliable than JSON mode | **Recommended primary** |
| **LM Studio** | Free for home and work (announced 2025-07-08) | JSON Schema support; docs warn models under ~7B often fail at structured output | Recommended alternative, better GUI |
| **llama.cpp** | MIT | Schema→grammar conversion, but only a **subset** of JSON Schema is supported and some features are silently skipped | Recommended when maximum control is needed |
| **Google AI Studio / Gemini** | Free tier exists but limits are **model- and project-specific** | JSON Schema (documented subset) | Optional cloud fallback only |
| **Groq** | Free tier exists; published table is labelled Developer-plan, **not** free-tier | Strict constrained decoding on select models | Optional cloud fallback |
| **OpenRouter free models** | 20 req/min and **50/day** (1,000/day after $10 lifetime credit) | Supported only on compatible endpoints | Optional, but 50/day is a hard ceiling |
| **Mistral free mode** | No credit card required; numeric limits not published | JSON mode + custom schema | Optional |
| **Cerebras** | **Not a free tier** — $5 credit expiring in 30 days | Strict JSON Schema | Rejected as a dependency |
| **GitHub Models** | **Retired 2026-07-30** | — | Rejected |

### The critical caveat for this project

**Constrained decoding guarantees shape, not truth.** A grammar can force a valid
`{"summary": "..."}` object with a plausible P&L figure that is simply wrong. Structured output and
schema validation are necessary but nowhere near sufficient.

The Gemini free tier's terms (effective 2026-03-23) also state that **unpaid-service prompts and
responses may be used to improve Google services and reviewed by humans**. For a tool whose entire
purpose is proprietary analysis, that is a genuine reason to prefer local inference even though
cloud quality is higher.

**Model sizing, as practical guidance rather than a guarantee:** a quantised **7–8B** model at
Q4_K_M is the realistic target on 16 GB (Ollama lists Qwen3 8B Q4_K_M at 5.2 GB, Apache 2.0), with
the remaining memory going to the OS, context and KV cache. On 8 GB, prefer 3–4B. These are starting
points for benchmarking, not requirements.

Note the licence split: the runtime (Ollama, llama.cpp, LM Studio) and the model weights carry
**separate** licences, and neither is a blanket commercial-use grant.

---

## 7. Scheduling and notification

| Concern | Recommendation |
|---|---|
| **Scheduling** | **cron** for periodic batch jobs, or **APScheduler** if scheduling should live inside the Python process. Both are effectively free of operational overhead. |
| **Orchestration** | **Dagster OSS** or **Prefect OSS** only when run history, retries, backfills and dependency graphs justify running a webserver and daemon. Airflow is excessive for one or two local scripts. |
| **Notifications** | **Telegram Bot API** (free; limit ~1 msg/s per chat, 20/min to groups) for personal alerts, or **ntfy** (public server: 60-request burst then 1 per 10 s; self-hostable). **Discord webhooks** for a private server — read `Retry-After` on 429 rather than assuming a fixed rate. |
| **Email** | Resend free tier is 100/day and 3,000/month; Gmail enforces a ~500/day send limit. Use for scheduled summaries and errors only. |

**Ingestion checkpoint pattern (restart-safe).** Persist a durable high-water mark — last processed
slot/time plus event identifier. On each run, re-fetch from slightly before the mark, deduplicate on
a stable event key, and advance the checkpoint **only after** successful ingestion. The deliberate
overlap handles late data and restarts.

**Store two clocks.** For point-in-time analysis, `event_time` (block/slot time) and `ingest_time`
(when the system learned it) must both be recorded. Filtering on `event_time` alone lets
late-arriving data leak into a historical "as-known-then" analysis — a subtle look-ahead bug that is
invisible without the second column.

---

## 8. Free tools worth reusing rather than rebuilding

| Project | Licence | Reuse |
|---|---|---|
| [DegenRadar](https://github.com/carlosmmora26/DegenRadar) | MIT | Transparent weighted scoring (PnL 35% / win rate 25% / copyability 25% / consistency 15%), sample-size penalties, discovery from DexScreener+pump.fun. Closest prior art to this design. |
| [tracced](https://github.com/mostronton-commits/tracced) | MIT | Range-based early-buyer analysis across **winners and losers**; funding-source and bundle tags. Its explicit rejection of a black-box score is a useful counterweight to DegenRadar. |
| [HoldTech](https://github.com/co-numina/holdtech) | MIT | Holder concentration (Gini, HHI), common-funder clusters, same-slot cohort detection. |
| [pumpfun-research](https://github.com/haccer/pumpfun-research) | MIT | Python pump.fun BUY/SELL classification, per-mint PnL, launchpad program-ID fingerprinting. |
| [MELT](https://github.com/git-disl/MELT) | **CC BY-NC 4.0** | Event-classification features and bundle linking (Jito bundle ID, co-purchase, funding flow) across 41k launches. **Non-commercial** — study, do not vendor. |
| [solana-copy-trader](https://github.com/tumf/solana-copy-trader) | MIT | Copy-execution architecture. Requires a trading key; **out of scope here by design**. |

Known detection heuristics worth adopting (all heuristic, none is proof):

- **Bundle / coordinated buy:** shared Jito bundle ID (a Jito bundle is up to five transactions in one
  slot); common first SOL funder; buys in the launch transaction or the next few; co-purchase in one
  transaction. **Exclude exchange funders** from funding heuristics — unrelated users withdraw through
  the same exchange, so it produces false positives.
- **Sniper bot:** buying within the first block(s) across many *unrelated* launches from many
  distinct deployers. A single block-0 buy is weak evidence; sustained early buying across launches
  is stronger.
- **Sybil / operator cluster:** build a wallet relationship graph over shared funder, co-purchases,
  same-slot co-occurrence, and synchronised exits.

Two research caveats worth carrying into any implementation: a published study on Pump.fun sniper
cohorts has had its **numerical results withdrawn pending corrected analysis** (its buyer records
included sells and missed buys), and clustering establishes *likely coordination*, never ownership.
