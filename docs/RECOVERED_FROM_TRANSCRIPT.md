# Design notes recovered from the imported prior-session transcript

**Status: archived source material, not a specification.** Nothing here is authoritative until it has
been reconciled against `solscope/` and `docs/`. It is what a prior session wrote, recovered verbatim.

Source: `.cloud-attachments/imported-conversation-15e0d1e04a7a7c2abc1520b3ca3df284a68fa14edd507c67307d3efcb7e4ab4d.json`
- 1,771,473 bytes, one physical line, 46 messages (23 user, 23 assistant), 1,692,577 characters of text.

## Method

The file is a single JSON line, so `grep -n` locates nothing, and its path is 275 characters, past the
Windows MAX_PATH limit, so Python `open()` on it fails while shell tools succeed. It was copied to a
short path and parsed as JSON.

Each excerpt is a verbatim window of decoded message text: 4,000 characters centred on a needle hit,
opened on a line boundary, capped at 50 lines. No excerpt is summarised, reordered or reworded. The only
transformations are: JSON envelope escaping removed (the source stores message bodies as escaped JSON
strings, so backslash-n and backslash-quote arrive as newlines and quotes), and credential-shaped tokens
redacted.

Where a needle occurs only in assistant turns, the excerpt is taken from the occurrence with the least
adjacent serialized tool-call JSON, so the window lands on prose rather than on a transport dump.

Provenance gives the message index (0-based, of 45) and where in that message the excerpt starts.

Redaction: any UUID becomes `<UUID-REDACTED>`, which covers both the revoked Helius key and the
ad-tracking impression and creative IDs, none of which belong in a design document; 32+ character hex
becomes `<HEX-REDACTED>`; other opaque 32+ character tokens become `<REDACTED>`. Solana base58
addresses, mints and 88-character signatures are kept, since they are public identifiers.

Needles were chosen as the load-bearing symbols of the design. A prior turn specified a literal needle
list that did not survive into this session, so sections are keyed on the identifiers the recovered text
itself uses. Section 7 comes from the one message that defines aggregation, split across three windows;
together they are that whole message.

---

## Section 1: Phase 1a: what survived the moves and what did not

Needles: `PHASE1A` = 174 (total 174)

Excerpts: 1 (of 174 needle hits).

### Needle `PHASE1A`, message 18 of 45 (role=user), from char 0

```text
User confirms: no session logs from Phase 1a. Cloud environment killed by provider for resource exhaustion, not recoverable. The recovery question is closed.

Two deliverables, in order. No code. No credits. No Phase 1b implementation.

Deliverable 1 — docs/PHASE1A_RECOVERY.md.

Before anything else, synthesize what is still known about Phase 1a's design from this conversation. The prior session log is gone, but the design was worked out in this thread across many turns. Capture it so it is not lost to context pruning.

Write docs/PHASE1A_RECOVERY.md with these sections:

§1 — Prior schema (for reference, not for porting)
The six Phase 1a tables and their purpose:

swap_events (wallet swaps, mint-delta representation)

wallet_token_positions (reconstructed positions, closed-flag)

swap_hops (multi-hop denormalization — one row per (swap, pool))

pool_cache (mint→pool resolution cache)

pool_candidates (diagnostics for ambiguous resolutions)

mint_pools (view aggregating swap_hops by mint)

Marked LOST — design only, no DDL survived. Current solscope/ solves equivalents differently; see NOTES.md § PHASE1A_AUDIT.

§2 — Mint-based route
Query the mint, not the pool. Verified: a mint's first transaction is mint creation (create_v2, initialize_mint2). Age is computed from that blockTime. Price series from swap ratios (sol_delta / abs(amount_delta)).

Marked VERIFIED 2026-10-15 (prior session), not re-verified against solscope/. When Phase 1b implements this, re-verify against current derive.py.

§3 — Quality filter findings and the genesis-burst rule
Measured: 44/45 false negatives on a known-bundled wallet set (2026-10-17).

Failure mode: same-deployer, same-second bundles.

Proposed genesis-burst rule: if a wallet's first buy for ≥N anchors occurs within Y seconds of each anchor's genesis, reject.

Suggested defaults: N=2, Y=60.

Prior filter's three heuristics (sandwich, throughput, router_only) were untested and are not to be ported without re-validation.

Marked VERIFIED measurement, PROPOSED rule. No code existed at any point.

§4 — Anchor criteria
Independent anchors require:

Deployer diversity (no two anchors share deployer; deployer not in another's first-20 buyers)
 [...]
```

---

## Section 2: swap_events and the swap representation

Needles: `swap_events` = 112 (total 112)

Excerpts: 1 (of 112 needle hits).

### Needle `swap_events`, message 6 of 45 (role=user), from char 0

