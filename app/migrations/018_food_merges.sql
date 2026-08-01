-- Migration 018 - Undo a food merge.
--
-- Merging rewrites every reference to a food across seven tables, folds its aliases into the
-- target, copies over metadata the target lacks, and deletes the source row. Afterwards there is
-- no way to tell which of the target's references used to belong to the source, so the operation
-- was genuinely irreversible - guarded only by a generic browser confirm, on a screen whose
-- target is picked from a list of every food in the database.
--
-- This table is the receipt. It holds the deleted source food and the exact row ids that moved,
-- per table, so an undo can put back precisely what the merge took and nothing else. Recomputing
-- that set later is impossible, which is why it has to be captured at merge time.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE food_merges (
    id          INTEGER PRIMARY KEY,
    source_name TEXT NOT NULL,               -- for the "merged X into Y" message
    target_id   INTEGER REFERENCES foods(id) ON DELETE CASCADE,
    target_name TEXT NOT NULL,
    payload     TEXT NOT NULL,               -- source food row + moved row ids + moved aliases
    merged_at   TEXT NOT NULL DEFAULT (datetime('now')),
    merged_by   INTEGER REFERENCES users(id),
    undone_at   TEXT                         -- set once reversed; makes undo single-shot
);
CREATE INDEX idx_food_merges_merged_at ON food_merges(merged_at DESC);
