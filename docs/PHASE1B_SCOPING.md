# PHASE 1B SCOPING — building on the current `solscope/`

**Status: scoping document. Not code. Nothing here is implemented.**

How Phase 1b (retrospective anchor discovery → quality filter → early-entry scoring) would
be built on the codebase **as it exists today**, informed by `docs/PHASE1A_RECOVERY.md`.

Every claim about current code was checked against the working tree on **2026-10-26**;
line references are to that tree. Numbers marked **ESTIMATED** are arithmetic over measured
per-case figures, not measurements — they carry the same warning `PHASE1A_RECOVERY.md` §6
carries.

**Standing context.** `solscope/` is a rewrite; Phase 1a is absent from this repository in
every form (`NOTES.md` § `PHASE1A_AUDIT`). So Phase 1b is built on the current primitives,
and the recovery document supplies *what was learned*, never *what to port*.

**Constraint that shapes everything below: the anchor set is currently empty.** The prior
set was rejected as non-independent (`PHASE1A_RECOVERY.md` §4) and no replacement has been
supplied. That is why Q7 proposes building the whole pipeline against **mocked** anchors —
anchor supply must be the last blocker, not the first.

---

## Q1 — Where does Phase 1b's data acquisition come from?

### Used directly, unchanged

| Primitive | Location | Why it already fits |
|---|---|---|
| Helius `signatures_for_address` | `adapters/helius.py:249` | Takes an arbitrary address and returns signatures. Nothing about it is pool-specific; a **mint seed works with no change to the call**. |
| Helius `transaction(sig)` | `adapters/helius.py:277` | Fetches any signature; returns `(result, body)`. |
| `Deriver.fetch_transaction` | `derive.py:247` | **Archives the payload verbatim before decoding**, then returns it. That is exactly the genesis path's requirement: the raw bytes land on disk before anything interprets them, so a decoder bug is recoverable. |
| `Deriver.already_decoded` | `derive.py:258` | `SELECT 1 FROM transactions WHERE signature = %s`. This is what makes a re-run free — and it is why Q5's cost figures come down sharply once a mint is reconstructed once. |
| `Deriver.persist` | `derive.py:267` | Inserts `chains`/`tokens`/`wallets`/`pools`/`transactions`/`swaps`, **registering wallets lazily** on first sight. Anchor-derived wallets are unknown wallets, so lazy registration is precisely the needed behaviour. |
| `decode.py` (whole module) | `decode.py` | Real swap decoding across 5 programs. Phase 1b needs swap facts; this produces them. |
| `Deriver.pool_spot_from_tx` | `derive.py:461` | Per-transaction spot price from balance deltas. The per-tick source for a price series. |
| Credit ledger + `budget.yaml` | `ledger.py`, `config/budget.yaml` | Hard monthly cap, pre-flight costing, fail-closed on ledger error. Phase 1b inherits cost safety rather than re-inventing it. |

### Adapted — reused primitives that need real behavioural change

**`Deriver.fetch_signatures(pool, *, limit=50)` — `derive.py:236`.**
The RPC call inside is address-generic, so calling it with a mint returns the mint's
signatures. Two changes are required, and the second is not cosmetic:

1. **Naming and parameter semantics.** The parameter is `pool`; a mint seed is the same type
   but a different meaning, so the shared primitive must be expressed generically (or given a
   mint-seeded sibling) rather than silently overloaded.
2. **Direction — this is the substantive change.** The pool use case wants the **newest**
   signatures (sample recent swaps). The genesis use case must page to the **oldest** page
   using the `before` cursor until signatures are exhausted, because mint creation is the
   mint's *first* transaction. `getSignaturesForAddress` returns newest-first, so genesis
   discovery is *reverse pagination* — new behaviour on a reused primitive, and the place
   where an off-by-one silently reports "no genesis" for an old mint.

**`Deriver.known_pools()` — `derive.py:225`, and `Deriver.process_pool(pool, *, limit=30)` — `derive.py:349`.**
`process_pool` is the driver loop, but it binds a pool to `(base_mint, quote_mint)` by
looking the pool up in the `pools` registry. **A mint seed has no registry entry** — the
registry is empty in production and by construction holds nothing for a launch the collector
never saw. So the mint route needs a driver that discovers the pool(s) from the mint rather
than assuming it. This is the void `pool_cache` and `mint_pools` used to fill
(`PHASE1A_RECOVERY.md` §1), and it is **new work, not a port**.

