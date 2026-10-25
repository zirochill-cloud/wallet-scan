# 08 — Risks, Data Limitations, Bottlenecks and Cost Traps

Ordered by expected damage, not by likelihood. The first two are the ones that can kill the project.

---

## 1. Risks that can kill the project

### R1 — The historical data wall (severity: critical, likelihood: certain)

There is no free path to complete Solana history. Verified: Dune's free tier is **view-only**,
Bitquery's "free developer tier" is a **7-day trial**, Flipside Creator Studio **shut down**, the
legacy BigQuery Solana transactions table **stopped updating 2025-03-31**, Vybe's free history is
**48 hours**, Moralis has **no published free tier**, and a self-hosted RPC node needs **512 GB RAM**.

**Consequences:** no multi-cycle backtesting; a cold-start period of 6–12 weeks; deep profiles limited
to wallets inside the collection window.

**Mitigation:** forward collection is the primary asset (doc 07 Phase 1). Funnel the credit budget so
depth is spent only on a shortlist. Own the raw archive so re-derivation costs CPU instead of credits.

**Residual risk: high, and irreducible.** Stated plainly in the deliverable rather than hidden.

### R2 — The persistence hypothesis may be false (severity: critical, likelihood: unknown)

The entire premise is that wallets with demonstrated early-entry ability keep exhibiting it. This may
be wrong. Early-entry performance has several non-replicable causes:

- **Insider access** — private group, developer relationship, or launch participation.
- **Infrastructure** — co-located bots, shred-level latency, Jito bundles.
- **Informational edge that decays** once the wallet is noticed and copied.
- **Pure luck** in a fat-tailed distribution, where the top decile of a large population always looks
  skilled.

If skill does not persist, the tool's ranking has no forward value no matter how well it is built.

**Mitigation:** doc 09 tests persistence directly and pre-registers what would falsify it. Discovery
uses outcomes to *select* candidates but scoring uses the wallet's **complete** history, which removes
the largest single source of the illusion (doc 02 §4.4). Copyability is measured rather than assumed.

**Residual risk: high — and this is the point of the validation phase.** A negative result here is a
successful project outcome, not a failure.

---

## 2. Serious risks

### R3 — Selection on the dependent variable (severity: high, likelihood: high without discipline)

Wallets are discovered *through* tokens that succeeded. A wallet found via its winners will look
skilled if only those tokens are examined.

**Mitigation:** the hard rule that evaluation uses the full trade history within the window, including
every token that went nowhere. Phase 4 exit criteria explicitly test that the candidate pool contains
the counterparts of losers. `data_quality_flags` records the residual bias.

### R4 — Sybil / coordinated-wallet false independence (severity: high, likelihood: high)

One operator running 20 wallets appears as 20 independent confirmations. A leaderboard built this way
overstates its evidence by an order of magnitude.

**Mitigation:** cluster flags (`common_funder`, `co_purchase`, `same_slot`, `jito_bundle`);
`independence_factor = 1/√(cluster_size)`; `max_per_cluster_in_top_n = 2`;
`min_independent_wallets` on co-buy alerts where cluster members count as one.

**Known limitation:** clustering establishes *likely coordination*, never ownership. Exchange and
custodial funders produce false positives, so `exclude_exchange_funders` defaults to true — but the
allowlist must be maintained from observed high-fanout funders, and it will be incomplete.

### R5 — Price derivation fails on some venues (severity: high, likelihood: medium)

The design depends on recovering pool reserves from the swap. For an AMM with a stable, known
instruction layout this is reliable. It is not guaranteed for every launchpad, aggregator route, or
novel pool type — and newly launched memecoins are exactly where exotic mechanisms appear.

**Mitigation:** Phase 2 is a hard gate with a falsifiable criterion (20 live pairs reconciling against
DexScreener). Decoder versions are recorded per row so improvements can be re-run.
`data_quality_flags` surfaces unrecognised activity rather than silently dropping it.

**If it fails:** the early-entry capability is unreachable at this budget. Stop rather than substitute
candle interpolation while keeping the same claims of precision.

### R6 — Overfitting the score to historical winners (severity: high, likelihood: medium)

With ~200 wallets and one collection window, fitting weights to historical winners will produce a
model that explains the past perfectly and predicts nothing.

