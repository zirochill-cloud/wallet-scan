# NOTES — carried state, open blockers, and measured loss

Working notes, not design documentation. Everything here is either measured in this
session or explicitly marked **carried from the prior session's handoff and not
re-verified**.

> **Provenance:** no `NOTES.md` existed on disk at the start of this session
> (`ls NOTES.md` → not found, 2026-10-26). The prior session's copy did not survive
> the workspace move. This file is recreated so the references below have a real
> target. Sections marked *carried* restate what the handoff asserted; they could not
> be re-verified against any artifact in this repo, because no artifact for them
> survived either.

---

## § ANCHOR_SELECTION_CRITERIA

*Status: REVISED 2026-11-07. The drawdown gate is retired; the opportunity gate is
operative and was applied to the 2026-07/09 anchor set (5/5 PASS).*

### Criteria (revised 2026-11-07)

An anchor must satisfy:

1. **Peak multiple >= 5x** over the genesis price within 30 days of launch.
2. **Peak legitimacy:** the peak price is not achieved by a single transaction. At
   least 3 distinct trades at or near the peak (>= 50% of it) must be observed, or
   >= 2 grid samples at that level spanning more than one 6-hour bucket. When the
   sampled series cannot settle it — buckets with no decoded transaction are simply
   absent — the peak window is measured densely before a verdict is recorded.
3. **Early-buyer opportunity:** at least 10 of the first 20 buyers entered at <= 25%
   of the peak price. This is the property the tool actually consumes: a run is only
   an opportunity if early buyers could be in it far below the top.
4. **Deployer diversity:** no two anchors in a set share a deployer.
5. **Temporal diversity:** no two anchors launch within 24 hours of each other.
6. **Timestamp spread:** at least one anchor in the set has first-20 buyer
   transactions spanning >5 minutes (evidence of organic discovery).

**Rationale.** Every memecoin gives its pump back: the 2026-07/09 set ran 95x to
1947x and fell 95.9%-99.999% from peak by day 30. That is the category, not a rug.
A rug is exit impossibility — liquidity pulled, holders trapped — and these tokens
kept trading: 47-76 dust-filtered trades *after* the peak and 16-28 in days 20-30
alone, so exit was continuously available for a month. The retired drawdown gate
asked "did this token hold its value", which no memecoin does, so it excluded the
entire population this tool exists to analyse. The opportunity gate asks the two
questions that separate a tradeable run from a single wash print instead: was there
a real market at the top, and could anyone be in below it.

**Dated entries.**

* **2026-11-07: drawdown gate retired.** Replaced by peak legitimacy and early-buyer
  opportunity. Previously: "<95% drawdown from peak", which no memecoin anchor
  passes.
* **2026-11-07: verified against the 5-anchor 2026-07/09 set — 5/5 PASS** (0/5 under
  the retired gate). Peak multiples 95.3x-1947x; 2-10 distinct trades at >= 50% of
  peak; 19-20 of 20 first buyers at <= 25% of peak. TIGRINO's verdict needed the
  dense window: its sampled grid had a 26-hour hole and showed only 2 near-peak
  trades, while the token actually sat at 93% of its peak a day earlier on a 3.3 SOL
  trade, and a dense sample of the peak window found 5 distinct transactions at or
  above half the peak.
* **2026-11-07: the pipeline was then run on this set** (Path B manual seeding, see
  `docs/PHASE1B_ANCHORS.md`). No wallet is rankable: each appears in exactly one
  anchor, and rankability needs >= 3 per wallet (D15). That is the gate working, not
  a seeding failure.

### History: the blocker this replaced

*Carried from the prior handoff; not re-verified in this repo. Kept because the
independence finding below still governs anchor sets.*

The retrospective seed-anchor approach for Phase 1b (candidate wallet discovery) is
blocked on two findings from the prior session:

1. **Non-independent anchors.** The anchors selected as "early discoverers" were not
   independent of one another — a cluster of wallets behaving as one entity counts as
   one data point, not many. Scoring them as separate successes would manufacture
   repeatable-looking performance from a single actor.
2. **The quality filter passed 44/45 bundled wallets.** A filter intended to reject
   coordinated or bundled launches admitted 44 of 45 wallets known to be bundled. As a
   quality filter it therefore has essentially no discriminating power on that sample,
   and any discovery built on it inherits that blindness.

**What unblocks it:** a re-specified anchor selection that (a) de-duplicates anchors by
behavioural cluster before they are counted as evidence, and (b) a quality filter whose
rejection rate on *known-negative* bundled sets is measured before the filter is trusted
on unknown sets. Neither is done.

**Independence:** this blocker belongs to **Phase 1b (discovery)**. It is independent of
the Phase 2 gate — Phase 2 passing says nothing about anchors, and fixing anchors does
not affect the reserve-derivation result. Phase 2 may proceed while this stays open; a
Phase 4 discovery build may not be *trusted* while it is open.

**Rankability threshold (D15, 2026-11-06):** anchor **count per wallet** is now a scoring
input, not only a discovery concern. The confidence gate requires
`distinct_computed_anchors >= 3`, so the requirement is ≥3 anchors per wallet *for
rankability* — not the ≥2 that suffice for a metric to be computed. A wallet with two anchors
cannot be ranked at any raw score; it is banded `promising_thin` and left unranked. Practical
consequence for anchor selection: **to rank N wallets, the anchor set must produce ≥3 anchors
per wallet on average across the rankable population** — i.e. anchors must overlap in their
buyer sets. A set sized for coverage (each anchor bought by a few wallets) yields 1–2 anchors
per wallet and a run where almost nothing is rankable. Check `promising_thin` volume after any
anchor-set change: if it dominates, the set is too thin to support a ranking and the answer is
more/overlapping anchors, not a lower gate. Recorded in `docs/PHASE1B_SCOPING.md` § Q4 (D15)
and `docs/PHASE1B_SECTION12.md` §5/§8.

**Status: operative.** As of S12b the gate reads `confidence_gate.min_distinct_anchors: 3`
from `config/scoring.yaml` and applies it alongside `confidence_gate.confidence_min: 40`; a
wallet below the anchor floor is excluded from ranking whatever its composite. This is the
threshold to plan anchor selection against.

---

## § DATA_LOSS_2026_10

*Status: quantified 2026-10-26. Prior-session row counts are **carried from the
handoff**; no artifact in this repo records them, so they cannot be re-verified here.*

### What the lost database contained

The schema defines 41 tables (`schema/001_init.sql`). Only Phase 0–1 writers existed,
so almost all were empty. As reported in the handoff:

