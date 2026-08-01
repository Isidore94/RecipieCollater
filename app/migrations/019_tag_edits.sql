-- Migration 019 - Tag upkeep, reversibly.
--
-- Tags arrive from three places - the extraction prompts' controlled vocabulary, the AI
-- backfill, and a free-text field on the recipe form - and until now nothing could correct
-- them afterwards. A typo, a stray capital ("Soup" beside "soup") or a word outside the
-- vocabulary ("Entree" where the rest of the cookbook says "dinner") could only be fixed by
-- editing every recipe carrying it, one at a time. At a few dozen recipes that is tedious; at
-- several hundred it means the filter chips slowly stop being trustworthy.
--
-- Renaming needs no receipt: nothing is lost, and renaming back restores it exactly.
-- Merging and deleting do, for the same reason food merges do (migration 018) - afterwards
-- there is no way to tell which of the target's recipes used to belong to the source, so the
-- set has to be captured while it is still knowable.
--
-- The payload records two lists, because they differ and both matter:
--   unlinked - every recipe that carried the source tag; undo puts the tag back on these.
--   linked   - the subset that did NOT already carry the target; undo removes the target from
--              exactly these, leaving alone a recipe that legitimately had both all along.
-- A delete uses `unlinked` only.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE tag_edits (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('merge', 'delete')),
    source_name TEXT NOT NULL,               -- the tag that went away
    target_id   INTEGER REFERENCES tags(id) ON DELETE SET NULL,  -- what it became, for a merge
    target_name TEXT,                        -- the name it had then, for the message
    payload     TEXT NOT NULL,               -- {"unlinked": [recipe ids], "linked": [...]}
    edited_at   TEXT NOT NULL DEFAULT (datetime('now')),
    edited_by   INTEGER REFERENCES users(id),
    undone_at   TEXT                         -- set once reversed; makes undo single-shot
);
CREATE INDEX idx_tag_edits_edited_at ON tag_edits(edited_at DESC);

-- recipe_tags' primary key is (recipe_id, tag_id), which indexes lookups that start from a
-- recipe. Every operation in this migration starts from the other end - "which recipes carry
-- this tag" - and so did nothing before it, which is why the index was never missed.
CREATE INDEX idx_recipe_tags_tag ON recipe_tags(tag_id);