### New code required

| # | Piece | Evidence it does not exist |
|---|---|---|
| 1 | **Mint genesis decoder** — `create_v2` / `initialize_mint2` → genesis slot + `blockTime` | The strings `create_v2`, `initialize_mint2`, `getTokenSupply`, `mint_authority` appear **zero times** in `solscope/`, `tests/`, and `docs/`. There is no mint-creation decoder of any kind. **Largest new piece.** |
| 2 | **Mint age** distinct from pool age | `swaps.token_age_seconds` is documented as `event_time - pool.created_at` — **pool age**. For a mint that traded before graduating, the two differ. Must not be reused as mint age without redefining it. |
| 3 | **mint → pool resolution** with an empty `pools` registry | `known_pools()` reads `pools`; nothing resolves mint→pool. Successor to `pool_cache`; the ambiguity diagnostic is the successor to `pool_candidates`. |
| 4 | **Price series over a mint's own history** | `pool_spot_from_tx` is per-pool-per-transaction. A *mint* series must be assembled across every pool the mint traded through — this is exactly the problem `swap_hops` solved by denormalizing one row per (swap, pool). |
| 5 | **Wallet-history direction** | `fetch_signatures(wallet)` + first-buy-per-(wallet, anchor). Note `swaps_wallet_token_idx ON swaps (trader, base_mint, event_time)` is documented in the schema as *"the query every early-entry calculation makes: first buy per wallet per token"* — the index for this already exists; the reader does not. |
| 6 | **Helius v0/v1 defensive parsing** | `decode.py:286/299/301` and `derive.py:474/482/490` read `uiTokenAmount.amount`; `derive.py:530` reads parsed `info["tokenAmount"]`. There is **no branch on `rawTokenAmount`** and no response-version check. Real gap (`PHASE1A_RECOVERY.md` §7). |
| 7 | **Anchor registry storage** | `anchor`, `genesis`, `first_buy`, `early_buy`, `deployer` — **zero occurrences** in `schema/001_init.sql`. See Q2. |

---

## Q2 — Where do anchor-derived positions live?

### Positions: the existing tables suffice

`swaps`, `positions` and `pools` are enough for the position *facts*, and `positions`
already carries more of what §5 needs than expected:

`entry_price_usd`, `entry_mcap_usd`, `entry_liquidity_usd`, `entry_token_age_seconds`,
`entry_slot`, `max_price_1h/6h/24h/7d/30d_usd`, `max_multiple_30d`, `max_drawdown_pct`,
`realized_capture`, `exit_impact_bps`, `inventory_origin`, `pnl_completeness`,
`pnl_limitation`.

That maps almost directly onto §5's inputs — `max_subsequent_return` ← `max_multiple_30d`
(or the `max_price_*` columns against `entry_price_usd`), and age is present as
`entry_token_age_seconds`.

### Invariants that already hold

| Invariant | Where |
|---|---|
| **Two clocks** — `event_time` + `ingest_time` | On `tokens`, `pools`, `transactions`, `swaps`, `token_transfers`. Point-in-time safety is structural, not conventional. |
| **`inventory_origin`** ∈ `{swap_only, includes_transfer_in, includes_airdrop}` | `positions`. Transfers cannot become cost basis. |
| **Transfers are not purchases** | `lot_matches` links only `buy_swap_id` ↔ `sell_swap_id`; there is no schema path by which a transfer acquires cost basis. |
| **One open position per (wallet, token)** | Partial unique index `positions_one_open_per_wallet_token`. |
| **Swap dedupe** | `UNIQUE NULLS NOT DISTINCT (signature, instruction_index, inner_index)`. |
| **No double-counting across sub-scores** | `metric_definitions.owning_subscore` CHECK-constrained to the 8 dimensions. |

### A new table is needed — for anchor provenance, not for positions

The grep is decisive: **`anchor`, `genesis`, `first_buy`, `early_buy` and `deployer` appear
zero times in the schema.** Positions are fine; what has nowhere to live is the anchor side
of the relationship. Without it, four questions are unanswerable:

- Which anchor **set** produced this wallet? (independence de-duplication, §4)
- Which anchor did this wallet enter, and at what **buyer rank**?
- What did the **quality filter** decide, and on what measured values?
- Which wallets were **rejected**, and why?

**Additional invariants Phase 1b requires:**

