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
