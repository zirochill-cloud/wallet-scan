-- 007 — quality-filter verdicts for the three implemented heuristics.
--
-- 002 deliberately shipped only the genesis-burst rule, and both its enums and its
-- consistency CHECK named that one verdict explicitly. sandwich, throughput and
-- router_only are implemented as of 2026-11-08 (solscope/quality_filter.py), so:
--
--   1. their verdicts and rule ids must be storable — a rejection that cannot be
--      written is a decision the consumer cannot enforce, and enforcement is the
--      only thing that makes a verdict matter;
--   2. the CHECK must express the INVARIANT ("a rejection names its rule") rather
--      than one rule's name.
--
-- ADD VALUE IF NOT EXISTS keeps this idempotent, and new enum values are appended,
-- so stored representations of existing rows do not change.

ALTER TYPE anchor_wallet_verdict_kind ADD VALUE IF NOT EXISTS 'wallet.reject_sandwich';
ALTER TYPE anchor_wallet_verdict_kind ADD VALUE IF NOT EXISTS 'wallet.reject_throughput';
ALTER TYPE anchor_wallet_verdict_kind ADD VALUE IF NOT EXISTS 'wallet.reject_router_only';
ALTER TYPE anchor_wallet_verdict_kind ADD VALUE IF NOT EXISTS
  'wallet.not_computable_insufficient_swaps';

ALTER TYPE quality_rule_id ADD VALUE IF NOT EXISTS 'rule.sandwich';
ALTER TYPE quality_rule_id ADD VALUE IF NOT EXISTS 'rule.throughput';
ALTER TYPE quality_rule_id ADD VALUE IF NOT EXISTS 'rule.router_only';

-- The invariant, not the rule name: every wallet.reject* verdict carries a rule id,
-- and no other verdict does. Written as an explicit prefix test because a CHECK
-- cannot enumerate an enum's future members.
ALTER TABLE anchor_wallet_verdicts DROP CONSTRAINT verdict_rule_consistency;
ALTER TABLE anchor_wallet_verdicts ADD CONSTRAINT verdict_rule_consistency
  CHECK ((verdict::text LIKE 'wallet.reject%') = (rule_id IS NOT NULL));
