# RecipeCollater — Data Model

> SQLite is the sole datastore (WAL mode). This schema is the contract for the whole app; the builder agent should implement it via small hand-written migration files (numbered SQL scripts applied in order, tracked in a `schema_migrations` table — no Alembic needed at this scale).
>
> Design principles, distilled from research on Mealie/Tandoor/Grocy:
> 1. **Never destroy source data.** Raw ingredient strings, raw scraped payloads, and source URLs are kept forever so recipes can be re-parsed and edits can be undone.
> 2. **Units have dimensions from day one.** Tandoor's biggest open regret (issue #1954) is text-label units with no semantics; scaling, pantry math, and shopping aggregation all depend on canonical grams/milliliters.
> 3. **Quarantine what imports create.** Import pollution of the foods/units vocabulary is Tandoor's top hygiene complaint (issue #1855). New foods proposed by ingestion stay `pending` until a human confirms.
> 4. **The pantry is approximate by design.** Graduated quantity granularity (exact count / full-half-low gauge / have-out binary) is what makes family pantry tracking survive — forcing exact quantities everywhere is the documented #1 cause of abandonment.

## 1. SQLite configuration

Per-connection pragmas (set in the connection factory):

```sql
PRAGMA journal_mode = WAL;        -- persists, but harmless to re-set
PRAGMA busy_timeout = 5000;
PRAGMA synchronous = NORMAL;
PRAGMA cache_size = -64000;       -- 64 MB
PRAGMA foreign_keys = ON;
```

Web process and worker process share the same DB file on local disk (never a network mount). One writer at a time is fine at family scale.

## 2. Identity & devices

```sql
CREATE TABLE users (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,        -- "Aaron", "Sam"
  is_admin      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per browser/device install. Token lives in an HttpOnly cookie
-- (rc_session, Max-Age 400 days, sliding renewal). Always persistent, never
-- a session cookie (WebKit bug 272325 punishes mixing strategies on iOS).
CREATE TABLE device_sessions (
  id            INTEGER PRIMARY KEY,
  token_hash    TEXT NOT NULL UNIQUE,        -- sha256(secrets.token_urlsafe(32))
  user_id       INTEGER NOT NULL REFERENCES users(id),
  device_name   TEXT NOT NULL,               -- "Sam's iPhone"
  issued_at     TEXT NOT NULL,
  last_seen_at  TEXT,
  expires_at    TEXT NOT NULL,
  revoked_at    TEXT
);

-- Long-lived Bearer tokens for the iOS Shortcut / scripts (Mealie pattern).
-- Browser identity and ingest identity are deliberately separate.
CREATE TABLE api_tokens (
  id            INTEGER PRIMARY KEY,
  token_hash    TEXT NOT NULL UNIQUE,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  label         TEXT NOT NULL,               -- "Aaron iPhone Shortcut"
  created_at    TEXT NOT NULL,
  last_used_at  TEXT,
  revoked_at    TEXT
);

-- Single-use magic links / pairing codes for onboarding a new device.
CREATE TABLE onboarding_tokens (
  id            INTEGER PRIMARY KEY,
  token_hash    TEXT NOT NULL UNIQUE,
  kind          TEXT NOT NULL CHECK (kind IN ('magic_link','pairing_code')),
  user_id       INTEGER NOT NULL REFERENCES users(id),
  device_name   TEXT,
  expires_at    TEXT NOT NULL,               -- ~15 minutes
  used_at       TEXT
);
```

## 3. Food & unit ontology (the scaling/pantry foundation)

Hand-rolled units table — **not** pint for app math (pint lacks pinch/dash/stick, treats cup as US-liquid only, and handles count units badly). ~30 rows, exact static factors.

