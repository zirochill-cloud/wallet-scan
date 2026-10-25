# 00 — Feasibility Assessment

## Verdict

**Feasible, in reduced form.** The full vision as written is not achievable at zero budget. A
specific, narrower version of it is, on an ordinary laptop, with no subscriptions, and it can be
validated within roughly three months.

The binding constraint is not compute, not AI, and not storage. It is **historical on-chain data
access under a free tier**. Everything else in the design follows from working around that one
constraint.

| Capability requested | Feasible at $0? | Reality |
|---|---|---|
| Discover candidate wallets without manual input | **Yes** | Free keyless APIs + cheap program-account queries |
| Exact point-in-time price / market cap at entry | **Yes, if built** | Must be derived from AMM pool reserves locally; no free API provides it |
| Realized + unrealized P&L per wallet | **Yes** | Deterministic FIFO reconstruction, with fee attribution |
| Early-entry ability measured without look-ahead | **Yes** | Requires the pool-reserve price derivation above |
| Multi-dimensional 0–100 scoring with explanations | **Yes** | Pure computation; no LLM needed |
| AI-written wallet reports | **Yes** | Local model via Ollama, plus a template fallback |
| Live monitoring and alerts | **Yes** | Free WebSocket stream is included on Helius' free plan |
| Deep history for *many* wallets (2023–2025) | **No** | Free tier cannot pay for it; see §2 |
| Backtesting across multiple past market cycles | **No** | Same reason. One cycle is the realistic ceiling |
| Multi-chain support | **Later, not free** | Architecturally cheap, but each chain restarts the data problem |

## 1. What "feasible" is contingent on

Three conditions must all hold. If any fails, the project should be cut back rather than pushed on.

1. **Forward collection starts early.** Historical data cannot be bought at this budget, so the
   system has to accumulate its own. This introduces a **cold-start period of roughly 6–12 weeks**
   before the scoring model has enough of its own data to be meaningful. This is a schedule cost,
   not a money cost, and it is unavoidable. Starting the collector is therefore the first
   implementation task, ahead of everything else, even though nothing depends on it yet.

2. **Price is reconstructed locally from pool state.** The entire early-entry objective depends on
   knowing a token's price and market cap *at the moment of a wallet's entry*. No free API does
   this (doc 01 §4). If this reconstruction cannot be made to work reliably across pump.fun's
   bonding curve and post-migration AMMs, the core capability collapses and the project should be
   reduced to a descriptive wallet dashboard.

3. **The persistence hypothesis survives testing.** The whole exercise assumes wallets with
   demonstrated early-entry ability keep exhibiting it. That may simply be false — early-entry
   performance may be insider information, private-group access, or bot infrastructure that decay
   the moment they are noticed. Doc 09 defines the test that would falsify this, and the plan is
   built so that finding out costs a few months rather than a few years.

## 2. The historical data wall, quantified

This is the single most important finding. Verified from provider documentation:

| Source | Claim | Actual limit | Source |
|---|---|---|---|
| Dune | "Query Solana on-chain data" | Free tier is **view-only**; running queries needs a paid plan or the 14-day trial | [Dune pricing FAQ](https://docs.dune.com/learning/how-tos/pricing-faqs) |
| Bitquery | "Free developer tier" | **7-day trial**, 1,000 API points; its Solana archive history sits behind a paid add-on | [Bitquery pricing](https://bitquery.io/pricing) |
| Flipside | "Free SQL analytics" | Creator Studio **shut down**; blockchain data business acquired May 2026 | [Dune migration guide](https://docs.dune.com/learning/flipside-migration-guide) |
| Google BigQuery | "Free public Solana dataset" | Legacy `crypto_solana_mainnet_us.transactions` reported as **not updating after 2025-03-31**; freshness of any replacement must be checked per table | [Google Cloud forum thread](https://discuss.google.dev/t/public-solana-bigquery-dataset-crypto-solana-mainnet-us-stopped-updating-on-march-31-2025/185629/2) |
| Vybe Network | Free plan | Historical window is **48 hours** only | [Vybe plans](https://docs.vybenetwork.com/docs/plans-rate-limits) |
| Moralis | Free plan | **No free tier published**; lowest tier is $149/month | [Moralis pricing](https://docs.moralis.com/get-started/pricing) |
| Vybe / Birdeye / Solscan | Free API usage | Solscan Free/Lite are **personal/research use only**; commercial use needs Pro Level 2+ | [Solscan terms](https://docs.solscan.io/solscan-api/solscan-api-terms-and-services) |

The pattern is consistent and worth internalising: **free-tier blockchain analytics has been
retreating, not expanding.** Three separate providers removed or gated their free query tiers
between 2025 and 2026. This is a structural trend, and the architecture must be built to survive
the next removal (see §5).

What remains genuinely usable:

- **Helius free plan — 1M credits/month, 10 RPC req/s.** Archival methods `getSignaturesForAddress`
  and `getTransaction` cost **1 credit each**, so the free tier buys roughly **500,000 historical
  transactions per month**. That is a real budget, and it is enough for a narrow funnel.
- **DexScreener's keyless public API** — 300 req/min for pair and search endpoints, no key. Current
  snapshots only; **no historical OHLCV endpoint exists**.
- **GeckoTerminal's keyless public API** — pool-level **hourly OHLCV** is available, rate-limited to
  roughly 10 req/min. Useful for cross-checking, not for trade-level precision.

### Why a self-hosted node is not an option

Anza's own hardware guidance for an RPC node is **16+ cores and 512 GB RAM** for full account
indexes, plus separate NVMe volumes for ledger and snapshots. A validator needs 256 GB RAM minimum.
This is orders of magnitude outside "ordinary personal computer" and is not a path to explore at
any budget described here. ([Anza requirements](https://docs.anza.xyz/operations/requirements))

## 3. The credit budget is the real design constraint

1M credits/month sounds generous until you price the naive approach. Verified Helius credit costs:

| Operation | Credits | Note |
|---|---|---|
| `getSignaturesForAddress` | 1 | List an address's signatures |
| `getTransaction` | 1 | Full transaction |
| `getProgramAccountsV2` | **1** | Paginated program-account scan |
| `getProgramAccounts` | 10 | Unpaginated equivalent |
| `getTransactionsForAddress` | 10 + | 10 per 100 full txs returned |
| `getTransfersByAddress` | 10 | **Free plan is blocked — returns an error** |
| DAS API (any endpoint) | 10 | Token metadata, holders |
| Enhanced Transactions | 100 | Legacy, in maintenance mode |
| Parsed Events | 10 | Successor to Enhanced Transactions |

Two consequences drive the architecture:

**(a) The obvious approach is unaffordable by three orders of magnitude.** Analysing 10,000 wallets
with 500 transactions each is 5,000,000 transactions ≈ 10M credits ≈ the $49/month Developer plan.
At $0 it is not possible, and no amount of clever engineering makes it possible.

**(b) The funnel is affordable.** Because discovery (DexScreener, keyless) and program-account
enumeration (`getProgramAccountsV2`, 1 credit) are nearly free, the expensive per-transaction cost
only applies to a shortlist. A realistic monthly allocation:

| Stage | Volume | Credits |
|---|---|---|
| Watch new pools, filter to tokens that show traction | ~2,000 tokens | ~0 (DexScreener + GeckoTerminal, keyless) |
| Fetch early-buyer signature lists for the shortlist | ~200 tokens × 6 calls | ~1,200 |
| Fetch the transactions behind those signatures | ~50,000 tx | ~50,000 |
| Resolve tokens that survive into the scoring pool | ~300 tokens × 2 DAS calls | ~6,000 |
| Deep history for ~60 candidate wallets | ~60 × 3,000 tx | ~180,000 |
| **Headroom for retries, monitoring and re-runs** | | ~760,000 of 1,000,000 |
| **Total committed** | | **~237,000** |

So the free tier supports a funnel of roughly **60 wallets analysed in depth per month**, with
substantial headroom. That is a real product for a personal tool. It is not a scanner of the whole
chain, and the documentation should never pretend otherwise.

**The one trap to avoid above all others:** `getTransfersByAddress` is documented at 10 credits and
looks like the right tool for wallet history, but it **errors out on the free plan**. Any code path
that depends on it fails only in production, after the design is committed. It must be excluded
from the adapter layer from day one.

## 4. Where the vision must be narrowed

Honest scoping, in the order the requirements list them.

**Kept essentially as specified:**

- Discovery from public on-chain activity rather than a manual list (doc 02 §4).
- Full P&L reconstruction with fee attribution and explicit separation of realized/unrealized.
- Point-in-time early-entry measurement.
- The eight sub-score dimensions, with a separate confidence dimension.
- Grounded AI reports with deterministic facts.
- Watchlists and alerts — the free Helius WebSocket stream makes this genuinely free.

**Narrowed:**

- **"Backtesting across different market conditions"** becomes *one* discovery window plus *one*
  held-out validation window. Multiple macro regimes require historical data the budget cannot buy.
  The honest substitute is **cross-sectional benchmarking** (doc 05 §5), which makes tokens from
  different weeks comparable without needing a longer history.
- **Consistency across market conditions** becomes consistency across calendar months and across
  token-cohort vintages within the collected period.
- **Survivorship bias correction** cannot be done properly from an incomplete historical universe.
  It can only be *measured* on forward-collected data, from the date collection began. Before that
  date, the tool must state plainly that survivor bias is unquantified.
- **Holder-growth and volume-inflection signals** depend on historical holder counts, which no free
  tier provides reliably. Approximated with trader-count growth per pool, derived locally.

**Dropped from the initial scope:**

- Deep historical profiles of arbitrary wallets. Analysable wallets are limited to those inside the
  collected window, or those whose transaction counts fit the credit budget.
- Any form of real-time sniper-grade detection. The collector will lag by seconds to minutes; it is
  a monitoring tool, not a trading system, and copyability is explicitly measured rather than
  assumed (doc 09 §6).

## 5. Design principles forced by the constraints

1. **Own your raw data.** Store the raw RPC JSON for every transaction fetched, not just the derived
   fields. Providers have repeatedly removed free tiers; anything derived can be recomputed from
   raw, and anything not stored is re-purchased with credits that are not available twice.
2. **One adapter per provider, behind a narrow interface.** Helius, DexScreener and GeckoTerminal all
   get their own module implementing a common `PriceSource` / `TradeSource` contract. Provider
   replacement should be a new file, not a refactor.
3. **Spend credits deliberately.** The ingestion layer maintains a ledger of credits consumed per
   job. A runaway loop that spends a month of budget in an hour is the most likely way to lose
   access, and it should be a mechanical guard rather than a discipline.
4. **Compute deterministically, narrate with AI.** Anything a user might act on — P&L, score,
   threshold — is computed in ordinary code and is reproducible from stored inputs. The LLM's job is
   explanation, and its output is validated against the computed facts before it is shown (doc 06).
5. **Report uncertainty, never hide it.** Every metric carries a provenance and completeness flag.
   `null` with a reason is always preferable to an invented zero.

## 6. Recommended decision

**Proceed, with the MVP as scoped in doc 03, and with the validation gate in doc 09 treated as a
real stopping condition.**

The recommended sequence is deliberate about what is expensive to get wrong:

- Build the **forward collector first**, before any analytics. It is the only component whose value
  decays if delayed.
- Build **price reconstruction second** and test it against DexScreener snapshots while they are
  still current. This is the highest-risk technical assumption; it should be falsified in week 2,
  not month 3.
- Build **scoring third**, with weights frozen before any validation run.
- Treat **validation as the deliverable**, not an afterthought. If the ranking demonstrates no
  out-of-sample value, the honest outcome is to publish the negative result and reduce the tool to
  descriptive analytics — which is still useful, and still what most of the work produces.

**Do not** build a web dashboard, an alerting system, or multi-chain support before a single
validation run has been completed. All three are pleasant to build and none of them is evidence
that the ranking works.