```text
Phase 2 for PumpSwap accepted as complete. Three items before the closeout is clean.

Item 1 — record the third data-loss event, and the pattern.

Add to NOTES.md § PROCESS_FAILURES:

2026-10-26: Third data-loss event. Git history did not survive the cloud VM → desktop move. Prior commits are gone; 1944c74 is the new baseline. The code survived as files on disk; the history is lost. This is the third incident of the same class (git restore on NOTES.md, 2026-10-18; VM → desktop move, 2026-10-26; git history lost, same event but separate consequence). Mitigation going forward: (a) git remote (GitHub, GitLab, or self-hosted) configured before any further work; (b) nightly DB export to data/exports/ (gitignored, and shipped to the remote as a release asset if the DB matters); (c) NOTES.md and INVENTORY.md regenerated from session log after each loss. The pattern is not the loss, it is the discovery of the loss after further work was done. git push costs nothing and eliminates it.

Then: configure a git remote and push 1944c74. Do not proceed to further work without an off-machine backup.

Item 2 — the malformed tool call.

Report: what tool, what args, what error message? If identifiable, add to NOTES.md § ANTI_PATTERNS (harness instability). If not, accept it happened but re-verify git status before the next tool call — a malformed call may not have corrupted state, but the next one may.

Item 3 — 41-table inventory and Phase 1a data verdict.

The report says "transactions / swaps / wallets: 0 / 0 / 0 — nothing derived ever existed." But Phase 1a had ~1,510 swap_events and ~497 wallet_token_positions. Clarify:

Is swap_events present in the schema? Row count?

Is wallet_token_positions present? Row count?

Is swap_hops, pool_cache, pool_candidates, mint_pools present? Row counts?

If present-but-empty: Phase 1a data is lost, needs re-profiling (~300 credits, ~1 hour).
If absent: Phase 1a schema is also lost, and the "Phase 1a complete" claim from prior sessions applies to code, not data. State this.

Then list the 41 tables in docs/INVENTORY.md § schema with purpose and current row count. 41 is more than Phase 0+1a+2 should produce; identify the extras.

Do NOT:

Restart the collector. Phase 1b does not use WSS data. There is no current consumer. Do not spend credits filling a table no phase is reading.

Begin Raydium coverage this turn. Scheduled work, not blocking.

Do not begin Phase 3 or Phase 1b code.

Do not re-tune the gate.

Report back with:

NOTES.md § PROCESS_FAILURES entry, and the git remote push confirmation.

The malformed-call error message if identifiable.

The 41-table list with row counts, and Phase 1a data verdict.

Whether docs/INVENTORY.md § schema now lists all 41 tables with purposes.

Any new limitation from the above. [...]
```

---

## Section 3: Quality filter and the genesis-burst rule

Needles: `quality_filter` = 147 (total 147)

Excerpts: 1 (of 147 needle hits).

### Needle `quality_filter`, message 36 of 45 (role=user), from char 0

```text
The work presented is accepted across all five items. The DB harness is green twice-verified; the one-command bootstrap with both traps (SKIP-exits-zero, MSYS-no-conversion) encoded is careful engineering. The S4 calibration is correctly interpreted as validating the implementation, not the parameters — GENESIS_BURST_DEFAULTS_VALIDATED = False and the pending section staying open are exactly right. The S5 metric bodies with 29 hand-computed values are what a metric implementation should look like. cohort_excess is DEFINED (doc 05 §9 + cohort_benchmarks), not deferred — that resolves my earlier flag. Jupiter v6 registration is orthogonal and correct.

Two definitional confirmations:

pct_entries_before_volume_spike — the "candidacy not exclusion" reading is correct for the target population. Approve as implemented.

cohort_excess — the definition is verified; weight stands at 0.40 inside early_entry (or per the block that owns it — confirm the weight location is correct in scoring.yaml).

Next turn: Option A — anchor registry DDL + filter verdict writer.

Requirements:

schema/002_anchor_registry.sql:

anchors table: anchor_id (PK), mint (unique), genesis_slot, genesis_time, deployer, first_buy_observed_at, source (enum: user_supplied / dex_screener / synthetic), created_at, created_by_rule_version.

anchor_wallet_verdicts table: anchor_id (FK), wallet (FK), verdict (enum matching the reason codes from quality_filter.py), rule_id, rule_version, evaluated_at, evidence (JSONB), PK on (anchor_id, wallet, rule_version).

anchor_wallet_metrics table: anchor_id, wallet, metric_name (enum: max_subsequent_return, buyer_rank_at_entry, pct_entries_before_volume_spike), window_days (nullable; for max_subsequent_return), value, status (enum: computed / INSUFFICIENT_WINDOW / NO_OBSERVATIONS / NOT_COMPUTABLE), peak_day (nullable), computed_at. PK on (anchor_id, wallet, metric_name, window_days).

Add FK to wallets on the wallet columns; confirm wallets is the existing table (per Phase 1 audit, positions references it).

Writer module solscope/anchor_writer.py:

write_anchor(anchor: AnchorInput) -> anchor_id — idempotent on mint.

write_verdict(anchor_id, wallet, verdict, rule_id, rule_version, evidence) — idempotent on (anchor_id, wallet, rule_version).

write_metric(anchor_id, wallet, metric_name, window_days, value, status, peak_day) — idempotent on the PK.

All writes go through the existing db.py migration-aware path.

Test tests/test_anchor_writer.py:

Idempotency: writing the same anchor twice → one row.

Same for verdicts and metrics.

Schema smoke: FK violations raise cleanly.

Runs under the bootstrap venv.

Extend bootstrap_db.sh:

The migrate step already runs schema/001_init.sql; add 002_anchor_registry.sql to the applied set.

Assert new tables exist in the scratch DB after migration.

Assert admin DB is untouched (already done).
 [...]
```

---

## Section 4: Anchor metric: buyer_rank_at_entry

Needles: `buyer_rank_at_entry` = 78 (total 78)

