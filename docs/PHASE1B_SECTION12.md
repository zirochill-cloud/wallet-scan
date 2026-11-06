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

**Known join problem, not fixed here:** `positions` carries `entry_price_usd` and
`opened_at` but no `entry_swap_signature`, so a position cannot be traced to its entry swap
by key. S12a must either join on `(address, mint, opened_at)` with a documented tolerance, or
add a column in a migration. See §10.

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
bands. A wallet that fails any gate gets `band = 'exclude'`, `gated_by` naming the failing
conjunct, and no rank.

---

## §5 Band derivation

Bands are assigned to `confidence_adjusted_score = 50 + (score − 50) × confidence/100`.

Proposed rule:

| Band | Condition |
|---|---|
| `track` | `adjusted >= 70` **AND** `confidence >= 60` |
| `investigate` | `adjusted >= 40` **OR** (`raw >= 70` **AND** `confidence >= 30`) |
| `exclude` | otherwise, or any gate failed |

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
`<45` deprioritize). The proposed rule deviates in three ways, each deliberate:

1. **Track gains a confidence conjunct** (≥60). Without it, a 78-adjusted wallet at 45
   confidence is indistinguishable from a 78-adjusted wallet at 90, and the first is a
   shrinkage artifact — a raw 88 with three anchors stores 78 once, and 88's evidence is what
   the band is supposed to be about.
2. **The investigate floor drops from 45 to 40** and gains an override: `raw ≥ 70 AND
   confidence ≥ 30`. The pull-to-50 mechanic is symmetric, so a genuinely strong wallet with
   middling data gets compressed toward 50 and can fall below a flat floor. The override
   surfaces it as `investigate` — never as `track` — so strong-but-thin evidence is visible
   without being ranked alongside strong-and-rich.
3. **`watch` and `deprioritize` disappear** as bands. `investigate` absorbs the 60–74 range
   (those wallets are ≥40 and so already investigate), and `deprioritize` was the catch-all
   that `exclude` now covers. Nothing in the schema breaks — `band` and `recommendation_kind`
   still permit both values — but leaving two unreachable enum values in place invites a
   reader to assume a distinction the rule no longer makes. S12c must decide: keep `watch` as
   a sub-band (adjusted ≥60 AND confidence ≥60), or mark both values deprecated in the schema
   comment. See §10.

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
| band | `track` | `exclude` | `exclude` | `exclude` |

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

Read the bands carefully: the *pairs* differ (track vs exclude, both thin wallets excluded),
which is the whole point. The score separates them; confidence does not. Two things worth
noting in the matrix:

- `bad+rich` sits at 29.25, and the band rule would call it `exclude` on the score alone. Rich
  data does not rescue a bad wallet — confidence is not a benefit that good wallets earn.
- `good+thin` has the *highest* adjusted score among the non-track wallets (66.94) and is
  still excluded, because two anchors cannot be ranked. It will appear in `scores` with
  `band='exclude'` and `gated_by='confidence_gate: distinct_anchors below minimum (2 < 3)'`,
  which is the audit trail a user needs to see why a promising wallet is not on the list.

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

---

## §9 Implementation sequencing

Three separable sub-turns. Each lands green on `bash bootstrap_db.sh` and each keeps the
previous behaviour selectable until the next is merged.

**S12a — the five factors, hand-computed fixtures.**
Pure factor functions (one per factor) plus the queries they need, returning
`(value, explanation, reason_if_missing)`. Fixtures are the four wallets of §6 plus the
per-factor fixtures in §2. Asserts each factor is independent of every performance column
(§1), that missing inputs yield 0.0 with a reason and never an imputation, and that the
anchor-based factors agree with D1's denominator. No composite, no gate change, no schema
change. Resolves the provenance join (§10.1) or records the tolerance it chose.

**S12b — composite, gate wiring, config.**
`confidence_score = 100·Σ w·f`, stored and explained; replaces Option 3's
`blend_confidence` gate with `confidence >= 40 AND distinct_computed_anchors >= 3`; adds
`sample_full_credit_at: 5` and `min_distinct_anchors: 3`; migrates `confidence_gate` from
`existing_min/sample_min/coverage_min` and reconciles the duplicate floor with
`gates.confidence_min: 25` (§10.3); retires `sample_full_credit_n_eff: 30` (§10.2). This is
the turn in which wallets can start ranking, so it must show a run where the numbers are no
longer all zero and a run where a 2-anchor wallet is still excluded.

**S12c — band derivation and `solscope scores` read command.**
Applies §5's rule, decides the `watch`/`deprioritize` question (§10.4), and adds the read-only
`solscope scores` command so gate outcomes and bands are inspectable from the shell — the
capability whose absence was noted at the end of the Option 3 turn. No scoring logic changes
in S12c; if a band value looks wrong there, it is a §5 decision to revisit, not a patch to
apply.

Ordering is load-bearing: a band rule applied to an all-zero confidence column (S12c before
S12b) would look correct in tests and be meaningless in production.

---

## §10 Open questions (not fixed here)

1. **Position → entry swap.** `positions` has no entry signature, so price provenance must
   either join approximately on `(address, mint, opened_at)` — admitting a small
   mis-attribution rate that must be stated — or a migration adds `entry_swap_signature`.
   S12a must choose one and say which. Adding the column is the honest option and costs one
   migration plus backfill; the approximate join is free but makes the factor's error rate
   undocumented.
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
4. **`watch` and `deprioritize`.** The proposed band rule never emits them, but the schema's
   `band` CHECK and `recommendation_kind` enum still allow them, and doc 05 §13 documents
   four bands. S12c keeps `watch` as a sub-band or marks both deprecated; silently leaving
   unreachable enum values is the one option to avoid.
5. **Recency formula text.** doc 05 §12 writes `exp(−days/60)`; the config key is
   `recency_half_life_days`. S12a corrects the doc to `0.5 ** (days/60)`. If the intended
   curve was the faster e-folding decay, the key must be renamed instead — but the two cannot
   keep disagreeing.

None of these block S12a except 1, which must be decided within it.
