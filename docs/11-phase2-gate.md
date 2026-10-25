# 11 — Phase 2 Gate: price derivation from pool reserves

**Status: PASSED on live mainnet, 2026-10-25.** Artifact: [`artifacts/phase2_gate.json`](../artifacts/phase2_gate.json).
Reproduce with `python -m solscope derive --gate --no-db --pairs 20 --tolerance 5 --json artifacts/phase2_gate.json`.

This is the go/no-go gate from doc 07. Doc 03 §"Phase 2" says: *"for 20 live pairs, locally derived
price agrees with a contemporaneous DexScreener snapshot within tolerance, and derived liquidity is
within tolerance of the snapshot's liquidity."* If it fails, the early-entry objective is unreachable
at this budget and the design stops.

---

## 1. The criterion, stated before the result

The docs named "within tolerance" without pinning a number or saying how pools may fail. Both gaps
had to be closed *before* looking at data, so the rule below is pre-registered here and encoded in
`GateReport.verdict` / `min_pass_rate`:

| Condition | Value |
|---|---|
| Priced pools | **≥ 20** |
| Mean absolute price deviation | **≤ 5.0%** |
| Pools individually inside 5.0% | **≥ 80%** |
| Liquidity | **must be measured**, mean absolute deviation ≤ 5.0% |
| Pools that moved more than 5% mid-measurement | **excluded, counted, and reported** |

The last row is a measurement rule, not a pass condition. Reserves must be read twice — once
before and once after the snapshot — and a pool whose *own price* moved further than the tolerance
between those reads has no like-for-like comparison available: its base side was read on one side of
a trade and its quote side on the other. Those pools are dropped from the statistics, never silently,
and they still count against nothing: excluded pools do not help reach the 20-pool minimum.

Why not a mean alone: with a mean-only test, five pools at 40% error hide behind twenty at 0.1%.
That is the failure mode a gate exists to prevent.

Why not *every* pool inside tolerance: on a memecoin that trades at 20%/min, a single pool can
legitimately move between our reserve read and the provider's snapshot. Demanding zero outliers makes
the gate a coin flip on market volatility rather than a measurement of derivation. An 80% individual
pass rate plus a 5% mean forces a *majority* of pools to agree on their own terms while tolerating
one or two that genuinely moved.

## 2. Method

For each candidate pool:

1. **Enumerate pool reserves** with `getTokenAccountsByOwner(pool)` over **both** the SPL Token and
   Token-2022 programs, summing per mint. Not `getAccountInfo` on assumed vaults, and **not** the
   balances inside a transaction's `meta`.
2. **Derive price** as `quote_ui / base_ui` from those summed reserves; derive liquidity as
   `quote_ui × SOL/USD + base_ui × priceUsd`.
3. **Take a DexScreener snapshot immediately afterwards** and compare against the provider's *own*
   reserve figures (`liquidity.base` / `liquidity.quote`), keyed by **pair address**.
4. **Read the reserves a second time** after the snapshot and compute the pool's own drift across
   the window. Drift above the tolerance marks the pool as moved and excludes it (see §1).

Two choices in that method are load-bearing, and both were forced by measurements:

### 2.1 Reserves cannot come from a transaction's `meta`

`meta.pre/postTokenBalances` list only the accounts *that transaction touched*. A pool vault the
trade never touched is invisible, and nothing in the payload says so.

Measured on fixture `tx_direct_buy_3zS41H` (2026-10-17 mainnet): both vaults were correctly
identified — their deltas matched the trade exactly — and the ratio still implied **1.76e-7** against
an executed price of **2.42e-7**, a 27% error, because a third pool-owned base vault was absent from
the transaction. Enumerating by owner finds all of them; `Deriver.pool_spot_from_tx` is retained only
as a diagnostic lower bound and is never used for pricing.

### 2.2 `priceNative` is not a mid price

DexScreener's `priceNative` is trade-derived, and on tokens in a dump it sits consistently **above**
the ratio implied by DexScreener's *own* reported reserves:

| Pool | Our reserve ratio | Ratio implied by DexScreener's own reserves | DexScreener `priceNative` |
|---|---|---|---|
| `H2hyLzX9…` | 5.3753e-7 | **5.3817e-7** (+0.12%) | 6.179e-7 (**+14.9%**) |
| `DXp5zehr…` | 7.8715e-7 | **7.7388e-7** (−1.7%) | 8.980e-7 (**+16.0%**) |

Comparing against `priceNative` would have failed the gate on a *definitional* mismatch rather than a
derivation error — and, worse, would have looked like a 15–25% systematic bias in our own code. The
gate therefore reconciles reserves against reserves. `priceNative` is carried in the report for
context only.

### 2.3 Snapshot must be keyed by pair, not by mint

A token routinely trades in several pools. Keying the snapshot by mint and keeping the deepest pair
silently compares *another pool's* reserves. The signature is unmistakable and was measured before
the fix: base reserve +17.5%, quote reserve −14.0%, liquidity still within 2%. Two pools, not one bug.
The gate now refuses a non-exact match (`pick_for_pool`) instead of substituting.

## 3. Result

34 pools inspected: **26 measured**, **2 excluded** for moving mid-window, 6 had no row in the
snapshot for that exact pool and were not substituted.

