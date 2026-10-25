# INVENTORY — status of every document and artifact

Purpose: so the next session does not contradict this one. Each entry says what the file claims, how
much of it has been **verified against something real** (a running database, a live API, a test), and
what is known to be inconsistent.

Status key: **verified** (re-checked against a live system or test this session) · **carried**
(verified in an earlier session, evidence not in this repo) · **draft** (design intent, not yet
built or tested) · **stale** (out of date; superseded).

Verification date for everything marked *verified*: **2026-10-26**, PostgreSQL 16.6, Helius mainnet.

---

## `docs/`

| File | Status | What it claims | Notes |
|---|---|---|---|
| `00-feasibility.md` | carried | The project is feasible at ~$0/month; free tiers are retreating | Free-tier facts dated at writing (Oct 2026). Re-check anything load-bearing before relying on it. |
| `01-data-sources.md` | carried | Which sources are genuinely free, with limits and licence traps | Birdeye/GeckoTerminal retention explicitly marked *unverified* in-doc — correctly, still unverified. |
| `02-architecture.md` | verified, with a known gap | Components, two-tier storage, data flows | Naive reserve formula removed after doc 10. **Gap:** component `C derive` is described generically as "pool reserves → price …", but the implementation prices **PumpSwap only** (see inconsistencies #1). |
| `03-mvp-scope.md` | verified | Phase acceptance criteria; Phase 2 gate | Status line added with measured results. |
| `04-data-model.md` | verified — one figure corrected | The schema and why it is shaped that way | 41/484/106/53 re-measured and confirmed. **"306 check constraints" was wrong** — it counted 285 NOT NULL pseudo-checks + 22 real checks. Corrected to 22 (inconsistency #2). |
| `05-scoring-methodology.md` | verified as config, **unimplemented** | 8 sub-scores, weights, shrinkage, gates, bands | `config/scoring.yaml` validates (`test_scoring_config` green), but `solscope score` is still a stub. Design ≠ behaviour until Phase 6. |
| `06-ai-pipeline.md` | draft | Fact/LLM separation, claim verifier | Phase 8 not started; nothing to verify against. |
| `07-roadmap.md` | verified for Phases 0–2 | Ten phases with exit criteria | Status block added under Phase 2 recording that **criteria 2 and 3 were not evaluated as written** (inconsistency #3). Later phases remain planning. |
| `08-risks.md` | draft | Risk register and cost traps | Not revisited this session. |
| `09-validation.md` | draft | How the ranking is proven useful or not | Phase 9 cannot run until 6–12 weeks of data exist and DEX coverage is complete. |
| `10-phase2-spike.md` | carried — method superseded | First live price-derivation test, 3 runs, 37 swaps | Historically accurate, including its *correction* of the naive formula. Its method (one sampled trade vs `priceNative`) is superseded by doc 11 (reserves vs reserves). Cross-linked. |
| `11-phase2-gate.md` | **verified — current authority** | The go/no-go gate: criterion, method, results, scope, blocker | Three passes recorded: 0.39% (run A), **0.36% grade run**, 0.95% (three-number run). Carries the PumpSwap scope qualifier and the Phase 1b blocker. |
| `INVENTORY.md` | current | This file | — |

---

## `artifacts/`

| File | Status | Contents | Do not confuse with |
|---|---|---|---|
| `phase2_gate.json` | **verified — the grade run** | Fresh pull, shipped criterion, 2026-10-26: 34 inspected / **24 measured** / 5 excluded / 5 unpriced; mean **0.36%**, median 0.007%, p90 0.71%, max 5.42%, 96% inside 5%; liquidity 0.04%. Verdict **PASS**. | `phase2_gate_exec.json` — a *different* sample. |
| `phase2_gate_run.txt` | verified | The grade run's full stdout, committed so the pass can be audited against code that did not change during it. | — |
| `phase2_gate_exec.json` | verified | Three-number run (`--sigs 8`): 23 measured, mean 0.95%, 91% inside 5%, liquidity 0.09%, **PASS**. Carries the `exec_price_quote` field the grade run predates. | Its numbers differ from the grade run **because the sample differs**, not because results regressed. |
| `phase2_gate_exec_run.txt` | verified | Its stdout. | — |
| `triangulation.json` | verified | Two rounds 30s apart: chain reserves, provider reserves, `priceNative`, executed price. Chain ≡ provider to 4 s.f.; `priceNative` tracks execution. | — |

**Schema drift between the two gate artifacts (inconsistency #4):** `phase2_gate.json` was written
before `exec_price_quote` existed, so its rows lack that key. Both are correct for the code that
produced them. Re-running the grade command reproduces the newer schema.

---

## `spikes/` (cited by docs 10 and 11)

| File | Status | Contents |
|---|---|---|
| `phase2_spike.py` | carried | The original stdlib-only live test (doc 10). |
| `phase2_reconciliation.json` | carried | Its Run 3 output — documented in doc 10. |
| `phase2_spike_result.json`, `pumpswap_derived.json` | carried | Earlier spike outputs. |
| `triangulate.py` | **verified** | The two-round DexScreener triangulation; reproduces `artifacts/triangulation.json`. |

---

## Known inconsistencies

1. **Scope wording in `02-architecture.md`.** The `derive` component is described as if reserves →
   price applies to any DEX; the shipped code covers PumpSwap only. Doc 11 §8 and README carry the
   qualifier; doc 02 does not. *Open, cosmetic, but misleading to a reader who starts here.*
2. **`04-data-model.md` check-constraint count was wrong** (306 → 22, a NOT NULL double-count).
   *Fixed this session.*
3. **Two of doc 07's three Phase 2 exit criteria were not evaluated as written** — the same-tx
   slippage-sign check (its reference was measured 27% wrong) and the deepest-pool market-cap rule
   (market cap is Phase 5). *Annotated this session rather than left claiming a clean sweep.*
4. **The two gate artifacts have different row schemas** (see above). *Expected; documented.*
5. **`NOTES.md` did not exist** at session start — the prior session's copy did not survive the
   workspace move. It is recreated; its anchor and data-loss sections are marked *carried / not
   re-verified* because no artifact supporting them survived either.
6. **`docs/10` vs `docs/11` method.** Doc 10 reconciles a sampled trade against `priceNative`; doc 11
   reconciles reserves against reserves. Doc 10 is historical and cross-linked, not wrong.

## What is *not* covered by anything in this repo yet

- Any wallet, score, position, metric, alert, or backtest — **zero rows ever existed** (`NOTES.md`
  § `DATA_LOSS_2026_10`).
- Any DEX other than PumpSwap at the reserve/pricing layer.
- Phases 3–10 code: `discover`, `score`, `report`, `watch`, `validate` are stubs returning exit 2.