```sql
CREATE TABLE units (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,        -- 'cup'
  plural_name   TEXT,
  abbreviation  TEXT,
  dimension     TEXT NOT NULL CHECK (dimension IN ('mass','volume','count','approx')),
  to_canonical  REAL                         -- g for mass, ml for volume; NULL for count/approx
);
CREATE TABLE unit_aliases (
  alias   TEXT PRIMARY KEY COLLATE NOCASE,
  unit_id INTEGER NOT NULL REFERENCES units(id)
);

CREATE TABLE foods (
  id               INTEGER PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- canonical singular: 'scallion'
  plural_name      TEXT,
  category         TEXT,                    -- 'produce', 'baking', 'spices' — doubles as shopping aisle
  fdc_id           INTEGER,                 -- USDA FoodData Central link (nutrition later)
  density_g_per_ml REAL,                    -- generic volume↔mass bridge, nullable
  status           TEXT NOT NULL DEFAULT 'confirmed'
                     CHECK (status IN ('confirmed','pending')),  -- quarantine for import-proposed foods
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE food_aliases (
  alias   TEXT PRIMARY KEY COLLATE NOCASE,  -- 'green onion' → scallion
  food_id INTEGER NOT NULL REFERENCES foods(id)
);

-- Per-food unit bridges: '1 cup flour = 120 g', '1 clove garlic = 5 g',
-- '1 can chickpeas = 400 g'. Same row shape as Tandoor open-data & Grocy.
CREATE TABLE food_unit_conversions (
  food_id  INTEGER NOT NULL REFERENCES foods(id),
  unit_id  INTEGER NOT NULL REFERENCES units(id),
  quantity REAL NOT NULL DEFAULT 1.0,
  grams    REAL NOT NULL,
  source   TEXT,                            -- cite where the number came from
  PRIMARY KEY (food_id, unit_id)
);
```

**Seed data** (ship in `seed/` as JSON, loaded by a bootstrap script):
- TandoorRecipes/open-tandoor-data: 391 FDC-linked foods, 163 sourced per-food conversions, 14 dimension-typed units (ODbL/DBCL — fine for private use).
- Mealie en-US foods list for vocabulary breadth (2,689 names; seed our own alias pairs — scallion=green onion, courgette=zucchini, cilantro=coriander — since Mealie ships almost none).
- USDA FDC `food_portion.csv` (public domain) to generate additional gram-weight conversions; FAO/INFOODS densities for liquids.
- Target ≈200 confirmed foods with conversions at launch; everything else arrives via ingestion as `pending`.

## 4. Recipes

```sql
CREATE TABLE recipes (
  id              INTEGER PRIMARY KEY,
  slug            TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'inbox'
                    CHECK (status IN ('inbox','cookbook','archived')),
  tier            TEXT CHECK (tier IN ('meal_prep','family','company')),  -- NULL until triaged
  tldr            TEXT,                     -- 1–3 sentence method summary, AI-drafted, user-editable
  description     TEXT,
  base_servings   REAL NOT NULL DEFAULT 4,
  servings_text   TEXT,                     -- 'one 9-inch pie' when servings aren't countable
  prep_minutes    INTEGER,                  -- as claimed by the source
  cook_minutes    INTEGER,
  total_minutes   INTEGER,
  our_minutes     INTEGER,                  -- what it ACTUALLY takes in our kitchen (user-set after cooking)
  source_type     TEXT NOT NULL CHECK (source_type IN ('youtube','web','manual','photo')),
  source_url      TEXT,
  source_name     TEXT,                     -- channel or site name
  video_id        TEXT,                     -- YouTube id, for embedded player / step deep-links
  image_path      TEXT,                     -- relative path under data/images/<recipe_id>/
  created_by      INTEGER REFERENCES users(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  promoted_at     TEXT,                     -- when moved inbox → cookbook
  raw_extraction  TEXT                      -- JSON: everything the ingester saw (description, transcript,
                                            -- JSON-LD, comments) — enables re-parse without re-fetch
);

CREATE TABLE recipe_steps (
  id             INTEGER PRIMARY KEY,
  recipe_id      INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  sort_order     INTEGER NOT NULL,
  section        TEXT,                      -- 'For the sauce' step grouping, nullable
  instruction    TEXT NOT NULL,             -- markdown
  minutes        INTEGER,                   -- optional per-step time
  video_seconds  INTEGER                    -- timestamp into source video (Preplo pattern: tap to seek)
);

CREATE TABLE recipe_ingredients (
  id             INTEGER PRIMARY KEY,
  recipe_id      INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  sort_order     INTEGER NOT NULL,
  section        TEXT,                      -- ingredient group: 'For the sauce' (never flatten groups)
  original_text  TEXT NOT NULL,             -- '2 cups flour, sifted' — always preserved
  quantity       REAL,                      -- NULL ⇒ unscalable / no amount
  unit_id        INTEGER REFERENCES units(id),   -- NULL for bare counts ('2 eggs')
  food_id        INTEGER REFERENCES foods(id),
  note           TEXT,                      -- 'finely chopped'
  scalable       INTEGER NOT NULL DEFAULT 1,     -- 0 for 'to taste', 'a splash', oil-for-frying
  step_id        INTEGER REFERENCES recipe_steps(id),  -- optional: which step uses it (stable ID, never positional)
  parse_confidence REAL                     -- from ingredient-parser-nlp; low values flagged in editor
);

CREATE TABLE tags (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE  -- cuisine, protein, course, equipment...
);
CREATE TABLE recipe_tags (
  recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (recipe_id, tag_id)
);

-- Cheap undo/history: JSON snapshot of the full recipe before each user edit.
CREATE TABLE recipe_revisions (
  id         INTEGER PRIMARY KEY,
  recipe_id  INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  saved_at   TEXT NOT NULL DEFAULT (datetime('now')),
  saved_by   INTEGER REFERENCES users(id),
  payload    TEXT NOT NULL                  -- full recipe JSON
);
```

