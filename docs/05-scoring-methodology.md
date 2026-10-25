# 05 — Wallet Scoring Methodology

All parameters referenced here live in [`config/scoring.yaml`](../config/scoring.yaml). Formulas are
written so they can be implemented directly; nothing is left to interpretation.

## 1. The shape of the score

```
overall_score            = Σ (weight_d × shrunk_subscore_d)   over 7 performance dimensions
confidence_score         = Σ (w_q × quality_indicator_q)      over 5 quality indicators
confidence_adjusted_score = 50 + (overall_score − 50) × (confidence_score / 100)
```

Then hard gates, then banding. **Confidence is reported separately and applied as a shrinkage
toward neutral — never folded into the performance score.** A wallet with a 92 performance score and
a 30 confidence ends up near 63, which is the honest reading: the evidence does not support a strong
claim in either direction.

## 2. Weights and why

| Dimension | Weight | Justification |
|---|---|---|
| Early-entry ability | **0.25** | The stated primary objective, so it leads — but only at a quarter. Buying early is the *easiest* thing to fake (buy everything, early, and you will be early to the winners too), so it cannot dominate. |
| Behavioral integrity | **0.15** | The single largest source of false positives is a coordinated or insider wallet with genuinely excellent statistics. It carries a heavy weight *and* a hard gate. |
| Profitability | **0.15** | Deliberately not the top weight. The goal is to find wallets that *identify* opportunities, not the richest wallets. A whale with one enormous win scores high on profitability and is useless as a monitoring signal. |
| Repeatability | **0.15** | The main defence against one lucky trade. Weighted equally with profitability because "consistently decent" beats "once spectacular" for this purpose. |
| Risk management | **0.10** | Matters, but memecoin trading is inherently high-variance. Over-weighting drawdown would select for inactive wallets. |
| Token selection | **0.10** | Benchmarked, so it measures real selection skill rather than market beta. Kept modest because it overlaps conceptually with early-entry. |
| Exit effectiveness | **0.10** | Important and genuinely separate from entry skill, but measured on a smaller subset of positions (only those where a favourable exit existed), so it has less evidence behind it. |

Sum: 1.00. Asserted at load time.

**Two dimensions are deliberately not weighted at all** — confidence (reported separately, §8) and
data completeness (carried through `metric_quality`, §9). Mixing them into the weighted sum would
conflate "how good is this wallet" with "how much do we know", which the requirements explicitly say
must be kept apart.

## 3. Shrinkage — the core anti-luck mechanism

Every rate-based input is shrunk toward a neutral prior by sample size:

```
shrunk = (n / (n + k)) × observed + (k / (n + k)) × prior
```

with `prior = 50` on the 0–100 scale and a per-dimension `k` (early-entry 15, token selection 15,
profitability 12, exit effectiveness 12, repeatability 8, risk 6).

Why this rather than a minimum-trades floor:

| | Hard threshold (e.g. "cap at 35 if n<5") | Shrinkage |
|---|---|---|
| 4 trades, 100% win rate | capped at 35 | ≈ 21 |
| 5 trades, 100% win rate | **35 → jumps to full credit** | ≈ 24 |
| 40 trades, 60% win rate | full credit | ≈ 58 |
| Gaming | add one filler trade to cross the line | no line to cross |
| Behaviour | discontinuous, exploitable | smooth, monotone in evidence |

The hard floor at `gates.min_closed_positions: 5` still exists, but only to decide whether a wallet is
*reportable at all* — below it the wallet is marked unscored, not scored low. Conflating "we don't
know" with "this is bad" is a modelling error.

## 4. Dimension 1 — Early-entry ability (weight 0.25)

### Per-entry score

For each position `i` that qualifies (see the liquidity gate below):

```
e_i = 0.45 × age_i + 0.35 × mcap_i + 0.20 × prevolume_i
```

**Age** — the dominant term, because entering a token minutes after launch is the behaviour being
sought:

```
age_i = 100 × 2^(−age_hours / 12)      →  0h:100   12h:50   24h:25   48h:6
```

**Market cap at entry** — measured in log space, since token valuations are log-distributed:

```
mcap_i = 100 × (1 − log(mcap / 50000) / log(2_000_000 / 50000))   clamped to [0, 100]
       →  ≤$50k: 100   $316k: 50   ≥$2M: 0
```

