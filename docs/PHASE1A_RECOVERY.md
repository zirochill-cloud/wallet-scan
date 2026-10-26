# PHASE 1A RECOVERY — what is still known about the lost design

**Purpose.** The Phase 1a session logs are gone (provider resource exhaustion; not
recoverable, confirmed 2026-10-26). The *design* was worked out across many turns of
this conversation and would otherwise be lost to context pruning. This document
captures it.

**What this document is not.** It is not a specification to be implemented, and it is
not a source of code. Every section carries an explicit status:

| Mark | Meaning |
|---|---|
| **LOST** | Implementation unrecoverable. Design recorded here for reference only. |
| **VERIFIED** | Confirmed against a live system or measurement in a prior session. |
| **MEASURED** | A number obtained by measurement. |
| **PROPOSED** | A design that was never implemented and never tested. |
| **REQUIREMENT** | A condition that must hold; not yet demonstrated. |
| **ESTIMATED** | An aggregate inferred from measurements, not itself measured. |

**Standing instruction:** do not reconstruct implementation from memory, and do not
port any rule below without re-validating it against the current codebase. The audit
that established why this was needed is in `NOTES.md` § `PHASE1A_AUDIT`: **`solscope/`
is a rewrite; the Phase 1a architecture is absent from this repository in every form.**

Verification of the *current-code* claims in §2 and §7 against `solscope/` was performed
2026-10-26 while writing this file. Those checks are marked **[checked today]**.

---

## §1 — Prior schema (for reference, not for porting)

**Status: LOST — design only. No DDL survived.**

The six Phase 1a tables and their purpose:

| Table | Purpose |
|---|---|
| `swap_events` | Wallet swaps, **mint-delta representation** — one row per swap expressed as mint balance deltas rather than as pool reserves. |
| `wallet_token_positions` | Reconstructed positions with a **closed flag** — the output of position reconstruction over a wallet's history. |
| `swap_hops` | **Multi-hop denormalization**: one row per *(swap, pool)*, so a single routed swap participating in several pools is decomposed rather than collapsed. |
| `pool_cache` | **mint → pool resolution cache**, so re-resolving a mint does not cost another scan. |
| `pool_candidates` | **Diagnostics for ambiguous resolutions** — records the candidates when a mint resolves to more than one plausible pool, so an ambiguous pick is auditable rather than silent. |
| `mint_pools` | A **view** aggregating `swap_hops` by mint, giving every pool a mint has traded through. |

**Not portable.** Current `solscope/` solves the same problems differently, and the
differences are structural, not cosmetic:

| Prior | Current | Difference |
|---|---|---|
| `swap_events` | `swaps` + `transactions` | Split into a decoded-tx table and a decoded-swap table; price carried as `exec_price_quote` vs `pool_price_quote` (the gap *is* the slippage) rather than implied by deltas alone. |
| `wallet_token_positions` | `positions` | Adds `inventory_origin`, `pnl_completeness`, forward-outcome columns, and a partial unique index enforcing one open position per wallet+token. |
| `swap_hops` | *(no table)* | Multi-hop is handled by flattening inner instructions into swap candidates in `decode.py`; there is no per-hop row. |
| `pool_cache` | `pools` | The registry itself carries the resolution; no separate cache table. |
| `pool_candidates` | *(no table)* | The Python `PoolCandidate` dataclass in `solscope/adapters/` is a *discovery* candidate (a pool to consider subscribing to), not an ambiguity diagnostic. Same name, different concept — do not conflate. |
| `mint_pools` | *(no table)* | Not represented; would be a query over `swaps`/`pools` today. |
| `wallet_quality_filter` | *(nothing)* | Absent in every form. Greenfield — see `NOTES.md` § `PHASE1A_AUDIT`. |

**Why this section exists if it is not for porting:** it is the only surviving record
of *what problem each table solved*. When Phase 1b hits the ambiguity problem or the
multi-hop problem, this says what was already learned about the shape of a solution —
including that `swap_hops` existed precisely because collapsing a routed swap into one
pool silently mis-attributes liquidity.

---

