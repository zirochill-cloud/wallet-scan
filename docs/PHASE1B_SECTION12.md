# Phase 1b — §12 Data confidence: design

Status: **design only**. No §12 code is written; `blend_confidence` (Option 3) remains the
live gate until S12b replaces it. Written 2026-11-06.

Scope: the five confidence factors, the composite, the gate, band derivation, and what the
resulting number can and cannot mean. It supersedes doc 05 §12's formulas where they differ,
and every difference is named below rather than made silently.

---

## §1 Core principle

> Confidence measures how much evidence supports the score. It is orthogonal to wallet
> quality. A genuinely good wallet with rich data and a genuinely bad wallet with rich data
> must receive identical confidence; the score separates them. A genuinely good wallet with
> thin data receives low confidence not because it looks bad but because we cannot yet
> confirm it looks good.

This is a structural claim, not an aspiration. None of the five inputs in §2 reads a
performance number: they read row status, price provenance, anchor counts, timestamps and
token metadata. No factor touches `sub_scores.value`, `realized_pnl`, `roi`,
`max_subsequent_return` or any other outcome measure, so there is no path by which a better
wallet can accumulate more confidence than a worse one with the same data. `S12a` must
enforce this with a test that asserts the five input queries reference no performance
column, so a future edit cannot quietly introduce one.

A corollary that matters for reading the output: confidence is capped by data quality, never
raised by performance. A 99-raw wallet with six anchors can still sit at 70 confidence; a
40-raw wallet with the same six anchors sits at exactly the same 70.

---

## §2 The five factors

Weights are fixed by `scoring.yaml` `confidence.weight_*` and sum to 1.00. Each factor is
computed for **every** scored wallet, in 0.0–1.0, and stored. 0.0 is a legitimate value.

### 2.1 Coverage — weight 0.30

*Definition:* the share of the wallet's anchor set whose metrics were actually computed.

| | |
|---|---|
| Input | `anchor_wallet_metrics`: `count(DISTINCT anchor_id) FILTER (WHERE status='computed')` over `count(DISTINCT anchor_id)` for the wallet |
| Range | 0.0–1.0 |
| How it fails | the aggregator excluded most anchors — insufficient forward window, no genesis time, unparseable pools — so the composite describes a minority of the anchor set |
| Fixture | 19 computed of 20 anchors → **0.95** |