**Mitigation:** weights are reasoned priors, not fitted; they are **frozen in version control before
validation**; parameter count is kept deliberately small; weight perturbation (±25%) reports ranking
stability via Kendall's τ; validation is on a held-out later period.

**Residual risk: medium.** The weight-stability check is the main early warning.

### R7 — Provider churn (severity: medium-high, likelihood: **high — it is already happening**)

Three providers gated or removed free query access between 2025 and 2026. The trend is one-directional.

**Mitigation:** narrow adapter protocols per source; raw responses archived append-only so derived
values are recomputable; provenance tags so affected metrics can be identified; a credit ledger that
fails loudly rather than degrading silently.

**Residual risk: medium.** A provider withdrawing history mid-project costs time but not already
collected data.

---

## 3. Data limitations that must be disclosed, not papered over

| Limitation | Impact | Handling |
|---|---|---|
| **No complete historical universe** | Survivorship bias is unquantifiable before the collection start date | Flag it; never claim a corrected figure |
| **Wallet ≠ identity** | Cannot attribute to people, teams or funds | Reported as "address"; clustering = coordination evidence only |
| **Third-party labels are weak** | Heuristic and often stale | Stored in `wallet_labels` with source and confidence, never as ground truth |
| **Missing historical prices for some tokens** | Some positions have no derivable entry price | `NULL` + reason; excluded from the affected metric's denominator |
| **Incomplete transaction histories** | Win rates computed on unknown samples | `history_completeness` on `wallets`; coverage feeds the confidence score |
| **Holder counts unreliable on free tiers** | Cannot measure holder growth directly | Approximated by distinct-trader growth per pool, derived locally; labelled as an approximation |
| **Multi-pool liquidity fragmentation** | Naive mcap is wrong when a token trades in several pools | Compute from the deepest pool or liquidity-weighted aggregate; record which |
| **Chain finality** | Unfinalised data can be reorged | `commitment` recorded; scoring requires finalized data |
| **`blockTime` occasionally absent** | Row unusable for point-in-time analysis | Excluded by the `event_time <= as_of` filter until backfilled from slot |
| **Decimals errors** | Silent 10^6 or 10^9 P&L errors | Raw integers + decimals only; never floats |
| **Fees are partially observable** | Some platform fees appear as separate transfers | Sum priority fee + tip + platform transfer + implied LP fee; record what was found |

---

## 4. Bottlenecks, in the order they will actually bind

1. **Credit budget — binding immediately.** 1M credits/month ≈ 500k transactions at 1 credit each,
   which supports ~60 wallets analysed in depth per month. This is the primary constraint on scale and
   cannot be engineered around.
2. **RPC rate limit — 10 req/s on free.** A 50,000-transaction wallet takes ~83 minutes of wall-clock
   fetch time. Fine for a nightly batch; painful for interactive exploration.
3. **Discovery coverage — structural.** Shortlisting by current traction systematically misses tokens
   that looked unpromising for hours before taking off. This biases discovery toward fast movers.
4. **Decoder coverage — ongoing.** Every unrecognised program is an invisible gap in the data.
5. **Analytical iteration speed — hidden.** Interpretation and re-analysis is where most time actually
   goes. DuckDB over Parquet is chosen specifically to keep this fast.
6. **Raw archive size — later.** Manageable now; needs periodic compaction. Size from a measured sample
   rather than row count, since addresses and signatures dominate bytes.

---

## 5. Cost traps

These are the specific ways a "zero-budget" project quietly acquires a bill or loses its free tier.

### Trap 1 — `getTransfersByAddress` is blocked on the free plan

Documented at 10 credits, it looks like the obvious tool for wallet history. On Free it **returns an
error**. Any code path depending on it fails only in production, after the design is committed.

**Guard:** the adapter layer never routes through it; a startup assertion fails fast if it is called.

### Trap 2 — Credit costs vary by 100× between endpoints

| Endpoint | Credits |
|---|---|
| `getTransaction` | **1** |
| `getProgramAccountsV2` | **1** |
| DAS API (any) | **10** |
| Parsed Events | **10** |
| Enhanced Transactions | **100** |

Routing history through Enhanced Transactions costs 100× a raw `getTransaction` for the same data.
Enhanced Transactions is also legacy and in maintenance mode.

