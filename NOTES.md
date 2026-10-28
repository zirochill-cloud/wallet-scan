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

*Status: OPEN BLOCKER. Carried from the prior handoff; not re-verified in this repo.*

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

## § QUALITY_FILTER_PENDING

**Status: OPEN. The unvalidated parameters of the Phase 1b quality filter. Do not present
them as tuned, and do not treat a filter result as evidence until they are measured.**

### Genesis-burst rule: `N` and `Y` are guesses

`docs/PHASE1A_RECOVERY.md` §3 proposes:

> If a wallet's first buy for **≥N anchors** falls within **Y seconds** of each anchor's
> genesis, reject the wallet.

**`N = 2` and `Y = 60` are guesses.** They were never fitted to data, because the data they
would be fitted on does not exist. They are recorded as defaults so the rule has a starting
shape — not because any measurement supports them.

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

### The three prior heuristics remain unported

`sandwich`, `throughput`, `router_only` have **no codes and no rules**. They were never
tested, and the filter they belonged to admitted 44 of 45 bundled wallets. They are listed in
`NOT_PORTED_HEURISTICS` so the omission stays deliberate and cannot be silently reintroduced.

### Related open item — anchor count

Phase 1b has **no anchor-count requirement**: nothing states whether 3 anchors, 20, or 200 are
needed to support a claim. That needs a power calculation, and it is marked **TBD** in
`docs/PHASE1B_SCOPING.md`. The minimum viable run uses 3 *mocked* anchors to prove the
plumbing — a fixture size, not a statistical one.