This is the `anchor_coverage` value the consumer already computes (D1's denominator), so the
factor adds no new query.

### 2.2 Price provenance — weight 0.25

*Definition:* the share of the wallet's entry events whose price came from the swap's own pool
reserves rather than an interpolation, a provider decode or a point-in-time snapshot.

| | |
|---|---|
| Input | `swaps.price_provenance` over the wallet's entry swaps (first buy per wallet per token) inside the analysis window: `swap_derived` = 1, everything else = 0 |
| Range | 0.0–1.0 |
| How it fails | entries priced from `pool_ohlcv` (candles), `snapshot_api` (current state, valid only at fetch time) or `provider_parsed`; entry timing and valuation become approximate, and the early-entry dimension is precisely about timing |
| Fixture | 18 of 20 entries `swap_derived` → **0.90** |

**Join, decided (D10):** the position names its entry swap by key. `positions.entry_swap_signature`
is added by `schema/004_positions_entry_swap_signature.sql`, populated in `reconstruct()` from
the opening leg and backfilled by `rebuild()`. No approximate `(address, mint, opened_at)`
join: the column costs one migration and makes the factor exact. NULL has exactly one meaning —
the episode was opened by a sell of pre-window inventory, so no entry buy exists — and such a
position earns no provenance credit rather than being dropped from the denominator.

### 2.3 Sample — weight 0.20

*Definition:* how close the wallet is to the number of distinct computed anchors at which
more anchors stop adding confidence.

| | |
|---|---|
| Input | `anchor_wallet_metrics` distinct computed anchors, as in 2.1 |
| Formula | `min(1, distinct_computed_anchors / sample_full_credit_at)`, `sample_full_credit_at: 5` (D5) |
| Range | 0.0–1.0 |
| How it fails | 1–2 anchors; the composite is built from almost nothing and the shrinkage factor is 0.29–0.38, so the score itself is mostly prior |
| Fixture | n=2 → **0.40**, n=3 → **0.60**, n=5 → **1.00**, n=9 → **1.00** |

D5 changed this from `min(1, n/10)`, which reached full credit at ten anchors and spread the
expected 2–5 anchors across 0.20–0.50 — compressed exactly where the discrimination is
needed. At `/5` the same range is 0.40–1.00, and full credit lands on the same five anchors
at which shrinkage starts giving the observation half weight. The gate's anchor floor is a
**separate** key (§4), not this divisor.

### 2.4 Recency — weight 0.15

*Definition:* how long since the wallet was last observed trading, as a half-life decay.

| | |
|---|---|
| Input | `max(swaps.event_time)` for the wallet |
| Formula | `0.5 ** (days_since_last_trade / recency_half_life_days)`, existing key `recency_half_life_days: 60` |
| Range | (0, 1.0] |
| How it fails | a dormant wallet, or a wallet whose recent trades were never ingested — both read as "no recent evidence", which is the honest reading |
| Fixture | 7 days → **0.9223**, 14 days → **0.8507**, 60 days → **0.5000**, 180 days → **0.1250** |

A missing `max(event_time)` scores 0.0 with the reason recorded. It is **not** imputed to 1.0
("probably still active") or to the median: an unmeasured wallet must not receive the benefit
of a measurement it does not have.

*Deviation from doc 05 §12:* that section writes `exp(−days/60)`, which is an e-folding time,
not a half-life (7 days → 0.889, 60 days → 0.368). The config key is
`recency_half_life_days`, so this design implements the half-life reading and S12a should
correct doc 05's formula text rather than the code.

### 2.5 Metadata — weight 0.10

*Definition:* the share of the wallet's anchor tokens whose mint metadata is resolved.

| | |
|---|---|
| Input | `tokens.metadata_json IS NOT NULL AND tokens.metadata_completeness <> 'unknown'`, over the anchors the wallet was measured on; an anchor with no `tokens` row counts as unresolved (`LEFT JOIN`) |
| Range | 0.0–1.0 |
| How it fails | unresolved mints, or anchors that precede ingestion and never got a token row |
| Fixture | 19 of 20 resolved → **0.95** |

Metadata does not enter any dimension's arithmetic; it caps how much of the token population
we can characterise, which is why its weight is smallest.

---

## §3 Composite

```
confidence_score = 100 × (0.30·coverage + 0.25·price_provenance + 0.20·sample
                          + 0.15·recency + 0.10·metadata)
```

Clamped to [0, 100] and stored at 2 dp (`NUMERIC(6,2)`). All five factors are **always**
computed — a factor is never omitted because its input was missing; a missing input is a 0.0
with an explanation. `confidence_score = 0.00` is a valid, reportable value meaning "no
evidence", not an error and not NULL.

**No re-normalisation.** If metadata is 0.0, the wallet loses 10 points; the other four
weights are not rescaled to 1.0. Rescaling is how a wallet with unmeasured provenance would
end up *more* confident than one with measured-and-poor provenance — the same failure §9's
ownership rule forbids for metrics.

Because the factors are independent in kind (coverage: anchors, provenance: prices, sample:
count, recency: time, metadata: tokens), a single perfect factor cannot carry the composite:
0.30 is the largest weight, so no lone 1.0 exceeds 30.

---

## §4 Gate

A wallet ranks only if **both** hold:

```
confidence_score    >= confidence_gate.confidence_min    (40)
distinct_computed_anchors >= confidence_gate.min_distinct_anchors (3)
```

Both are config keys. The anchor conjunct counts **computed** anchors — the same denominator
D1 established — because anchors with no computed metric are not evidence; their absence is
already priced by coverage.

Why 40 rather than the legacy 25: at confidence 40 the adjusted score keeps 40% of its
distance from 50, so a 100-raw wallet still lands at 70 and an 80-raw wallet at 62. At 25 the
same wallet keeps 25% and everything converges to the middle, which reads as a ranking while
carrying almost no ranking information. 40 is the point where the scale still has range.

Why a **separate** anchor floor rather than deriving it from `sample_full_credit_at`: they
answer different questions. The divisor answers "how many anchors is plenty" (a calibration
of the factor); the gate answers "how few anchors is too few to rank at all" (a validity
rule). Deriving one from the other means a future calibration change silently moves the
validity floor. The thin fixture makes the point concrete: it scores **59.51**, comfortably
above 40, so confidence alone would rank a 2-anchor wallet; only the anchor conjunct stops
it.

Order of evaluation: quality filter → gates (`integrity`, positions) → confidence gate →
bands. A wallet that fails any gate gets `gated_by` naming the failing conjunct and no rank.
For a non-anchor gate failure the band is `exclude`; the anchor conjunct is a *rankability*
failure rather than an inadmissibility failure, so a wallet that fails only it, with
`raw >= 70` and `confidence >= 30`, is banded `promising_thin` instead (§5, D15).

---

## §5 Band derivation

Bands are assigned to `confidence_adjusted_score`. **Which score the pull applies to is a
phase property (D21).** While five of eight dimensions are unbuilt, the score is
`shrunk_early_entry × confidence/100` and the bands are *early-entry* bands — "is this
wallet's early-entry ability worth tracking?" — with `scores.band_basis = 'shrunk_early_entry'`.
Once the other dimensions exist, the basis becomes `overall_score` and the pull is
`50 + (overall − 50) × confidence/100`, making the same five rules full-wallet bands. The
pull-to-50 form is not usable yet: overall is the neutral 50.00, so it returns 50.00 for every
wallet. The band *rules* below are the same in both phases; only the number they read changes,
and every row says which one it read.

Rule (five bands, D15):

| Band | Condition |
|---|---|
| `track` | gate passed **AND** `adjusted >= 70` **AND** `confidence >= 60` |
| `watch` | gate passed **AND** `adjusted >= 60` (and not `track`) |
| `investigate` | gate passed **AND** `adjusted >= 40` (and not above) |
| `promising_thin` | failed the **anchor conjunct only** **AND** `raw >= 70` **AND** `confidence >= 30` |
| `exclude` | otherwise, or any other gate failed |

`promising_thin` exists for one case that no other band can reach. A wallet in two anchors
is excluded by the rankability rule (§4) before any band can speak, so the good+thin fixture —
raw 78, adjusted 66.66, two anchors — vanished from the output entirely. That is the tool
hiding a wallet the human should look at: the mechanism now says "we can see this is
promising; we cannot confirm it; look at it yourself." `investigate` cannot do that job,
because reaching it requires a passed gate. The band is deliberately **not** `track` and
carries no rank, so it surfaces the wallet without pretending two anchors are enough. Note
what it gates on: `raw`, not `adjusted` — a 2-anchor wallet is shrunk hard, and asking a
shrunken number to clear 70 would ask the wallet to prove again the very thing it lacks
anchors to prove.

Evaluation order: gate outcome first (any non-anchor gate failure → `exclude`), then the
anchor conjunct (`promising_thin` or `exclude`), then adjusted thresholds for gate-passing
wallets. Every excluded or thin wallet keeps a `gated_by` reason (`raw_below_investigate`,
`distinct_anchors below minimum`), so collapsing four bands into five does not collapse the
diagnostics — see D13 in `docs/PHASE1B_SCOPING.md` § Q4.

**Values in `scoring.yaml`, after D22:**

```yaml
bands:
  track:                  70   # was 75
  track_min_confidence:   60
  watch:                  60
  investigate:            40   # was 45
  deprioritize:            0
```

D22 aligned the config with the design, so the rule below no longer carries a threshold
deviation. What the yaml still does not express is the rest of the rule — the bands are not
four flat windows on `confidence_adjusted_score`, and doc 05 §13's `≥75 / 60–74 / 45–59 / <45`
describes the version this replaces. The rule above differs in five ways, each deliberate:

1. **Track gains a confidence conjunct** (≥60), now a visible config key. Without it, a
   78-adjusted wallet at 45 confidence is indistinguishable from a 78-adjusted wallet at 90,
   and the first is a shrinkage artifact — a raw 88 with three anchors stores 78 once, and
   88's evidence is what the band is supposed to be about.
2. **The investigate floor is 40, not 45** — the design value, applied by D22 once threshold
   changes were in scope. The old raw-override escape hatch stays gone: the genuinely
   unreachable case was the anchor-constrained one, and that is `promising_thin`'s job.
3. **`watch` survives; `deprioritize` does not.** `watch` is currently configured
   (`bands.watch: 60`), and D15 keeps it at that threshold, so the band is not new — what
   changes is its meaning: from the flat 60–74 window to "60 and up that `track` did not
   claim", which now also catches a 72-adjusted wallet at 45 confidence. The map keeps a
   place for "worth a look, not a conclusion" instead of forcing it into `investigate`.
   `deprioritize` had no distinct meaning left once `exclude` existed: it was the catch-all
   floor, and its value 0 made it exactly "everything else". It is replaced, not renumbered.
   Both values remain legal in the schema (`band` CHECK, `recommendation_kind`) until S12c
   either comments `deprioritize` deprecated or drops it with a migration.
4. **`promising_thin` is added** — the reason this section was rewritten. See above.
5. **`investigate` loses the raw override.** The old rule's
   `raw >= 70 AND confidence >= 30` escape hatch only ever rescued wallets that had already
   passed the gate, and for those the 40 floor plus the adjusted score is the honest test.
   The genuinely unreachable case was the anchor-constrained one, and that is
   `promising_thin`'s job, gated on raw because adjusted is shrunken by the missing anchors.

A deviation that is *not* proposed: the confidence pull's shape. The pull keeps its meaning
— a low-confidence wallet is neither rewarded nor punished, just declined — and band rules
decide what to *do* with a declined claim, never what the number is. What changed under D21
is only which score the pull is applied to in this phase (`shrunk_early_entry` rather than the
placeholder `overall_score`); `50 + (score−50)·conf/100` remains the form for the `overall_score`
basis, and the code keeps it as `confidence_adjusted_score` for that day.

---

## §6 The 2×2 orthogonality matrix

Four hand-computed wallets. Factors are chosen to be plausible and identical within each data
class, so the only difference between the good and bad member of a pair is the raw score.

| | good+rich | bad+rich | good+thin | bad+thin |
|---|---:|---:|---:|---:|
| coverage (0.30) | 0.95 | 0.95 | 0.50 | 0.50 |
| price provenance (0.25) | 0.90 | 0.90 | 0.75 | 0.75 |
| sample (0.20) | 1.00 | 1.00 | 0.40 | 0.40 |
| recency (0.15) | 0.9223 | 0.9223 | 0.8507 | 0.8507 |
| metadata (0.10) | 0.95 | 0.95 | 0.50 | 0.50 |
| **confidence_score** | **94.33** | **94.33** | **59.51** | **59.51** |
| distinct computed anchors | 19 | 19 | 2 | 2 |
| shrinkage weight `n/(n+5)` | 0.7917 | 0.7917 | 0.2857 | 0.2857 |
| raw score (early-entry composite) | 82.00 | 28.00 | 78.00 | 24.00 |
| shrunk early-entry score | 75.33 | 32.58 | 58.00 | 42.57 |
| `confidence_adjusted_score` (`shrunk_early_entry`) | 71.06 | 30.74 | 34.52 | 25.33 |
| gate | pass | pass | **fail** (2 < 3) | **fail** (2 < 3) |
| band (70/60/40, D22) | `track` | `exclude` | `promising_thin` | `exclude` |

Arithmetic, written out so a reviewer can check it without running code. Confidence first:

- rich: `0.30·0.95 + 0.25·0.90 + 0.20·1.00 + 0.15·0.9223 + 0.10·0.95`
  = `0.2850 + 0.2250 + 0.2000 + 0.1383 + 0.0950` = `0.9433` → **94.33**
- thin: `0.30·0.50 + 0.25·0.75 + 0.20·0.40 + 0.15·0.8507 + 0.10·0.50`
  = `0.1500 + 0.1875 + 0.0800 + 0.1276 + 0.0500` = `0.5951` → **59.51**

Then shrinkage at `k=5`, prior 50 — `n/(n+5)·raw + 5/(n+5)·50`, with `n` the distinct computed
anchors, which is the same count the confidence sample factor uses:

- good+rich: `0.7917·82 + 0.2083·50` = `64.9167 + 10.4167` = **75.33**
- bad+rich: `0.7917·28 + 0.2083·50` = `22.1667 + 10.4167` = **32.58**
- good+thin: `0.2857·78 + 0.7143·50` = `22.2857 + 35.7143` = **58.00**
- bad+thin: `0.2857·24 + 0.7143·50` = `6.8571 + 35.7143` = **42.57**

Then the D21 pull — `shrunk × confidence/100`:

- `75.33·0.9433 = 71.06`; `32.58·0.9433 = 30.74`
- `58.00·0.5951 = 34.52`; `42.57·0.5951 = 25.33`

> **D23 — the previously published adjusted column was 80.19 / 29.25 / 66.66 / 34.53.** Those
> values are `50 + (raw − 50) × confidence/100`: the pull-to-50 form that D21 retired for this
> phase, applied to an unshrunk score. They are the correct numbers for the `overall_score`
> basis that arrives with the other five dimensions, and they are not the number S12c bands.
> Because they were plausible rather than impossible, nothing flagged them until the config
> change made the 75/70 boundary visible; see `NOTES.md` § PROCESS_FAILURES.
>
> Shrinkage and the pull also mean the column no longer orders the way a reader expects:
> bad+thin (25.33) is below bad+rich (30.74) even though the thin wallet's raw score is closer
> to good+rich's. Three anchors' worth of evidence is a large penalty, and the matrix is now
> read for band outcomes rather than for a monotonic score ordering.

> **Fixture correction (D20), thin column only.** Coverage and metadata divide by the same
> denominator — the anchors the wallet was measured on — so with 2 measured anchors metadata
> is limited to 0/2, 1/2 or 2/2. The originally specified 0.60 was incompatible with coverage
> 0.50; it is corrected here to 0.50, the largest compatible value below the original. No
> other row changes.

**good+rich and bad+rich have identical confidence: 94.33.** **good+thin and bad+thin have
identical confidence: 59.51.** No factor causes a difference, because no factor has access to
a difference — that is the structural argument of §1, and these two identities are the check
that it survived implementation.

Read the bands carefully: the *pairs* differ (track vs exclude; promising_thin vs exclude),
which is the whole point. The score separates them; confidence does not. Three things worth
noting in the matrix:

- `bad+rich` sits at 30.74 — `exclude` on `raw_below_investigate` (raw 28 is below the 40
  floor too). Rich data does not rescue a bad wallet: confidence is not a benefit that good
  wallets earn, and 19 anchors of it move a raw 28 to 32.58, not to a ranking.
- `good+thin` is the row that forced `promising_thin`, and under the current formula it is
  *not* the highest adjusted score among non-track wallets — it is 34.52, second from the
  bottom. Its raw 78 at two anchors shrinks to 58.00 before the 59.51 pull. That is exactly
  why the band gates on raw: the adjusted number has already been reduced by the missing
  anchors, so asking it to clear the bar would demand the wallet prove the thing its anchors
  prevent it from proving. It appears in `scores` with `band='promising_thin'` and
  `gated_by='distinct_anchors below minimum'`: visible, unranked, explained.
- `bad+thin` shares that confidence (59.51) and is `exclude`, because its raw 24 fails the
  `raw >= 70` conjunct. The two thin wallets separate on band while sharing confidence
  exactly, and they now also share the shrinkage weight (0.2857): the raw score is the only
  thing that differs, and it is enough — orthogonality holding in the branch that was just
  added.

---

## §6b Real shrinkage and the band threshold change

Section §6 *is* the real-shrinkage arithmetic: the design's anchor counts (19 computed for
rich, 2 for thin) are counts a real wallet can have, so once the current formula is applied
the matrix already carries shrinkage. §6b exists for the part §6 does not show — what the
threshold change from 75/45 to 70/40 does to these four wallets, and where the boundary sits.

