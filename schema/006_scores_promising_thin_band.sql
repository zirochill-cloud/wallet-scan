-- 006_scores_promising_thin_band.sql — admit the fifth band to the schema.
--
-- §5/D15 adds `promising_thin`: a wallet that fails only the anchor conjunct but
-- looks strong on the raw score. It is a real outcome of the band rule, so leaving
-- it out of the CHECK would make the rule unimplementable — and leaving
-- `deprioritize` in place after D13 removed it from the rule is the stale-value
-- problem D13 is about. `deprioritize` stays legal (no rows are rewritten, and a
-- historical row must remain readable) but is commented as unreachable, so a
-- reader can tell the difference between "no wallet is in this band" and "this
-- band no longer exists".
--
-- `recommendation_kind` gains the same value so `recommendation` can say what
-- `band` says instead of silently degrading to a neighbouring verdict.
-- ALTER TYPE ... ADD VALUE runs inside this transaction because the value is not
-- used until later consumer runs, in different transactions (PostgreSQL 12+).

BEGIN;

ALTER TABLE scores DROP CONSTRAINT scores_band_check;
ALTER TABLE scores ADD CONSTRAINT scores_band_check CHECK (band IN
  ('track', 'watch', 'investigate', 'deprioritize', 'exclude', 'promising_thin'));

ALTER TYPE recommendation_kind ADD VALUE IF NOT EXISTS 'promising_thin';

COMMENT ON COLUMN scores.band IS
  'track/watch/investigate/promising_thin/exclude. deprioritize is a legacy value: no band rule emits it (D13), retained only so historical rows stay readable.';

COMMIT;
