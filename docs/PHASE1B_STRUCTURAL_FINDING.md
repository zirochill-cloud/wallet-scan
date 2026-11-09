# PHASE 1b — STRUCTURAL FINDING: the pipeline cannot rank at five anchors

Written 2026-11-09. Scope: one finding, three paths, one recommendation. No code was written
for this document.

**Status: operative.** The quality filter is calibrated and working on real data. The
pipeline still produces **zero ranked wallets**, and the cause is coverage, not quality.

---

## 1. The finding

A wallet becomes rankable only if it appears in **≥3 distinct anchors' first-20 buyer lists**
(`confidence_gate.min_distinct_anchors: 3`, `config/scoring.yaml`). That is a coverage
condition on the anchor set, and coverage is a counting question, not a scoring one.

With `N` anchors contributing 20 first buyers each, and a buyer pool of `P` wallets from
which those buyers are drawn, a given wallet lands in a specific anchor's list with
probability `20/P`. The expected number of wallets appearing in **≥3** of `N` independent
lists is

```
E = P · Pr[ Binomial(N, 20/P) ≥ 3 ]  ≈  C(N,3) · 20³ / P²
```

The `C(N,3)` is the part that dominates: the count of *triples of anchors* grows cubically,
one factor of `N` is consumed by the population sum, and `P` is left squared in the
denominator.

| Anchors `N` | `P` = 12,500 | `P` = 50,000 | `P` = 100,000 |
|---|---|---|---|
| **5 (today)** | **0.0005** | **0.00003** | **0.000008** |
| 10 | 0.006 | 0.0004 | 0.0001 |
| 20 | 0.057 | 0.004 | 0.0009 |
| 30 | 0.20 | 0.013 | 0.003 |
| 50 | 0.95 | 0.062 | 0.016 |
| 100 | 7.4 | 0.50 | 0.13 |
| 200 | 53 | 4.0 | 1.0 |

*(Exact binomial tail, `k = 3`, `m = 20`.)*

**Observed: 0 of 100.** The seeded set is 5 anchors × 20 buyers = 100 slots and 100 distinct
wallets — not merely zero triplets, but zero *duplicate pairs*. Under the same model the
expected number of sharing pairs is `C(5,2)·(20/P)²·P = 4,000/P`, i.e. 0.32 at `P` = 12,500
and 0.04 at `P` = 100,000. Observing zero is therefore unremarkable, and it is also too small a
sample to infer `P`: both 0.04 and 0.32 are consistent with 0/100, so **the sample cannot
discriminate `P`**. The anchor-count requirement is stated as a range for that reason — it
cannot be reduced to a single number until `P` is measured, and measuring `P` requires either a
much larger anchor set or population-size data this tool does not have. Recorded in
`docs/PHASE1B_ANCHORS.md` ("cross-anchor overlap: 0 of 100");
the consumer run for that set reads `wallets seen 100 / scored 0 / gated out 100`, every
skip reason `early_entry_not_rankable`.

**One honest correction to the arithmetic that prompted this document.** The figures first
quoted for this finding — `~0.0004` at `N = 5`, `~0.22` at `N = 30`, `~1.0` at `N = 50` — are
all consistent with the formula above, but they correspond to `P` ≈ 12,500, not `P` = 100,000.
At `P` = 100,000 the same formula is roughly 40× smaller and pushes the requirement from
~49 anchors to ~196. `P` is unmeasured, so the required anchor count is a range, not a number:
**tens to low hundreds.** Every value in that range is far beyond 5.

*Caveat on the model.* Uniform sampling is the null hypothesis, and the null is the point:
zero overlap is what a world with no shared wallets looks like. Persistent sniper bots that buy
every launch would raise overlap above the null — and those are precisely the wallets the
filter rejects 72% of the time. Overlap and quality are therefore not independent, which makes
Path 1's yield worse than the table implies (see §3).

## 2. Why this is not a filter problem