1. **Genesis is point-in-time.** The genesis `blockTime` must be read once, stored, and
   stamped with `ingest_time`. If age and rank re-read genesis later, a provider correction
   silently changes a historical score — the same look-ahead class the schema's two clocks
   exist to prevent. (`cohort_benchmarks.available_at` is the precedent.)
2. **Buyer rank must come from a *complete* first-N set.** A sampled first-20 makes rank a
   function of sampling, not of behaviour — and rank is 0.30 of the early-entry score (§5).
   The set must be provably complete, or the rank must be flagged not-computed.
3. **Filter verdicts are immutable and versioned**, like `scores`. If a threshold change
   overwrites old verdicts, the 44/45 failure can never be re-measured against a corrected
   filter — which is the entire point of having recorded it.
4. **Anchor → wallet edges must record which anchor**, or §4's deployer/temporal independence
   tests are not computable.
5. **A rejected wallet must be storable.** "Rejected" is an outcome, not an absence.
   Otherwise the false-negative rate — the 44/45 metric — is unmeasurable by construction.

---

## Q3 — Does the quality filter get built fresh?

**Yes, entirely fresh.** No filter code existed at any point
(`PHASE1A_RECOVERY.md` §3).

### What it reads from — the anchor→wallet→swaps chain, not positions alone

Positions are the wrong primary source for the genesis-burst rule.
`positions.entry_token_age_seconds` gives the *delta* between entry and genesis, but the rule
needs to compare a wallet's behaviour **across anchors**, which requires the anchor identity
and the absolute genesis time — neither of which is in `positions`.

So the filter reads:

- **`swaps`** — first buy per (wallet, mint), served by the existing
  `swaps_wallet_token_idx (trader, base_mint, event_time)`.
- **anchor genesis** — per-anchor, point-in-time (Q2 invariant 1).
- **anchor deployer** and each anchor's first-20 buyer set — for §4's independence tests.
- **the wallet's own history shape** — only if the untested heuristics are re-validated.

### What it outputs

For each (wallet, anchor) pair:

| Output | Purpose |
|---|---|
| **verdict** — accept / reject | The decision. |
| **rule id that fired** | Which rule, versioned. A rejection with no rule id is not auditable. |
| **the measured values that triggered it** | The numbers, not a summary — e.g. the burst deltas that crossed `Y`. |
| **rule version** | So a threshold change does not retroactively rewrite history (Q2 invariant 3). |

**The trace is the point.** The prior filter admitted 44 of 45 bundled wallets and left no
record of *why*, which is precisely why that failure was undiagnosable after the fact.

### Genesis-burst as a first-class rule

It gets **its own rule id and its own measurable input** — burst delta per anchor — so its
rejection rate is measurable on both **known-bundled** and **known-clean** sets. That second
measurement is what the prior filter never had, and its absence is why "44/45" was the first
signal anyone saw.

```
genesis-burst (§3, PROPOSED, defaults N=2 Y=60):
  for each wallet w, across anchors a:
      count a where (first_buy(w,a) - genesis(a)) <= Y
  if count >= N: reject
```

**Open, and must be resolved before the rule is trusted:** what `N`/`Y` separate the bundled
set from organic early buyers; whether `N=2` over-fires on legitimately fast wallets; and the
false-positive rate on a *known-clean* set, which was never measured.

**Not to be ported.** `sandwich`, `throughput`, `router_only` remain untested names. They are
listed in `PHASE1A_RECOVERY.md` §3 so Phase 1b knows they were considered — not so they are
re-implemented as if they had worked.

---

## Q4 — What does the scoring consumer read and output?

### Reads

`wallet_metrics` (JSONB metrics with `as_of` + `metric_quality`) and/or `positions`
aggregates, keyed by §5's inputs. `metric_definitions` must register each new metric with its
`owning_subscore` — the CHECK constraint restricts ownership to the 8 dimensions, so
`max_subsequent_return`, `buyer_rank_at_entry` and `pct_entries_before_volume_spike` each
need exactly one owner. That registration is the mechanism that turns "avoid double-counting"
from a promise into a constraint.

**The three definitions now exist as code:** `solscope/metrics.py` carries
`max_subsequent_return`, `buyer_rank_at_entry` and `pct_entries_before_volume_spike` as typed
signatures with complete docstrings. **Bodies are not implemented** (they raise
`NotImplementedError`), so the interface, the output contract and the failure modes are fixed
before any logic is written against them. The corresponding thresholds live under
`early_entry` in `config/scoring.yaml`. Registering the three metrics in
`metric_definitions` is still outstanding — it is a data step, not DDL.