**Guard:** the credit ledger records cost per job; monthly caps are enforced in code and fail loudly.

### Trap 3 — "Free trial" tiers that create a cliff

Bitquery (7 days), Dune (14 days), Cerebras ($5 expiring in 30 days). Building a component on any of
them means it works until it doesn't.

**Guard:** trial-tier providers are excluded from the architecture, not merely deprioritised.

### Trap 4 — Free tiers with non-commercial licences

Solscan Free/Lite are **personal or research use only**; commercial application use requires Pro Level
2+. Bitquery's Personal plan is explicitly **personal-use only**. DexScreener's terms prohibit building
a competing product. Birdeye's terms restrict redistribution.

A personal analysis tool is fine. The moment it is offered to others — even free of charge, if it
redistributes provider data — the licence position changes.

**Guard:** document the intended use of each source; prefer Helius' terms (which permit use in the
customer's own products but restrict resale) for anything load-bearing.

### Trap 5 — Unverified free tiers

Birdeye's 30,000 CU/month and 1 RPS appear in secondary sources, but the official pricing and auth
pages defer limits to the account/support. Which endpoints are enabled on free is **unverified**.

**Guard:** never architect around an unverified limit. Verify in the account dashboard first.

### Trap 6 — Runaway polling loops

DexScreener allows 300 req/min on pair endpoints but only **60 req/min** on discovery endpoints. A
careless loop exhausts the lower one and can get the client blocked.

**Guard:** per-provider token-bucket rate limiters; the 60/min endpoints are the ones to throttle.

### Trap 7 — Cloud LLM free tiers that train on your data

The Gemini free tier's terms (effective 2026-03-23) permit unpaid-service prompts and responses to be
used to improve Google's services, with possible human review. For proprietary analysis, that is a
real cost even though the invoice is zero.

**Guard:** local inference by default; cloud is opt-in and never used for anything not explicitly sent.

### Trap 8 — Paid upgrades taken to escape a binding constraint

The pressure to "just pay $49" will arrive during backfill. It should be a deliberate decision against
a stated budget, not a drift.

**Guard:** the 1M-credit cap is a hard failure, not a soft warning, so the decision is forced and
visible rather than made accidentally at 3am.

---

## 6. Failure modes of the model itself

| Failure | Detection | Mitigation |
|---|---|---|
| **Looks like skill, is survivorship** | Candidate pool contains no losers | Phase 4 exit criterion; full-history evaluation |
| **Looks like skill, is one lucky trade** | Score drops sharply when the best position is removed | Shrinkage; median-based profitability; `n_eff` |
| **Looks like skill, is an insider bundle** | High score + high integrity penalties | Integrity weight + hard gate at 30 |
| **Looks like skill, is a whale** | Score driven by `magnitude_component` | Log scaling and a cap on that term |
| **Looks like skill, is uncopyable** | Shadow portfolio negative despite good wallet P&L | Doc 09 §6 measures it directly |
| **Looks like skill, is a bull market** | Cohort-excess returns near zero despite high absolute returns | Cohort benchmarking in token_selection |
| **High win rate, worthless trades** | Win rate high, profit factor ≈ 1 | Win rate is shrunk and owned by one dimension only |
| **Confident score on thin evidence** | Confidence low while score high | Confidence reported and applied separately; gate at 25 |

---

## 7. Ethical and legal boundaries

- **Read-only, always.** No private keys, no signing, no custody, no trade execution.
- **Addresses are not identities.** No attempt to deanonymise, and no assertion that a cluster is a
  person, team, or fund.
- **Coordination is reported as evidence, not accusation.** "Evidence of possible coordination" is the
  strongest permitted phrasing; the verifier rejects stronger claims.
- **No profit promises anywhere in the product.** Historical wallet performance does not imply future
  results, and the reporting layer must say so. The claim verifier blocks price predictions.
- **Respect provider terms.** Free tiers are used within their licence scope, and the tool is personal
  unless the licence position is revisited.
- **Do not build a harassment tool.** If a public deployment is ever considered, the labelling and
  accusation surface needs a separate review — publishing "insider wallet" claims about addresses
  attached to real people has consequences this document does not currently address.