The filter and the rankability gate answer different questions and share no inputs:

- `quality_filter` reads **one wallet's own swap history** and returns a verdict. Nothing in
  it observes how many anchors mention the wallet.
- `min_distinct_anchors` reads the **anchor-wallet join** and counts distinct anchors. Nothing
  in it observes swap history, and no threshold in `quality_filter.py` can change it.

So the filter being right or wrong cannot change whether a wallet appears in three first-20
lists. The two failure modes are disambiguated by the data already on hand:

| Observation | What it rules out |
|---|---|
| 100 wallets seen, 0 scored, all skipped `early_entry_not_rankable` | Says nothing about wallet quality — no wallet reached the scoring stage in a bandable state. |
| All 25 accepted wallets have `distinct_anchors = 1` | The filter admits 25 wallets, and all 25 are unrankable for the same reason: coverage. |
| `aggregation.MIN_ANCHORS = 2` already excludes single-anchor aggregates | The gate's `3` is not even the first blocker — nothing aggregates at 1. |

`min_distinct_anchors` is a **validity rule** (D15): two anchors compute but cannot be ranked.
It was deliberately separated from `sample_full_credit_at: 5` (D5) so that a calibration edit
could not silently move the validity floor. It is the correct thing to be blocked on. A
pipeline that ranked a wallet from one anchor would be ranking a coincidence.

The re-calibration of the filter in `docs/PHASE1B_CALIBRATION.md` therefore changes nothing
about this finding, and this finding changes nothing about the filter's verdict.

## 3. Three paths to a ranked wallet

### Path 1 — a large independent anchor set

Buy or seed 30–50 independent anchors (different deployers and weeks, per
`docs/PHASE1B_STATE.md` §3). Statistical overlap then yields roughly 0–2 rankable wallets at
`P` ≈ 12,500, and needs ~200 anchors at `P` = 100,000.

- **Prerequisite:** 30+ *quality* anchors — tokens that pumped ≥5× within 30 days, in
  different weeks, with price history. That is a substantial user-supplied supply, not a quick
  ask.
- **Cost:** 30 launches × 20 first buyers = 600 wallets to profile, ≈3 paginated
  `getTransactionsForAddress` calls each (500 txs ≈ 2.2 s, cap 1,500) — roughly 40 credits and
  ~7 s per wallet, so **~24,000 credits and about an hour of RPC**.
- **Anti-correlation between overlap and quality:** persistent snipers are what produces
  overlap at scale, and they are the 72% the filter rejects on the real sample. The raw yield
  calculation (0.22 at `N` = 30, 1.0 at `N` = 50) is therefore an **upper bound**; the
  filter-adjusted yield is below it, so size the run against the filter's pass rate rather than
  the raw expectation. Path 1 may produce **zero** rankable wallets even at 196 anchors if the
  anchor set's buyer population is dominated by the wallets the filter correctly rejects.
- **Scale mismatch:** no amount of anchors in the 5–10 range moves `E` off ~0. 10 anchors is
  0.006 expected wallets. Ten more anchors is not a step toward rankability.

### Path 2 — KOL-curated anchors

Choose anchors because **known smart wallets bought them early**, so overlap exists by
construction rather than by luck. The KOL's early entry is the selection criterion.

- **Prerequisite:** an external label source. MadeOnSol's free tier is sufficient to start:
  200 calls/day (60/min burst), 2,465 tracked KOL wallets, KOL leaderboards and per-KOL PnL
  real-time even on the free tier (live trade *feeds* are 5-minute delayed; free keys last 30
  days and regenerate in one click). Base URL `https://madeonsol.com/api/v1`; `api.madeonsol.com`
  does not exist. Verified reachable 2026-11-09. Paginated per-wallet trade history is PRO+, so
  **trade reconstruction stays on Helius** — the API supplies the label, not the history.
- **Non-circular:** the KOL label is external evidence produced by someone else, not this
  tool's own output. It is not a re-scoring of our own verdicts.