### Writes — **the equivalent table already exists**

**No new table is needed for scores.** `scores` exists in the current schema:

`score_run_id`, `address`, `as_of`, `overall_score`, `confidence_score`,
`confidence_adjusted_score`, `band`, `rank_overall`, `rank_at_percentile`, `recommendation`,
`cluster_id`, `independence_factor`, `gated_by`, `computed_at`,
`UNIQUE (score_run_id, address)`.

Surrounded by `sub_scores` (with `explanation` NOT NULL), `score_evidence`,
`score_sensitivity`, `score_runs` and `score_config_versions` (frozen weights +
`config_sha256`).

**So Q4's answer: the `wallet_scores` equivalent already exists.** What Phase 1b adds is
metric *definitions* and the weight change — not a table.

### Corrected: `config/scoring.yaml` now matches §5 (2026-10-26)

| Was | Now |
|---|---|
| `weight_age: 0.45` | `weight_token_age: 0.00` — retained, **diagnostic only** |
| `weight_mcap: 0.35` | `weight_mcap: 0.00` — retained, **deferred** (no historical supply source) |
| `weight_prevolume: 0.20` | `weight_pct_entries_before_volume_spike: 0.30` |
| *(absent)* | `weight_max_subsequent_return: 0.40` |
| *(absent)* | `weight_buyer_rank_at_entry: 0.30` |

`early_entry` sub-weights sum to 1.00; top-level `weights` still sum to 1.0 (both asserted by
`tests/test_scoring_config.py`, which passes). `version` bumped `1.0.0` → `1.1.0` so the
changed weighting is distinguishable in `score_config_versions.config_sha256` — the freeze
machinery depends on telling two weight sets apart, and `frozen` remains `false`.

The edit was made **before** any validation run, which matters: the freeze rule says weights
changed after seeing results invalidate the validation.

### Double-count — resolved: `early_entry` owns forward appreciation

`max_subsequent_return` (`early_entry`, 0.40) and `token_selection.hit_multiple: 3.0` measure
the **same signal**. Resolution, recorded in `PHASE1A_RECOVERY.md` §5 and in the
`token_selection` block of `config/scoring.yaml`:

- **`hit_multiple` leaves the weighted score** and is retained as a **diagnostic column**.
- `token_selection.weight_hit_rate` → **0.00**; `weight_cohort_excess` → **1.00**, so the
dimension still sums to 1.0 and still scores token *quality* (did the chosen tokens beat
their launch-week peers?) without re-scoring appreciation.

