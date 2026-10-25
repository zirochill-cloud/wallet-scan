# 09 — Validation and Backtesting

This is the deliverable the whole project exists to produce. The scoring model is a hypothesis; this
document is the test, and it is written **before** any results exist so the criteria cannot be
adjusted to fit an unflattering outcome.

## 0. The question, stated precisely

> Given a wallet ranked highly at time `T` using only information available at `T`, do that wallet's
> subsequent entries outperform the entries of a randomly chosen wallet, after accounting for the
> price a follower could actually get?

Three things this question deliberately excludes:

- **Not** "did the wallet make money?" Many wallets made money in a rising market.
- **Not** "did the wallet's tokens go up?" The wallet's own returns are already in the score, so using
  them as the target would be circular.
- **Not** "is the score internally consistent?" A model can be perfectly consistent and predict nothing.

The target is **out-of-sample, benchmark-relative, copy-adjusted** performance.

## 1. Point-in-time discipline

Everything rests on being able to answer: *what did the system know at `T`, and when did it know it?*

The schema enforces this with **two clocks** on every event (`event_time`, `ingest_time`) and an
`as_of` on every score and metric snapshot. Three rules follow:

1. **An event may be used at `T` only if `event_time ≤ T` AND `ingest_time ≤ T`.** Filtering on
   `event_time` alone lets late-arriving data leak backwards.
2. **A benchmark may be used only if `cohort_benchmarks.available_at ≤ T`.** A 30-day benchmark is not
   available on day 1 of those 30 days.
3. **A score is never recomputed with later data.** `scores` rows are immutable and keyed by
   `(score_run_id, address)`; `score_runs.as_of` fixes the cutoff.

### The automated look-ahead test

This must be a test that fails the build, not a convention:

```
for a random sample of 500 stored positions:
    T = the position's opened_at
    assert every input row used to score it has event_time  <= T
    assert every input row used to score it has ingest_time <= T
    assert every joined cohort_benchmarks row has available_at <= T
    assert the score row's as_of <= T
```

Additionally: **replay determinism.** Re-running a score for a past `as_of` must reproduce the stored
result bit-for-bit. If it does not, something in the pipeline is reading current state — the usual
cause is a lookup against a current-state table (a `pool_snapshots` row, a live price) or a
`now()`-derived default.

## 2. Temporal split

Forward collection only, so the split is temporal thirds of the collected window:

| Segment | Share | Purpose |
|---|---|---|
| **Discovery** | first 40% | Form the candidate wallet pool; compute metrics; inspect behaviour |
| **Validation** | middle 30% | Primary out-of-sample test |
| **Test** | final 30% | Confirmatory, touched once |

Weights are frozen (`frozen: true`) before the validation segment is scored. If the config changes
after seeing validation results, the validation is void and must be re-run on the test segment with
the new config — and the change must be documented.

**Sub-period stability** is the practical substitute for multi-regime testing given one collection
window: rank IC is computed separately across launch-week cohorts, and across **up-weeks** (weeks where
the median new token appreciated) versus **down-weeks**. A model that works only in up-weeks is a
market-beta detector, not a skill detector.

## 3. Baselines

A ranking is meaningless without something to beat. Four baselines, all computed on identical data and
windows:

| Baseline | Definition | What it rules out |
|---|---|---|
| **Random wallets** | 1,000 wallets sampled uniformly from all wallets that traded in the discovery window | "The metric is positive for everyone in a bull market" |
| **All-wallet median** | The median forward performance across every wallet in the pool | "Anything beats a random wallet because the pool is preselected" |
| **Buy-every-new-token** | Equal-weight entry into every new token meeting the discovery liquidity filter | "You don't need wallets at all, just buy everything new" |
| **Random weights** | The same 7 dimensions scored with **randomly drawn weights**, averaged over 200 draws | "The specific weights add nothing over any weighting of these same metrics" |

The last one is the most diagnostic and the most often skipped. If a random-weight ensemble of the same
inputs performs as well as the tuned weights, the *weights* carry no information — the model is
equivalent to an equal-weight average, and should be simplified to one.

## 4. Primary metric: rank information coefficient

```
rank_IC = Spearman( confidence_adjusted_score(as_of = T),  forward_performance( T → T+30d] )
```

where `forward_performance` is the **cohort-excess** forward return of the wallet's entries — the same
log-ratio used in the token-selection sub-score, but computed strictly after `T`.

This single number answers the core question: does a higher rank predict better forward entries?

**Uncertainty via block bootstrap, not naive resampling.** Token outcomes within a launch week are
strongly correlated — a single narrative can lift or sink every token that week. Resampling wallets
independently would understate uncertainty badly. The bootstrap therefore resamples **launch-week
cohorts** with replacement, carrying all wallets and tokens in each drawn week, with 10,000 iterations
for 95% intervals.