| Table | Rows (reported) | Note |
|---|---|---|
| `pools` | **10** | the shortlist the collector was subscribed to |
| `raw_payloads` | **112,134** rows / 112,134 distinct sha256 | deduplicated across kill/recovery overlap |
| `credit_ledger` | 86 `websocket_data` rows in run 7 alone | bytes 86,115,303 recorded vs 86,370,338 archived |
| `job_runs` / `job_checkpoints` | runs 1–7 | run 2 SIGKILLed, run 7 killed by a DB outage |
| `transactions` | **0** | Phase 2 writes these; Phase 2 was not run until after the loss |
| `swaps` | **0** | ditto |
| `wallets` | **0** | ditto |
| all scoring/metrics/alerts tables | **0** | Phases 5–10 never ran |

Raw archive on disk (not in the DB): **165.8 MB**, growing at **18.98 MB/h** →
projected **455.6 MB/day**, plus 53 gap-recovery archives.

**Which wallets / mints / window:** *none and none.* Zero wallets and zero swaps were
ever persisted, so no analysed wallet, score, or position was lost. The 10 pool
addresses lived only in the lost database and are not recorded in this repo — the
surviving mainnet fixtures (`tests/fixtures/`) contain two of them incidentally
(`GKULyZJr9E…`, plus mints `48YEeDMG6u…`, `9rjHzQPpVL…`, `CsovxRtEM6…`). Observation
window: first collector run **2026-10-15** through at least **2026-10-25**, with
interruptions between runs.

Month-to-date spend when it was lost (also carried): helius **2,399**/1,000,000,
dexscreener 225/300,000, geckoterminal 50/40,000.

### Cost to recover — a footnote, not a crisis

| What | How | Cost |
|---|---|---|
| Equivalent WSS volume going forward | `ceil(bytes/100KB) × 2` credits | 165.8 MB → **≈3,316 credits** (0.33% of monthly cap) |
| Backfill the same transaction history | `getSignaturesForAddress` ≈1 credit/1,000 txs + `getTransaction` 1 credit each | ≈112k payloads → **≈112,200 credits (≈11% of the monthly cap)** |
| Discovery replay | GeckoTerminal / DexScreener | **free** (keyless) |
| Exact raw wire bytes as delivered | cannot be replayed | unrecoverable — but the underlying signed transactions are re-fetchable, which gives equivalent provenance |

**Correction to an earlier claim.** The framing "the collection window cannot be bought
back" was **too strong**. The *raw frames* cannot be replayed, but every analytically
useful artefact — which transactions happened, their instructions and balance deltas —
is on chain and retrievable for roughly 11% of one month's credit budget. What actually
cannot be recovered is the *unexamined* history beyond what we choose to backfill, and
that is a budget decision rather than a wall.

**Verdict: footnote.** Nothing derived was lost, backfill is affordable, and the only
genuinely lost artefact (raw wire bytes) has a signed equivalent.

### Addresses supplied at closeout (2026-10-26)

Carried verbatim from the closeout message — user-supplied, not recoverable from any
artifact in this repo, and **not yet classified** (wallet vs mint) beyond the four
whose vanity suffix marks them as pump.fun mints:

```
GB68eELgf864nrQHjNvrXHF6Xywpp1kYsb7fsGBupump   (pump.fun mint)
zj1jpp7QMveWHLs61vL9KMZf254KvW7j4AAmBF8ry2k    (unclassified)
EVgwa5CHBVwk6sCTa4QJrVXYBKngZhYqh2a2ZGDApump   (pump.fun mint)
7Y7V1a4m2nWK7BMgbka5B4vR1pDvCK7yva3Hnrqkraze   (unclassified)
91ryaCo5yGpYZM3bs6GUPs97VWJQj7RozBmqPULgpump   (pump.fun mint)
```

If these are the lost Phase 1a seeds, they are the only surviving record of that set.

### Identification of the two unclassified addresses (2026-11-07)

**Status: resolved on chain.** Both "unclassified" addresses are token **mints**, not
wallets, and both are now named. Verified with `getAccountInfo` (jsonParsed) on
2026-11-07, not from the user report alone:

| Address | Reported symbol | On-chain | Resolution |
|---|---|---|---|
| `zj1jpp7QMveWHLs61vL9KMZf254KvW7j4AAmBF8ry2k` | $BULLSHIT | SPL Token (classic) mint, 6 dp, supply 978,079,929.862300, mint authority revoked, **no token metadata** | Mint confirmed. Symbol is user-supplied only — a classic SPL mint with no metadata carries no on-chain symbol, so `$BULLSHIT` is not independently verifiable here. |
| `7Y7V1a4m2nWK7BMgbka5B4vR1pDvCK7yva3Hnrqkraze` | $NASDUQ | Token-2022 mint, 6 dp, metadata name/symbol **"Nasduck"**, mint authority revoked | Mint confirmed. Metadata says *Nasduck*, not NASDUQ — treat `$NASDUQ` as the user's label and `Nasduck` as the on-chain one. |

**Address-form correction.** The 2026-11-07 message typed this mint as
`7YV1a4m2nWK7BMgbka5B4vR1pDvCK7yva3Hnrqkraze` (43 characters), which has **no account on
chain**. The 44-character form recorded above — `7Y7V1a4…` — resolves to the mint. The
NOTES record was right; the message dropped a `7` after the leading `7Y`. Any future
reference uses the 44-character address.

**These five addresses are the 2026-11-07 anchor set** (user-supplied launch window:
2026-07-29 through 2026-09-19). They are the surviving record of the lost Phase 1a seeds,
and they are the only anchor candidates the project has. Per-anchor on-chain verification
(genesis, deployer, price path, first-20 buyers) is recorded in
`docs/PHASE1B_ANCHORS.md`; the raw probe is `spikes/verify_anchors.py`.

---

## § PROCESS_FAILURES

**2026-10-26: Third data-loss event.** Git history did not survive the cloud VM →
desktop move. Prior commits are gone; `1944c74` is the new baseline. The code
survived as files on disk; the history is lost. This is the third incident of the
same class (git restore on NOTES.md, 2026-10-18; VM → desktop move, 2026-10-26;
git history lost, same event but separate consequence). Mitigation going forward:
(a) git remote (GitHub, GitLab, or self-hosted) configured before any further work;
(b) nightly DB export to `data/exports/` (gitignored, and shipped to the remote as
a release asset if the DB matters); (c) NOTES.md and INVENTORY.md regenerated from
session log after each loss. The pattern is not the loss, it is the discovery of
the loss after further work was done. `git push` costs nothing and eliminates it.