**Why this direction.** `max_subsequent_return` is continuous and measures magnitude;
`hit_multiple ≥ 3.0` is a binary threshold that collapses every outcome above 3× into one
bucket. Scoring both double-rewards appreciation, and the binary half **structurally favours
a wallet with a few large wins over one with consistent mid-sized wins** — the opposite of the
stated objective ("repeatable performance across multiple tokens rather than one lucky
trade", requirements §A).

`metric_definitions.owning_subscore` is where this ownership is enforced at the database
level once the three metrics are registered. The prior session never flagged the overlap; it
was found while scoping Phase 1b, not recovered.

---

## § Aggregation — S5 (implemented 2026-11-05)

Per-anchor metrics become one wallet-level number here. The full specification — the exact
signatures, the rule list and the hand-computed fixtures — is recovered verbatim in
**`docs/RECOVERED_FROM_TRANSCRIPT.md` §7**; this section states the rules the code enforces and
records where the recovered spec had to be reconciled with the schema that actually exists.

### The three reductions

| Metric | Reduction | Defaults |
|---|---|---|
| `max_subsequent_return` | geometric mean of `(1 + winsorised r)`, minus 1, floored at 0 | `winsorize_at=100`, `min_anchors=2` |
| `buyer_rank_at_entry` | median percentile across computed anchors | `min_anchors=2` |
| `pct_entries_before_volume_spike` | mean fraction across computed anchors | `min_anchors=2` |

A geometric mean, not an arithmetic one, because the wallet's outcome across anchors compounds:
three 5x entries (4.0) beat one 99x and two duds (≈3.64). Winsorising at 100 keeps a single
lottery ticket from dominating every comparison it appears in. `buyer_rank_at_entry` uses a median
because rank is an ordering — one 0.001 first-buy should not drag the centre away from the
wallet's ordinary behaviour. `pct_entries_before_volume_spike` is already a bounded fraction, so
the mean is the honest centre.

### `not_rankable` versus `not_computable`

- **`not_computable`** — no anchor produced a number at all (including the empty case). Coverage is
  0.0, not 1.0: nothing was measurable.
- **`not_rankable`** — something was measured, but fewer than `min_anchors` anchors were. The value
  is `NULL` and the counts still report what existed; one anchor is an anecdote, not a ranking.

Neither ever stores 0.0. A missing number must stay distinguishable from a measured bad one — the
same rule the per-anchor writers already follow.

### Weight re-normalisation

When a metric is `not_rankable` or `not_computable`, its weight leaves the `early_entry` blend and
the remaining computed metrics are re-scaled to the dimension's original weight. Scoring a missing
metric as 0 would convert a coverage gap into a behavioural verdict. If every metric in a dimension
is uncomputable, the dimension is uncomputable and the wallet takes the confidence penalty rather
than a score.

### Confidence, not score

Two aggregate-level facts enter `confidence` and never the score itself:
`anchor_sample_size` = `anchor_count_computed`, and `anchor_coverage` = `coverage`. A 4.0 computed
from two anchors out of nine is not the same claim as a 4.0 from nine, and the confidence score is
where that difference belongs. As recorded in §Q4, the exact blend of these two into the
confidence figure remains the consumer's decision.

### Reconciliation against the shipped schema

Three points where the recovered §7 assumed something the database does not have:

1. **Aggregates do not live in `sub_scores`.** §7's writer was specified to write to `sub_scores`
   with columns `(wallet, metric_name, rule_version, value, status, anchor_count_computed,
   anchor_count_excluded, evidence)`. `sub_scores` as built is a *dimension* table: `score_id NOT
   NULL` referencing `scores(id)`, `dimension` CHECKed to the eight scoring dimensions, and
   `UNIQUE (score_id, dimension)`. A wallet aggregate has no score run and no dimension. Storing one
   there would need a fabricated `scores` row and a widened dimension CHECK. `wallet_metrics` is one
   JSONB blob per snapshot, and `anchor_wallet_metrics` is `NOT NULL anchor_id` — per-anchor by
   construction. So `schema/003_wallet_aggregates.sql` adds **`wallet_aggregates`**, keyed
   `(wallet, metric_name, rule_version)`, with `coverage NUMERIC(6,4)` and `evidence JSONB` as an
   array of per-anchor traces.
2. **Status vocabulary.** Aggregates use their own enum, `wallet_aggregate_status`
   (`computed` / `not_rankable` / `not_computable`). It cannot reuse `anchor_metric_status`, whose
   members (`INSUFFICIENT_WINDOW`, `NO_OBSERVATIONS`, `NOT_COMPUTABLE`) describe why *one anchor's*
   window produced nothing — a different question from whether a wallet can be ranked.
3. **The `[-1, 4, 4]` fixture.** §7 records the total-loss case as `0`; the literal
   `geomean(1+r) - 1` gives `-1`. The code returns `0` (the product collapses to 0 and the result is
   floored there) to match the recorded fixture. Flagged here rather than silently chosen: if the
   intent was `-1`, the floor is a one-line change in `_geometric_mean_minus_one`.

**Code:** `solscope/aggregation.py` (reductions, `AggregateResult`, per-anchor trace),
`solscope/anchor_writer.write_aggregate` (idempotent upsert), `tests/test_aggregation.py` (fixtures
above plus DB idempotency), and the fifth suite in `bootstrap_db.sh`.

---

## Q5 — What is the cost model under `solscope/`'s primitives?

`config/budget.yaml` charges: `get_signatures_for_address` **1**, `get_transaction` **1**,
`get_token_accounts_by_owner` **10** (UNVERIFIED conservative assumption), against a
**1,000,000/month** Helius cap.

| Operation | Recovery §6 | Under current primitives | Difference |
|---|---:|---|---|
| Wallet profiling | ~40 credits | **~40 — same.** 1–2 signature pages (1 each) + ~38 `getTransaction` (1 each). | None. **But falls toward 0** when the wallet's swaps were already decoded, because `already_decoded()` skips them. |
| Mint genesis discovery | ~70 credits/mint | **~70 — same.** ~1–2 pages + ~68 txs for a typical young mint. | None in *cost*; **new in *direction*** — reverse pagination to the oldest page. |
| Price-series reconstruction | ~200 → ~6,000/mint | **~200 → ~6,000 — same**, i.e. ~200 to ~6,000 `getTransaction`. | None — **and this is the finding.** See below. |
| Full retrospective set | ~5,000 (ESTIMATED) | **~5,600 ESTIMATED** — 3 anchors × (70 genesis + ~1,000 reconstruction) + 60 wallet profiles × ~40. | Consistent order of magnitude. Re-measure. |
| DexScreener screening | ~100,000–200,000 credits | **Units are wrong — see below.** | **Largest discrepancy.** |

### The finding: the mint route avoids the budget's most expensive primitive

Because the mint-based route derives price **from the swap's own deltas** — the transaction
already fetched — price-series reconstruction costs **0 additional credits beyond the fetch**.
By contrast the reserve-based route calls `vault_reserves` (`derive.py:505`), which issues
**two** RPC calls per pool (Token + Token-2022) at **10 credits each**: **~20 credits per pool
per read**. A series needing 100 reserve reads costs ~2,000 credits — comparable to
reconstructing an entire moderate mint.

So the mint-based approach sidesteps both the most expensive *and* the least verified entry in
`budget.yaml`. That is a cost argument for §2's method, independent of its accuracy argument,
and it should be re-measured rather than assumed.

### The DexScreener figure's units are wrong

`budget.yaml` defines `dexscreener` with `monthly_cap_requests: 300000` and
`operation_costs: {token_profiles: 1, token_pairs: 1}` — **requests, not credits**.
GeckoTerminal likewise (`new_pools: 1`, cap 40,000 requests). DexScreener is keyless and
spends **0 Helius credits**.

So "~100,000–200,000 **credits** for DexScreener screening" cannot be DexScreener calls.
Two readings, and **they hit different caps**:

- **(a)** It is Helius credits spent *reconstructing* the candidates DexScreener surfaces —
  e.g. ~100 candidates × ~1,000 credits ≈ 100,000. Then it belongs against the 1,000,000
  **credit** cap (~10–20% of a month).
- **(b)** It is request volume mislabelled as credits — then it belongs against the 300,000
  **request** cap.

**Unresolved. It must be resolved before any screening budget is committed**, because the two
answers differ by an order of magnitude in what the screening actually consumes.

---

## Q6 — What are the sequencing dependencies?

### Strictly ordered — one hard chain

```
mint genesis decoder  →  mint age  →  buyer rank  →  genesis-burst rule
                                       ↑
                          (rank requires the complete
                           ordered first-N set, which
                           requires genesis-known ordering)
```

**Genesis is the true bottleneck.** Three separate outputs depend on it:
`entry_token_age_seconds`, `buyer_rank_at_entry` (0.30 of §5), and the genesis-burst rule.
Nothing else in Phase 1b is blocked this way.

### Parallelizable — starts immediately, no dependency on the mint route

- **Quality filter (Q3).** Needs only the *shape* of anchor→wallet→swaps. Can be built and
  tested against **mocked `positions`/`swaps` rows** before the mint route works end to end.
  Precedent: `tests/test_schema_smoke.py` already inserts 3 positions / 2 swaps / 2
  transactions as fixtures and asserts the constraints — a mocked-positions harness exists in
  spirit.
- **Scoring consumer (Q4).** Same argument: reads `wallet_metrics`/`positions`, writes
  `scores` — all of which can be populated with fixture rows.
- **`scoring.yaml` §5 correction + `metric_definitions` registration.** Pure config and
  reference data; depends on nothing but the decision about forward-appreciation ownership.
- **mint → pool resolution.** Independent of genesis; built from the `pools` registry plus
  `fetch_snapshot` (`derive.py:391`, the keyless DexScreener batch).

### Not blocked by anchor supply

Everything except the **values** of the anchor set. The pipeline can be built and run on
mocked anchors — which is why Q7 is possible at all.

### Blocked by anchor supply

Only the real run. Currently empty.

### Downstream

Anchor discovery (Q8) sits **after** the pipeline: it is the most expensive path, and
discovered anchors have nowhere to go until the pipeline exists.

---

## Q7 — What is the minimum viable Phase 1b?

Given an empty anchor set, the smallest pipeline that proves the plumbing end to end
**before** anchor supply becomes the blocker.

### Real input already on disk — this is the key enabler

| Asset | Contents | Cost |
|---|---|---|
| `data/raw/tx/2026-10-25/` | **372 archived `getTransaction` payloads**, 372 distinct sha256, 6.5 MB | **0 credits** |
| `tests/fixtures/` | **12** `getTransaction` payloads | **0 credits** |

Both are real chain data, replayable through `already_decoded()`-style paths. The mint route
can therefore be exercised **without spending anything** — which is exactly what makes a
zero-credit MVP possible.

### The MVP pipeline

1. **Mocked anchor set** — 3 anchors as a fixture: `(mint, genesis_time, deployer,
   first-20 buyers with timestamps)`. Built to satisfy §4's three independence requirements
   **by construction**, so the fixture is a valid test case rather than a convenient one.
2. **Genesis decoder** — run against the archived payloads, or 3 live mints for validation.
3. **Positions** — reconstruct into `positions` (exists).
4. **Quality filter** — genesis-burst first-class, reads the anchor→wallet→swaps chain,
   emits verdicts with rule ids and traces.
5. **Scoring** — read `positions`/`wallet_metrics` → write `scores` + `sub_scores` (both
   exist).

### Exit criterion

An end-to-end run on 3 mocked anchors producing **3 scores with 3 filter traces**, at
**~0 credits**, with genesis-derived age and rank, and **anchor supply as the only remaining
blocker**.

Cost to reach it: **0 credits** if the genesis decoder is validated against the 372 archived
payloads; **~210 credits** (3 × ~70) if it is also validated against 3 live mints to prove
reverse pagination against a real provider.

---

## Q8 — Where does the anchor-discovery path sit?

### Placement: after the pipeline, and only if anchors stay unsupplied

Four reasons, in order of weight:

1. **§8 says cheap-first screening selects the wrong population.** It cannot be made cheap by
   tuning — high-velocity tokens are expensive to reconstruct, and low-cost tokens are barely
   traded (SHIB: 5,200 txs, **15 usable swaps**). Building it first optimises the most
   expensive path knowing it cannot be made cheap.
2. **Cost.** ~100,000–200,000 (one-time) is 10–20% of a monthly cap, on the reading where
   those are Helius credits — and Q5 shows the units are unresolved.
3. **Nothing to consume it.** Discovered anchors have nowhere to go until the pipeline
   produces scores from them.
4. **It is the alternative, not the primary.** User-supplied anchors remain the cheaper path.

### Minimum cost to prove screening on 3 anchors — ESTIMATED

| Step | Estimate |
|---|---:|
| GeckoTerminal / DexScreener discovery calls | **~0 Helius credits** (keyless requests) |
| Genesis discovery, 3 mints | **~210 credits** (3 × ~70) |
| Anchor reconstruction, modest activity | **~600–3,000 credits** (3 × ~200–1,000) |
| First-20-buyer profiling, 60 wallets | **~0–2,400 credits** — mostly **free**, because those buyers' transactions *are* the anchor's own early transactions, already fetched and archived during reconstruction; `already_decoded()` skips them |
| **Proof total** | **~800–3,200 credits ≈ 0.1–0.3% of the monthly cap** |

**Checkpoint requirement:** the 3 proof anchors must satisfy §4's independence criteria. Three
non-independent anchors prove nothing — that is §4's own lesson, learned the expensive way.

**TBD — anchor count is an open question, not a settled number.** Nothing here establishes how
many anchors Phase 1b needs to support a claim about a wallet: 3 is the size of the *proof*
fixture, not a statistical requirement. A power calculation is required, and it cannot be done
usefully until the pipeline produces per-wallet metrics to estimate variance from. **Marked
TBD, to be resolved after the S6 checkpoint.** The same open item is recorded in `NOTES.md` §
`QUALITY_FILTER_PENDING`.

---

## Proposed sequencing

Effort in **turns** (agent turns of work), not hours. Cost is Helius credits.

| # | Work | Turns | Cost | Depends on |
|---|---|---:|---:|---|
| **S1** | Mint genesis decoder + mint age + reverse pagination **(critical path)** | 1–2 | 0–210 | — |
| **S2** | mint → pool resolution + ambiguity diagnostic; fix `token_age_seconds` semantics | 1 | 0 | S1 |
| **S3** | Price series from swap ratios over archived txs; positions reconstruction | 1–2 | 0 | S2 |
| **S4** | Quality filter, genesis-burst first-class, against **mocked** positions — **DONE 2026-10-28** | 1 | 0 | — *(parallel)* |
| **S5** | `scoring.yaml` §5 correction + `metric_definitions` + scoring consumer → `scores` — **config + metric bodies DONE 2026-10-28; consumer still open** | 1 | 0 | — *(parallel)* |
| **S6** | Anchor registry DDL + end-to-end mocked-anchor run — **DDL + writers DONE 2026-10-31; end-to-end mocked-anchor run still open (needs S1–S3 for real values)** | 1 | 0 | S3, S4, S5 |
| **S7** | *Only if anchors stay unsupplied:* screening proof on 3 independent anchors | 1–2 | ~3,000 | S6 |

**Critical path: S1 → S2 → S3 → S6.**
**S4 and S5 are parallel and can start now**, because both are testable against mocked rows.

**Status 2026-10-28.** **S4 is complete:** `solscope/quality_filter.py` implements the
genesis-burst rule, the three-way verdict (accepted / rejected / not_computable), and the
§4 anchor-set preconditions; `tests/test_phase1b_filter.py` calibrates it against synthetic
bundled and clean sets — bundled **100% rejected**, clean **0% false positives**, the 35
single-anchor clean wallets reported as not_computable in their own bucket. That
calibration validates the *implementation against the spec*, **not** N=2/Y=60: both sets
are synthetic, so `NOTES.md` § `QUALITY_FILTER_PENDING` stays open. **S5 is partly
complete:** `config/scoring.yaml` carries the §5 weights, `solscope/metrics.py` implements
all three metric bodies with hand-computed tests (`tests/test_phase1b_metrics.py`), and the
double-count resolution is recorded in Q4 — **the scoring consumer that reads those
metrics and writes `scores` is still open**, which is the remainder of S5. S1–S3 remain
untouched.

**Status 2026-10-31. S6's storage half is complete.** `schema/002_anchor_registry.sql` adds
`anchors`, `anchor_wallet_verdicts` and `anchor_wallet_metrics`; `solscope/anchor_writer.py`
writes them idempotently (a re-run rewrites a row instead of doubling the evidence, and a
new `rule_version` adds a row because verdicts under different thresholds are not
comparable); `tests/test_anchor_writer.py` proves both against real PostgreSQL through
`bootstrap_db.sh`, which now asserts each migration was *recorded* and that the three tables
exist (45 tables total, all three DB suites green, admin database untouched). Verdicts,
metrics and anchors now persist — but S6's other half, the **end-to-end mocked-anchor run**,
still cannot produce real values without S1–S3, so the checkpoint below is unchanged.

**Total to the checkpoint: ~5–7 turns, ~0–210 credits.**

### Go/no-go checkpoint — after S6

**PASS** requires all of:

- 3 scores + 3 filter traces produced from **mocked** anchors;
- genesis-derived age and buyer rank, computed **point-in-time** (Q2 invariant 1);
- buyer rank from a **complete** first-N set, or explicitly flagged not-computed (Q2
  invariant 2);
- total spend **≤ ~210 credits**;
- the only remaining blocker is **anchor supply**.

**FAIL** — if genesis or rank cannot be computed from archived data, or if either requires
unbudgeted live fetching. In that case re-scope *before* spending: the failure would mean the
mint route's foundation is not verifiable at this budget, which is the same class of finding
the Phase 2 gate was designed to surface early.

### Two things to settle before S5

1. **Forward-appreciation ownership** — **SETTLED 2026-10-28** (Q4): `early_entry` owns
   forward appreciation through `weight_max_subsequent_return`; `hit_multiple` leaves the
   weighted score at `weight_hit_rate: 0.00` and stays a diagnostic column. What remains is
   the mechanical part — mirroring the ownership in `metric_definitions.owning_subscore`
   when the scoring consumer is built.
2. **The DexScreener unit question** (Q5), before any screening budget is committed in S7. **Still open.**

---

## See also

- `docs/PHASE1A_RECOVERY.md` — the recovered design this document scopes against.
- `NOTES.md` § `PHASE1A_AUDIT` — why Phase 1b is a greenfield build.
- `NOTES.md` § `ANCHOR_SELECTION_CRITERIA` — the open blocker that shapes Q7.
- `NOTES.md` § `DATA_LOSS_2026_10` — quantified cost of the lost work.
- `docs/11-phase2-gate.md` §8 — DEX coverage, which bounds what Phase 1b can reconstruct.
- `docs/INVENTORY.md` — status of every document and artifact.
