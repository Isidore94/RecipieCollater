-- Migration 011 - Phase 4: pantry (locations, items, adjustment history) and the shopping list,
-- plus the recipe-side mapping/trust columns that make review-first cook deductions possible.
--
-- The pantry is approximate by design (docs/06 section 1): graduated granularity - exact counts for
-- discrete items, a full/half/low/out gauge for bulk staples, have/out binary for condiments. Every
-- change writes a pantry_adjustments row (append-only history, NOT a mandatory double-entry ledger:
-- pantry_items holds current state) so cook-deduction Undo is atomic and "why did the flour drop?"
-- is answerable. All quantity math routes through app.services.quantity (canonical mg/uL/milli-each).
--
-- SPEC DEFERRALS (CONVENTIONS: record implementation-driven doc corrections in the same commit):
--  * docs/03 section 7 gives shopping_item_sources a meal_plan_entry_id REFERENCES meal_plan_entries
--    and a 'meal_plan' source_type. Meal plans are Phase 5, so meal_plan_entries does not exist yet.
--    The 'meal_plan' source_type is kept in the CHECK (harmless; avoids a Phase-5 table rebuild to
--    widen it) but meal_plan_entry_id is a bare INTEGER with no REFERENCES clause until Phase 5 owns
--    the parent table. Phase 4 only ever writes recipe/staple/manual sources.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

-- --------------------------------------------------------------------------------------
-- Pantry
-- --------------------------------------------------------------------------------------

CREATE TABLE locations (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,          -- 'Kitchen Cupboards', 'Downstairs Freezer'
    is_freezer INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE pantry_items (
    id                     INTEGER PRIMARY KEY,
    food_id                INTEGER REFERENCES foods(id),   -- nullable: free-text items allowed
    display_name           TEXT NOT NULL,                  -- what the family calls it
    location_id            INTEGER NOT NULL REFERENCES locations(id),
    quantity_mode          TEXT NOT NULL DEFAULT 'gauge'
                             CHECK (quantity_mode IN ('exact','gauge','binary')),
    quantity_text          TEXT,                     -- exact mode: user-facing decimal
    unit_id                INTEGER REFERENCES units(id),
    canonical_quantity     INTEGER,                  -- mg / uL / milli-each when resolvable
    gauge                  TEXT CHECK (gauge IN ('full','half','low','out')),  -- gauge mode
    have                   INTEGER,                  -- binary mode: 1/0
    is_staple              INTEGER NOT NULL DEFAULT 0,     -- always want on hand
    min_quantity_text      TEXT,
    canonical_min_quantity INTEGER,
    expires_on             TEXT,                     -- optional, mostly for freezer/fridge
    step_down_on_cook      INTEGER NOT NULL DEFAULT 0,     -- gauge/binary: opt-in auto-step-down
    updated_at             TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by             INTEGER REFERENCES users(id)
);
CREATE INDEX idx_pantry_items_location ON pantry_items(location_id);
CREATE INDEX idx_pantry_items_food ON pantry_items(food_id);

-- Append-only history behind pantry_items: every change with a reason and source.
CREATE TABLE pantry_adjustments (
    id                  INTEGER PRIMARY KEY,
    pantry_item_id      INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
    food_id             INTEGER REFERENCES foods(id),      -- kept even if the item row is deleted
    delta_quantity_text TEXT,                    -- signed exact decimal in the item's unit
    canonical_delta     INTEGER,                 -- signed mg / uL / milli-each
    from_gauge          TEXT, to_gauge TEXT,
    from_have           INTEGER, to_have INTEGER,-- binary transitions, for undo
    reason              TEXT NOT NULL CHECK (reason IN
                          ('cook','manual_remove','spoiled','restock','stock_take','correction')),
    source              TEXT,
    cook_log_id         INTEGER REFERENCES cook_log(id) ON DELETE SET NULL,
    batch_id            TEXT,                    -- groups one cook's deductions for one-tap Undo
    user_id             INTEGER REFERENCES users(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_pantry_adjustments_item ON pantry_adjustments(pantry_item_id);
CREATE INDEX idx_pantry_adjustments_batch ON pantry_adjustments(batch_id);
CREATE INDEX idx_pantry_adjustments_cook ON pantry_adjustments(cook_log_id);

-- --------------------------------------------------------------------------------------
-- Shopping list
-- --------------------------------------------------------------------------------------

CREATE TABLE shopping_lists (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','completed','archived')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE shopping_list_items (
    id                 INTEGER PRIMARY KEY,
    list_id            INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
    food_id            INTEGER REFERENCES foods(id),
    display_text       TEXT NOT NULL,
    quantity_text      TEXT,
    unit_id            INTEGER REFERENCES units(id),
    canonical_quantity INTEGER,
    category           TEXT,                     -- aisle grouping, from foods.category, overridable
    is_manual          INTEGER NOT NULL DEFAULT 0,    -- protects hand-added lines from regeneration
    checked            INTEGER NOT NULL DEFAULT 0,
    checked_at         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_shopping_list_items_list ON shopping_list_items(list_id);

CREATE TABLE shopping_item_sources (
    id                 INTEGER PRIMARY KEY,
    item_id            INTEGER NOT NULL REFERENCES shopping_list_items(id) ON DELETE CASCADE,
    source_type        TEXT NOT NULL CHECK (source_type IN ('meal_plan','recipe','staple','manual')),
    recipe_id          INTEGER REFERENCES recipes(id),
    meal_plan_entry_id INTEGER,                  -- Phase 5 wires the FK; unused in Phase 4
    quantity_text      TEXT,
    unit_id            INTEGER REFERENCES units(id),
    label              TEXT
);
CREATE INDEX idx_shopping_item_sources_item ON shopping_item_sources(item_id);

-- --------------------------------------------------------------------------------------
-- Recipe-side pantry mapping + deduction trust (docs/03 lines 169-211).
-- These land with Phase 4, alongside the pantry tables they reference (incremental-migration rule).
-- --------------------------------------------------------------------------------------

ALTER TABLE recipes ADD COLUMN deduction_mode TEXT NOT NULL DEFAULT 'review'
    CHECK (deduction_mode IN ('review','auto'));

ALTER TABLE recipe_ingredients ADD COLUMN deduct_from_pantry INTEGER NOT NULL DEFAULT 1;
ALTER TABLE recipe_ingredients ADD COLUMN pantry_item_hint INTEGER REFERENCES pantry_items(id);
ALTER TABLE recipe_ingredients ADD COLUMN deduction_trusted_at TEXT;
ALTER TABLE recipe_ingredients ADD COLUMN deduction_trust_signature TEXT;
