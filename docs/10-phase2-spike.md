# 10 — Phase 2 Spike Report: Price Derivation from Raw Chain Data

**Date:** 2026-10-15
**Status:** Executed against live Solana mainnet. **The load-bearing assumption holds, with one important correction.**
**Reproduce:** `python3 spikes/phase2_spike.py`
**Follow-up:** the production gate that supersedes this spike's reconciliation method is
[doc 11](11-phase2-gate.md) — it enumerates pool vaults by owner and reconciles reserves against
reserves, rather than comparing a single sampled trade to `priceNative`.

---

## 1. What this was for

Doc 07 §Phase 2 defines a go/no-go gate: *locally derived price must agree with a contemporaneous
DexScreener snapshot within tolerance.* If it does not, early-entry scoring is unreachable at this
budget and the project stops.

That gate was designed to be run in weeks 3–4, after the collector existed. It was run **on day
one**, because it is the single cheapest way to find out the project should not be built — and
because the finding changes how Phase 2 must be implemented.

The test used **only public infrastructure**: the keyless Solana mainnet RPC and DexScreener's
keyless API. No Helius key, no paid tier, no collector. This was deliberate: if the assumption
survives the *cheapest possible* test, it is not an artefact of an expensive toolchain.

## 2. Result

Three independent runs against live mainnet, same method, different market conditions:

| Run | Swaps | Mean deviation | Max deviation | Within 5% |
|---|---|---|---|---|
| Run 1 (calm, ~$20k liq) | 14 | **2.03%** | 4.85% | 14/14 |
| Run 2 (volatile, ~$15k liq) | 12 | **2.26%** | 12.01% | 10/12 |
| Run 3 (falling, ~$15k liq) | 11 | **4.23%** | 7.79% | 7/11 |

`spikes/phase2_reconciliation.json` currently holds **Run 3** (the most recent execution); Runs 1
and 2 are the table above from earlier captures. Run 3 was run at `--tolerance-pct 10.0` and
passed 11/11 there, but only 7/11 would clear 5% — the tolerance is stated rather than quietly
raised to make the artifact read as a pass.

Reference: DexScreener `priceNative` for the deepest pool, mean of two bracketing snapshots.
Run 1 drift was 0.000%; Run 2 drift was 2.637% — the token was actively falling, which is
deliberately a harder test, not a best case. Run 3 was captured during a continued decline, which
is why its mean deviation is the highest of the three.

The residual spread is **not derivation error** — it is the expected and correct gap between a
trade's *execution* price (which includes that trade's own price impact) and the pool's *spot*
price at snapshot time. The one 12.01% outlier was inspected account-by-account: it was a
**sell of 28,888 tokens into a ~$15k pool during a downtrend**, and the pool's wSOL vault really
did pay out 0.0090 SOL for those tokens. The derivation correctly captured a genuinely bad fill.
A price that slippage-blind would have reported "spot" for that trade would have been *wrong*.

**This passes the doc 07 Phase 2 gate.** The early-entry objective is reachable at this budget.

## 3. The correction the spike forced

Doc 02 and doc 05 both lean on `price = quote_reserve / base_reserve`. **That formula, applied
naively, is wrong** — and it is wrong in a way that would have silently corrupted every price
in the database:

- **Identifying the pool vault as "the account holding the largest balance of that mint" fails
  catastrophically.** Measured directly: off by **4–5 orders of magnitude on every swap** (mean
  ~41,900%), because the largest token holder is frequently a bonding curve, virtual-reserve
  account, or unrelated holder — not the AMM vault. The resulting price looks perfectly
  well-formed while being useless.
- **The naive reserve ratio is still wrong even when the vault is correctly identified.** On the
  inspected sell, the correctly-identified wSOL vault (59.81 SOL) over token vault (244.7M)
  gives ~2.44e-7 against a true spot of ~3.5e-7 — an independent, ~30% error. PumpSwap holds
  quote asset beyond the single largest wSOL account, so reserve pricing **requires decoding the
  pool's real state**, not inferring it from balances present in a swap transaction.
- **The reliable signal is the swap's own balance deltas**, not the reserve snapshot. A swap
  leaves equal-and-opposite legs; `|Δquote| / |Δbase|` is exact for that trade and needs no
  vault identification at all.