*Status: mitigation (a) executed 2026-10-26 — `origin` =
`https://github.com/codebrokee/solscope.git` (private), `master` pushed at
`1944c74` and verified with `git ls-remote`. Mitigations (b) and (c) are
procedural and not yet automated.*

**2026-11-05: Design specs must be committed as they are written.** The aggregation
spec sat in this thread for several turns before it was committed to `docs/`. When
the cloud environment was killed, the only durable copy was the transcript import in
`.cloud-attachments/`. The recovery turn converted that into
`docs/RECOVERED_FROM_TRANSCRIPT.md`, which is the right outcome — but the correct
discipline is to commit a design spec to `docs/` **in the turn it is approved**, not
after a thread-loss event. Any spec that guides more than one turn of work should
exist in git the turn it is agreed. A conversation is not storage, and a transcript
import is a copy of a conversation, not a document; recovering this one depended on
the transcript still existing, which was luck rather than process.

*Status: closed 2026-11-05 — the spec is committed as
`docs/RECOVERED_FROM_TRANSCRIPT.md` §7, and the implementation in the same turn
(`solscope/aggregation.py`, `schema/003_wallet_aggregates.sql`) is derived from that
committed text rather than from the transcript.*

**2026-11-06: A fixture was arithmetically right and structurally impossible.** The §6 thin
row of the confidence design specified coverage 0.50 (2 of 4 anchors) *and* metadata 0.60
(3 of 5) — but both factors divide by the same set, the anchors the wallet was measured on, so
no wallet can satisfy both. The hand-check was done on the *factor values* and never on whether
one wallet could produce them; the composite 60.51 was therefore correct arithmetic about an
impossible object. Caught when S12b had to seed the row as real anchors rather than pass the
numbers in.

**Process rule that follows:** a fixture is a set of inputs to one entity, not a list of
independently plausible values. When two factors share a denominator, the fixture must be
built bottom-up from rows (how many anchors, how many computed, how many resolved) and the
factor values derived from it — never chosen per factor and then asserted. Verifying against
synthetic *rows* rather than synthetic *numbers* is what surfaced this, and is why D18 asks for
real seeded anchors.

**2026-11-06: My own arithmetic slip, caught by the same discipline.** While wiring the
composite into the consumer I predicted a five-anchor wallet would score 52.00, having used
sample 0.60 from the two-anchor case; five anchors against a divisor of five is 1.00, so the
composite is 60.00. The test failed, the check was corrected, and the assertion stayed — the
prediction being wrong in the test output is the process working, not a reason to loosen the
check.

**2026-11-06: §6's adjusted column was the wrong formula, and it looked fine.** Every adjusted
value in the matrix (80.19 / 29.25 / 66.66 / 34.53) was computed as
`50 + (raw − 50) × confidence/100`. That is `confidence_adjusted_score`, the pull-to-50 form
that D21 retired for this phase; the bands read `shrunk_early_entry × confidence/100`, which
also shrinks first. So the column was arithmetically self-consistent about a number the
pipeline does not produce, and S12c's band tests banded it (80.19 → `track`) while a live
19-anchor wallet at the same raw score lands at 71.06.

The metadata fixture error was caught because it produced impossible row values — metadata
0.60 at two anchors. This was not caught because it produced *plausible* values that simply
did not match `raw × composite / 100`. Same class, opposite failure mode: in both cases one
side of an equation was checked against the formula and the other side was never derived.
D23 recomputes the column and §6b shows the band consequences.

**The discipline that catches both:** every hand-computed column must be re-derived from the
other columns by a second, independent path. If `adjusted = f(raw, composite)`, invert it — or
better, recompute it with the code that will store it — and assert the two agree. A column
that only agrees with itself is a transcription, not a check. The §6b tests now call
`shrink()` and `shrink_for_confidence()` and compare their output against the matrix
constants, so a formula change breaks the doc's numbers instead of silently orphaning them.

**A correction to the correction — supplied figures rejected.** The first draft of §6b carried
supplied figures of **42.75 / 40.33 for bad+rich** and **52.67 / 31.34**, **32.33 / 19.24 for
the thin pair**, and drew a design question from bad+rich landing in `investigate`. At `k=5`,
`n=19`, raw 28 the shrunk score is **32.58** (adjusted **30.74**), and the thin pair is
**58.00** / **42.57** (adjusted **34.52** / **25.33**) — the supplied numbers are not
reproducible from the implemented shrinkage. 42.75 at that weight would need a raw score of
about 39, not 28. bad+rich therefore stays `exclude` under both 70 and 75, the
exclude → investigate finding is **retracted**, and no design decision is pending on it. The
rejected values are kept here rather than deleted so a later reader does not reintroduce them.

The tests now derive the matrix from `shrink()` and `shrink_for_confidence()` and compare that
output against the §6 constants, so a future formula change breaks the doc's numbers instead of
silently orphaning them.

**2026-11-07: Second user-side arithmetic error in the same session.** The first was "+rich
anchors 4 ≥ 3" (inconsistent with sample 1.00). The second was checking §6's adjusted column
against `raw × conf/100` when the operative formula is `shrunk_early × conf/100`, and then
recomputing the real-shrinkage matrix with wrong factors (42.75 / 52.67 / 32.33). Both errors
were caught by the agent deriving from actual `shrink()` and `shrink_for_confidence()` calls
rather than accepting the instruction's arithmetic. The discipline that catches these — derive
every fixture from the implementation, not from a mental formula — applies to user instructions
symmetrically to agent fixtures. When a user instruction contains hand-computed values, derive
them from code before acting.

**2026-11-07: "No new capability" was scoped too broadly.** The instruction was meant to prevent
speculative Phase 5 / S1–S3 work on top of unverified anchor input. It was applied by the agent to
a CLI wrapper around an already-tested function (`run_consumer`), so the closeout shipped with a
documented command that does not exist, and the runnable end-to-end path slipped by one turn. The
distinction: **new capability** = new modules, new external calls, new schema, new behaviour.
**Access to existing capability** = CLI registration, read commands, docs — always in scope, and
refusing it is an over-application of a scope limit, not caution. When a do-not list can be read
two ways, read it against its stated purpose.

**2026-11-07: State doc overstatement of readiness.** `docs/PHASE1B_STATE.md` § 2 originally said
the pipeline runs end to end when anchors exist. "Anchors exist" means *anchor mints exist*, and
converting mints into the rows the consumer reads requires S3 discovery (mint → genesis →
first-20 buyers) plus anchor-table seeding. Neither is built. The distinction between "the
pipeline works when anchor rows are seeded" and "the pipeline works when the user supplies anchor
mints" was blurred by writing the doc from the consumer's input rather than from the user's.
Mitigation: any state doc claiming end-to-end capability states the **input format** and any
**intermediate step** that must be run first. "Supply anchors" is not enough; "supply anchors and
run X" is.

