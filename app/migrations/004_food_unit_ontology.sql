-- Migration 004 - Phase 1: food & unit ontology (the exact-scaling foundation).
--
-- Hand-rolled units (CONVENTIONS 1 / docs/03 section 3). Canonical storage is integer
-- micro-units: milligrams (mass), microlitres (volume), milli-each (count), so unit
-- factors stay effectively exact and recipe/pantry/shopping math never touches binary
-- float. Approximate units ('pinch', 'to taste') carry no factor. Foods carry aliases
-- (green onion -> scallion) and per-food unit bridges ('1 cup flour = 120 g').
--
-- Imperial factors are rounded to the nearest canonical micro-unit; the sub-milligram /
-- sub-microlitre error is far below any cooking tolerance. Metric factors are exact.
--
-- Reference units are seeded idempotently at startup (app.services.units.seed_core_units),
-- not here, so the set can grow without a schema migration. Recipe/pantry columns land with
-- their own phase. The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE units (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    plural_name             TEXT,
    abbreviation            TEXT,
    dimension               TEXT NOT NULL CHECK (dimension IN ('mass','volume','count','approx')),
    to_canonical_microunits INTEGER,
    CHECK ((dimension = 'approx' AND to_canonical_microunits IS NULL) OR
           (dimension <> 'approx' AND to_canonical_microunits > 0))
);

CREATE TABLE unit_aliases (
    alias   TEXT PRIMARY KEY COLLATE NOCASE,
    unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE
);
CREATE INDEX idx_unit_aliases_unit ON unit_aliases(unit_id);

CREATE TABLE foods (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    plural_name       TEXT,
    category          TEXT,                    -- doubles as shopping aisle
    fdc_id            INTEGER,                 -- USDA FoodData Central link (nutrition later)
    density_mg_per_ml INTEGER,                 -- generic volume<->mass bridge, nullable
    status            TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed','pending')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE food_aliases (
    alias   TEXT PRIMARY KEY COLLATE NOCASE,
    food_id INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE
);
CREATE INDEX idx_food_aliases_food ON food_aliases(food_id);

-- General per-food bridges: '1 cup flour = 120 g', '1 can chickpeas = 400 g',
-- '1 bunch scallions = 6 each'. Supports count<->count as well as count/volume<->mass.
CREATE TABLE food_unit_conversions (
    id                 INTEGER PRIMARY KEY,
    food_id            INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    from_unit_id       INTEGER NOT NULL REFERENCES units(id),
    from_quantity_text TEXT NOT NULL,
    to_unit_id         INTEGER NOT NULL REFERENCES units(id),
    to_quantity_text   TEXT NOT NULL,
    source             TEXT,
    UNIQUE (food_id, from_unit_id, to_unit_id)
);
CREATE INDEX idx_food_conversions_food ON food_unit_conversions(food_id);
