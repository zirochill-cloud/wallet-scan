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

Given **anchor rows in the database**, the stages run in this order — every one of them from the
shell:

| Command | Stage | What it does |
|---|---|---|
| `python -m solscope.cli positions` | Phase 3 | FIFO position reconstruction from decoded swaps: one episode per wallet+token, buy/sell lot matching, open inventory left open. `--wallet`, `--mint`, `--limit`, `--show`, `--mark-usd`. |
| `python -m solscope.cli consumer [--rule-version V] [--as-of ISO8601] [--dry-run]` | §12 | Aggregate → early-entry composite → five confidence factors → gate → `confidence_adjusted_score` (`shrunk_early_entry × confidence/100`) → band, in one transaction. Prints wallets seen / scored / gated out, the skip reasons and the band split. `--rule-version` defaults to the newest value in `wallet_aggregates`; `--dry-run` computes the same numbers and persists nothing. |
| `python -m solscope.cli scores [--wallet \| --band \| --limit \| --as-of]` | §12 read path | Prints band, adjusted, raw, confidence, gate outcome and `gated_by` for the latest run, best first, with the basis line. |
| `bash bootstrap_db.sh` | verification | Rebuilds the scratch cluster, applies migrations 001–006, runs seven suites (schema smoke, live ledger, anchor writer, positions, aggregation, scoring consumer, confidence factors). Expect `SCRIPT_RC=0` and `DB HARNESS GREEN`. |

What the shell does **not** give you is a way to turn anchor mints into those rows. That step is
not automated in either direction — see § 3.

Everything is committed and pushed: `git log --oneline origin/master` is the record. The §12 work
is `d03acd6` (composite + gate), `a5a1bc2` (band basis), `969bdab` (bands + read command),
`bdb5575` (design thresholds), plus the docs commits around them.

---

## 3. What it requires to produce rankings

**Anchor mints are not enough.** The consumer reads `wallet_aggregates` and the anchor tables; an
anchor **mint** becomes rows only after discovery and seeding. Two paths get there, and neither is
automated today.

What the anchor set itself must satisfy, on either path:

- 3–5 memecoin mints from tokens that launched in **different weeks** (different deployers or
  weeks, so anchors are independent), within the last **30–90 days** so price history exists;
- each token **pumped ≥5×** within 30 days of launch, so the retrospective question ("did this
  wallet buy before the move?") has a real answer;
- enough anchor coverage that a wallet has **≥3 distinct anchors**. Two anchors compute but cannot
  be ranked (`confidence_gate.min_distinct_anchors`), so a two-anchor wallet lands in
  `promising_thin` or `exclude` no matter how good it looks.

### Path A — mints plus S3 discovery (needs S3 built)

Supply anchor mints → **S3 discovery** (mint → genesis → first-20 buyers; *not implemented*) →
seed the anchor tables from that output → `positions` → `consumer` → `scores`.

This is the full pipeline with no manual step, and it is **one turn of work** for a small set. It
is not built, and nothing downstream of it can be exercised without it or Path B.

### Path B — mints plus manual seeding (workaround, works today)

Supply anchor mints → identify the first-20 buyers for each mint by hand (Solscan's token page,
ordered by first buy) → seed the anchor tables with a one-off script calling `write_anchor`,
`write_verdict` and `write_metric` → `positions` → `consumer` → `scores`.

Legitimate for a small curated set: 3–5 anchors, ~15 wallets each is an hour of browser work,
and the rows it produces are identical in shape to Path A's. It needs no discovery code. The
input format is written up in `data/seed_anchors.md`; the script itself does not exist yet.

### Both paths end in the same place

`consumer` → `scores`, reading `wallet_aggregates` plus anchor rows. **Neither is currently
automated.** Without either path, no wallet passes the gate: every wallet lands in
`promising_thin` or `exclude`, and `scores` prints an empty ranking. That is the tool working,
not failing.

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

1. **Check for anchors first.** `ls data/seed_anchors.md` (is it filled in?), any `data/anchor_*.json`,
   or ask the user directly. Do not start new capability work before this check — the answer
   changes the task.
2. **If anchor mints exist:** choose the path in § 3 with the user — Path A means building S3
   discovery first; Path B means a one-off seed script plus their manual buyer lists. Only then
   run `positions` → `consumer` → `scores`, and report the band distribution: how many wallets are
   `track` / `watch` / `investigate` / `promising_thin` / `exclude`, how many were gated for
   anchors versus score, and what `band_basis` the rows carry.
3. **If anchor mints do not exist:** do **not** build Phase 5 or S1–S3. Escalate the § 5 decision
   to the user — user-supplied, screening, or defer — and wait. Capability without a consumer is
   how the Phase 1a work was lost.