A related distinction worth keeping: the §6 column was **self-consistent and semantically
stale, not arithmetically broken** — it reproduced the formula in force when it was written,
and D21 changed the formula. Checking a column against a formula that was never operative for
it is a different error from checking a column against nothing at all, and only the second is
caught by re-deriving from the other columns.

---

## § AGGREGATION_EDGE_CASES

**2026-11-05. Why `max_subsequent_return` floors at 0, and what would change that.**

`max_subsequent_return` is peak-based: a peak cannot be below entry, so a legitimate
`r = -1` should never occur. Flooring at 0 reads an anomaly as "no forward run",
which is the correct semantic for this metric; flooring at −1 would read it as
total loss, which is both the wrong semantic and does not map onto a 0–100 score.
The recovered fixture (docs/RECOVERED_FROM_TRANSCRIPT.md §7) specifies 0 and the
implementation matches.

**The sharp edge:** the compound (geometric mean) reading is a product, so
`[4]*9 + [-1]` returns 0 — worse than a clean `[4, 4, 4]` at 4.0, despite nine of
ten anchors having run 5x. If real anchor data shows legitimate `r < 0` values with
any frequency, the compound reading is the wrong summary for this purpose and the
aggregator should move to an arithmetic mean or an explicit
consistency-plus-magnitude model. **Do not change the formula pre-emptively.**

**Observability:** every observed `r < 0` emits a structured warning from
`solscope.aggregation` with `metric=max_subsequent_return`, `wallet`, `anchor_id`
and `value`, and is flagged `anomaly: negative_return` in the aggregate's evidence
trace. The anchor still counts as computed, because dropping it would shrink
`anchor_count_computed` and make a data fault look like reduced coverage.
Covered by `tests/test_aggregation.py` (`[4]*9 + [-1] -> 0`, warning fires once,
names all four fields, and no warning for a clean set).

Revisit condition: real anchor data with more than incidental negative values.

**2026-11-06. Shrinkage can invert two wallets below the prior — intentionally.**

`shrunk = f·raw + (1−f)·50` with `f = n/(n+k)`. Below the prior, a thinner wallet is
pulled **less** far from 50, so a worse raw score with fewer anchors can outrank a better
one:

    raw 46, n=5  ->  0.50·46 + 0.50·50 = 48.00
    raw 44, n=2  ->  0.286·44 + 0.714·50 = 48.29     (k=5)

At the old `k=15` the same pair inverted too, just less visibly (49.00 vs 49.29). This is a
property of shrinkage, not a bug: with two anchors the system does not know the wallet is
bad, and "unknown" must not be recorded as "worse than a measured 46".

**Do not "fix" this** by pinning the factor, by sorting on `raw_value`, or by lowering the
prior. Pinning or sorting makes the stored value disagree with the ranking; lowering the
prior punishes good thin wallets (a 76.40 wallet with two anchors would store 31.05 — see
`§ SCORING_PARAMETERS`). If ranking stability matters more than honesty about thin
evidence, the correct fix is the confidence gate, which excludes a thin wallet from ranking
instead of pretending it was measured.

---

## § SCORING_PARAMETERS

Every number that changes a stored score, with why it holds its current value and what
would make us revisit it. Decisions are recorded when they are made, not reconstructed
later — the same discipline as `§ DATA_LOSS_2026_10` and `§ PROCESS_FAILURES`.

| Parameter | Value | Rationale | Revisit when |
|---|---|---|---|
| Sample denominator | `COUNT(DISTINCT anchor_id)`, `status='computed'` | An anchor measured on three metrics is one observation. Summing per-metric counts tripled the sample and inflated `f` by 3x (9 instead of 3). | Never — this was a correctness bug, not a tuning choice. |
| `shrinkage.k.early_entry` | 5 | At `k=15` with 2–5 anchors the cohort compressed into 45.0–56.6 (11.6-pt spread); five anchors carried only 25% weight. `k=5` gives 40.0–63.2 (23.2-pt spread), a reachable window of 25–75, and half weight at five anchors. | Wallets routinely carry ≥10 anchors (reconsider 8–15). |
| `shrinkage.k.token_selection` | 5 | Same arithmetic and the same anchor/position scarcity as `early_entry`. | Positions per wallet routinely ≥10. |
| `shrinkage.prior` | 50 | Encodes ignorance, not suspicion. Lowering it to 25 leaves the good-vs-bad gap identical at n=2 (5.46 pts) while calling an unmeasured good wallet (76.40) bad at 31.05. With the population unknown, the midpoint is the least committal prior Doc 05 §3 already names as neutral. | Real data shows the population median raw is far from 50 — then set the prior to that median, with evidence. |
| `confidence_gate.confidence_min` | 40 | The §12 composite floor. At 40 the adjusted score keeps 40% of its distance from the old neutral, so the scale still has range; at the old 25 everything converged toward the middle while reading as a ranking. `gates.confidence_min: 25` is deleted (D12) so only one floor exists. | A measured distribution showing wallets bunched at the floor. |
| `confidence_gate.min_distinct_anchors` | 3 | A validity rule, not a calibration: two anchors compute but cannot be ranked. Deliberately separate from `sample_full_credit_at` (D5) so a calibration edit cannot move the rankability floor. | A power analysis showing two-anchor ranking is defensible after all. |
| `confidence.sample_full_credit_at` | 5 | Distinct computed anchors at which more anchors stop adding confidence (D5). Reaches full credit at the same five anchors where shrinkage gives an observation half weight. | Anchor counts routinely exceed 10 and the score needs more resolution at the top. |
| `scores.band_basis` | `shrunk_early_entry` | Bands read the one measured dimension, scaled by confidence (D21); `overall_score` is the neutral 50.00 placeholder, and pulling it returns 50.00 for every wallet. | Five more dimensions built — flip to `overall_score`, at which point the same bands become full-wallet bands. |
| `bands.track` (configured) | 70 | Matches §5 as of D22 (was 75). With the early-entry basis `track` also requires confidence ≥60. The difference is load-bearing, not cosmetic: a real 19-anchor wallet at raw 82 adjusts to 71.06, so it was `watch` at 75 and is `track` at 70 (§6b). | The first real anchor run: if 70–75 fills with wallets that do not survive inspection, `track` is too cheap. |
| `bands.track_min_confidence` | 60 | §5: adjusted score alone lets a shrinkage artifact look like a ranking (§5 point 1). Now an explicit config key rather than a module default, so the conjunct is visible beside the threshold it guards. | A high-adjusted, low-confidence wallet is manually validated and turns out to be real. |
| `bands.investigate` (configured) | 40 | Matches §5 as of D22 (was 45). Lowers the floor for manual review by five points; whether that is too generous is a question for real anchor runs, not for the fixture. | The first real anchor run: inspect the 40–45 band and see whether those wallets are worth a human's time. |

