-- Migration 015 - Undo for individual pantry adjustments.
--
-- pantry_adjustments already carried everything needed to reverse a change in place: the gauge
-- and have transitions (from_gauge/to_gauge, from_have/to_have) and the signed canonical delta.
-- Two things were missing.
--
-- undone_at makes an undo single-shot. Without it, a double tap or a page reload would apply the
-- reversal twice and over-restore the pantry. The cook-deduction undo solves the same problem
-- with a terminal marker row keyed on batch_id; that works for a batch but not for a single
-- adjustment, which has no batch of its own.
--
-- undo_payload covers the one case the transition columns cannot: a hard delete. Deleting an
-- item removes the row, so restoring it means recreating it with the configuration the user
-- chose - name, location, tracking mode, staple flag, minimum, expiry. That is captured as JSON
-- at delete time and is the only way "I deleted the wrong thing" is recoverable at all.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

ALTER TABLE pantry_adjustments ADD COLUMN undone_at TEXT;
ALTER TABLE pantry_adjustments ADD COLUMN undo_payload TEXT;

-- Offering "Undo" means finding the newest adjustment for an item, and later resolving one by id
-- while checking it has not already been reversed.
CREATE INDEX idx_pantry_adjustments_undo ON pantry_adjustments(pantry_item_id, id DESC);
