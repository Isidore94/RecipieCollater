-- Migration 017 - A deleted recipe can be brought back.
--
-- Deleting a recipe cascades: ingredients, steps, tags, revisions, the cook log and any plan
-- entries all go with it. recipe_revisions cannot help, because it cascades too - the snapshots
-- kept "for cheap undo" are destroyed by the very operation someone would want to undo.
--
-- So the snapshot has to live outside the recipe's own foreign-key graph. This table holds the
-- full recipe as JSON, written just before the delete, with no reference to recipes(id) that
-- could cascade it away. Restoring re-creates the recipe from that payload, which rebuilds the
-- ingredient, step and tag rows and the FTS entry through the normal create path.
--
-- What comes back is the recipe: title, ingredients, steps, tags, timings, source, rating and
-- notes. What does not is the cook log - those rows reference a recipe id that no longer exists
-- and re-pointing them at a new one would be inventing history. The delete confirmation says so
-- when there is a log to lose, and points at Archive, which is reversible and keeps everything.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE deleted_recipes (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL,               -- the URL it had, for the "restored as ..." message
    title       TEXT NOT NULL,
    payload     TEXT NOT NULL,               -- full RecipeDetail JSON
    deleted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    deleted_by  INTEGER REFERENCES users(id),
    restored_at TEXT                         -- set once brought back; makes restore single-shot
);
CREATE INDEX idx_deleted_recipes_deleted_at ON deleted_recipes(deleted_at DESC);