Factor `f = n/(n+k)` at `k=5`, by sample size: n=2 → 0.2857, n=3 → 0.3750, n=5 → 0.5000,
n=9 → 0.6429, n=15 → 0.7500, n=30 → 0.8571. At `k=15` the same sizes gave 0.1176, 0.1667,
0.2500, 0.3750, 0.5000, 0.6667 — the reason a three-anchor wallet stored 54.40 instead of
59.90.

**Unit mappings are not parameters but are equally load-bearing**, and changing one is a
scoring change: `r/(1+r)` for `max_subsequent_return` (unbounded → [0,1)), `1 − pct` for
`buyer_rank_at_entry`, the fraction itself for `pct_entries_before_volume_spike`, and
`min(1, anchors/10)` for `anchor_sample_size`. `experiment_label =
aggregate_rule:<rule_version>` in every run key is likewise fixed, because it is what makes
a re-run idempotent rather than a new run.

Today the gate's first conjunct fails for every wallet (`existing_confidence = 0.00` until
§12), so no wallet ranks. See `docs/PHASE1B_SCOPING.md § Q4` for the full decisions.

---

## § ANTI_PATTERNS

**Harness instability: malformed tool calls (2026-10-26).** Five structurally
invalid calls were observed in one session. In every case a required field was
dropped or renamed at serialization time — the call arrived without it:

| # | Tool | Defective args | Error message (verbatim prefix) |
|---|---|---|---|
| 1 | `str_replace` on `solscope/derive.py` | top-level `newString` instead of a `replacements` array | `Invalid parameters for str_replace: expected "array", path ["replacements"], message: "Invalid input: expected array, received undefined"` |
| 2 | `str_replace` on `solscope/derive.py` | same defect, repeated | same as #1 |
| 3 | `str_replace` on `requirements-dev.txt` | `replacements[0].oldString` missing | `Missing required replacement fields: - replacements[0].oldString. If the intent is deletion, set "newString": "" explicitly.` |
| 4 | `write_file` on `NOTES.md` | `instructions` missing | `expected "string", path ["instructions"], message: "Invalid input: expected string, received undefined"` |
| 5 | `suggest_prompts` | passed `{"ppts": "[]"}` instead of `{"prompts": []}` | `expected "array", path ["prompts"], message: "Invalid input: expected array, received undefined"` |

All five were rejected by schema validation **before execution** — none modified a
file. Verified: `git status` clean after each, no partial writes found.

**Rule:** a malformed call may not have corrupted state, but the next one may.
After any `Invalid parameters` error, re-verify `git status` before the next tool
call. Since the remote now exists, `git status` + a pushed baseline is the cheap
recovery path rather than hope.

**MSYS path conversion does not apply inside quoted subprocess strings
(2026-10-28, `bootstrap_db.sh`).** A path passed to a Windows tool via an `-o` or
`--opt` flag is handed over verbatim: `pg_ctl -o "-k /tmp/foo"` gives
`postgres.exe` the literal `/tmp/foo`, which resolves against the drive root and
fails with `could not create lock file "/tmp/...": No such file or directory`.
Only *standalone* path arguments get converted. Any path inside such a string must
be `cygpath -w`-converted before the call (`SHORT_WIN=$(cygpath -w "$SHORT")`).
Caught by running the harness end-to-end, not by reasoning about it — the flag
looked correct and the failure surfaced only as a server that would not start.

**A directory existing is not a cluster being sound (2026-10-31,
`bootstrap_db.sh`).** `initdb` creates several *empty* directories (`pg_notify`,
`pg_tblspc`, `pg_twophase`). Temp cleaners delete empty directories, so five days
after a clean `initdb` the cluster failed to start with
`FATAL: could not open directory "pg_notify": No such file or directory` — with
`PG_VERSION` present and `pg_ctl status` silent. A `PG_VERSION` existence check is
not a health check: validate the required directory skeleton and rebuild the
cluster when it is incomplete. Both failures were latent until the harness was
actually executed; reasoning about the script had passed it twice.

**Pasted transcripts carry credentials (2026-11-05, `.cloud-attachments/`).**
A prior-session transcript imported into `.cloud-attachments/` (1,771,473 bytes,
one line) contained the live Helius API key in plaintext **three times**, at byte
offsets 11742, 39655 and 40613 — because the prior session had pasted the key
into a diagnostic prompt and into a `.env` write. The file was untracked and was
never committed (`git log --all -- .cloud-attachments` is empty), so the exposure
was disk-side and provider-side, not in history. It still required rotation: the
key was valid and readable for as long as the file existed. Rotated and revoked
2026-11-05 — old sha256 prefix `7a016c39558863d4` is dead, the replacement was
verified with `getHealth` returning 200 `ok` and hashes to a different prefix.

**Rule:** any file imported from another environment is credential-swept before
it is stored, with the UUID, `sk-`, JWT and `Authorization: Bearer` patterns. An
untracked file is not a safe file; `.gitignore` prevents the commit, not the read.
After a hit, rotation is the first action, not the last — deleting the file does
not un-disclose the value.

**Sweep for the value shape, not for the hash (2026-11-05).** The first sweep
grepped the transcript for the key's sha256 prefix and got `0`, which reads as
clean and is meaningless: the file holds the value, not its hash. The key was
found by extracting every UUID-shaped candidate, hashing each, and comparing
prefixes (1 of 7 matched `.env`). A negative hash-prefix grep is not evidence.

**Imported files also carry the session's furniture.** The same transcript held
five ad-tracking UUIDs (impression and creative IDs) and the freebuff project
path id. Not credentials, but not design material either; they were redacted in
`docs/RECOVERED_FROM_TRANSCRIPT.md` rather than carried into the repo.

---

## § PHASE1A_AUDIT

*Status: audit run 2026-10-26, against the working tree at `df8579a`. Verdict:
**Scenario A — `solscope/` is a rewrite, not a continuation.***

The question this settles: the closeout claimed ~1,510 `swap_events` and ~497
`wallet_token_positions` existed in Phase 1a. Data loss leaves DDL behind with zero
rows. So if those table names are absent from DDL, code, tests, and every live DB,
the Phase 1a *architecture* is absent — not just its rows.