## §2 — Mint-based route

**Status: VERIFIED 2026-10-15 (prior session). Not re-verified against `solscope/`.
Must be re-verified when Phase 1b implements this.**

**Query the mint, not the pool.**

- A mint's **first transaction is mint creation** — `create_v2` / `initialize_mint2`.
- **Token age** is computed from that transaction's `blockTime`.
- **Price series** comes from swap ratios: `sol_delta / abs(amount_delta)`.

Why the mint rather than the pool: the pool registry only knows pools someone has
already shortlisted. A launch the collector never saw has no registry entry, but it
always has a mint, and the mint's own transaction history begins at creation. That
makes genesis determinable for any token, observed or not.

**[checked today] Where this stands in current code:**

- **Genesis detection: entirely unimplemented.** The strings `create_v2`,
  `initialize_mint2`, `getTokenSupply` and `mint_authority` appear **zero times** in
  `solscope/`, `tests/`, and `docs/`. There is no mint-creation decoder of any kind.
- **Age: partially present but computed differently.** `swaps.token_age_seconds` is
  documented in the schema as `event_time - pool.created_at` — i.e. **pool age, not
  mint age**. For an established pool these differ, and for a mint whose token
  graduated after early trading they can differ substantially. Phase 1b must not reuse
  this column as mint age without changing its definition.
- **Fetch primitives: reusable as-is.** `Deriver.fetch_signatures` (`derive.py:236`)
  already takes an arbitrary `address` and returns signature strings; it happens to be
  named for pools but does the RPC call a mint seed needs.
  `Deriver.fetch_transaction` (`derive.py:247`) archives the payload verbatim before
  decoding, which is exactly the genesis path's requirement.
- **Price from swap ratios: available per-transaction.** `Deriver.pool_spot_from_tx`
  (`derive.py:461`) reads balance deltas out of a transaction to get the spot price of
  the pool involved. That is the per-tick source for a price series; the prior
  session's `sol_delta / abs(amount_delta)` formula is the same idea expressed over
  mint deltas instead of pool deltas.
- **Pool resolution: the real gap.** `Deriver.known_pools` (`derive.py:225`) reads the
  `pools` registry, which is empty in production. A mint seed has no registry entry, so
  the mint → pool resolution that `pool_cache`/`mint_pools` used to serve must be
  rebuilt — the prior design's answer, not merely ported.

**Re-verification requirement:** when Phase 1b builds this, re-verify against the
current `derive.py` that (a) genesis is identified from the mint's oldest transaction
rather than assumed, (b) age is mint age and not `pool.created_at`, and (c) the
price series survives an empty `pools` registry.

---

## §3 — Quality filter findings and the genesis-burst rule

**Status: MEASURED (44/45) 2026-10-17. PROPOSED (genesis-burst rule). The three prior
heuristics were never tested. No filter code existed at any point.**

### The measurement

**44/45 false negatives** on a known-bundled wallet set (2026-10-17).

A filter intended to reject coordinated or bundled launches admitted 44 of 45 wallets
known to be bundled. On that sample it had essentially no discriminating power, and any
discovery built on it inherits that blindness. Recorded in `NOTES.md` §
`ANCHOR_SELECTION_CRITERIA`.

### Failure mode

**Same-deployer, same-second bundles.** The wallets arrived together from one actor
inside a single second — the filter's inputs were individual-wallet properties, and
the bundle's signature was a *relationship between* wallets that no per-wallet test
could see.

### Proposed genesis-burst rule

**Status: PROPOSED — never implemented, never tested.**

> If a wallet's **first buy** for **≥N anchors** occurs within **Y seconds** of each
> anchor's **genesis**, reject the wallet.

Suggested defaults: **N = 2, Y = 60.**

Why this targets the measured failure mode: it is a *cross-anchor* test. A wallet that
shows up at the first buy of several independent launches, always inside the burst
window, is exhibiting exactly the pattern the 44/45 sample displayed — and no
single-anchor property captures it. It is cheap: for each candidate wallet, compare the
timestamp of its first buy in each anchor against that anchor's genesis, and count
anchors within `Y`.