**Statistical power must be stated, not glossed over.** Using Fisher's z:

```
n ≈ ((z_{α/2} + z_β) / atanh(ρ))² + 3
ρ = 0.25, two-sided α = 0.05, power = 0.80  →  n ≈ 123 wallets
```

So **~123 wallets is the minimum to detect ρ = 0.25.** With 150–200 candidates the test can detect a
moderate relationship and cannot detect a weak one (ρ ≈ 0.10 would need ~780). If the observed CI is
wide, the honest conclusion is "insufficient power", **not** "no effect" and certainly not "works".

**Multiple-testing discipline.** The primary metric is pre-registered above. Testing 7 dimensions × 3
windows × 4 baselines would generate ~84 comparisons, and at α = 0.05 roughly 4 would be "significant"
by chance. Dimension-level results are therefore reported as **exploratory** with Benjamini–Hochberg
adjustment, and only the pre-registered primary metric can support a claim of success.

## 5. Secondary metrics

| Metric | Definition | Purpose |
|---|---|---|
| **Precision@k** | Of the top-k ranked wallets, the share whose forward performance exceeded the baseline | Directly measures watchlist quality |
| **Lift** | Precision@k ÷ baseline precision | Effect size in a readable form |
| **Recall proxy** | Of the top-N forward performers in the validation window, how many were in the top decile at `T` | "Missed opportunities" |
| **False-positive rate** | Watchlist wallets whose forward performance fell below the 50th percentile | "False positives", per the requirements |
| **Ranking stability** | Kendall's τ between rankings under ±25% weight perturbation | Detects a fragile, overfit ranking |
| **Coverage** | Share of the pool with confidence above the gate | Whether the model is silent too often to be useful |

## 6. The decision-relevant test: the shadow portfolio

A wallet can score 90 and still be **useless to a follower**. This section measures the thing a user
actually cares about, and it is the test most likely to overturn a good rank IC.

**Setup.** For each entry by a top-decile wallet at time `t`:

1. **Follower entry at `t + Δ`, Δ ∈ {next slot, +1 min, +5 min, +30 min}.** The four deltas separately
   reveal *how fast an edge is required to exist*.
2. **Price at `t + Δ`** is taken from the reconstructed pool reserve state at that time — not from the
   wallet's own execution price, which the follower cannot obtain.
3. **Slippage and impact** from the constant-product formula at a realistic size: a position of `q`
   quote units into a pool with quote reserve `Q` incurs roughly `q/Q` price impact each way. Position
   size is capped at 1% of pool liquidity, so a wallet whose optimal size was 20% of the pool is not
   credited with a return the follower cannot reach.
4. **Fees** applied: priority fee, platform fee, and the AMM LP fee, both directions.
5. **Exit rule, fixed in advance** (to avoid hindsight): exit when the wallet exits, or after a fixed
   horizon (7 days), whichever comes first.
6. **Aggregate** into a portfolio that equal-weights signals as they arrive.

**Compared against** the buy-every-new-token baseline over the same period, same slippage model,
same fees.

**Interpretation:**

| Result | Meaning |
|---|---|
| Shadow return materially above baseline, net of costs | The ranking is **actionable**. This is the only result that justifies Phase 10. |
| Shadow return ≈ baseline | The wallets are skilled but **the edge is not transferable**. Useful as research, not as a signal. |
| Shadow return ≈ 0 or negative despite positive rank IC | The edge requires sub-second execution. Explicitly label the ranking as **informational only**. |
| Shadow return degrades sharply as Δ grows | Quantifies the latency budget, and is a direct input to any future alerting design. |

This is why copyability is measured rather than assumed: it is the difference between "these wallets are
good" and "you can do something with this".

## 7. Bias audits

Each is a specific, runnable check.

**Survivorship bias.** The candidate pool is formed from wallets found via successful tokens, so it is
survivor-biased by construction. Measure it: compare the score distribution of wallets discovered
*only* through winning tokens against wallets that appear in the pool through losing tokens too. If
discovery-only-through-winners wallets score systematically higher, the bias is present and its
magnitude is now known and reportable. **It cannot be eliminated from an incomplete historical
universe** — it can only be bounded and disclosed.

**Look-ahead bias.** The automated test in §1, plus replay determinism.

**Overfitting.** Four checks: parameter count is small (7 weights + ~20 thresholds); weights frozen
before validation; ±25% perturbation stability; and the random-weights baseline in §3. A ranking that
survives all four is unlikely to be an artifact.