### Evidence

**A1 — modules.** 17 Python modules under `solscope/` (2,977 lines). Every one has
exactly **one** commit (`derive.py` two — both from this session). `git log --all`
holds **3 commits total**; the root is `00438e6`, created this session. There is no
history before it, so nothing was renamed or moved within git.

**A2 — the six table names.** Checked across DDL, code, tests, docs, `spikes/`, and
the live DBs:

| Identifier | Occurrences outside `docs/INVENTORY.md` |
|---|---|
| `swap_events` | **0** |
| `wallet_token_positions` | **0** |
| `swap_hops` | **0** |
| `pool_cache` | **0** |
| `pool_candidates` | **0** (only the *Python* `PoolCandidate` dataclass exists) |
| `mint_pools` | **0** |
| `wallet_quality_filter` | **0** anywhere, including `INVENTORY.md` |

`docs/INVENTORY.md` is the only file in the repo containing those names — and I
wrote it this session. The negative finding therefore rests on a fresh grep
(re-run for this audit), not on the doc that reports it.

**A3 — schema provenance.** `schema/001_init.sql` was added in `00438e6`, the root
commit. No earlier commit touches it. It references none of the six identifiers.

**A4 — framing.** README and every module docstring describe the system as
**Phase 0/1/2**, citing doc 07. Nothing says "rewrite", "continuation", or
"building on Phase 1a". Doc 07 defines **Phase 1 = "Forward collector … start on
day one"** and justifies it with the project's core premise: *complete history is
unaffordable, so forward collection is the primary asset*. A retrospective anchor
phase contradicts that premise, so its absence is **by design, not an omission**.
The repo's own phase list has no 1a/1b rows; the 1b blocker entered only as carried
state (see § ANCHOR_SELECTION_CRITERIA).

**A2 further — what the phase tests actually pass.**

| Suite | Lines | Subject | Helius | Phase 1a refs |
|---|---:|---|---|---|
| `test_phase0` | 278 | CLI parser registration; `budget.yaml` validation; ledger cap/idempotency/preflight | only as a **config name** (cap 1,000,000; `get_transaction` = 1) | none |
| `test_phase1` | 660 | Pool filter, token bucket, archive dedupe, checkpoint overlap, WSS metering, kill-recovery resume, collector tick idempotency | imports `LaserStream`; asserts websocket op costs | none |
| `test_phase2` | 431 | Decoder + derivation against 12 real mainnet fixtures | only as **fixture provenance** | none |

All three run **offline** against fakes and fixtures. "Phases 0–2 pass" means the
CLI/ledger contract, the forward-collector contracts, and the PumpSwap
decoder/gate contract — none of it exercises a wallet-history path.

### Verdict: Scenario A — rewrite

1. No history before the root commit; every file written once.
2. Six identifiers absent from DDL, code, tests, docs and live DBs.
3. Schema was authored complete at the root commit. The *equivalent concepts* exist
   under new names but are **not** renames:

   | Prior Phase 1a | Current equivalent | Same thing? |
   |---|---|---|
   | `swap_events` | `swaps` (+ `transactions`) | different design: split tables, `exec_price` vs `pool_price` |
   | `wallet_token_positions` | `positions` | different design: partial unique index, `inventory_origin` |
   | `swap_hops` | *no table* | multi-hop lives in prose ("aggregator hops") and in `decode.py`'s inner-instruction flattening — a different mechanism |
   | `mint_pools` | `pools` | different design |
   | `pool_cache` | `pool_reserve_snapshots` | different design |
   | `wallet_quality_filter` | *nothing* | absent in every form |

   Whether those are ports or independent redesigns is **unknowable**: no history
   survived, and the designs differ in ways the prior names say nothing about.
4. There is no wallet-history extractor, no anchor selector, no quality filter, and
   no per-wallet position builder. `positions` is Phase 3 and unwritten.
   `solscope discover` is a registered CLI **stub** returning exit 2.

### Caveats — three real capabilities survive

Not a clean "nothing survived". A Phase 1a rebuild starts from these, not from zero:

- `solscope/adapters/helius.py` implements exactly the three RPC primitives a
  retrospective extraction needs — `getSignaturesForAddress`,
  `getTokenAccountsByOwner`, `getTransaction` — each credit-metered and
  cap-checked. (Note: `getTokenAccountsByOwner`'s credit cost is still an
  **unverified conservative assumption of 10**.)
- `derive.py` already runs pool → signatures → transactions → archive → persist.
  That is the mechanical core of a historical extraction; it differs from Phase 1a
  only in **seed** (pool address rather than wallet/launch) and **destination**
  tables.
- `decode.py` decodes real swaps, so the hard part of Phase 1a's data layer was
  solved under a different name.

### Surviving on-chain payloads (the only real data in the tree)

- `tests/fixtures/` — **12** `getTransaction` payloads fetched live from Helius on
  **2026-10-17**, i.e. inside the prior observation window. Real Phase 1a-era residue.
- `data/raw/tx/2026-10-25/` — **372** archived `getTransaction` payloads, 372 distinct
  sha256, 6.5 MB, written **2026-10-26 00:12–00:29 local** (UTC date rolls back to
  10-25). These are from *this* session's `derive` runs, **not** prior-session
  Phase 1a collection. `data/` is gitignored and 0 files are tracked.
- No collector day-directories and no `gaps/` directory exist: the forward collector
  never archived to disk in this tree. The 165.8 MB raw archive in § DATA_LOSS_2026_10
  lived in the lost cloud VM and is not here.

### Implication for Phase 1b

**Large** (this is Scenario A). Phase 1b cannot "consume anchors" — there is no
anchor pipeline to consume them, no `swap_events`/`wallet_token_positions` to read,
and no quality filter whose rejection rate could be measured. A Phase 1b build means:
(H)elium wallet-history adapter = reuse `helius.py`; (S)wap/position storage =
reuse `swaps`/`positions`, which already carry the two-clocks and
`inventory_origin` invariants; (M)int-based route and (Q)uality filter = **greenfield**;
(A)nchor pipeline = **greenfield**. The `NOTES.md` § `ANCHOR_SELECTION_CRITERIA`
blocker still applies unchanged to the greenfield parts.

### Corrections to earlier reporting this session

- "Doc 11 references `swap_hops`" — **wrong**. Doc 11 says "aggregator hops" in
  prose; the literal identifier appears only in `docs/INVENTORY.md`.