| | good+rich | bad+rich | good+thin | bad+thin |
|---|---:|---:|---:|---:|
| shrunk early-entry | 75.33 | 32.58 | 58.00 | 42.57 |
| `confidence_adjusted_score` | 71.06 | 30.74 | 34.52 | 25.33 |
| gate | pass | pass | fail (2 < 3) | fail (2 < 3) |
| band @ 75 / 60 / 45 (pre-D22) | `watch` | `exclude` | `promising_thin` | `exclude` |
| band @ 70 / 60 / 40 (D22) | **`track`** | `exclude` | `promising_thin` | `exclude` |

Three things are worth reading off this table:

- **Only good+rich moves, and it moves because 71.06 lies between 70 and 75.** At 75 the
  wallet is `watch`; at 70 it is `track`. The matrix at the old design number (80.19) could
  not have shown this: 80.19 clears both thresholds, so the D22 change would have looked
  inert. The boundary wallet is the test, and it is asserted.
- **`bad+rich` stays `exclude` under both thresholds.** A draft of this table put it at
  42.75 (adjusted 40.33) and treated the move to `investigate` as a finding about shrinkage
  being too gentle. That figure is not reproducible: with `n=19`, `k=5`, raw 28 the shrunk
  score is 32.58, not 42.75 — 42.75 would require a raw score of about 39 at the same weight.
  Shrinkage is *not* gentle enough to lift a raw-28 wallet into `investigate`, and at 40 the
  band remains `exclude` on `raw_below_investigate`. No design decision is needed on this
  point.