**Insufficient sample size.** Every metric carries `n`; every sub-score records `sample_size` and
`shrinkage_applied`; the confidence score gates thin wallets out of the ranking entirely. Report the
distribution of `n_eff`, not just its mean — if the median wallet has `n_eff` of 4, the leaderboard is
mostly noise and should be presented as such.

**Multiple comparisons.** §4.

**Selection of the discovery window.** If the discovery period happened to contain an unusual number of
large winners, the model may be tuned to a regime. Check by re-running discovery on each half of the
discovery segment and comparing the resulting candidate pools. Substantial divergence means the
candidate set is unstable.

## 8. Tracking forward: alerts and outcomes

Once monitoring ships, every alert gets an `alert_outcomes` row at fixed horizons. Consequences:

- **False-positive rate is measured**, not estimated. `was_false_positive` is set mechanically against
  a pre-declared threshold.
- **The watchlist's performance is tracked prospectively**, which is the strongest available evidence:
  it is genuinely out-of-sample in time, with no possibility of look-ahead.
- **Calibration is observable.** If wallets scored 80 behave identically to wallets scored 50, the
  0–100 scale is uninformative and should be replaced with the band alone.

This is the only validation that gets harder to argue with as time passes, and it costs nothing but
patience to start early.

## 9. Pre-registered success and failure criteria

Declared in advance, so that the conclusion is a measurement rather than a judgement call.

### Success requires all four

| # | Criterion |
|---|---|
| S1 | `rank_IC > 0` in the validation segment with a **95% bootstrapped CI excluding zero**, on ≥123 wallets |
| S2 | Same sign in **at least 2 of 3** held-out sub-periods |
| S3 | `precision@10` at least **1.5×** the random-wallet baseline |
| S4 | Shadow portfolio (Δ = 1 min) net return **above** the buy-every-new-token baseline |

### Failure — and what each result means

| # | Finding | Conclusion | Action |
|---|---|---|---|
| F1 | CI includes zero, on ≥123 wallets | No demonstrated predictive value | Reduce to descriptive analytics; publish the negative result |
| F2 | CI too wide (n < 123) | **Insufficient power — not a negative result** | Collect longer; do not conclude either way |
| F3 | S1–S3 hold but S4 fails | Wallets are skilled; the edge is **not copyable** | Ship as informational ranking; label it clearly; no alerts |
| F4 | Random-weights baseline matches the tuned weights | The weights add nothing | Simplify to equal weights |
| F5 | Rank IC positive in up-weeks only | Detecting market beta, not skill | Rebuild with stronger cohort normalisation or abandon |
| F6 | Ranking unstable under ±25% weight perturbation | Overfit | Fewer parameters; re-validate |

**F2 deserves emphasis.** A wide confidence interval is not evidence of failure, and reporting it as
such would be as dishonest as claiming success from noise. The correct response is to collect longer.

## 10. Reporting the result

The validation output is a written document containing, at minimum:

1. The pre-registered criteria, verbatim, alongside the observed values.
2. Discovery/validation/test window boundaries and sample sizes.
3. Every metric with its `n`, its confidence interval, and its bootstrap method.
4. Baselines on identical windows, including the random-weights ensemble.
5. The shadow portfolio at all four latency deltas, with the slippage model stated.
6. An explicit list of what could **not** be measured, and why.
7. The conclusion, drawn mechanically from §9 — **including a negative result if that is what the
   data shows.**

## 11. Known limitations of this validation

Stated up front so they cannot be discovered later as excuses.

- **One collection window.** "Across different market conditions" is approximated by cohort and
  up/down-week splits, not by testing multiple regimes. A model validated on one window may be fitted
  to that window's idiosyncrasies, and only longer collection addresses this.
- **Historical validation is impossible at this budget.** The forward-only design is a deliberate
  trade-off (doc 00 §2), and it means the first honest validation arrives months after the first line
  of code.
- **The shadow portfolio is a simulation.** Slippage from the constant-product formula is an
  approximation; real memecoin execution includes MEV, failed transactions, and competition. Real
  execution returns would be **worse**, never better — so this is an optimistic bound, and should be
  described as one.
- **`rank_IC` on a small pool is noisy.** With 150–200 wallets, a single extreme wallet can move the
  statistic. Report the metric with and without the top 1% of wallets.
- **Survivorship cannot be fully corrected**, only bounded and disclosed.
- **The score may be useful without `rank_IC` being significant.** If the top 10 wallets are genuinely
  better while the rest of the ranking is noise, `precision@10` will show it and rank IC will not.
  Both are reported for this reason.
- **This validates the ranking, not the tool.** A validated ranking with poor reports is still a poor
  tool, and no statistic here measures whether a human can act on the output.