- "No Phase 1a text exists anywhere in the repo" — true when measured, but
  `INVENTORY.md` (written after) is now the sole place "Phase 1a" appears. The
  substantive claim — no Phase 1a *artifacts* — is what the greps above establish.
- `docs/INVENTORY.md` § "Why 41 is more than Phases 0–2 should produce" **misattributes
  the Phase 1a tables** to `chains`/`tokens`/`pools`/`raw_payloads`. Per this audit the
  Phase 1a tables do not exist at all; that section's phase attribution describes  the *current* schema's writers (correct) but must not be read as evidence Phase 1a was
  implemented here. **Fixed 2026-10-26** — that section now carries an explicit "do not
  misread this" warning, and inconsistency #7 records the rewrite.

---

## § QUALITY_FILTER_IMPLEMENTED

**Status: IMPLEMENTED AND ENFORCED, THRESHOLDS UNVALIDATED. All four rules exist and the
consumer honours their rejections. Not one threshold is fitted to real data — do not present
them as tuned, and do not treat a filter verdict as evidence until the rows in the validation
table below are measured.**

### Genesis-burst rule: `N` and `Y` are guesses

`docs/PHASE1A_RECOVERY.md` §3 proposes:

> If a wallet's first buy for **≥N anchors** falls within **Y seconds** of each anchor's
> genesis, reject the wallet.

**`N = 2` and `Y = 60` are guesses.** They were never fitted to data, because the data they
would be fitted on does not exist. They are recorded as defaults so the rule has a starting
shape — not because any measurement supports them.

**Synthetic calibration exists (2026-10-28), and it is NOT validation.**
`tests/test_phase1b_filter.py` runs the implemented rule over two labelled synthetic sets:
**100% of 50 bundled wallets rejected, 0% of 50 clean wallets rejected**, with 35
single-anchor clean wallets held in their own not_computable bucket. That proves the
implementation matches the specification. It says nothing about whether N = 2 and Y = 60
separate real bundles from organic buyers, because both sets are synthetic — so nothing in
this section's status changes.

What validation requires, and what is missing:

| Needed | Status |
|---|---|
| Rejection rate on the **known-bundled** set (the 44/45 set) | **Blocked** — that set's wallet identities were lost (`PHASE1A_RECOVERY.md` §9). Recoverable only by rebuilding a bundled set from on-chain data. |
| **False-positive** rate on a **known-clean** set | **Never measured, and no such set exists.** The larger gap: the prior filter failed on *false negatives*, and a rule tuned to catch bundles is precisely the kind that starts rejecting organic early buyers. |
| Sensitivity of the rejection rate to `N` and `Y` | Not measured. |

**Guard against misuse:** `solscope/quality_filter.py` exports
`GENESIS_BURST_DEFAULTS_VALIDATED = False` beside the two constants, so a caller cannot pick
the numbers up without also picking up the fact that they are unvalidated. Flip it only after
both rows above are measured.

### The three prior heuristics are now ported

`sandwich`, `throughput`, `router_only` previously had no codes and no rules; the filter they
belonged to admitted 44 of 45 bundled wallets. As of 2026-11-08 they are implemented in
`solscope/quality_filter.py` as `sandwich()`, `throughput()`, `router_only()`, and
`NOT_PORTED_HEURISTICS` is now empty — the tuple stays so the omission remains expressible.

Defaults, all **unvalidated** (`SCREEN_DEFAULTS_VALIDATED = False`):

| Rule | Default | Source of the number |
|---|---|---|
| `genesis_burst` | ≥2 anchors within 60 s of genesis | `PHASE1A_RECOVERY.md` §3, a guess |
| `sandwich` (v2) | ≥3 quick round trips ≤90 s in ≥2 mints, and ≥20% of the last 30 days' swaps (window must hold ≥10 swaps) | designed from the v1 failure below; 90 s inherited, the rest unvalidated |
| `throughput` | >200 swaps/day, window floored at 60 s | guess; ~8× a busy manual trader |
| `router_only` | >95% aggregator-routed **and** zero direct pool swaps | guess; retail uses aggregators too |

### `screen_wallet` — three outcomes, and the third is not a clean bill

`screen_wallet()` applies the rules in order (genesis burst → swap sufficiency → sandwich →
throughput → router_only) and returns one verdict. Rejections outrank not-computable: a
wallet that burst across anchors is rejected even with too few swaps to screen.

* `ACCEPTED` — every evaluable rule ran and none fired
* `REJECT_*` — a named rule fired, with the measured values on the row
* `NOT_COMPUTABLE_*` — evidence was absent (fewer than 3 swaps, missing genesis, incomplete
  buyer set). **Not screened is not cleared**, and it is kept as its own bucket so a
  coverage gap can never be mistaken for a pass rate.

`SCREEN_RULE_VERSION = "quality_screen/v1"` is separate from the burst-only `RULE_VERSION`,
so adding three rules did not relabel existing verdicts as having been screened by four.

### Enforcement: the consumer is where a verdict binds

`consumer.load_filter_rejections()` reads `anchor_wallet_verdicts` for `wallet.reject*`
(any rule version — a rejection is a property of behaviour, not of the threshold set that
caught it) and `run_consumer()` skips those wallets before scoring at all. They get
`skipped[wallet] = "quality_filter:<verdict>"` and appear in a separate
`ConsumerReport.filter_rejected` and CLI line, so "we would not score it" never reads as
"we could not rank it". A rejected wallet produces no `scores` row even with three computed
aggregates; `tests/test_scoring_consumer.py` proves exactly that.

Schema: `schema/007_quality_filter_verdicts.sql` adds the new verdict and rule-id enum values
and generalises `verdict_rule_consistency` from "is the genesis-burst verdict" to "every
`wallet.reject*` names a rule, and nothing else does".

### Measured on synthetic sets (2026-11-08) — implementation check, not validation

`tests/test_phase1b_filter.py`, same 50 bundled / 50 clean wallets as the burst calibration,
now run through `screen_wallet`:

| Set | Wallets | Rejected | Accepted | not_computable | Target |
|---|---|---|---|---|---|
| bundled | 50 | **50 (100%)** | 0 | 0 | ≥90% rejected |
| clean | 50 | **0 (0%)** | 50 | 0 | ≤10% false positive |

Each heuristic also fires on its own targeted signature, and not on its neighbour: sandwich
needs quick round trips in *different* mints, one direct pool swap defeats `router_only`, and
two swaps lands in `not_computable` rather than either verdict. Both sets remain synthetic,
so the validation gap below is unchanged.

### Real-data calibration, v1 (2026-11-09) — first evidence, and it was adverse

