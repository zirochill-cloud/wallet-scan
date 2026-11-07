# SolScope — Smart-Money Wallet Intelligence for Solana

> **Tool is functionally complete. Requires an anchor set to produce rankings — see
> [docs/PHASE1B_STATE.md](docs/PHASE1B_STATE.md).**

A zero-budget, evidence-driven system for **discovering, scoring, and monitoring Solana wallets
that repeatedly buy promising tokens before the broader market does.**

The design is complete and falsifiable, and implementation has begun: Phases 0–2 exist in
`solscope/` with tests, and the price-derivation gate has **passed on live mainnet**.

> **Scope of “Phase 2 complete.”** It means *PumpSwap only*: reserve-based price and
> liquidity derivation for PumpSwap pools quoted in wSOL/USDC. **Not covered:** Raydium
> AMM v4 / CPMM / LaunchLab, pump.fun bonding curves, Orca Whirlpool, Meteora DLMM,
> Jupiter routing and aggregator hops. Those DEXes do not block Phases 3–6, but full
> coverage **is** required before any Phase 9 validation claim — scoring wallets on
> incomplete history biases every per-wallet rate. Coverage table: [doc 11 § 8](docs/11-phase2-gate.md).
>
> **The two are independent.** Phase 2 passing does **not** unblock Phase 1b: the
> retrospective anchor blocker (`NOTES.md` § `ANCHOR_SELECTION_CRITERIA`) remains open.
> A Phase 4 discovery build cannot be trusted until anchors are de-duplicated by cluster
> and the quality filter is measured against known-negative bundled sets.

## Read in this order

| Doc | Answers |
|---|---|
| [00-feasibility.md](docs/00-feasibility.md) | Is this possible for ~$0/month? Where does it break? |
| [01-data-sources.md](docs/01-data-sources.md) | What is *actually* free, with real limits and licence traps |
| [02-architecture.md](docs/02-architecture.md) | Components, data flows, storage choices, build-vs-borrow |
| [03-mvp-scope.md](docs/03-mvp-scope.md) | What to build first, and what to deliberately not build |
| [04-data-model.md](docs/04-data-model.md) | Tables for wallets, trades, tokens, prices, scores, alerts, evidence |
| [05-scoring-methodology.md](docs/05-scoring-methodology.md) | The 8 sub-scores: formulas, weights, shrinkage, confidence |
| [06-ai-pipeline.md](docs/06-ai-pipeline.md) | Separating deterministic computation from LLM interpretation |
| [07-roadmap.md](docs/07-roadmap.md) | Phased plan, each phase independently testable |
| [08-risks.md](docs/08-risks.md) | Technical risks, data limits, bottlenecks, cost traps |
| [09-validation.md](docs/09-validation.md) | How we prove the ranking is useful — or conclude it isn't |
| [10-phase2-spike.md](docs/10-phase2-spike.md) | Live test: can price be derived from raw chain data? |
| [11-phase2-gate.md](docs/11-phase2-gate.md) | The go/no-go gate: criterion, method, measured result |
| [INVENTORY.md](docs/INVENTORY.md) | Status of every doc/artifact, and known inconsistencies |
| [NOTES.md](NOTES.md) | Open blockers, carried state, quantified data loss |

Machine-readable companions:

- [`schema/001_init.sql`](schema/001_init.sql) — the proposed Postgres schema, executable
- [`config/scoring.yaml`](config/scoring.yaml) — every weight, threshold and parameter in one place
- [`config/budget.yaml`](config/budget.yaml) — per-provider credit caps and per-operation costs
- [`spikes/phase2_spike.py`](spikes/phase2_spike.py) — the live price-derivation test (stdlib only)
- [`artifacts/phase2_gate.json`](artifacts/phase2_gate.json) — the gate's measured output

Run it:

```bash
set -a; source .env; set +a          # HELIUS_API_KEY (and SOLSCOPE_PGDSN for DB-backed runs)
solscope doctor                       # config + budget sanity
solscope derive --gate --no-db --pairs 20 --tolerance 5
python tests/test_phase2.py           # decoder + gate contract, against real mainnet fixtures
bash bootstrap_db.sh                  # ONE command: venv + PostgreSQL + migrations + both DB suites
```

`bash bootstrap_db.sh` is the only supported way to run the two DB suites
(`test_schema_smoke`, `test_ledger_live`) on Windows: it stages everything at a short
path (this project's 164-character path breaks psycopg and Postgres itself), runs
`solscope migrate` for real, and fails if a suite merely SKIPs. Details:
[requirements-dev.txt](requirements-dev.txt).

## The three ideas this design turns on

**1. You cannot afford the historical data, so start the clock now.**
Complete Solana history is not available on any free tier. Dune's free tier is view-only, Bitquery's
"free" plan is a 7-day trial, Flipside Creator Studio is shut down, the legacy BigQuery Solana
transactions table stopped updating, and a self-hosted RPC node needs 512 GB RAM. The design
therefore treats **forward-collected data as the primary asset** and historical backfill as a
narrow, targeted sample used only to seed and validate. A collector started today is complete and
bias-free in six weeks; a collector started later never catches up on this period.

**2. Derive price and market cap from the swap itself, not from a price API.**
No free API returns historical market cap at an arbitrary past timestamp. But a decoded AMM swap
carries the amounts it actually exchanged, and `execution price = |Δquote| / |Δbase|` is exact for
that trade — point-in-time correct, free, and impossible to look ahead with.

*Verified on live mainnet (doc 10), three independent runs: 37 swaps total, mean deviation*
*2.03%, 2.26% and 4.23% against DexScreener. One correction the test forced: this must be derived*
*from the swap's own balance deltas, not from `quote_reserve / base_reserve` read off the pool —*
*the naive reserve formula was off by 4–5 orders of magnitude, because the largest balance of a*
*mint is usually not the AMM vault.*

**3. Score and confidence are different numbers, reported separately.**
A wallet with 4 trades and a great return is not the same as a wallet with 60 trades and the same
return. The model shrinks every rate toward the population mean by sample size, reports a 0–100
**Score** and a separate 0–100 **Confidence**, and ranks on a confidence-adjusted score that pulls
weak-evidence wallets toward neutral rather than rewarding or punishing them arbitrarily.

## Explicit non-goals

- This tool does **not** place trades, hold funds, or request private keys. It is read-only.
- It does **not** promise profit, and it does not claim historical wallet performance predicts
  future returns. Establishing whether it predicts anything is the subject of doc 09.
- It does **not** claim that a wallet address is a person. Clustering is presented as
  "likely related", never as identity.