Excerpts: 1 (of 78 needle hits).

### Needle `buyer_rank_at_entry`, message 24 of 45 (role=user), from char 0

```text
Both deliverables accepted. Three priorities before the parallel work begins.

Priority 1 — record verification findings in the recovery doc, not just the commit message.

The three corrections in §7 (Jupiter absent from registry, genesis decoder absent, version handling absent) and the §2 warning (token_age_seconds = pool age, not mint age) belong in docs/PHASE1A_RECOVERY.md itself. Without them, this thread's context is the only record, and it will be pruned.

Update §7:

Each program ID gets a status: VERIFIED_IN_CURRENT_CODEBASE / VERIFIED_PRIOR_ONLY / ABSENT_FROM_CURRENT_CODEBASE

For each ABSENT/PRIOR_ONLY: note the implication (Jupiter registry → coverage gap in decode.py's PROGRAMS, verify against fixtures; genesis decoder → S1 work; version handling → S4/S6 work)

Update §2:

Add a prominent callout: swaps.token_age_seconds is pool age (event_time - pool.created_at), not mint age. §2's mint-age route must NOT reuse this column.

Update §3:

Confirm the input domain: wallet_quality_filter reads the anchor→wallet→swaps chain, not positions alone. This resolves the naming ambiguity noted in item 6.

Update §5:

Add the double-count finding and its resolution (below).

Priority 2 — fix scoring.yaml + resolve the double-count.

config/scoring.yaml ships weight_mcap: 0.35 and lacks a max_subsequent_return term. It must be corrected to match §5:

weight_max_subsequent_return: 0.40

weight_buyer_rank_at_entry: 0.30

weight_pct_entries_before_volume_spike: 0.30

weight_mcap: 0.00 (retained, deferred)

weight_token_age: 0.00 (retained, diagnostic only)

Double-count resolution: drop token_selection.hit_multiple from the weighted score. Keep it as a diagnostic column. Rationale: max_subsequent_return (continuous) and hit_multiple ≥ 3.0 (binary threshold) measure the same signal. Including both double-rewards forward appreciation and structurally favors wallets with a few large wins over wallets with consistent mid-sized wins — the wrong incentive for "repeatable early-entry ability."

Record the decision in scoring.yaml as a comment and in docs/PHASE1B_SCOPING.md §Q4.

Priority 3 — define the three undefined metrics.

The definitions below are recommended. Implement them as the operationally-defined versions of §5's metrics. Note in the doc that they are revisitable if the retrospective anchor set's dynamics differ.

max_subsequent_return: peak SWAP-implied price ÷ entry price, over N days after entry. Report N ∈ {1, 7, 30}, headline 30d. Also report peak_day (integer, day-of-peak from entry). If entry is <N days ago, report INSUFFICIENT_WINDOW for that N; do not impute.

buyer_rank_at_entry: ordinal rank (1 = first buyer) by first-buy timestamp among unique buyers ever observed for the mint. Also report percentile = rank / total_unique_buyers. Score uses percentile. Ordinal is a diagnostic — rank 3 on a mint with 2,000 buyers is more impressive than rank 3 on one with 10.
 [...]
```

---

## Section 5: Anchor metric: pct_entries_before_volume_spike

Needles: `pct_entries_before_volume_spike` = 86 (total 86)

Excerpts: 1 (of 86 needle hits).

### Needle `pct_entries_before_volume_spike`, message 24 of 45 (role=user), from char 0

```text
Both deliverables accepted. Three priorities before the parallel work begins.

Priority 1 — record verification findings in the recovery doc, not just the commit message.

The three corrections in §7 (Jupiter absent from registry, genesis decoder absent, version handling absent) and the §2 warning (token_age_seconds = pool age, not mint age) belong in docs/PHASE1A_RECOVERY.md itself. Without them, this thread's context is the only record, and it will be pruned.

Update §7:

Each program ID gets a status: VERIFIED_IN_CURRENT_CODEBASE / VERIFIED_PRIOR_ONLY / ABSENT_FROM_CURRENT_CODEBASE

For each ABSENT/PRIOR_ONLY: note the implication (Jupiter registry → coverage gap in decode.py's PROGRAMS, verify against fixtures; genesis decoder → S1 work; version handling → S4/S6 work)

Update §2:

Add a prominent callout: swaps.token_age_seconds is pool age (event_time - pool.created_at), not mint age. §2's mint-age route must NOT reuse this column.

Update §3:

Confirm the input domain: wallet_quality_filter reads the anchor→wallet→swaps chain, not positions alone. This resolves the naming ambiguity noted in item 6.

Update §5:

Add the double-count finding and its resolution (below).

Priority 2 — fix scoring.yaml + resolve the double-count.

config/scoring.yaml ships weight_mcap: 0.35 and lacks a max_subsequent_return term. It must be corrected to match §5:

weight_max_subsequent_return: 0.40

weight_buyer_rank_at_entry: 0.30

weight_pct_entries_before_volume_spike: 0.30

weight_mcap: 0.00 (retained, deferred)

weight_token_age: 0.00 (retained, diagnostic only)

Double-count resolution: drop token_selection.hit_multiple from the weighted score. Keep it as a diagnostic column. Rationale: max_subsequent_return (continuous) and hit_multiple ≥ 3.0 (binary threshold) measure the same signal. Including both double-rewards forward appreciation and structurally favors wallets with a few large wins over wallets with consistent mid-sized wins — the wrong incentive for "repeatable early-entry ability."

Record the decision in scoring.yaml as a comment and in docs/PHASE1B_SCOPING.md §Q4.

Priority 3 — define the three undefined metrics.

The definitions below are recommended. Implement them as the operationally-defined versions of §5's metrics. Note in the doc that they are revisitable if the retrospective anchor set's dynamics differ.

max_subsequent_return: peak SWAP-implied price ÷ entry price, over N days after entry. Report N ∈ {1, 7, 30}, headline 30d. Also report peak_day (integer, day-of-peak from entry). If entry is <N days ago, report INSUFFICIENT_WINDOW for that N; do not impute.

buyer_rank_at_entry: ordinal rank (1 = first buyer) by first-buy timestamp among unique buyers ever observed for the mint. Also report percentile = rank / total_unique_buyers. Score uses percentile. Ordinal is a diagnostic — rank 3 on a mint with 2,000 buyers is more impressive than rank 3 on one with 10.
 [...]
```