**Scaling math** (server-side, deterministic): for scalable rows, `display_qty = quantity × target/base_servings`, rendered as kitchen fractions from a fixed denominator set (2, 3, 4, 8) — `1.5 → 1½`. Non-scalable rows pass through with their original text. Scale UI offers presets 2/4/6/8 plus free-form (including big-event numbers); "show original amounts" toggle included from the start (Tandoor's #1325). A "save as scaled copy" action clones the recipe at the new base.

## 5. Cook log & attribution (what makes it *our* cookbook)

```sql
CREATE TABLE cook_log (
  id             INTEGER PRIMARY KEY,
  recipe_id      INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  user_id        INTEGER REFERENCES users(id),
  cooked_at      TEXT NOT NULL DEFAULT (datetime('now')),
  servings_made  REAL,
  actual_minutes INTEGER,                   -- feeds recipes.our_minutes suggestion
  rating         INTEGER CHECK (rating BETWEEN 1 AND 5),
  notes          TEXT                       -- 'doubled the chilies, kids loved it'
);

-- Per-ingredient 'what I actually used' for a given cook.
CREATE TABLE cook_log_ingredients (
  cook_log_id   INTEGER NOT NULL REFERENCES cook_log(id) ON DELETE CASCADE,
  ingredient_id INTEGER NOT NULL REFERENCES recipe_ingredients(id),
  used_quantity REAL,
  used_unit_id  INTEGER REFERENCES units(id),
  PRIMARY KEY (cook_log_id, ingredient_id)
);
```

The after-cook flow (rate → correct time → note actual quantities → optionally decrement pantry) is the promotion gate from inbox to cookbook and the primary pantry-consumption event.

## 6. Pantry

```sql
CREATE TABLE locations (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,          -- 'Kitchen Cupboards', 'Downstairs Freezer'
  is_freezer INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE pantry_items (
  id               INTEGER PRIMARY KEY,
  food_id          INTEGER REFERENCES foods(id),   -- nullable: free-text items allowed
  display_name     TEXT NOT NULL,                  -- what the family calls it
  location_id      INTEGER NOT NULL REFERENCES locations(id),
  quantity_mode    TEXT NOT NULL DEFAULT 'gauge'
                     CHECK (quantity_mode IN ('exact','gauge','binary')),
  quantity         REAL,                    -- exact mode: number of units
  unit_id          INTEGER REFERENCES units(id),
  gauge            TEXT CHECK (gauge IN ('full','half','low','out')),  -- gauge mode
  have             INTEGER,                 -- binary mode: 1/0
  is_staple        INTEGER NOT NULL DEFAULT 0,    -- always want on hand
  min_quantity     REAL,                    -- exact-mode staples: below this → shopping list
  expires_on       TEXT,                    -- optional, mostly for freezer/fridge
  updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_by       INTEGER REFERENCES users(id)
);
```

Rules:
- Adding an item = autocomplete on `foods` + one tap on a location. Locations/foods/units creatable inline (Grocy's master-data detours are its #1 UI complaint).
- `out` gauge or `quantity < min_quantity` on a staple ⇒ auto-candidate for the shopping list.
- "Stock-take mode": walk one location, every item shows big tap targets (full/half/low/out or +/-), two taps max per item.
- Pantry-aware matching: recipe ⇄ pantry via `food_id`; per-recipe-ingredient opt-out flag isn't needed because `approx`/`scalable=0` items (spices, oil) are excluded from "can I make this" scoring by default.

## 7. Shopping list

```sql
CREATE TABLE shopping_list_items (
  id           INTEGER PRIMARY KEY,
  food_id      INTEGER REFERENCES foods(id),
  display_text TEXT NOT NULL,               -- '5 onions (Pasta ×2, Curry)'
  quantity     REAL,
  unit_id      INTEGER REFERENCES units(id),
  category     TEXT,                        -- aisle grouping, from foods.category, user-overridable
  sources      TEXT,                        -- JSON: [{recipe_id, title, qty}] or 'staple' or 'manual'
  checked      INTEGER NOT NULL DEFAULT 0,
  checked_at   TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Aggregation math (Grocy has open bugs here — get it right): merge per `(food_id, dimension)`; convert within a dimension to canonical g/ml and sum; bridge count↔mass only via `food_unit_conversions`; emit one line per dimension when no bridge exists; `approx` units and NULL quantities never merge (they append as notes). Checking off an item offers "add to pantry → location?" as a bulk step after shopping.

## 8. Meal planning

```sql
CREATE TABLE meal_plan_entries (
  id         INTEGER PRIMARY KEY,
  plan_date  TEXT NOT NULL,                 -- ISO date
  slot       TEXT NOT NULL DEFAULT 'dinner',-- free text, not a fixed enum (Mealie complaint)
  recipe_id  INTEGER REFERENCES recipes(id),
  note       TEXT,                          -- 'leftovers', 'pizza night' — recipe-less entries allowed
  servings   REAL,                          -- per-entry servings (guests!) drives shopping math
  created_by INTEGER REFERENCES users(id)
);

-- Reusable week templates ('Menus', Plan to Eat's beloved feature).
CREATE TABLE saved_menus (
  id      INTEGER PRIMARY KEY,
  name    TEXT NOT NULL,                    -- 'Standard week', 'Christmas menu'
  payload TEXT NOT NULL                     -- JSON of entries (day offsets + recipe ids + servings)
);
```

## 9. Ingestion jobs

```sql
CREATE TABLE ingest_jobs (
  id            INTEGER PRIMARY KEY,
  url           TEXT,
  supplied_html INTEGER NOT NULL DEFAULT 0, -- client captured the page HTML for us
  source_type   TEXT,                       -- detected: youtube | web | photo
  status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','fetching','extracting','done','failed')),
  error         TEXT,
  recipe_id     INTEGER REFERENCES recipes(id),
  submitted_by  INTEGER REFERENCES users(id),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at   TEXT
);
```

Duplicate detection: on submit, warn if `source_url` (normalized: strip tracking params, canonicalize youtu.be) already exists.

## 10. AI bookkeeping

```sql
CREATE TABLE ai_conversations (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER REFERENCES users(id),
  title      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE ai_messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content         TEXT NOT NULL,            -- assistant messages may embed proposal JSON blocks
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Every API call logged for cost visibility + a monthly cap guardrail (Tandoor's AI guardrail pattern).
CREATE TABLE ai_usage_log (
  id            INTEGER PRIMARY KEY,
  purpose       TEXT NOT NULL,              -- 'extract', 'tldr', 'chat', 'meal_plan', 'repair_ingredients'
  model         TEXT NOT NULL,
  input_tokens  INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER,
  est_cost_usd  REAL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## 11. Search

- **FTS5** external-content table over `recipes` (title, tldr, description, ingredient original_texts, tags, cook-log notes) with INSERT/UPDATE/DELETE sync triggers; `rebuild` after bulk imports. This is v1 search — instant and free.
- **sqlite-vec** (`vec0` virtual table, 384-dim) added in a later phase for semantic search; embed `title + tldr + ingredient names + tags` per recipe via fastembed (ONNX all-MiniLM-L6-v2, no PyTorch) in a background job; store embedding-model name alongside vectors so a model swap triggers clean re-embed. Pin the pre-1.0 sqlite-vec version and wrap it in a small DAO.

## 12. Files on disk

```
data/
  recipecollater.db          # + -wal/-shm
  images/<recipe_id>/        # original + 2048/1024/300px WEBP (Mealie's PillowMinifier pattern)
  backups/                   # nightly VACUUM INTO snapshots, 14-day rotation
seed/                        # foods/units/conversions JSON
```

Every recipe is exportable as JSON and printable markdown (`GET /recipes/<slug>.json|.md`) — "my recipes are database-locked" is a standing complaint against Mealie; avoiding it costs almost nothing.