`spikes/calibrate_filter.py` ran the four rules over the 100 first-20 buyers already in
the database (read-only; nothing reseeded). Full record: `docs/PHASE1B_CALIBRATION.md`.

**95/100 rejected, every rejection from `sandwich`.** Verdicts: 95 `reject_sandwich`, 3
`not_computable_insufficient_swaps`, 2 `accepted`. Evaluated alone: `sandwich` 95,
`throughput` 32, `router_only` 0, `genesis_burst` 0 (and it *cannot* fire — each wallet
is in one anchor's first-20 set, so N=2 is unreachable).

**Threshold sensitivity:** `throughput_max_swaps_per_day` is load-bearing (58 wallets at
50/day → 32 at 200 → 9 at 1000). `sandwich_max_seconds` is **inert**: 95 wallets at 30 s,
60 s, 90 s and 180 s alike. `router_only` is inert at 0.80–0.99 because the sample trades
pool-direct (one wallet: 1,389 of 1,389 swaps direct). `genesis_burst` is structurally
unmeasurable on this sample.

**Overlap:** 32 wallets have ≥2 rules firing and all 32 are sandwich ∩ throughput, so
throughput adds no decision sandwich does not already make. `router_only` never co-fires
because it never fires.

**Manual spot-check:** 7 of 8 sampled wallets agreed with the filter (3 clear bots, 1
bot-like, the only 2 accepted wallets both human). The mismatch is the point: a wallet
with 0.5 swaps/day over 417 days, active in all 24 hours, was rejected because 30 of its
207 swaps were sub-90 s round trips — the rule counts them over the entire history with
no rate and no recency, so a 32,488-swaps/day bot and a two-year retail trader get the
same verdict.

**Conclusion: the filter is too aggressive on this sample**, and `sandwich` is the cause —
not its threshold, which is inert, but its shape. Thresholds were **not** changed and
`SCREEN_DEFAULTS_VALIDATED` remains `False`: a fix is a logic change that needs its own
decision, and this run was measurement.

Two sampling facts a reader needs: the first pass sampled oldest-first and mislabelled a
wallet as `insufficient_swaps` from a quiet early slice, so the sample was re-fetched
newest-first and all numbers above are from that pass; and 25 wallets hit the
1,500-transaction cap, so their rates describe the recent window, not a lifetime.

### Sandwich redesigned to concentration-based, and re-calibrated (v2)

The **design principle**: share of recent activity, not count over lifetime. `sandwich`
is now `sandwich(wallet, positions, swaps)` and fires when, in the last 30 days of the
wallet's own activity, at least 3 round trips lasted ≤90 s across ≥2 mints **and** those
trips are ≥20% of the window's swaps, with ≥10 swaps in the window. Under 10 window
swaps is a **pass**, not `not_computable`: a sparse trader is not a sandwich bot, while
insufficient *lifetime* data stays `NOT_COMPUTABLE_INSUFFICIENT_SWAPS`. The window ends at
the wallet's own last swap rather than wall clock, because a historical sample would
give every inactive wallet an empty window and pass it by default. `SCREEN_RULE_VERSION`
is now `quality_screen/v2`; only `sandwich` changed.

**Before → after, same 100 wallets:** rejection 95% → **72%** (67 `reject_sandwich` +
5 `reject_throughput`), accepted 2 → **25**, not_computable 3 → 3. Overlap fell 32 → 27,
and throughput now fires **as primary** on 5 wallets sandwich no longer catches.

**The anchor case is fixed:** `8a6JShiMBw` — 0.5 swaps/day over 417 days, 27 quick trips
of 198 recent swaps, share 13.6% — is now **accepted**. Both unambiguous bots stay
`reject_sandwich` (`Gfds2Ur4UV` at 85.6%, `AviBqnzP8m` at 46.4%), and both human
regression guards (`FwA2rjEzvs`, `GRmwzXEBvD`) stay accepted.

**One prediction did not hold; it is kept as a known false negative.** `8GSDbEhMy3` is
**accepted** at a 17.1% share against a 20% floor — 3.5 points above the retail wallet's
13.6%, too narrow a gap to place a threshold in without fitting it to two wallets, so
`min_share` was not moved for it. `52NUhyYcpw` is **not** a failed prediction: its 1.5%
share is below the retail wallet's because it buys and distributes in one direction rather
than flipping, and it is correctly rejected by **throughput** at 11,226 swaps/day. The two
rules measure different bot patterns — sandwich catches flip-flopping, throughput catches
one-directional volume — and the sample validates that they complement rather than
duplicate.

**Round-trip reconstruction was corrected too:** v1 opened an episode only when none was
open, collapsing "buy, buy, buy, sell-all" into one round trip and scoring a 1,462-swap
wallet as 21 trips. v2 uses FIFO lot matching (as `positions.reconstruct` does), which
raised the confirmed bot's quick trips from 273 to 1,189.

**Threshold sensitivity flipped:** under v1 `sandwich_max_seconds` was inert across
30–180 s; under v2 `min_share` (80→39 wallets), `window_days` (58→72) and `max_seconds`
(51→77) are all load-bearing. That is the point of the redesign — the parameters now
carry the decision, so they are exactly what must not be fitted to 100 unlabelled
wallets. `throughput_max_swaps_per_day` remains load-bearing (58→9 over 50–1000).

**Conclusion: working** on this sample, with the one known false negative above.
`SCREEN_DEFAULTS_VALIDATED` remains `False`. Full record: `docs/PHASE1B_CALIBRATION.md`.
Next validation target: an externally labelled bundled/clean set, or a larger anchor
sample (S3 discovery or more user-supplied anchors).

### Related open item — anchor count

Phase 1b has **no anchor-count requirement**: nothing states whether 3 anchors, 20, or 200 are
needed to support a claim. That needs a power calculation, and it is marked **TBD** in
`docs/PHASE1B_SCOPING.md`. The minimum viable run uses 3 *mocked* anchors to prove the
plumbing — a fixture size, not a statistical one.

**Quantified 2026-11-09:** `docs/PHASE1B_STRUCTURAL_FINDING.md`. Expected wallets in ≥3
first-20 lists is `C(N,3)·20³/P²` — 0.0005 at five anchors, ~1 at 49 anchors (small buyer
pool) or ~196 (large). Five anchors cannot rank anybody and 5–10 more cannot either; the
rankability floor is a coverage question no filter edit touches. Three paths: 30–50
independent anchors, KOL-curated anchors (recommended), or relaxing
`min_distinct_anchors` to 1 with filter+profit as substitutes (a semantic change needing
approval).