---

## Section 6: sub_scores storage and rule_version

Needles: `rule_version` = 76, `sub_scores` = 87 (total 163)

Excerpts: 2 (of 163 needle hits).

### Needle `rule_version`, message 36 of 45 (role=user), from char 0

```text
The work presented is accepted across all five items. The DB harness is green twice-verified; the one-command bootstrap with both traps (SKIP-exits-zero, MSYS-no-conversion) encoded is careful engineering. The S4 calibration is correctly interpreted as validating the implementation, not the parameters — GENESIS_BURST_DEFAULTS_VALIDATED = False and the pending section staying open are exactly right. The S5 metric bodies with 29 hand-computed values are what a metric implementation should look like. cohort_excess is DEFINED (doc 05 §9 + cohort_benchmarks), not deferred — that resolves my earlier flag. Jupiter v6 registration is orthogonal and correct.

Two definitional confirmations:

pct_entries_before_volume_spike — the "candidacy not exclusion" reading is correct for the target population. Approve as implemented.

cohort_excess — the definition is verified; weight stands at 0.40 inside early_entry (or per the block that owns it — confirm the weight location is correct in scoring.yaml).

Next turn: Option A — anchor registry DDL + filter verdict writer.

Requirements:

schema/002_anchor_registry.sql:

anchors table: anchor_id (PK), mint (unique), genesis_slot, genesis_time, deployer, first_buy_observed_at, source (enum: user_supplied / dex_screener / synthetic), created_at, created_by_rule_version.

anchor_wallet_verdicts table: anchor_id (FK), wallet (FK), verdict (enum matching the reason codes from quality_filter.py), rule_id, rule_version, evaluated_at, evidence (JSONB), PK on (anchor_id, wallet, rule_version).

anchor_wallet_metrics table: anchor_id, wallet, metric_name (enum: max_subsequent_return, buyer_rank_at_entry, pct_entries_before_volume_spike), window_days (nullable; for max_subsequent_return), value, status (enum: computed / INSUFFICIENT_WINDOW / NO_OBSERVATIONS / NOT_COMPUTABLE), peak_day (nullable), computed_at. PK on (anchor_id, wallet, metric_name, window_days).

Add FK to wallets on the wallet columns; confirm wallets is the existing table (per Phase 1 audit, positions references it).

Writer module solscope/anchor_writer.py:

write_anchor(anchor: AnchorInput) -> anchor_id — idempotent on mint.

write_verdict(anchor_id, wallet, verdict, rule_id, rule_version, evidence) — idempotent on (anchor_id, wallet, rule_version).

write_metric(anchor_id, wallet, metric_name, window_days, value, status, peak_day) — idempotent on the PK.

All writes go through the existing db.py migration-aware path.

Test tests/test_anchor_writer.py:

Idempotency: writing the same anchor twice → one row.

Same for verdicts and metrics.

Schema smoke: FK violations raise cleanly.

Runs under the bootstrap venv.

Extend bootstrap_db.sh:

The migrate step already runs schema/001_init.sql; add 002_anchor_registry.sql to the applied set.

Assert new tables exist in the scratch DB after migration.

Assert admin DB is untouched (already done).
 [...]
```

### Needle `sub_scores`, message 21 of 45 (role=assistant), from char 460