Open questions a validation run must answer: what `N`/`Y` separates the bundled set
from organic early buyers (a reflexive buyer legitimately enters several launches
early); whether `N=2` over-fires on genuinely fast-moving wallets; and what the
false-positive rate is on a *known-clean* set, which was never measured.

### The prior filter's three heuristics

**Status: untested — do not port without re-validation.**

| Heuristic | Purpose it was meant to serve |
|---|---|
| `sandwich` | Detect sandwich-style entry/exit around a victim trade. |
| `throughput` | Detect implausible trade rate for a human actor. |
| `router_only` | Detect a wallet whose activity is only routing, never directional. |

Only the names survive. No thresholds, no formulas, no sample results. They are listed
so Phase 1b knows these were considered, **not** so they are re-implemented as if they
had worked — the measurement says the filter they belonged to rejected 44 of 45.

---

## §4 — Anchor criteria

**Status: REQUIREMENT. Not yet satisfied by any anchor set.**

Independent anchors require all three:

1. **Deployer diversity** — no two anchors share a deployer, and the deployer is not
   among another anchor's first-20 buyers.
2. **Temporal diversity** — no two anchors within 24 hours of each other.
3. **Timestamp spread** — at least one anchor whose first-20 buyers span more than
   5 minutes.

**What the rejected set did.** The 2026-09-14/15 anchor set failed all three: one
deployer launched **3 of 5**, all within **26 hours**, and buyer transactions landed
within **37 seconds** of genesis.

**Why each rule exists.** Requirement 1 kills the case where several "independent"
successes are one actor — a cluster behaving as one entity is one data point, not many.
Requirement 2 stops a single market regime from supplying the whole sample, so
"repeatable performance" is not one afternoon's trend. Requirement 3 is the statistical
enabler: a first-buy set compressed into 37 seconds has no observable ordering to
reason about, so *rank at entry* — one of the scoring inputs — cannot be computed with
any confidence. Without spread, buyer rank is a tie among near-simultaneous
transactions.

**State of the anchor set: empty.** The prior set was rejected as non-independent, and
no new anchors have been supplied. This is the binding constraint on Phase 1b as a
whole, which is why `PHASE1B_SCOPING.md` builds the pipeline against mocked anchors
first.

---

## §5 — Scoring weight change

**Status: MEASURED 2026-10-15. The distribution was over the prior seed set, which is
gone. Direction of the change is settled; specific weights must be revisited if the
retrospective anchor set's age distribution differs.**

### Phase 1b's Early-Entry Ability scoring

| Input | Weight |
|---|---:|
| `max_subsequent_return` | **0.40** |
| `buyer_rank_at_entry` | **0.30** — *replaces* `token_age_at_entry` |
| `pct_entries_before_volume_spike` | **0.30** |
| `mc_at_entry` | **0.00** — deferred: **no historical supply source exists** |

`token_age_at_entry` is **retained as a diagnostic, not scored.**

### The justification — Q7 distribution

**Median age 1 day · IQR 2 days · 86% of mints under 7 days.**

When almost every mint is younger than a week and the median is a single day, age has
almost no dynamic range left to discriminate on: a half-life curve spreads a
population that is already compressed at zero. Worse, in a population that young, an
age score mostly rewards *slightly older* tokens — the opposite of the stated
objective. Ranking by buyer position and by whether entry preceded the volume spike
uses the same information without that compression, and both remain meaningful when
everything is new.

`mc_at_entry` goes to 0.00 for a different and harder reason: market cap at entry needs
**circulating supply at that historical instant**, and no free source provides it. The
current schema's `positions.entry_mcap_usd` exists, but `mcap_usd` is documented as
`price * circulating supply, as of this slot` — supply that itself must come from
`token_supply_events`, which no writer populates. Scoring on a column that cannot be
populated point-in-time would make early-entry a function of data availability. The
honest move is to score it at zero and say so, rather than to impute supply.

**[checked today] The shipped config contradicts this.** `config/scoring.yaml` still
encodes the superseded weighting:

```yaml
early_entry:
  weight_age: 0.45        #  <- should be buyer_rank_at_entry 0.30
  weight_mcap: 0.35       #  <- should be 0.00 (no historical supply source)
  weight_prevolume: 0.20  #  <- should be pct_entries_before_volume_spike 0.30
```

There is no `max_subsequent_return` term anywhere in `scoring.yaml`, so the largest
weight in §5 (0.40) has no counterpart at all. `config/scoring.yaml` is **not frozen**
(`frozen: false`), so this is a live inconsistency rather than a frozen-weights
violation — but it must be corrected *before* any validation run, because the freeze
rule says weights changed after seeing validation results invalidate the validation.

**One double-counting risk to settle before that edit:** `max_subsequent_return` in
`early_entry` and `token_selection.hit_multiple: 3.0` ("token reached ≥ 3× entry mcap
within the window") are the same underlying signal — forward appreciation. The
requirements forbid double-counting correlated indicators, and
`metric_definitions.owning_subscore` exists precisely to enforce single ownership.
Whichever subscore owns forward appreciation must own it once. This is unresolved and
is listed as a thin spot below.

---

## §6 — Cost model

**Status: MEASURED per case; ESTIMATED for aggregate. Re-measure under `solscope/'s`
primitives before committing any budget to it.**

| Operation | Cost | Mark |
|---|---:|---|
| Wallet profiling | **~40 credits** each | MEASURED per case |
| Mint-based genesis discovery | **~70 credits/mint** for a typical young mint | MEASURED per case |
| Full price-series reconstruction | **~200** (low activity) → **~6,000** (high activity; e.g. PAPER at 60k txs) credits/mint | MEASURED per case, extreme range |
| Full retrospective set | **~5,000 credits** | ESTIMATED aggregate |
| DexScreener screening, if anchors cannot be supplied | **~100,000–200,000 credits**, one-time | ESTIMATED aggregate — **units disputed, see below** |

### Why the spread is four orders of magnitude

Price-series cost scales with transaction count, and transaction counts across mints
are not remotely comparable: a quiet mint costs ~200 credits to reconstruct, an active
one costs ~6,000 — a **30× spread** on the same operation. Under Helius Free's
1,000,000 credits/month, a full retrospective set at ~5,000 credits is **0.5% of the
monthly cap**, while a single PAPER-class mint costs more than an entire quiet set.
Cost is therefore driven almost entirely by *which mints are chosen*, not by how many.

### Re-measurement targets under current primitives

The prior figures were measured under a prior session's code. `solscope/` charges
through `config/budget.yaml`, and two current assumptions differ enough to move these
numbers:

1. **`get_signatures_for_address` = 1 credit, `get_transaction` = 1 credit** — matches
   the prior model. A ~40-credit wallet profile and ~70-credit genesis discovery are
   arithmetically consistent with this: one signature page plus one `getTransaction` per
   relevant signature.
2. **`get_token_accounts_by_owner` = 10 credits, unverified.** This is a deliberately
   conservative assumption in `budget.yaml`, not a published figure. `vault_reserves`
   calls it **twice per pool** (Token + Token-2022), so a pool-reserve read is booked at
   ~20 credits. Any prior measurement that used owner enumeration inherits this
   assumption and must be re-run once the real per-method cost is read from the
   dashboard.

### The DexScreener figure's units are wrong or mislabeled

**[checked today]** `config/budget.yaml` defines `dexscreener` with
`monthly_cap_requests: 300000` and `operation_costs: {token_profiles: 1, token_pairs: 1}`
— **requests, not credits.** DexScreener is keyless and rate-limited; it does not spend
Helius credits. GeckoTerminal is the same (`new_pools: 1`, cap 40,000 requests).

So "~100,000–200,000 **credits** for DexScreener screening" cannot be DexScreener
calls. Either (a) the figure is Helius credits spent fetching transactions *after*
DexScreener narrows the candidate set — in which case it is a Helius cost and belongs
against the 1,000,000 cap — or (b) it is request volume mislabeled as credits, in which
case it sits against the 300,000-request cap. The distinction changes which budget the
screening consumes and whether it is affordable in one month. **Unresolved; see thin
spots.**

---

## §7 — Protocol and data findings

**Status: VERIFIED per case in the prior session. Each item checked against current
`decode.py` / `derive.py` on 2026-10-26 [checked today].**

### Settlement and provider-schema findings

| Finding | Prior status | **[checked today]** against `solscope/` |
|---|---|---|
| pump.fun SOL arrives in `nativeTransfers` (lamports, field `amount`) | VERIFIED | **Concept present, field name not used.** No code in `solscope/` reads `nativeTransfers` or `tokenTransfers` by name. `decode.py` instead exposes `quote_settlement: 'token' \| 'native'` with a `reason` note and derives settlement from balance deltas — tested (`test_phase2` asserts a native-settled swap's quote equals lamport delta minus fee: `1,500,000 - 5,000`). Equivalent conclusion, different mechanism; the provider-field rule is unasserted. |
| All other DEXes: wSOL in `tokenTransfers` | VERIFIED | Consistent — PumpSwap is registered with `quote_is_native=False`, while pump.fun bonding curve and LaunchLab are `quote_is_native=True`. |
| Helius **v0 vs v1** schema differs: `tokenAmount` float vs `rawTokenAmount` + `decimals` int | VERIFIED | **No version handling exists.** `decode.py:286/299/301` and `derive.py:474/482/490` read `uiTokenAmount.amount`; `derive.py:530` reads parsed-account `info["tokenAmount"]`. There is no branch on `rawTokenAmount` and no response-version check. If Helius serves v1 for any of these paths, the read silently mis-parses. **Real gap — see thin spots.** |

### Program IDs — verified against `decode.py`

| Program | ID | **[checked today]** |
|---|---|---|
| PumpSwap AMM | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | **In `PROGRAMS`** — dex `pumpswap`; trades `buy`/`sell`/`buy_v2`/`sell_v2`/`buy_exact_quote_in`/`buy_exact_quote_in_v2`; `pool_index=0`, `trader_index=1`. **Gate-verified.** |
| pump.fun bonding curve | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | **In `PROGRAMS`** — dex `pump`; `buy`/`sell`/`buy_v2`/`sell_v2`/`buy_v3`/`sell_v3`; `pool_index=3`, `trader_index=6`, `quote_is_native=True`. **Matches §7.** |
| Raydium AMM v4 | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | **In `PROGRAMS`** — not Anchor; numeric first-byte tags 9/11 (v1) and 16/17 (v2); `pool_index=1`, `trader_index=17`. **Matches §7.** Registry entry exists but is **not validated on live fixtures** (doc 11 §8). |
| Orca Whirlpool | `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | **In `UNVERIFIED_PROGRAMS`** — deliberately not decoded; skips are named so they are explainable. **Matches doc 11: "explicitly not decoded (abstains)."** Also appears incidentally in 2 test fixtures. |
| Jupiter v6 | `JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4` | **Absent from `solscope/` entirely** — not in `PROGRAMS`, not in `UNVERIFIED_PROGRAMS`. It appears only incidentally in 4 test fixtures as a program id in the account list. So §7's verified ID has **no home in the decoder**; aggregator attribution is still open, consistent with doc 11 §8. |
| Meteora LbPair discriminator | `210b3162b565b10d` | **0 occurrences** in `decode.py`. Not registered. |
| `tessera_v` — **not Meteora-owned** | discriminator `61ef4409e1dd7b18` | **0 occurrences** in `decode.py`. The negative finding (it is *not* Meteora) is recorded nowhere in current code. |

**Additional registry entries current code has that §7 does not mention** — noted so
the two lists are not mistaken for inconsistent:

- Raydium CPMM `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` — in `PROGRAMS`
  (`swap_base_input` / `swap_base_output`, discriminator bytes independently verified).
- Raydium LaunchLab `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` — in `PROGRAMS`.
- Two Meteora program ids in `UNVERIFIED_PROGRAMS` that §7 does not list:
  `LMMJ8mwJZrFesCVYPsfkLKGvWqHhE9UbuNqtvJzCwrL` and
  `Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB` (both labelled `meteora_dlmm`).
- A discriminator observed live but **deliberately left unregistered** so decoding
  fails closed: `e445a52e51cb9a1d`, asserted in `test_phase2` as
  "unresolvable live discriminator stays unregistered (fail closed)".

---

## §8 — Cost/quality tension (the anchor-discovery finding)

**Status: MEASURED 2026-10-17. Informs anchor-selection strategy; it is not a
shortcut.**

Cheap-first anchor screening is **anti-correlated with the target population.**

| Observation | Consequence |
|---|---|
| **High-velocity tokens are expensive to reconstruct** | The tokens worth using as anchors — the ones with real trading to rank buyers against — are precisely the ones at the top of the price-series cost curve (§6: up to ~6,000 credits each). |
| **Low-cost tokens are often barely traded** | SHIB: **5,200 transactions but only 15 usable swaps.** Reconstruction is cheap because there is almost nothing to reconstruct. |
| **The cheap slice is systematically the wrong slice** | Cost-minimising anchor selection does not sample the target population — it samples the residue. |

**Why this is the load-bearing finding for Phase 1b sequencing.** The natural cost
reflex — "screen cheaply, reconstruct the affordable ones" — inverts the objective. A
filter run over low-cost mints will select tokens whose buyer sets carry almost no
information about *ranking* (15 usable swaps cannot separate a skilful early buyer from
a bystander), while the tokens that could distinguish them sit at 30× the cost. Any
screening budget is therefore allocated against *independence and information content*
(§4), with cost as a constraint to plan around, never as a selection criterion.

The practical reading: the ~5,000-credit full retrospective set and the
100,000–200,000-credit screening estimate are not alternatives where one is "the cheap
option". Screening is expensive *because* cheap screening is worthless.

---

## §9 — What is genuinely lost

**Status: LOST. Not to be recreated speculatively.**

**Lost — unrecoverable:**

- **Implementation specifics:** exact code, test cases, tuned parameters.
- **The prior `NOTES.md` § `SCHEMA` section**, which captured `swap_hops` verbatim
  (column-level DDL). It did not survive the workspace move; §1 above is a summary of
  *purpose*, not a schema.
- **The 44/45 bundled wallet set** and the 2026-09-14/15 anchor set — identities of the
  wallets. Only their aggregate behaviour survives (§3, §4).
- **The Q7 seed set** behind the age distribution (§5). Its direction survives; its
  sample does not.
- **Per-measurement provenance** for the §6 numbers — what was counted, over which
  sample, on which day, beyond the dates given.

**Recoverable — captured above:**

- The design of the six prior tables and the problem each solved (§1).
- The mint-based route's logic and why it queries the mint rather than the pool (§2).
- The quality-filter measurement, its failure mode, and the proposed genesis-burst rule
  with defaults (§3).
- The three anchor-independence requirements and the rejected set's exact violations (§4).
- The early-entry weight change and the distribution that justified it (§5).
- The cost figures, their units, and which of them are measured (§6).
- The protocol findings and program IDs, each re-checked against current code (§7).
- The cost/quality inversion and why it constrains sequencing (§8).

**Instruction:** do not attempt to reconstruct implementation from memory. §1's tables
are recorded so that a future implementer knows what problem to solve — not what DDL to
write. Where this document says **PROPOSED**, nothing was ever built, and a proposal
that has never met data must be validated before it is trusted, exactly as §3's filter
was measured and found to reject 44 of 45.

---

## See also

- `NOTES.md` § `PHASE1A_AUDIT` — the audit establishing that Phase 1a is absent from
  this repository, with evidence.
- `NOTES.md` § `ANCHOR_SELECTION_CRITERIA` — the open Phase 1b blocker.
- `NOTES.md` § `DATA_LOSS_2026_10` — quantified cost of what was lost.
- `docs/PHASE1B_SCOPING.md` — how Phase 1b is built on the *current* codebase.
- `docs/INVENTORY.md` — status of every document and artifact.