- **The thin pair is unaffected by the threshold change**, because neither reaches the
  adjusted floors at all and both are decided by the gate. Their bands are set by raw score
  and the anchor conjunct, which D22 did not touch.

---

## §7 What this design cannot detect

- **Manipulation.** A wallet washing its own trades to accumulate anchors raises coverage and
  sample — the two factors it can most easily inflate — without raising confidence above what
  the evidence mechanically supports. Confidence is blind to intent; the quality filter
  (§8) is where coordinated behaviour is priced, and it does so as *penalty*, not as doubt.
- **Look-ahead contamination.** Provenance records where a price came from, not whether the
  price existed at the decision date. A candle series that was backfilled *later* still reads
  `pool_ohlcv` and correctly lowers the factor; a series that was point-in-time valid but
  selected after seeing the outcome is undetectable from inside the row. Only the validation
  design (held-out windows, frozen configs) addresses that.
- **Probability of skill.** This is an *evidence* score, not a posterior. 94 confidence about
  a wallet that got lucky is exactly what it says: we are highly confident in a measurement
  of a lucky wallet. §12 does not estimate the chance that the edge persists; that is what
  validation experiments are for.

Stated as a one-liner for the UI: *confidence is confidence in the number, not in the wallet.*

---

## §8 Interaction with the quality filter

