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
validity floor. The thin fixture makes the point concrete: it scores **60.51**, comfortably
above 40, so confidence alone would rank a 2-anchor wallet; only the anchor conjunct stops
it.

Order of evaluation: quality filter → gates (`integrity`, positions) → confidence gate →
bands. A wallet that fails any gate gets `gated_by` naming the failing conjunct and no rank.
For a non-anchor gate failure the band is `exclude`; the anchor conjunct is a *rankability*
failure rather than an inadmissibility failure, so a wallet that fails only it, with
`raw >= 70` and `confidence >= 30`, is banded `promising_thin` instead (§5, D15).

---

## §5 Band derivation

Bands are assigned to `confidence_adjusted_score = 50 + (score − 50) × confidence/100`.

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
raw 78, adjusted 66.94, two anchors — vanished from the output entirely. That is the tool
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

**Current values in `scoring.yaml`, reported rather than replaced:**

```yaml
bands:
  track:         75
  watch:         60
  investigate:   45
  deprioritize:   0
```

These are four flat thresholds on `confidence_adjusted_score` with no confidence conjunct, and
doc 05 §13 documents the same four (`≥75` track, `60–74` watch, `45–59` investigate,
`<45` deprioritize). The rule above deviates in five ways, each deliberate:

1. **Track gains a confidence conjunct** (≥60). Without it, a 78-adjusted wallet at 45
   confidence is indistinguishable from a 78-adjusted wallet at 90, and the first is a
   shrinkage artifact — a raw 88 with three anchors stores 78 once, and 88's evidence is what
   the band is supposed to be about.
2. **The investigate floor drops from 45 to 40** and gains an override: `raw ≥ 70 AND
   confidence ≥ 30`. The pull-to-50 mechanic is symmetric, so a genuinely strong wallet with
   middling data gets compressed toward 50 and can fall below a flat floor. The override
   surfaces it as `investigate` — never as `track` — so strong-but-thin evidence is visible
   without being ranked alongside strong-and-rich.
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

A deviation that is *not* proposed: the confidence-adjusted pull itself. It stays
`50 + (score−50)·conf/100`, so a low-confidence wallet is neither rewarded nor punished, just
declined. Band rules decide what to *do* with a declined claim; they do not change the number.

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
| metadata (0.10) | 0.95 | 0.95 | 0.60 | 0.60 |
| **confidence_score** | **94.33** | **94.33** | **60.51** | **60.51** |
| distinct computed anchors | 19 | 19 | 2 | 2 |
| raw score | 82.00 | 28.00 | 78.00 | 24.00 |
| `confidence_adjusted_score` | 80.19 | 29.25 | 66.94 | 34.27 |
| gate | pass | pass | **fail** (2 < 3) | **fail** (2 < 3) |
| band | `track` | `exclude` | `promising_thin` | `exclude` |

Arithmetic, written out so a reviewer can check it without running code:

- rich: `0.30·0.95 + 0.25·0.90 + 0.20·1.00 + 0.15·0.9223 + 0.10·0.95`
  = `0.2850 + 0.2250 + 0.2000 + 0.1383 + 0.0950` = `0.9433` → **94.33**
- thin: `0.30·0.50 + 0.25·0.75 + 0.20·0.40 + 0.15·0.8507 + 0.10·0.60`
  = `0.1500 + 0.1875 + 0.0800 + 0.1276 + 0.0600` = `0.6051` → **60.51**
- good+rich: `50 + 32·0.9433 = 80.19`; bad+rich: `50 − 22·0.9433 = 29.25`
- good+thin: `50 + 28·0.6051 = 66.94`; bad+thin: `50 − 26·0.6051 = 34.27`

**good+rich and bad+rich have identical confidence: 94.33.** **good+thin and bad+thin have
identical confidence: 60.51.** No factor causes a difference, because no factor has access to
a difference — that is the structural argument of §1, and these two identities are the check
that it survived implementation.

Read the bands carefully: the *pairs* differ (track vs exclude; promising_thin vs exclude),
which is the whole point. The score separates them; confidence does not. Three things worth
noting in the matrix:

- `bad+rich` sits at 29.25, and the band rule would call it `exclude` on the score alone. Rich
  data does not rescue a bad wallet — confidence is not a benefit that good wallets earn.
- `good+thin` is the row that forced `promising_thin`. It has the *highest* adjusted score
  among the non-track wallets (66.94), raw 78 and confidence 60.51, and two anchors — so it
  is unrankable, and under the three-band rule it disappeared. It now appears in `scores`
  with `band='promising_thin'` and `gated_by='distinct_anchors below minimum'`: visible,
  unranked, and explained.
- `bad+thin` shares that confidence (60.51) and is `exclude`, because its raw 24 fails the
  `raw >= 70` conjunct. The two thin wallets therefore separate on band while sharing
  confidence exactly — orthogonality holding in the branch that was just added.

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
pair and 59.51 / 59.51 for the thin pair (see § Q4 D18 — the thin row's metadata 0.60 is
unreachable), with the thin wallets still failing the anchor conjunct. Bands remain
`exclude` until S12c, so no wallet is ranked on this turn's numbers.

**S12c — band derivation and `solscope scores` read command.** *The last implementation step.*
Applies §5's five bands to the stored `confidence_adjusted_score` and composite — including
`promising_thin` and the `gated_by` reasons of D13 — and adds the read-only `solscope scores`
command so gate outcomes and bands are inspectable from the shell, the capability whose
absence was noted at the end of the Option 3 turn. No scoring logic changes in S12c; if a band
value looks wrong there, it is a §5 decision to revisit, not a patch to apply.

Ordering is load-bearing: a band rule applied to an all-zero confidence column (S12c before
S12b) would look correct in tests and be meaningless in production.

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