**Pre-attention volume** — was the wallet in before the crowd arrived?

```
prevolume_i = 100 if (volume in the hour before entry / peak hourly volume in the 24h after entry) ≤ 0.10
              scaled linearly to 0 as that ratio approaches 1.0
```

**The qualifying gate is essential.** An entry earns early-entry credit only if the token had
`liquidity ≥ $3,000` at entry. Without it, a wallet that snipes the first two transactions of every
rug would score near-perfect on age and market cap. Entries below the floor are counted separately as
`junk_entry_rate` — reported, and fed into risk and integrity, but **not** rewarded.

Aggregate, then shrink:

```
EEA_raw    = mean(e_i) over qualifying entries
EEA_shrunk = shrink(EEA_raw, n_qualifying, k=15, prior=50)
```

**Why this is not look-ahead.** Every input is observable at the entry slot: the token's age from the
pool creation time, the market cap derived from the pool's own reserves, and volume already elapsed.
The only forward-looking element is `prevolume`'s denominator (peak volume in the following 24h) —
and it is used solely to establish *that the token had not yet been noticed*, never to reward
knowledge of the outcome. A token that never gets noticed yields a low ratio and a *high* score,
which is correct: the wallet was genuinely early regardless of whether the crowd ever arrived.

## 5. Dimension 2 — Profitability (weight 0.15)

```
profitability = 0.50 × roi_component
              + 0.30 × profit_factor_component
              + 0.20 × magnitude_component
```

```
roi_component      = 100 / (1 + exp(−median_roi / 0.50))
                     −100% → 12     −50% → 27      0% → 50      +50% → 73     +100% → 88
profit_factor_comp = 100 × min(gross_profit/gross_loss, 5.0) / 5.0
magnitude_comp     = 100 × clamp(log10(1 + realized_pnl_usd) / log10(1 + 100_000), 0, 1)
```

Three deliberate choices:

- **Median ROI, not mean.** A single 50× return must not be able to carry a dimension. The
  requirement to report both median and mean is satisfied in the metrics layer; the *score* uses the
  median precisely because it is outlier-resistant.
- **Profit factor alongside ROI.** ROI measures return on capital; profit factor measures whether
  wins outweigh losses in aggregate. They fail differently — a wallet can have a great median ROI on
  tiny positions and a terrible profit factor overall.
- **Magnitude is logarithmic and capped.** It exists so that a wallet earning $80k is distinguishable
  from one earning $400, but the log scale and the cap mean sheer size can never dominate. This is
  the specific guard against "rewarding raw trading volume".

Shrinkage `k = 12`.

## 6. Dimension 3 — Repeatability (weight 0.15)

```
repeatability = 0.35 × n_eff_component
              + 0.30 × win_rate_component
              + 0.20 × temporal_dispersion_component
              + 0.15 × regime_coverage_component
```

**Effective sample size** — correlated positions are down-weighted before counting:

```
c_i   = (# other positions by this wallet sharing a token)
      + (# other positions opened in the same slot/bundle as another of its positions)
w_i   = 1 / (1 + c_i)
n_eff = (Σw_i)² / Σw_i²                          (Kish effective sample size)
n_eff_component = 100 × min(n_eff, 15) / 15
```

This is the mechanism that stops `n_trades` from being a proxy for skill. A wallet with 40 buys
across 3 launches in one afternoon has an `n_eff` near 3, not 40.

**Temporal dispersion** — normalised Shannon entropy of closed positions across calendar months:

```
H = −Σ p_month × log(p_month)
temporal_dispersion_component = 100 × H / log(months in collection window)
```

Twenty wins concentrated in one week scores near zero; the same twenty spread across five months
scores high. This is the practical substitute for "consistency across different market conditions"
given a single collection window (doc 00 §4).

**Regime coverage** — the share of distinct launch-week cohorts (`regime_min_distinct_weeks: 3`) the
wallet has entered. Directly tests "repeatable across multiple independent token launches".

`win_rate` is owned *exclusively* here (§7) and is shrunk on its own with `k = 8`, so a 100% win rate
over 3 trades contributes ≈ 21 rather than 100.

## 7. Metric ownership — how double-counting is prevented

