-- 004_positions_entry_swap_signature.sql — key a position to the swap that opened it.
--
-- WHY THIS COLUMN EXISTS
-- §12's price-provenance factor asks what share of a wallet's entries were priced
-- from the swap's own pool reserves rather than a candle interpolation, a provider
-- decode or a current-state snapshot. That is a judgement about one *entry event*,
-- and `positions` recorded the entry's price and timestamp but not which swap
-- produced them. The only join available was approximate —
-- (address, mint, opened_at) with a tolerance — which is exactly the wrong place to
-- accept an undocumented error rate: provenance's whole job is to say how much the
-- entry numbers can be trusted.
--
-- NULL has one meaning, and it is not "not yet backfilled":
--   * a buy-opened episode records the opening buy's signature;
--   * a sell-opened episode (inventory acquired before the observed window) has no
--     entry buy, so it records NULL and earns no provenance credit. Dropping it
--     from the denominator instead would let a wallet improve its provenance score
--     by selling things it never bought.
--
-- No foreign key to `swaps(signature)`: `swaps.signature` is not unique (one
-- transaction can contain several swap instructions), so it is not a valid FK
-- target. No FK to `transactions(signature)` either — positions are derived rows,
-- re-created by rebuild(), and coupling derived history to the ingest lifecycle
-- (ON DELETE CASCADE) would silently delete P&L when a transaction was re-ingested.
-- The wallet/mint/side predicates in the join keep the reference honest.
--
-- The index on `swaps(signature)` is added here because it is this migration's own
-- join: reading one position's entry swap must not seq-scan the swap table.

BEGIN;

ALTER TABLE positions ADD COLUMN entry_swap_signature TEXT;

COMMENT ON COLUMN positions.entry_swap_signature IS
  'Signature of the swap that opened the episode; NULL when the episode opened on a sell (no entry buy exists).';

CREATE INDEX swaps_signature_idx ON swaps (signature);

COMMIT;