The quality filter runs before scoring and can produce three outcomes, only one of which
reaches confidence:

| Filter outcome | Rows produced | Confidence behaviour |
|---|---|---|
| rejected (integrity/quality) | **no `scores` row** | never computed — there is no claim to be confident in |
| `not_computable` (no usable dimension) | **no `scores` row** | never computed; appears in the unscored report, not the ranking |
| scored | one `scores` row per run | five factors, composite, gate, band |

Consequences the implementation must preserve:

- A rejected wallet cannot appear as "high confidence, excluded": it does not appear at all.
  The two exclusions are different facts and must stay different — `exclude` means "measured,
  not good enough or not evidenced enough to rank", absence means "not admissible".
- `blend_confidence` is not called on a wallet that gets no row; the current consumer already
  proves this with a spy test, and S12b must keep the equivalent assertion for the composite.
- Because the filter can remove a wallet whose data quality is *low for non-manipulation
  reasons* (e.g. truncated history), the surviving population's coverage is higher than the
  raw population's. Confidence is therefore not a measure of how much data Solscope holds
  overall — only of how much it holds about wallets it was willing to score.

**Hard constraint on anchor-set size (D15).** The 3-anchor gate conjunct turns the anchor-set
requirement from "≥2 anchors per wallet for computation" into "≥3 anchors per wallet for
rankability." Any wallet in only 2 anchors cannot be ranked regardless of raw score; it
appears in `promising_thin` instead. This is a documented constraint on anchor-set size: to
rank N wallets, the anchor set must produce ≥3 anchors per wallet on average for the
rankable population.