Correlated indicators must not be measured twice. Each base metric belongs to **exactly one**
dimension, enforced by `metric_definitions.owning_subscore` with a database CHECK constraint.

| Metric | Sole owner | Deliberately excluded from |
|---|---|---|
| `token_age_at_entry`, `mcap_at_entry`, `prevolume_ratio` | early_entry | token_selection |
| `median_roi`, `profit_factor`, `realized_pnl_usd` | profitability | repeatability, risk |
| `win_rate` | repeatability | profitability |
| `n_eff`, `temporal_dispersion`, `regime_coverage` | repeatability | risk |
| `max_drawdown_mtm`, `tail_return_p5`, `avg_win/avg_loss`, `hhi_cost_basis`, `failed_tx_rate` | risk | profitability |
| `cohort_excess_return`, `hit_rate_3x` | token_selection | early_entry |
| `realized_capture`, `adverse_exit_rate`, `achievable_exit_fraction` | exit_effectiveness | profitability |
| coordination/affiliation rates | integrity | risk |

Known residual overlaps, and how each is handled:

- `n_eff` derives from position count, and `risk.hhi` uses the same positions. They measure different
  things (statistical independence vs capital concentration), and are capped independently. Accepted.
- `median_roi` and `hit_rate_3x` both relate to returns. `hit_rate_3x` is *market-cap-relative and
  cohort-benchmarked*, whereas `median_roi` is the wallet's own realised return; a wallet can be
  excellent at selecting tokens and bad at exiting them, which is exactly why these are separate
  dimensions. Accepted, and worth watching in validation.
- `avg_win/avg_loss` (risk) and `profit_factor` (profitability) are mathematically related. Kept
  separate because one is bounded by rank-order and the other by magnitude, but **flagged as the most
  likely redundancy**; if validation shows they co-move above ρ≈0.85, drop `avg_win/avg_loss` and
  redistribute the 0.20 toward drawdown.

## 8. Dimension 4 — Risk management (weight 0.10)

```
risk = 0.30 × drawdown_component
     + 0.25 × tail_component
     + 0.20 × loss_ratio_component
     + 0.15 × concentration_component
     + 0.10 × failure_component
```

```
drawdown_component   = 100 × exp(−max_drawdown_mtm / 0.60)    0%→100  30%→61  60%→37  100%→19
tail_component       = 100 / (1 + exp(−(p5_roi + 0.60) / 0.20))   p5 −60%→50  −40%→73  −80%→27
loss_ratio_component = 100 × clamp((avg_win/avg_loss) / 3.0, 0, 1)
concentration_comp   = 100 × clamp((0.50 − HHI) / (0.50 − 0.05), 0, 1)
failure_component    = 100 × (1 − min(failed_tx_rate, 0.50) / 0.50)
```

**The critical detail is that drawdown is mark-to-market, not realized.** A wallet that buys and
never sells has zero realized drawdown. Scoring that as flawless risk management would reward exactly
the behaviour that destroys a follower — the wallet's own paper wealth looks intact right up until
the bags are worthless. `equity_snapshots.marked_value_usd` forces open positions into the curve.

`concentration_component` uses the Herfindahl index over cost basis. HHI ≤ 0.05 is treated as
well-diversified; 0.50 means the wallet is effectively a single-token bet, whatever its trade count
suggests.

Shrinkage `k = 6` — the smallest in the model, because drawdown and tail statistics are meaningful at
smaller samples than win rates.

## 9. Dimension 5 — Token selection (weight 0.10)

This dimension exists to answer a question no other dimension can: **did the wallet pick good tokens,
or did it pick tokens in a good week?**

```
excess_i = log(1 + token_max_multiple_30d_i) − log(1 + cohort_median_max_multiple_30d)
cohort_excess_component = 100 / (1 + exp(−mean(excess_i) / 0.50))
hit_rate_component      = 100 × clamp(hit_rate_3x / 0.25, 0, 1)     # token reached ≥3× entry mcap
token_selection = 0.60 × cohort_excess_component + 0.40 × hit_rate_component
```

The cohort is the set of tokens **launched in the same week** (falling back to all tokens when a
cohort has fewer than 10 members). Subtracting the cohort median removes the market's contribution:
if every token launched that week did 5×, a wallet needs to beat 5× to score, not merely achieve it.