```text
[...]  :236 — works on any address
- `fetch_transaction(self, sig)` :247 — archives verbatim to data/raw/tx
- `already_decoded(self, sig)` :258
- `persist(self, swap, *, raw_path)` :267 — inserts <REDACTED>
- `persist_failed_tx` :328
- `process_pool(self, pool, *, limit=30, ...)` :349 — driver loop
- `fetch_snapshot(mints, *, opener=None)` :391 — static, DexScreener batch (30 mints/req)
- `pick_for_pool` :446
- `pool_spot_from_tx(payload, pool, base_mint)` :461 — per-tx price from balance deltas
- `vault_reserves(pool)` :505 — owner enumeration, Token + Token-2022, 2 calls × 10 credits
- `price_from_reserves(reserves, base_mint, quote_mint)` :541 — staticmethod
- `discover_live_pools` :563
- `run_gate` :630
- `active_pools(limit)` :711
- `_gate_row` :720
- `_quote_to_usd` :821

**helius.py:** `signatures_for_address` :249, `token_accounts_by_owner` :259, `transaction` :277, `_rpc` :224, `fetch_usage` :291

**decode.py:** PROGRAMS 5 entries (pumpswap, pump, raydium_amm_v4, raydium_launchlab, raydium_cpmm), UNVERIFIED_PROGRAMS 3 (meteora_dlmm ×2, orca_whirlpool). No Jupiter. `quote_settlement` token|native. `anchor_disc`.

**Schema:**
- `swaps`: exec_price_quote, pool_price_quote, token_age_seconds (event_time - pool.created_at), mcap_usd, price_provenance, index `swaps_wallet_token_idx ON swaps (trader, base_mint, event_time)` — "The query every early-entry calculation makes: first buy per wallet per token"
- `positions`: entry_price_usd, entry_mcap_usd, entry_liquidity_usd, entry_token_age_seconds, entry_slot, max_price_1h/6h/24h/7d/30d, max_multiple_30d, max_drawdown_pct, realized_capture, exit_impact_bps, inventory_origin, pnl_completeness, pnl_limitation, two clocks, partial unique index
- `scores`: exists — score_run_id, address, as_of, overall_score, confidence_score, confidence_adjusted_score, band, rank_overall, recommendation, cluster_id, independence_factor, gated_by, UNIQUE(score_run_id, address)
- `sub_scores`, `score_evidence`, `score_sensitivity`, `score_runs`, `score_config_versions`, `wallet_metrics`, `metric_definitions` (owning_subscore CHECK), `cohort_benchmarks`
- NO anchor/genesis/deployer concept anywhere (grep = 0)
- ingest_time on: tokens(98), pools(138), transactions(270), swaps(298), token_transfers(346)

**scoring.yaml:** early_entry weight_age 0.45, weight_mcap 0.35, weight_prevolume 0.20; frozen: false; weights sum to 1.0 across 7 dims (early_entry 0.25). Gates: integrity_min 30, confidence_min 25, min_closed_positions 5. Bands: track 75, watch 60, investigate 45.

**budget.yaml:** get_signatures_for_address 1, get_transaction 1, get_account_info 1, get_token_accounts_by_owner 10 (UNVERIFIED conservative), websocket_connection 1, websocket_data 2 per 0.1MB. dexscreener monthly_cap_requests 300000 REQUESTS. geckoterminal 40000 requests.

**Gaps verified:** no create_v2/initialize_mint2 anywhere; no nativeTransfers/tokenTransfers literal reads; no v0/v1 handling (uiTokenAmount.amount only); no mint-genesis decoder; token_age_seconds is POOL age not mint age.

Now let me write the document. It must answer Q1-Q8 and end with proposed sequencing (turns, go/no-go).

Let me be thorough but structured. Then commit + push as its own commit.

For Q5 cost model comparison, let me think about the arithmetic:

Per-wallet profiling ~40 credits:
- Under budget.yaml: getSignaturesForAddress = 1 credit/page (up to 1000 sigs, but limit param). getTransaction = 1 credit each.
- A wallet profile needs: signatures for address (1-2 pages) + fetch each tx (N credits). If profiling a wallet = all its swaps over a window, say ~20-30 txs → ~20-32 credits + 2 sig pages = ~34. So ~40 is consistent. 
- BUT: there's a smarter approach — if the wallet's swaps are already decoded (in `swaps` from anchor reconstruction), profiling costs ~0 additional credits. The ~40 figure applies only when re-fetching a wallet's own history independently.

Per-mint gene [...]
```

---

## Section 7: Aggregation rules, in full

Needles: `aggregate_max_subsequent_return` = 1 (total 1)

Excerpts: 3 (of 1 needle hits).

### Needle `aggregate_max_subsequent_return`, message 42 of 45 (role=user), lines 0-49

```text
Aggregation defined. Write it into docs/PHASE1B_SCOPING.md § Aggregation and implement it as solscope/aggregation.py, tested against mocked anchor_wallet_metrics rows.

What to implement:

Create solscope/aggregation.py:

python
def aggregate_max_subsequent_return(
    per_anchor: Sequence[AnchorMetricRow],
    *, winsorize_at: Decimal = Decimal("100"), min_anchors: int = 2,
) -> AggregateResult

def aggregate_buyer_rank_at_entry(
    per_anchor: Sequence[AnchorMetricRow],
    *, min_anchors: int = 2,
) -> AggregateResult

def aggregate_pct_entries_before_volume_spike(
    per_anchor: Sequence[AnchorMetricRow],
    *, min_anchors: int = 2,
) -> AggregateResult

@dataclass
class AggregateResult:
    metric_name: str
    value: Decimal | None
    status: Literal["computed", "not_rankable", "not_computable"]
    anchor_count_computed: int
    anchor_count_excluded: int
    coverage: float
    evidence: list[dict]  # per-anchor trace
Rules as specified above. No shortcuts:

max_subsequent_return: geometric mean of (1 + winsorized_value), minus 1.

buyer_rank_at_entry: median percentile across computed anchors.

pct_entries_before_volume_spike: mean fraction across computed anchors.

computed_anchors < 2 → not_rankable.

computed_anchors + not_computable_anchors == 0 → not_computable.

Writer extension: solscope/anchor_writer.py gets a new function write_aggregate(conn, wallet, aggregate: AggregateResult, rule_version) writing to sub_scores with the schema above. Idempotent on (wallet, metric_name, rule_version).

Tests in tests/test_aggregation.py:

max_subsequent_return — hand-computed fixtures:

[4, 4, 4] (5× each) → (5^5^5)^(1/3) - 1 = 5 - 1 = 4.0
```