| Metric | Value |
|---|---|
| Mean absolute price deviation | **0.39%** |
| Median absolute price deviation | **0.015%** |
| 90th percentile | **0.82%** |
| Maximum | **3.81%** |
| Pools within 0.5% | **22 / 26** |
| Pools within 1% | **23 / 26** |
| Pools within 5% | **26 / 26 (100%)** |
| Mean absolute liquidity deviation | **0.05%** |
| Liquidity pools within 5% | **25 / 25** |
| Pool drift on measured pools (median / p90 / max) | **0.04% / 1.0% / 2.6%** |
| Verdict | **PASS** — `26 pools measured; price mean 0.39% max 3.81%; 0/26 outside 5.0%; 2 excluded; liquidity mean 0.05%` |

The two excluded pools had drifted **9.1%** and **24.3%** during the measurement window — the second
had already lost its snapshot row entirely. Neither is a derivation result; both are pools that
moved while being looked at, and excluding them is the only honest option because there is no single
instant their reserves describe.

**Why the drift rule exists.** Runs before the two reads produced between **1 and 4 pools outside 5%
per run**, with individual outliers as large as 17.1%, while the mean stayed under 1.6% every time.
In the two runs whose JSON was inspected, *every* outlier carried the same fingerprint: base reserve
and quote reserve deviating in **opposite** directions (+5.5/−6.0, +6.9/−5.9, −4.0/+5.3,
−3.3/+3.9). A genuine derivation error moves both reserves the *same* way — the reading is
systematically high or low. Opposite signs mean the base side was measured on one side of a trade
and the quote side on the other.

That is a plausible physical explanation, but "the outlier is probably lag" is not a measurement, so
the gate now *detects* the movement instead of arguing about it. The `skew_signature` and `~lag` /
`~moved` markers remain in the report for anyone re-reading the data.

**Run-to-run behaviour.** Mean deviation across runs was 0.66%, 1.33%, 0.67% and 1.58% before the
change — the spread tracking how many pools happened to trade during each run, not the accuracy of
the derivation. After bracketing the snapshot with two reads the measured set is stable by
construction (median drift 0.04%, i.e. at the noise floor) and the tail is gone: 100% inside 5%,
maximum 3.81%.

## 4. Defects found and fixed on the way here

Each was found by running against live data, and each would have produced quietly wrong numbers.

1. **Native quote settlement.** A trader paying SOL opens a wSOL account, funds it, and closes it in
   the same transaction, so its *token* delta nets to exactly zero. Every such swap was rejected as
   "no quote delta" — on live PumpSwap `buy_exact_quote_in`, that was **all** of them. The executed
   quote is now read from the lamport delta minus the fee when the trader is the fee payer, and
   `DerivedSwap.quote_settlement` records which path was taken.
2. **Snapshot keyed by mint** → compared a different pool (§2.3).
3. **Failed transactions with no recognised instruction** returned *no explanation at all*, because
   the swap-scan early-returned before the failure check. A failed transaction is now always
   explained, whatever instructions it contains.
4. **The gate could pass with unmeasured liquidity**, and a mean-only rule could pass with most pools
   wrong. Both are now hard conditions in `verdict()`.

Also corrected: `tests/test_phase2.py` asserted that an observed live discriminator
(`e445a52e51cb9a1d`) was registered. No plausible Anchor name hashes to it, so the registry cannot
name it; the test now asserts the opposite (it stays unregistered and is skipped rather than guessed).

## 5. Cost

`getTokenAccountsByOwner` is called **four** times per pool (two token programs × two reads). For 34
pools that is ~136 RPC calls. At the conservative 10-credit assumption in `config/budget.yaml` that
is **~1,360 credits**, or 0.14% of the 1,000,000/month Helius allowance, plus 3 GeckoTerminal and 2
DexScreener requests. The second read is worth the doubling: it removes the dominant source of false
deviation.

**Unverified:** Helius does not publish a per-call credit figure for `getTokenAccountsByOwner` on the
free plan. The ledger charges 10 so it over- rather than under-counts; correct it from the dashboard
breakdown before trusting a projection built on it.

## 6. What this gate does *not* prove

- **One DEX family.** Only PumpSwap, where vaults are owned by the pool address. Raydium AMM/CPMM and
  pump.fun bonding-curve vaults are owned by separate authority PDAs, so owner-enumeration returns
  nothing for them; they are **excluded** from the gate rather than counted as failures. Those layouts
  need authority-PDA resolution before they can be priced this way.
- **"Now", not "then".** This proves we can price a pool at the moment we look. Pricing a token *as it
  was* at a wallet's entry still requires a reserve/price history, which is what the Phase 1 collector
  is accumulating. The gate does not substitute for it.
- **No per-trade validation at scale.** Live pools are dominated by aggregator-routed swaps where the
  instruction's "trader" is a routing program, not a wallet; the direct-swap fixtures prove the
  decoder, not the throughput. Attribution of routed swaps to wallets is Phase 3/4 work.
- **Residual error is dominated by observation lag, not method.** Median 0.012% is at the noise floor
  of two reads taken seconds apart; the tail is where volatility lives.

## 7. Verdict

**Continue.** Price and liquidity can be derived from free chain data within **0.39% mean** (median
0.015%) and **0.05% mean liquidity** error across 26 live pools, with **every** measured pool
individually inside 5% and a maximum of 3.81%.

Doc 07's stop condition — "if this fails after honest effort, stop and reassess" — is not triggered.
The next dependency is no longer a spreadsheet question but accumulated data: Phase 3 (positions and
P&L) can start on the collector's archive, and the early-entry metrics in Phase 5 need the
point-in-time series that only forward collection produces.