Two consequences follow, and neither is a bug to be worked around later. First, anchor
selection is now a scoring input: a set of N anchors each bought by roughly half the sampled
wallets yields about two anchors per wallet, so almost nothing is rankable even though every
number is computable — the same set sized for overlap produces three or more. Second,
`promising_thin` will be large exactly when the anchor set is small, so it is the band that
shows whether the constraint is being met. A run where `promising_thin` dominates is a
run whose anchor set cannot support a ranking, and the right response is more anchors, not a
lower gate.

---

## §9 Implementation sequencing

Three separable sub-turns. Each lands green on `bash bootstrap_db.sh` and each keeps the
previous behaviour selectable until the next is merged.

**S12a — the five factors, hand-computed fixtures.** *Done (2026-11-06).*
Five factor functions in `solscope/confidence.py` (Decimal, 4 dp, `(0, 1]`, 0.0 for a missing
input, no imputation), plus `schema/004`'s `positions.entry_swap_signature` so provenance is a
key lookup rather than an approximate join (§10.1). `tests/test_confidence.py` reproduces the
§2 fixtures (0.95 / 0.90 / 0.40 / 1.00 / 0.9223 / 0.95) against real rows and proves
performance-blindness mechanically: the schema is parsed so the banned list is non-vacuous,
and a recording connection checks the statements actually executed, so a factor cannot bypass
the audit by building SQL inline.