### Needle `aggregate_max_subsequent_return`, message 42 of 45 (role=user), lines 50-99

```text

[99, 0, 0] → (100 × 1 × 1)^(1/3) - 1 ≈ 3.64

[4, 4, 4, 4, 4] (5× five times) → 4.0 (beats the single-lottery case — verify)

Winsorization: [1000, 4, 4] → same as [100, 4, 4] (1000 capped at 100)

Total loss: [-1, 4, 4] → 0 (geometric mean collapses to 0)

[4] → not_rankable (only 1 computed)

[4, 4, NOT_COMPUTABLE, NOT_COMPUTABLE] → computed = 2, coverage = 0.5, value ≈ 4.0

buyer_rank_at_entry — hand-computed:

[0.001, 0.001, 0.5] → median = 0.001

[0.01, 0.02, 0.5] → median = 0.02

[0.5, 0.5, 0.001] → median = 0.5

[0.1] → not_rankable

pct_entries_before_volume_spike:

[0.5, 0.5, 0.5] → 0.5

[1.0, 0.0] → 0.5

[1.0] → not_rankable

Edge cases:

Empty per_anchor → not_computable

All not_computable → not_computable

Mixed computed + not_computable → correct coverage, correct value

write_aggregate idempotency: writing same aggregate twice → one row; version bump → two rows.

Schema smoke: sub_scores exists with the columns expected (wallet, metric_name, rule_version, value, status, anchor_count_computed, anchor_count_excluded, evidence).

If sub_scores lacks any of those columns, add a schema/003_sub_scores_columns.sql migration — do not modify 001_init.sql.

Also update docs/PHASE1B_SCOPING.md:

Add § Aggregation with the three definitions, the preconditions, the edge-case table, and the explicit "not in scope" list. Note in §Q4 that the exact blend of anchor_sample_size and anchor_coverage into confidence is the consumer's decision and is not yet specified.

Verification:
```

### Needle `aggregate_max_subsequent_return`, message 42 of 45 (role=user), lines 100-149

```text

bash bootstrap_db.sh green, with 003_sub_scores_columns.sql applied if it exists.

pytest tests/test_aggregation.py -v green.

Commit and push. Report the commit hash and git status.

Do NOT:

Write the scoring consumer this turn — that is next turn's work.

Modify quality_filter.py, metrics.py, scoring.yaml, anchor_writer.py's existing functions.

Add dependencies.

Change schema/001_init.sql or schema/002_anchor_registry.sql.

Spend credits.

Start S7.

Report back with:

solscope/aggregation.py signatures + docstrings.

tests/test_aggregation.py pass counts.

The sub_scores schema check result (existing / migrated).

docs/PHASE1B_SCOPING.md § Aggregation section.

Commit hash + remote confirmation.
```

---

## Section 8: Positions, FIFO matching and cost basis

Needles: `lot_matches` = 18, `cost basis` = 9, `FIFO` = 5 (total 32)

Excerpts: 3 (of 32 needle hits).

### Needle `lot_matches`, message 23 of 45 (role=assistant), from char 13928

