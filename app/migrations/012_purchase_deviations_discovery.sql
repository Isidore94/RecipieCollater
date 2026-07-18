-- Migration 012 - GUI usability overhaul (Phase 4.6, docs/08): the shopping list learns how
-- food is BOUGHT, the cook log learns what ACTUALLY happened, and foods gain the grouping
-- that pantry-aware discovery ("what can I make with chicken?") joins on.
--
-- 1) Purchase info lives on FOODS, not recipe lines: "flour comes in 2 kg bags" is a fact about
--    how the household shops, invariant across recipes. recipe_ingredients keeps its per-line
--    package columns (005) as a per-recipe override; the food-level value is the default the
--    shopping list ceilings to. purchase_label is the human word for one package ('bag', 'can').
-- 2) parent_food_id groups cuts/variants under a family ('chicken breast' -> 'chicken') so
--    discovery can match at the family level without merging distinct foods.
-- 3) cook_log_ingredients.deviation records structured after-cook deviations (NULL = as written);
--    used_text / used_quantity_text (the latter added in 010, unused until now) carry what was
--    actually used. cook_log.additions is the free-text "we also threw in ...".
-- 4) food_substitutes is the household's LEARNED substitution memory, seeded from real cooks -
--    "missing buttermilk? you've subbed milk + lemon before" - and later a Phase-5 assistant tool.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

ALTER TABLE foods ADD COLUMN purchase_quantity_text TEXT;      -- '2', '400' (exact decimal string)
ALTER TABLE foods ADD COLUMN purchase_unit_id INTEGER REFERENCES units(id);
ALTER TABLE foods ADD COLUMN purchase_label TEXT;              -- 'bag', 'can', 'bunch', 'pack'
ALTER TABLE foods ADD COLUMN parent_food_id INTEGER REFERENCES foods(id);

CREATE INDEX idx_foods_parent ON foods(parent_food_id);

-- Recipe lines that can't be measured (no parsed amount/unit/food) used to be silently DROPPED
-- from the shopping list - the worst failure mode a list can have. They now land as visible
-- "check the amount" lines, flagged so the UI can style them and regeneration can dedupe them.
ALTER TABLE shopping_list_items ADD COLUMN needs_check INTEGER NOT NULL DEFAULT 0;

ALTER TABLE cook_log ADD COLUMN additions TEXT;

ALTER TABLE cook_log_ingredients ADD COLUMN deviation TEXT
    CHECK (deviation IN ('omitted','substituted','adjusted'));
ALTER TABLE cook_log_ingredients ADD COLUMN used_text TEXT;    -- what was used instead / note

CREATE TABLE food_substitutes (
    id                 INTEGER PRIMARY KEY,
    food_id            INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    substitute_text    TEXT NOT NULL,                          -- what the family used instead
    substitute_food_id INTEGER REFERENCES foods(id) ON DELETE SET NULL,  -- resolved when known
    note               TEXT,
    source             TEXT NOT NULL DEFAULT 'cook' CHECK (source IN ('cook','manual')),
    times_used         INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (food_id, substitute_text)
);
CREATE INDEX idx_food_substitutes_food ON food_substitutes(food_id);