Practical consequence for Phase 2: **derive execution price from deltas as the primary method.**
Reserve-ratio pricing remains useful (it gives the post-trade spot price for the *next* trade's
slippage check) but requires genuine pool-state decoding — Anchor IDL where available,
program-specific account layouts otherwise. It is a Phase 2 deliverable, not a Phase 2 starting
assumption. Doc 02 §"price derivation" should be read with this constraint.

## 4. Four operational findings that will bite the collector

Each of these was hit live and is a concrete requirement for the Phase 1 adapter, not speculation.

1. **Versioned transactions are mandatory.** Raydium and PumpSwap swaps on this pool are
   transaction **version 1**. `getTransaction` with `maxSupportedTransactionVersion: 0` returns
   a `-32015` error; the collector must request version ≥ 1 or it silently fetches nothing from
   exactly the pools that matter.

2. **`getSignaturesForAddress(pool)` does not return that pool's swaps.** On a busy pool the
   feed is dominated by **aggregator-routed transactions that merely *reference* the pool** in
   their account list while swapping against something else. Sampling the deepest SOL/USDC pool
   for "its" swaps therefore yields mostly transactions that never touch it. Discovery must key
   off the **token's own pool address** (resolved from DexScreener), not a shared mega-pool.

3. **Netting a mint across all accounts erases the trade.** Summing per-mint deltas across the
   whole transaction cancels user (−X) and pool vault (+X) to ~0. The spike's first iteration
   derived *zero* trades because of this. The fix is to take the **dominant-|delta| leg per
   mint**, not the net. Netting is valid only for detecting an *intermediate* asset (see 4).

4. **Intermediate hops are detectable and must be excluded from USD pricing.** In a routed
   memecoin→USDC swap, wSOL is an intermediate leg: it enters and leaves within one transaction
   and its net is ~0. That net is exactly the test for "SOL was intermediate, not settled" — and
   it is what makes the *same transaction* a direct USD-denominated price for the memecoin, with
   no SOL/USD feed involved at all.

Also minor but load-bearing: **DexScreener 403s Python's default `User-Agent`** on an otherwise
keyless endpoint, and public RPC rate-limits aggressively enough that backoff with `429` retry
is required on every call.

## 5. What this does *not* establish

Stated plainly, so the gate is not oversold:

- **Fees are not yet separated.** Execution price here is gross. Priority fee, Jito tip, platform
  fee and implied LP fee require instruction decoding and remain Phase 2 work (doc 05 §4.3).
- **One AMM family verified.** PumpSwap (pump.fun) was tested end-to-end. Raydium CLMM, Meteora
  DLMM and Orca Whirlpool each have distinct account layouts and need the same treatment before
  Phase 3 trusts their prices.
- **Reconciliation used a seconds-later snapshot,** not a true point-in-time oracle. For a
  volatile memecoin this inflates apparent deviation (Run 2: 2.6% drift during collection). A
  tighter test in Phase 2 proper compares against the **post-trade reserve ratio from the same
  transaction**, which removes time skew entirely — and requires the vault identification of §3.
- **Execution price includes that trade's price impact.** This is correct and desirable for P&L
  (it is what the trader actually got), but it means "derived price vs spot" will never be zero.
  Phase 2 must decide per-consumer whether it wants execution price (for P&L) or decoded spot
  (for market-cap marking), and not conflate them.
- **Sustained throughput is untested.** This fetched tens of transactions. The collector needs
  sustained polling; the credit ledger and backoff design in Phase 0 exist for that reason.

## 6. Decision

**Continue.** The Phase 2 gate passes on execution-price derivation, and the two failure modes
that would have corrupted the data (naive vault identification, aggregator-polluted discovery)
were found *before* any data was stored rather than after.

The spike is preserved as `spikes/phase2_spike.py` (stdlib only, no dependencies) and its raw
output as `spikes/phase2_reconciliation.json`. Reproduce with:

```
python3 spikes/phase2_spike.py -n 25 --tolerance-pct 5.0
```

It auto-resolves the token's deepest pool via DexScreener, derives execution prices from raw
balance deltas over public RPC, and reconciles against `priceNative`. Phase 2 proper should
treat §3's correction as a requirement, not a note.