**S12b — composite, gate wiring, config.** *Done (2026-11-06).*
`composite_confidence` and `evaluate_gate` live in `solscope/confidence.py`;
`scores.confidence_score` is the composite and `sub_scores.confidence` carries it; Option 3's
`blend_confidence`, `BlendResult` and `confidence_factors` are retired, as are
`existing_min/sample_min/coverage_min`, `gates.confidence_min` (D12) and
`sample_full_credit_n_eff` (D11). `sample_full_credit_at: 5` and `min_distinct_anchors: 3` are
config keys. Verified against the §6 matrix seeded as real rows: 94.33 / 94.33 for the rich
pair and 59.51 / 59.51 for the thin pair (see § Q4 D18/D20 — the thin row's metadata 0.60 was
unreachable and is corrected to 0.50), with the thin wallets still failing the anchor
conjunct. Bands remain `exclude` until S12c, so no wallet is ranked on this turn's numbers.

**S12c — band derivation and `solscope scores` read command.** *Done (2026-11-06).*
`solscope/bands.py` implements the five rules with the thresholds read from `scoring.yaml`
(70 / 60 / 40 since D22, matching §5);
`schema/006` admits `promising_thin` to the band CHECK and the recommendation enum and
comments `deprioritize` as a legacy value; `solscope/report.py` plus the `solscope scores`
command print band, adjusted, raw, confidence, gate outcome and `gated_by` from the latest run,
with `--wallet`, `--band`, `--limit` and `--as-of`. Exclusion reasons survive the band change:
`raw_below_investigate` (measured and not good enough), the fuller "raw score below investigate
threshold AND confidence below watch threshold", the anchor-conjunct text, and
`promising_but_unconfirmed` for `promising_thin`.

**§12 is now implemented end to end**: five factors, composite, gate, adjusted score with a
recorded basis, five bands, and a read path. The last remaining functional blocker is **real
anchors** — the pipeline can score, gate and band, but the anchor set is empty, so a live run
ranks nobody.

Ordering was load-bearing: a band rule applied to an all-zero confidence column (S12c before
S12b) would have looked correct in tests and been meaningless in production. S12c came after
the composite existed, so every band test compares against a real number.

---

## §10 Open questions (not fixed here)

1. **Position → entry swap.** *Resolved by D10.* `positions.entry_swap_signature` is added
   in `schema/004`, populated in `reconstruct()` from the opening buy and backfilled by
   `rebuild()`. No approximate join. NULL means no entry buy exists (sell-opened episode).
2. **`sample_full_credit_n_eff: 30`** still exists under `confidence:` and describes a
   trade-count scale, while D5's `sample_full_credit_at: 5` counts distinct anchors. Two keys
   that both look authoritative about "how much sample is enough" is precisely the confusion
   D5 was meant to end. S12b removes or renames the old one; until then, a reader of
   `scoring.yaml` sees both.
3. **Two confidence floors.** `gates.confidence_min: 25` and
   `confidence_gate.confidence_min: 40` will both exist in S12b. One of them must be the
   authority, and the other must be removed rather than left as a stale second opinion. The
   proposal is that 40 wins and 25 is deleted, since 25 was calibrated when confidence was a
   flat placeholder.
4. **`watch` and `deprioritize`.** *Resolved for the rule by D13/D15:* `watch` is kept
   (currently configured at 60) as the residual ≥60 band, `deprioritize` is replaced by
   `exclude`, and the two exclusion reasons stay distinguishable in `gated_by`. S12c still
   owns the enum comment so no reader assumes `deprioritize` is reachable.
5. **Recency formula text.** *Resolved by D14:* the curve is a half-life,
   `0.5 ** (days / recency_half_life_days)`; doc 05 §12 said `exp(−days/60)`, which is an
   e-folding time, and it is corrected rather than the key being renamed.

None of these block S12a except 1, which must be decided within it.
