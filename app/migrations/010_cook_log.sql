-- Migration 010 - Phase 3: cook log + immutable per-cook ingredient snapshots, and the
-- long-reserved recipe_steps.video_seconds deep-link column.
--
-- cook_log is the after-cook record (rate -> correct the time -> note actual quantities) that makes
-- this "our" cookbook and gates the inbox -> cookbook promotion (docs/03 section 5).
-- cook_log_ingredients is a write-once snapshot of what was actually used, so history survives
-- later recipe edits (ingredient_id ON DELETE SET NULL, original_text kept verbatim).
--
-- SPEC CORRECTION (CONVENTIONS: record implementation-driven doc corrections in the same commit):
-- docs/03 section 5 wrote cook_log.rating as 1-5, but recipes.rating was widened to 1-10 in
-- migration 009 and the after-cook rating mirrors the recipe's star rating, so both share the
-- 1-10 scale. docs/03 is corrected in this commit.
--
-- recipe_steps.video_seconds was reserved in the 005 header as Phase-2-owned, but migration 007
-- never added it; Phase 3 (per-step video seek) is its first consumer, so it lands here.
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE cook_log (
    id                     INTEGER PRIMARY KEY,
    recipe_id              INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    user_id                INTEGER REFERENCES users(id),
    cooked_at              TEXT NOT NULL DEFAULT (datetime('now')),
    servings_made          TEXT,
    actual_minutes         INTEGER,   -- feeds the recipes.our_minutes suggestion
    actual_active_minutes  INTEGER,
    actual_elapsed_minutes INTEGER,
    rating                 INTEGER CHECK (rating BETWEEN 1 AND 10),
    notes                  TEXT
);
CREATE INDEX idx_cook_log_recipe ON cook_log(recipe_id, cooked_at);

CREATE TABLE cook_log_ingredients (
    id                    INTEGER PRIMARY KEY,
    cook_log_id           INTEGER NOT NULL REFERENCES cook_log(id) ON DELETE CASCADE,
    ingredient_id         INTEGER REFERENCES recipe_ingredients(id) ON DELETE SET NULL,
    original_text         TEXT NOT NULL,
    food_id               INTEGER REFERENCES foods(id),
    planned_quantity_text TEXT,
    used_quantity_text    TEXT,
    used_unit_id          INTEGER REFERENCES units(id)
);
CREATE INDEX idx_cook_log_ingredients_log ON cook_log_ingredients(cook_log_id);

ALTER TABLE recipe_steps ADD COLUMN video_seconds INTEGER;
