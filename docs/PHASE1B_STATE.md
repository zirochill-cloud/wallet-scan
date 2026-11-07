# PHASE 1b — STATE AND NEXT DECISION

Written 2026-11-07. This is the entry point for a returning session: it says what exists, what it
needs to do its job, what was deliberately left out, and what to do first next time. Design
rationale lives in `docs/PHASE1B_SECTION12.md`; decisions and their reasons in
`docs/PHASE1B_SCOPING.md` § Q4 (D1–D23); parameters and failure records in `NOTES.md`.

---

## 1. Current state

The tool profiles wallets: it reconstructs positions, aggregates per-wallet metrics, computes a
composite confidence score from five evidence factors, gates on whether that evidence can support
a ranking at all, and assigns one of five bands from the confidence-adjusted **early-entry** score.
`solscope scores` reads the result back with the band basis printed on every run. §12 is
implemented end to end — factors, composite, gate, adjusted score with a recorded basis, five
bands, and a read path — and the harness is green across seven suites. What it does not have is
anchor input, so it ranks nobody.

---

## 2. What the pipeline produces

Given anchor rows, the stages run in this order:

| Command | Stage | What it does |
|---|---|---|
| `python -m solscope.cli positions` | Phase 3 | FIFO position reconstruction from decoded swaps: one episode per wallet+token, buy/sell lot matching, open inventory left open. `--wallet`, `--mint`, `--limit`, `--show`, `--mark-usd`. |
| `run_consumer(...)` (from `solscope.consumer`) | §12 | Aggregate → early-entry composite → five confidence factors → gate → `confidence_adjusted_score` (`shrunk_early_entry × confidence/100`) → band, all written in one transaction. **No CLI subcommand yet** — the harness and tests call it directly; `solscope score` is still a Phase 6 stub. Driver code: `tests/test_scoring_consumer.py` around line 784. |
| `python -m solscope.cli scores [--wallet \| --band \| --limit \| --as-of]` | §12 read path | Prints band, adjusted, raw, confidence, gate outcome and `gated_by` for the latest run, best first, with the basis line. |
| `bash bootstrap_db.sh` | verification | Rebuilds the scratch cluster, applies migrations 001–006, runs seven suites (schema smoke, live ledger, anchor writer, positions, aggregation, scoring consumer, confidence factors). Expect `SCRIPT_RC=0` and `DB HARNESS GREEN`. |

Everything is committed and pushed: `git log --oneline origin/master` is the record. The §12 work
is `d03acd6` (composite + gate), `a5a1bc2` (band basis), `969bdab` (bands + read command),
`bdb5575` (design thresholds), plus the docs commits around them.

---

## 3. What it requires to produce rankings

A **real anchor set**:

- 3–5 memecoin mints from tokens that launched in **different weeks** (different deployers or
  weeks, so anchors are independent), within the last **30–90 days** so price history exists;
- each token **pumped ≥5×** within 30 days of launch, so the retrospective question ("did this
  wallet buy before the move?") has a real answer;
- enough anchor coverage that a wallet has **≥3 distinct anchors**. Two anchors compute but cannot
  be ranked (`confidence_gate.min_distinct_anchors`), so a two-anchor wallet lands in
  `promising_thin` or `exclude` no matter how good it looks.

Without anchors, no wallet passes the gate: every wallet lands in `promising_thin` or `exclude`,
and `scores` prints an empty ranking. That is the tool working, not failing.

---

## 4. What is deferred, and why

- **The other five dimensions of `overall_score`** (profitability, repeatability, risk,
  token_selection, exit_effectiveness) are unbuilt, so `overall_score` is the neutral placeholder
  50.00 and the bands are **early-entry bands** — `scores.band_basis = 'shrunk_early_entry'` on
  every row. They answer "is this wallet's early-entry ability worth tracking?", not "is this
  wallet good?". When the remaining dimensions exist, the basis flips to `overall_score`, the pull
  becomes `50 + (score − 50) × confidence/100`, and the same five rules become full-wallet bands.
- **`metric_definitions` exists but is unregistered.** The table and its `owning_subscore` CHECK
  are in `schema/001_init.sql`; it holds 0 rows, no schema file seeds it, and no code in
  `solscope/` reads it (the consumer uses its own metric allowlist and `scoring.yaml` weights).
  Registering the three Phase 1b metrics matters when `score_evidence` or `score_sensitivity`
  start being populated — both carry an FK to it — or when a report wants to cite a definition's
  formula. Not needed for scoring, gating or bands.
- **Phase 5 (price series) and S1–S3 (genesis, mint→pool)** build capability the tool cannot
  exercise until anchors exist. Building them first produces code with no consumer, the exact
  failure mode this project has already paid for once.

---

## 5. Anchor acquisition — the three options

1. **User-supplied anchors.** 3–5 memecoin mints from different weeks and deployers in the 30–90
   day window, each having pumped ≥5× within 30 days of launch. Costs nothing, needs no API
   credits, and is the only option whose population is chosen for the right reason. **Preferred.**
2. **DexScreener screening.** ~100k–200k credits, one-time, roughly 10–20% of the monthly cap.
   The fallback if user-supplied mints are unavailable. A prior session found that cheap-slice
   screening selects the **wrong population** (survivors of one launch wave, not independent
   launches), so this is a fallback, not a preference.
3. **Defer.** The pipeline ships as documented and produces no rankings until anchors arrive.
   Nothing degrades; no capability is built on top of unverified input.

---

## 6. What to do on the next session

1. **Check for anchors first.** `ls data/anchor_first20.json` (and `data/`), or ask the user
   directly. Do not start new capability work before this check — the answer changes the task.
2. **If anchors exist:** run the pipeline end to end — `positions`, then the consumer (see §2),
   then `scores` — and report the band distribution: how many wallets are `track` / `watch` /
   `investigate` / `promising_thin` / `exclude`, how many were excluded for anchors versus score,
   and what `band_basis` the rows carry.
3. **If anchors do not exist:** do **not** build Phase 5 or S1–S3. Escalate the §5 decision to the
   user — user-supplied, screening, or defer — and wait. Capability without a consumer is how the
   Phase 1a work was lost.