- **Requires the filter:** a KOL wallet can itself be bundled, botted or an insider. The
  calibrated filter is what makes the label usable — 72% rejection on the real sample is the
  evidence that it can gate this without simply rubber-stamping celebrity wallets.
- **Limitation:** selection bias. KOLs are chosen for being followable and public, not for
  being the best traders, and their early buys are visible to everyone at the same time.

### Path 3 — relax `min_distinct_anchors` to 1, substituting filter + profit

Accept one-anchor wallets as candidates when they pass the filter and show a profitable exit.
"Repeatability" would then be claimed by profit + quality rather than by cross-anchor overlap.

- **Prerequisite:** explicit user approval. This is a **semantic change** to the validity rule
  — the score would mean "one good early buy that survived the filter" instead of "a pattern
  observed across independent launches". It also retires the reason D15 exists.
- **Technical consequence, now computed (2026-11-09).** Two changes are needed, not one: the
  gate's `min_distinct_anchors: 3` → `1`, **and** `aggregation.MIN_ANCHORS` (a code constant of
  `2`) → `1`. With one anchor every metric returns `not_rankable`, so the wallet never reaches
  the gate at all — which is exactly the observed 100 seen / 0 scored.
  With both changes: at `n = 1` the composite still passes the gate (84 for high-factor wallets),
  but shrinkage at `n = 1` (factor `1/(1+5) = 0.1667`) caps the adjusted score at ~46 for a
  raw-82 wallet. Output is **investigate-band, not track-band**. This is a real output — a list
  of wallets worth a human's time — not an empty ranking, but it is not the track-band ranking
  the tool was designed to produce. The cap is structural: even a raw-100 wallet at `n = 1`
  reaches only 49.00 adjusted (shrunk 58.33 × confidence 0.84), and `watch` needs 60, which
  would require a raw score above 178 on a 0–100 scale.
- **Advantage:** costs nothing and yields candidates from the 100 wallets already seeded.

## 4. Recommendation

**Path 2 — KOL-curated anchors.**

It is the only path that produces overlap without requiring 30+ user-supplied anchors (Path 1's
prerequisite) and without redefining what a rank means (Path 3's cost). It uses the asset this
project just built — a filter calibrated on 100 real wallets — as the gate that makes external
labels trustworthy rather than merely fashionable, and it keeps trade reconstruction on the RPC
path already implemented. The empirical entry cost is one free API key, not a campaign.

Its honest limitation is that it depends on external data: without MadeOnSol access or an
equivalent curated list, Path 2 does not start. Path 1 is the fallback if that data is
unavailable and the user can supply 30+ anchors; Path 3 remains available at any time but
should be chosen deliberately as a change of meaning, not as a workaround.

### What would change this recommendation

- MadeOnSol (or equivalent) access being unavailable **and** 30–50 verified anchors being
  cheaply available → Path 1.
- A measured one-anchor composite above 40 for filter-passing profitable wallets → Path 3
  becomes viable without a second config change, and the user may prefer it for cost.
- Evidence that KOL early entries are dominated by bundled/insider wallets the filter cannot
  separate → Path 2's premise is weak and Path 1 returns.
- External labels revealing 10+ wallets like `8GSDbEhMy3` → the filter has a systematic gap
  worth a rule, which changes the *filter*, not the path.

---

## In one paragraph

Five anchors produce 100 distinct buyers and zero cross-anchor overlap, which is exactly what
the arithmetic predicts: `C(N,3)·20³/P²` puts the expectation at 0.0005 wallets for today's
set and only reaches ~1 at 49 anchors (small buyer pool) or ~196 (large one). The filter is
working — 72% rejected, one documented false negative — and cannot influence this, because
rankability counts how many anchor lists contain a wallet while the filter reads that wallet's
own trades. The next move is a choice among a 30–50-anchor campaign, KOL-curated anchors, or
relaxing the validity rule; the recommendation is KOL curation.