```text
[...] ST signatures, the genesis use case must page to the OLDEST page via the `before` cursor. Reverse pagination is new behaviour on a reused primitive.
- `Deriver.process_pool(pool, *, limit=30)` :349 — the driver loop; needs a mint-seeded sibling. It binds pool → (base, quote) from `known_pools()`, which will be empty for a mint seed.
- `Deriver.known_pools()` :225 — reads `pools`; a mint seed has no registry entry. This is the void `pool_cache`/`mint_pools` used to fill.

**New code needed:**
1. **Mint genesis decoder** — `create_v2` / `initialize_mint2` → genesis slot + blockTime. ZERO occurrences today. The single largest new piece.
2. **Mint age** — must be mint age; `swaps.token_age_seconds` is `event_time - pool.created_at` (POOL age) and must not be reused as-is.
3. **mint → pool resolution** with an empty `pools` registry (successor to `pool_cache`/`mint_pools`), including the ambiguity diagnostic that `pool_candidates` provided.
4. **Price series over a mint's own history** — `pool_spot_from_tx` gives per-pool-per-tx spot; a mint series must be assembled across pools (this is where `swap_hops`' problem returns).
5. **Wallet-history direction** — `fetch_signatures(wallet)` + first-buy-per-(wallet,anchor). The index `swaps_wallet_token_idx` already anticipates exactly this query.
6. **Helius v0/v1 defensive parsing** — real gap; no branch today.
7. **Anchor registry storage** — see Q2.

Q2 — where anchor-derived positions live:
- `swaps`, `positions`, `pools` **suffice for position facts**. `positions` already carries entry_price_usd, entry_mcap_usd, entry_liquidity_usd, entry_token_age_seconds, entry_slot, max_price_1h/6h/24h/7d/30d, max_multiple_30d, max_drawdown_pct, realized_capture, exit_impact_bps — which is exactly what §5's inputs need.
- **Invariants that already hold:** two clocks (`event_time` + `ingest_time`) on tokens/pools/transactions/swaps/token_transfers; `inventory_origin ∈ {swap_only, includes_transfer_in, includes_airdrop}`; `lot_matches` links only buy_swap_id↔sell_swap_id so a transfer cannot acquire cost basis; partial unique index = one open position per (wallet, token); `UNIQUE NULLS NOT DISTINCT` on swaps dedupe.
- **A new table IS needed — but not for positions.** Positions are fine. What's missing is **anchor provenance**, and the grep proves it: `anchor`, `genesis`, `first_buy`, `early_buy`, `deployer` appear **zero times** in the schema. Without it you cannot answer: which anchor set produced this wallet, which anchor did this wallet enter, at what buyer rank, and what did the quality filter decide and why. That provenance is what makes the score *explainable* and what makes the genesis-burst rejection auditable.
- **Additional invariants Phase 1b requires:**
  a. **Genesis is point-in-time.** Genesis blockTime must be read once and stored, with `ingest_time`, so a later re-read cannot silently change age/rank. (Same reasoning as `cohort_benchmarks.available_at`.)
  b. **Buyer rank must come from a COMPLETE first-N set.** A sampled first-20 makes rank a function of sampling, not of behaviour. The set must be provably complete or the rank must be flagged not-computed.
  c. **Filter verdicts are immutable and versioned**, like `scores` — a re-run with changed thresholds must not overwrite the old verdict, or the 44/45 failure cannot be re-measured against a fixed filter.
  d. **Anchor → wallet edges must record which anchor**, so independence de-duplication (§4) is computable.
  e. **A rejected wallet must be storable.** "Rejected" is an outcome, not an absence — otherwise the false-negative rate (the 44/45 metric) is unmeasurable.

Q3 — quality filter:
- **Built fresh. Reads from the anchor→wallet→swaps chain, not from positions alone.** Reason: the genesis-burst rule needs (wallet, anchor, first-buy timestamp, anchor genesis timestamp). `positions.entry_token_age_seconds` gives the *delta* but not the anchor identity or the absolute genesis time needed for cross-anc [...]
```

### Needle `cost basis`, message 11 of 45 (role=assistant), from char 42098

```text
[...]  the extras": 41 tables vs what Phase 0+1a+2 should produce. Phase 0 (ops): credit_ledger, job_runs, job_checkpoints, ingestion_queue, stream_subscriptions, schema_migrations (6-7). Phase 1a (collection): raw_payloads, pools, pool_reserve_snapshots, pool_snapshots, tokens, token_supply_events, chains (7). Phase 2 (decode): transactions, swaps, token_transfers (3). That's ~17. The extras (~24) are forward-looking tables for Phases 3-10: positions/lot_matches/equity_snapshots (Phase 3), wallets + wallet intel (Phase 1/4), metrics (Phase 5), scoring 6 tables (Phase 6), data_quality_flags (Phase 3+), monitoring 5 tables (Phase 7), validation 3 tables (Phase 9). They exist because the schema was written once, complete, in the planning phase — not because data was written.

Wait — wallets: is wallets Phase 1 or Phase 1b? Wallets registry is populated by discovery (Phase 1b/4) and Phase 2 (decode). It's 0.

Let me build the 41-table list with purposes. From schema grep order + docs/04 map:

Reference/tokens/pools:
1. chains — chain registry (TEXT PK) — 0
2. tokens — token/mint registry: symbol, decimals, launchpad, created_at — 0
3. token_supply_events — supply changes; mcap depends on supply — 0
4. pools — trading pools per DEX with vault layout — 0
5. pool_reserve_snapshots — point-in-time reserve reads (price history) — 0
6. pool_snapshots — current-state provider snapshot (fetched_at only) — 0

Wallets:
7. wallets — address registry, history_completeness — 0
8. wallet_labels — third-party labels (weak evidence) — 0
9. wallet_clusters — behavioural clusters (common_funder, co_purchase...) — 0
10. wallet_cluster_members — cluster membership rows — 0
11. wallet_funding_links — funding paths; funder_is_exchange flag — 0

Raw/events:
12. raw_payloads — append-only raw archive; recomputation source — 0
13. transactions — decoded transactions — 0
14. swaps — decoded swaps (exec_price vs pool_price) — 0
15. token_transfers — non-swap transfers (never cost basis) — 0

Positions:
16. positions — FIFO position episodes, inventory_origin — 0
17. lot_matches — buy↔sell lot matching, realized P&L — 0
18. equity_snapshots — mark-to-market equity — 0

Metrics:
19. metric_definitions — metric registry, owning_subscore constraint — 0
20. wallet_metrics — computed metric values as-of — 0
21. cohort_benchmarks — benchmarks with available_at — 0

Scoring:
22. score_config_versions — frozen weights, config_sha256 — 0
23. score_runs — a scoring run, as_of — 0
24. scores — overall/confidence/confidence_adjusted per wallet — 0
25. sub_scores — the 8 sub-scores w/ explanation NOT NULL — 0
26. score_evidence — score→evidence links — 0
27. score_sensitivity — counterfactuals / monitoring triggers — 0

Quality:
28. data_quality_flags — per-metric missingness/reason — 0

Monitoring:
29. watchlists — watchlists — 0
30. watchlist_members — wallet membership — 0
31. alert_rules — rules, default is_active=false — 0
32. alerts — fired alerts, suppression_key dedupe — 0
33. alert_deliveries — delivery attempts — 0

Operations:
34. credit_ledger — provider credit accounting, monthly cap — 1 (test row)
35. job_runs — job execution records — 0
36. job_checkpoints — restart safety — 0
37. ingestion_queue — queue for ingestion work — 0
38. stream_subscriptions — WSS subscriptions — 0

Validation:
39. validation_experiments — backtest experiment definitions — 0
40. validation_results — results — 0
41. alert_outcomes — did alerts prove useful (false positives) — 0

Plus schema_migrations (42nd, created by the migration runner, not counted in the 41 in the DDL file).

Extras identification: Phase 0+1+2 writers touch ~16-17 of them (chains, tokens, token_supply_events, pools, pool_reserve_snapshots, pool_snapshots, raw_payloads, transactions, swaps, token_transfers, credit_ledger, job_runs, job_checkpoints, ingestion_queue, stream_subscriptions, wallets, token_transfers...). The remaining ~24 are Phase 3- [...]
```