Log-ratios rather than raw differences, because multiples are multiplicative — the difference between
2× and 4× should count the same as between 10× and 20×.

**`cohort_benchmarks.available_at` prevents a subtle leak:** a benchmark computed over a full 30-day
window is not available on day 1 of that window. A score dated mid-window may only use benchmarks
whose `available_at` has passed.

## 10. Dimension 6 — Exit effectiveness (weight 0.10)

```
capture_i = realized_return_pct / max_favorable_return_pct     clamped [0, 1]
exit_effectiveness = 0.60 × capture_component
                   + 0.25 × adverse_avoidance_component
                   + 0.15 × impact_component
```

**Capture is only computed where an opportunity existed.** If a token never traded above the wallet's
entry price, `max_favorable_return ≤ 0` and the position is **excluded** from the capture average —
not scored zero. Penalising a wallet for failing to sell into a pump that never happened would be
nonsense, and this is the single most common way exit metrics are miscalculated.

**Adverse avoidance** — the share of closed losers exited only *after* the token had already fallen
80% from entry. Measures whether losses get cut or held to zero.

**Impact** — the share of profitable exits whose position value was ≤1% of pool liquidity at exit.
This addresses "realistic returns achievable after the wallet's entry, accounting for liquidity,
slippage, and exit opportunities". A 40× return on a position that was 30% of the pool was never
actually withdrawable, and valuing it at face value is how copy-trading backtests become fiction.

Shrinkage `k = 12`.

## 11. Dimension 7 — Behavioral integrity (weight 0.15, plus a gate)

Starts at 100 and subtracts **rate-based** penalties:

| Signal | Penalty | Rationale |
|---|---|---|
| Launcher-affiliated entries | −40 × rate | Bought in the launch transaction, fee paid by the deployer, or within the first 4 transactions after launch. Insider by construction — the "skill" is not transferable. |
| Same-slot cohort buys | −25 × rate | Shares a slot or Jito bundle with ≥2 other wallets. Bundled launch participation. |
| Common-funder cluster | −15 × rate | Shares a first funder with ≥2 wallets. **Exchange funders excluded** — see below. |
| Sniper-bot signature | −20 | Median hold < 2 min across ≥30 trades. Not necessarily malicious, but not a human signal to follow either. |
| Wash-trade signature | −30 | Self-matching or circular SOL flows inflating apparent volume. |
| Fresh-wallet sophistication | −10 | Wallet < 24h old behaving like a specialist. |
| Concentration then dump | −20 | Held >20% of float, then exited heavily. |

```
integrity = clamp(100 − Σ penalties, 0, 100)
```

**Exchange funders must be excluded.** Custodial and exchange withdrawal wallets fund enormous
numbers of unrelated users. Treating a shared funder as evidence of common control without this
exclusion would flag almost every retail wallet on the same exchange — the most common way this kind
of analysis produces false positives.

**Integrity acts as both a weight and a gate.** At weight, it continuously degrades borderline cases.
At `integrity_min: 30`, it hard-excludes. The justification for the gate is that a coordinated insider
wallet's statistics are simply not the kind of signal this tool exists to transfer; a very high
performance score attached to obvious insider behaviour would be actively misleading, and averaging
would let other dimensions rescue it.

**This dimension is evidence of coordination, never proof of it.** Every penalty is a heuristic with a
known false-positive mode, which is why the penalties are rate-based (occasional coincidence costs
little) and why `exclude_exchange_funders` defaults to true.

## 12. Dimension 8 — Data confidence (reported separately, then applied)

```
confidence = 0.30 × coverage            # share of the window's activity actually decoded
           + 0.25 × price_provenance    # share of entries priced from the swap itself
           + 0.20 × sample_adequacy     # 100 × min(n_eff, 30)/30
           + 0.15 × recency             # 100 × exp(−days_since_last_trade / 60)
           + 0.10 × metadata_completeness
```

Hard caps, because these conditions make a high score unreliable regardless of other indicators:

- `coverage < 0.50` → confidence ≤ 40. We are seeing less than half the wallet's activity; its
  win rate is computed on an unknown sample.
- `interpolated_price_share > 0.50` → confidence ≤ 50. Entry prices came from candles rather than the
  swap itself, so entry timing and valuation are approximate.

