-- Migration 005 - Phase 1: manual cookbook recipe tables.
--
-- The recipe, step, ingredient, step-link, tag, and revision tables (docs/03 section 4),
-- restricted to PHASE-1 columns. Later-phase columns are intentionally absent and land with
-- the phase that owns them (CONVENTIONS 13, docs/03 section 4 note):
--   * Phase 2 ingestion: current_extraction_run_id, normalized_source_url, video_id,
--     recipe_steps.video_seconds, recipe_ingredients.parse_confidence.
--   * Phase 4 pantry: recipes.deduction_mode, recipe_ingredients.deduct_from_pantry,
--     pantry_item_hint, deduction_trusted_at, deduction_trust_signature.
--   * Phase 5 big-event: max_batch_servings, make_ahead_minutes, hold_minutes,
--     storage_notes, and the equipment / recipe_equipment tables.
--
-- Quantities are exact decimal strings (CONVENTIONS 1); SQL never does amount arithmetic.
-- FTS5 search arrives in migration 006. The runner owns transactions: no BEGIN/COMMIT/VACUUM.

CREATE TABLE recipes (
    id                 INTEGER PRIMARY KEY,
    slug               TEXT NOT NULL UNIQUE,
    title              TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'inbox'
                         CHECK (status IN ('inbox','cookbook','archived')),
    tier               TEXT CHECK (tier IN ('meal_prep','family','company')),
    tldr               TEXT,
    description        TEXT,
    base_servings      TEXT NOT NULL DEFAULT '4',   -- exact decimal string
    servings_text      TEXT,                        -- 'one 9-inch pie' when not countable
    prep_minutes       INTEGER,
    cook_minutes       INTEGER,
    total_minutes      INTEGER,
    active_minutes     INTEGER,                     -- hands-on effort
    elapsed_minutes    INTEGER,                     -- wall-clock incl. resting/braising
    our_minutes        INTEGER,                     -- what it actually takes in our kitchen
    our_active_minutes INTEGER,
    source_type        TEXT NOT NULL DEFAULT 'manual'
                         CHECK (source_type IN ('youtube','web','manual','photo')),
    source_url         TEXT,
    source_name        TEXT,
    image_path         TEXT,                        -- relative path under data/images/<recipe_id>/
    created_by         INTEGER REFERENCES users(id),
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    promoted_at        TEXT                         -- when moved inbox -> cookbook
);
CREATE INDEX idx_recipes_status ON recipes(status);

CREATE TABLE recipe_steps (
    id          INTEGER PRIMARY KEY,
    recipe_id   INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL,
    section     TEXT,                       -- 'For the sauce' grouping, nullable
    instruction TEXT NOT NULL,              -- markdown
    minutes     INTEGER
);
CREATE INDEX idx_recipe_steps_recipe ON recipe_steps(recipe_id, sort_order);

CREATE TABLE recipe_ingredients (
    id                    INTEGER PRIMARY KEY,
    recipe_id             INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    sort_order            INTEGER NOT NULL,
    section               TEXT,             -- ingredient group; never flatten groups
    original_text         TEXT NOT NULL,    -- '2 cups flour, sifted' - always preserved
    quantity_text         TEXT,             -- exact decimal; NULL => no amount
    unit_id               INTEGER REFERENCES units(id),
    food_id               INTEGER REFERENCES foods(id),
    note                  TEXT,
    scaling_mode          TEXT NOT NULL DEFAULT 'linear'
                            CHECK (scaling_mode IN ('linear','fixed','to_taste','round_to_package')),
    package_quantity_text TEXT,
    package_unit_id       INTEGER REFERENCES units(id),
    CHECK (quantity_text IS NULL OR unit_id IS NOT NULL)
);
CREATE INDEX idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id, sort_order);
CREATE INDEX idx_recipe_ingredients_food ON recipe_ingredients(food_id);

-- Ingredients may be used in multiple steps and divided between them.
CREATE TABLE recipe_step_ingredients (
    step_id       INTEGER NOT NULL REFERENCES recipe_steps(id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES recipe_ingredients(id) ON DELETE CASCADE,
    quantity_text TEXT,
    unit_id       INTEGER REFERENCES units(id),
    note          TEXT,
    PRIMARY KEY (step_id, ingredient_id)
);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE recipe_tags (
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (recipe_id, tag_id)
);

-- Cheap undo/history: a JSON snapshot of the full recipe before each user edit.
CREATE TABLE recipe_revisions (
    id        INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    saved_at  TEXT NOT NULL DEFAULT (datetime('now')),
    saved_by  INTEGER REFERENCES users(id),
    payload   TEXT NOT NULL                -- full recipe JSON
);
CREATE INDEX idx_recipe_revisions_recipe ON recipe_revisions(recipe_id);