### Needle `FIFO`, message 5 of 45 (role=assistant), from char 88800

```text
[...] .
+
 ## Verification status
 
 ```
","tooLarge":false},{"path":"docs\07-roadmap.md","status":"modified","lockfile":false,"binary":false,"evidence":"agent-edit","adds":14,"dels":0,"patch":"Index: docs\07-roadmap.md
<REDACTED>
--- docs\07-roadmap.md	before
+++ docs\07-roadmap.md	after
@@ -91,6 +91,20 @@
 the early-entry objective is unreachable at this budget. The honest outcome is to publish the finding
 and reassess scope, not to substitute candle-based prices and pretend the precision is unchanged.
 
+**Status 2026-10-26 — met for PumpSwap; two of the three criteria as written were not the ones
+evaluated.** Full evidence in [doc 11](11-phase2-gate.md).
+
+| Exit criterion as written above | Outcome |
+|---|---|
+| 20+ live pairs, price and liquidity within tolerance | **Met for PumpSwap** — 24 pools, mean price 0.36%, liquidity 0.04%, 96% inside 5% (fresh pull, shipped criterion). |
+| `exec_price` vs `pool_price` slippage plausible and correctly signed | **Not evaluated as written.** The reference it needs — the same-transaction pool spot — was measured 27% wrong on a correct-looking fixture, because a transaction's `meta` omits vaults it never touched. Replaced by reserve-vs-reserve reconciliation plus a mid-window drift check. Doc 11 §2.1. |
+| Multi-pool tokens: mcap from the deepest pool, choice recorded | **Not evaluated.** The gate prices one pool at a time and deliberately refuses to substitute another pool for the mint. Market cap is Phase 5 work. |
+
+**Scope: PumpSwap only** — Raydium AMM v4/CPMM/LaunchLab, pump.fun bonding curves, Orca Whirlpool,
+Meteora DLMM, Jupiter routing and aggregator hops are all outside the gate (doc 11 §8). Phases 3–6
+can proceed on PumpSwap data; **Phase 9 cannot make a ranking claim without the rest**, because
+scoring wallets on incomplete history biases every per-wallet rate.
+
 ## Phase 3 — Position reconstruction (1–2 weeks)
 
 FIFO lot ledger; position episodes; realized/unrealized split with mark-to-market equity; fee
","tooLarge":false},{"path":"docs\11-phase2-gate.md","status":"modified","lockfile":false,"binary":false,"evidence":"agent-edit","adds":6,"dels":1,"patch":"Index: docs\11-phase2-gate.md
<REDACTED>
--- docs\11-phase2-gate.md	before
+++ docs\11-phase2-gate.md	after
@@ -29,12 +29,17 @@
 | **Shipped** (mean ≤5%, ≥80% inside, liquidity ≤5%, movers excluded) | 24 | **PASS** — mean 0.36%, 96% inside 5% |
 | Original mean-only, no exclusions | 27 | **PASS** — mean 1.54% |
 | Shipped thresholds but **no exclusion at all** | 27 | **PASS** — mean 1.54%, 89% inside 5% |
-| Strictest: every pool inside 5%, no exclusion | 27 | **FAIL** — 23/27 (85%) |
+| Strictest: every pool inside 5%, no exclusion | 27 | **FAIL** — 24/27 (89%) |
 
 The only criterion it fails is the strictest one, which no version of the docs ever specified and
 which is unachievable in principle on a live pool: it requires the pool not to trade while being
 measured. Every criterion the project actually wrote down passes, including the original.
 
+(One caveat for the reader: rows 1 and 3 above differ only in whether the ≥80% sub-rule is applied —
+both use the same 27 pools and the same 1.54% mean. The strictest row fails on `24/27 ≠ 27/27`, not
+on a threshold. Counts here are recomputed from `artifacts/phase2_gate.json`; an earlier draft of this
+section reported 23/27 because the tally was taken over the 24 *measured* pools instead of all 27.)
+
 This is the go/no-go gate from doc 07. Doc 03 §"Phase 2" says: *"for 20 live pairs, locally derived
 price agrees with a contemporaneous DexScreener snapshot within tolerance, and derived liquidity is
 within tolerance of the snapshot's liquidity."* If it fails, the early-entry objective is unreachable
","tooLarge":false},{"path":"docs\INVENTORY.md","status":"added","lockfile":fals [...]
```

---