**Application:**

```
confidence_adjusted_score = 50 + (overall_score − 50) × (confidence_score / 100)
```

A 90 performance score at 40 confidence becomes 66. A 20 performance score at 40 confidence becomes
38. Weak evidence pulls toward neutral in **both** directions — it neither rewards nor punishes, it
declines to make a claim. This satisfies "distinguish confidence in a wallet's performance from the
wallet's performance score itself" and "penalize insufficient trading history and unreliable data"
without conflating the two.

## 13. Gates, bands and recommendations

Gates are evaluated first and short-circuit everything:

| Gate | Effect |
|---|---|
| `confidence < 25` | **Excluded** — too little evidence to rank |
| `closed_positions < 5` | **Unscored** — reported, not ranked |
| `integrity < 30` | **Excluded** — `gated_by = 'integrity<30'` |

Then, on `confidence_adjusted_score`:

| Score | Band | Recommendation |
|---|---|---|
| ≥ 75 | Track | Add to watchlist, monitor closely |
| 60–74 | Watch | Monitor; look for more evidence |
| 45–59 | Investigate | Insufficient or mixed evidence — gather more |
| < 45 | Deprioritize | No demonstrated edge |

**Cluster deduplication applies at ranking time.** The top-N list admits at most
`max_per_cluster_in_top_n: 2` wallets from any one cluster, and `independence_factor = 1/√(cluster
size)` discounts each member. Ten wallets in one operator's cluster are one signal, and a leaderboard
that presents them as ten independent confirmations is lying about the strength of its evidence.

**Percentiles are suppressed below `min_wallets_for_percentiles: 50`.** A "top 5%" claim computed over
12 wallets is meaningless.

## 14. Explaining a score — and what would change it

Three layers of explanation, all deterministic and all stored:

**1. `sub_scores.explanation`** (`NOT NULL`) — one sentence per dimension, generated from the numbers.
Example: `"Early-entry 71 (n=18, k=15): median entry 4.2h after pool creation at $180k mcap; 11 of 18
entries were before 10% of peak 24h volume."`

**2. `score_evidence`** — the specific rows behind each sub-score: the positions, swaps, benchmark
comparisons and quality flags. Every claim in a report must be traceable to an evidence row.

**3. `score_sensitivity`** — the counterfactual, computed by re-running the scorer with one metric
moved to its next-band boundary:

> `median_roi` −0.12 → +0.18 requires **6 more profitable closed positions**; early_entry would rise
> 34 → 58, overall 58 → 65, moving the wallet from *Investigate* to *Watch*. (`direction: increase`)

The three largest effects by `|score_delta|` are surfaced, in both directions — what would raise the
score and what would lower it. This answers requirement F directly, and because it is a re-run rather
than a narrative, it cannot drift from the model.

## 15. Known weaknesses of this methodology

Stated before implementation, so validation can target them:

1. **The weights are reasoned, not fitted.** They are defensible priors, not optimised values. Fitting
   them to historical winners would guarantee overfitting on a small sample — the exact failure mode
   doc 09 §7 guards against. They must be frozen before validation.
2. **`hit_rate_3x` inherits survivorship from discovery.** Candidates are found via tokens that
   succeeded. The mitigation is the full-history evaluation rule (doc 02 §4.4), which ensures losers
   are counted — but it does not remove every trace of the bias, and `data_quality_flags` should say so.
3. **Integrity heuristics have unknown false-positive rates.** They are priors, not measurements. Until
   they are calibrated against a hand-labelled sample, the penalties should be treated as soft.
4. **Benchmark cohorts inherit our own coverage gaps.** A launch-week median computed over the tokens
   we happened to observe is not the true launch-week median. Cohorts below 10 members fall back to
   `all_tokens`, which is a blunt correction.
5. **`prevolume` requires continuous volume observation** that a rate-limited collector may not have
   retroactively. Positions lacking it are   excluded from that term rather than defaulted, which biases
   early-entry scores toward well-sampled periods.
6. **Nothing here measures copyability.** A wallet can score 90 and still be uncopyable — sub-second
   entries, or positions too large for the pool. That is measured separately in doc 09 §6 and is the
   most decision-relevant test of all.
