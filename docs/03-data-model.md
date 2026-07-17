# RecipeCollater — Data Model

> SQLite is the sole datastore (WAL mode). This schema is the contract for the whole app; the builder agent should implement it via small hand-written migration files (numbered SQL scripts applied in order, tracked in a `schema_migrations` table — no Alembic needed at this scale).
>
> Design principles, distilled from research on Mealie/Tandoor/Grocy:
> 1. **Never destroy source data.** Raw ingredient strings, raw scraped payloads, and source URLs are kept forever so recipes can be re-parsed and edits can be undone.
> 2. **Units have dimensions and exact math from day one.** Tandoor's biggest open regret (issue #1954) is text-label units with no semantics. Scaling, pantry math, and shopping aggregation use Python `Decimal` plus canonical integers—never binary floating-point arithmetic.
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
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  -- Optional per-user PIN sign-in (migration 003; owner request over the original
  -- passwordless design). Never plaintext: pin_hash is a salted scrypt string. A short
  -- numeric PIN is protected by per-user lockout; a correct PIN only mints a device
  -- session (rc_session stays the one identity authority).
  pin_hash            TEXT,                  -- NULL = no PIN set
  pin_set_at          TEXT,
  pin_failed_attempts INTEGER NOT NULL DEFAULT 0,
  pin_locked_until    TEXT                   -- NULL = not locked out
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
  revoked_at    TEXT,
  renewed_at    TEXT NOT NULL                  -- throttles sliding renewal independently of issue time
);

-- Long-lived Bearer tokens for the iOS Shortcut / scripts (Mealie pattern).
-- Browser identity and ingest identity are deliberately separate.
-- `scope` (added during Phase 0 implementation to enforce requirements §2 /
-- architecture §5) restricts an ingest token to submitting ingestion jobs and nothing
-- else. Only 'ingest' exists today; future scopes extend the CHECK deliberately.
CREATE TABLE api_tokens (
  id            INTEGER PRIMARY KEY,
  token_hash    TEXT NOT NULL UNIQUE,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  label         TEXT NOT NULL,               -- "Aaron iPhone Shortcut"
  scope         TEXT NOT NULL DEFAULT 'ingest' CHECK (scope IN ('ingest')),
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

Hand-rolled units table—not pint for app math. Canonical storage uses integers: milligrams for mass, microlitres for volume, and milli-each for count. A unit factor therefore remains exact (`1 kg = 1,000,000 mg`, `1 dozen = 12,000 milli-each`). Approximate units have no factor.

```sql
CREATE TABLE units (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE,        -- 'cup'
  plural_name   TEXT,
  abbreviation  TEXT,
  dimension     TEXT NOT NULL CHECK (dimension IN ('mass','volume','count','approx')),
  to_canonical_microunits INTEGER,            -- mg / µL / milli-each per unit; NULL for approx
  CHECK ((dimension = 'approx' AND to_canonical_microunits IS NULL) OR
         (dimension <> 'approx' AND to_canonical_microunits > 0))
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
  density_mg_per_ml INTEGER,               -- generic volume↔mass bridge, nullable
  status           TEXT NOT NULL DEFAULT 'confirmed'
                     CHECK (status IN ('confirmed','pending')),  -- quarantine for import-proposed foods
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE food_aliases (
  alias   TEXT PRIMARY KEY COLLATE NOCASE,  -- 'green onion' → scallion
  food_id INTEGER NOT NULL REFERENCES foods(id)
);

-- General per-food bridges: '1 cup flour = 120 g', '1 can chickpeas = 400 g',
-- '1 bunch scallions = 6 each'. Supports count↔count as well as count/volume↔mass.
CREATE TABLE food_unit_conversions (
  id                       INTEGER PRIMARY KEY,
  food_id                  INTEGER NOT NULL REFERENCES foods(id),
  from_unit_id             INTEGER NOT NULL REFERENCES units(id),
  from_quantity_text       TEXT NOT NULL,
  to_unit_id               INTEGER NOT NULL REFERENCES units(id),
  to_quantity_text         TEXT NOT NULL,
  source                   TEXT,
  UNIQUE (food_id, from_unit_id, to_unit_id)
);
```

All quantities received from forms, AI output, or imported JSON are strings at the boundary and parsed with `Decimal`. Recipe-facing values remain exact decimal strings; arithmetic converts them to canonical integers with an explicit rounding policy. SQL never performs recipe or stock arithmetic with `REAL`.

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
  base_servings   TEXT NOT NULL DEFAULT '4', -- exact decimal string
  servings_text   TEXT,                     -- 'one 9-inch pie' when servings aren't countable
  prep_minutes    INTEGER,                  -- as claimed by the source
  cook_minutes    INTEGER,
  total_minutes   INTEGER,
  active_minutes  INTEGER,                  -- hands-on effort
  elapsed_minutes INTEGER,                  -- wall-clock incl. resting/braising
  our_minutes     INTEGER,                  -- what it ACTUALLY takes in our kitchen (user-set after cooking)
  our_active_minutes INTEGER,
  max_batch_servings TEXT,                  -- practical capacity for one batch, nullable
  make_ahead_minutes INTEGER,
  hold_minutes    INTEGER,
  storage_notes   TEXT,
  deduction_mode  TEXT NOT NULL DEFAULT 'review'
                    CHECK (deduction_mode IN ('review','auto')),
  source_type     TEXT NOT NULL CHECK (source_type IN ('youtube','web','manual','photo')),
  source_url      TEXT,
  normalized_source_url TEXT,
  source_name     TEXT,                     -- channel or site name
  video_id        TEXT,                     -- YouTube id, for embedded player / step deep-links
  image_path      TEXT,                     -- relative path under data/images/<recipe_id>/
  created_by      INTEGER REFERENCES users(id),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  promoted_at     TEXT,                     -- when moved inbox → cookbook
  current_extraction_run_id INTEGER REFERENCES extraction_runs(id)
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
  quantity_text  TEXT,                      -- exact decimal; NULL ⇒ no amount
  unit_id        INTEGER REFERENCES units(id),   -- quantified bare counts normalize to the canonical 'each' unit
  food_id        INTEGER REFERENCES foods(id),
  note           TEXT,                      -- 'finely chopped'
  scaling_mode   TEXT NOT NULL DEFAULT 'linear'
                   CHECK (scaling_mode IN ('linear','fixed','to_taste','round_to_package')),
  package_quantity_text TEXT,               -- for round_to_package, e.g. '1' package
  package_unit_id INTEGER REFERENCES units(id),
  deduct_from_pantry INTEGER NOT NULL DEFAULT 1, -- auto-set 0 for approximate/to-taste/unresolved lines
  pantry_item_hint INTEGER REFERENCES pantry_items(id), -- remembered mapping for ambiguous foods ('chicken' → 'chicken thighs')
  deduction_trusted_at TEXT,
  deduction_trust_signature TEXT,           -- hash of food/unit/qty/scaling/mapping; edits invalidate trust
  parse_confidence REAL,                    -- confidence score only; never quantity arithmetic
  CHECK (quantity_text IS NULL OR unit_id IS NOT NULL)
);

-- Ingredients may be used in multiple steps and may be divided between them.
CREATE TABLE recipe_step_ingredients (
  step_id        INTEGER NOT NULL REFERENCES recipe_steps(id) ON DELETE CASCADE,
  ingredient_id  INTEGER NOT NULL REFERENCES recipe_ingredients(id) ON DELETE CASCADE,
  quantity_text  TEXT,
  unit_id        INTEGER REFERENCES units(id),
  note           TEXT,
  PRIMARY KEY (step_id, ingredient_id)
);

CREATE TABLE equipment (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL UNIQUE COLLATE NOCASE,
  household_count INTEGER NOT NULL DEFAULT 1,
  notes         TEXT
);

CREATE TABLE recipe_equipment (
  id            INTEGER PRIMARY KEY,
  recipe_id     INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  equipment_id  INTEGER NOT NULL REFERENCES equipment(id),
  quantity      INTEGER NOT NULL DEFAULT 1,
  start_minute  INTEGER,
  duration_minutes INTEGER,
  temperature_c INTEGER,
  notes         TEXT
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

**Scaling math** is server-authoritative and deterministic. Values are `Decimal`; display fractions use a fixed denominator set (2, 3, 4, 8). `linear` multiplies by target/base servings, `fixed` stays unchanged, `to_taste` displays its original wording, and `round_to_package` rounds upward to a declared package increment with an explicit warning. The scaled view is ephemeral; meal-plan entries and cook logs store servings, so v1 does not create near-duplicate scaled recipe copies.

The SQL above is the target model, not migration 001. Phase 1 creates recipe tables without extraction pointers or pantry mapping/trust columns; phase 2 and phase 4 add those columns alongside the tables they reference. This preserves the roadmap's incremental-migration rule.

## 5. Cook log & attribution (what makes it *our* cookbook)

```sql
CREATE TABLE cook_log (
  id             INTEGER PRIMARY KEY,
  recipe_id      INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
  user_id        INTEGER REFERENCES users(id),
  cooked_at      TEXT NOT NULL DEFAULT (datetime('now')),
  servings_made  TEXT,
  actual_minutes INTEGER,                   -- feeds recipes.our_minutes suggestion
  actual_active_minutes INTEGER,
  actual_elapsed_minutes INTEGER,
  rating         INTEGER CHECK (rating BETWEEN 1 AND 5),
  notes          TEXT                       -- 'doubled the chilies, kids loved it'
);

-- Per-ingredient 'what I actually used' for a given cook.
CREATE TABLE cook_log_ingredients (
  id            INTEGER PRIMARY KEY,
  cook_log_id   INTEGER NOT NULL REFERENCES cook_log(id) ON DELETE CASCADE,
  ingredient_id INTEGER REFERENCES recipe_ingredients(id) ON DELETE SET NULL,
  original_text TEXT NOT NULL,             -- immutable cook-time snapshot
  food_id       INTEGER REFERENCES foods(id),
  planned_quantity_text TEXT,
  used_quantity_text TEXT,
  used_unit_id  INTEGER REFERENCES units(id)
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
  quantity_text    TEXT,                    -- exact user-facing decimal
  unit_id          INTEGER REFERENCES units(id),
  canonical_quantity INTEGER,               -- mg / µL / milli-each when resolvable
  gauge            TEXT CHECK (gauge IN ('full','half','low','out')),  -- gauge mode
  have             INTEGER,                 -- binary mode: 1/0
  is_staple        INTEGER NOT NULL DEFAULT 0,    -- always want on hand
  min_quantity_text TEXT,
  canonical_min_quantity INTEGER,
  expires_on       TEXT,                    -- optional, mostly for freezer/fridge
  step_down_on_cook INTEGER NOT NULL DEFAULT 0,  -- gauge/binary: opt-in to auto-step-down when cooked
  updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
  updated_by       INTEGER REFERENCES users(id)
);

-- Append-only history behind pantry_items: every change with a reason and source.
-- Makes cook-deduction Undo atomic and answers "why did the flour drop?".
-- Lightweight log, NOT Grocy's mandatory double-entry ledger — pantry_items holds current state.
CREATE TABLE pantry_adjustments (
  id            INTEGER PRIMARY KEY,
  pantry_item_id INTEGER REFERENCES pantry_items(id) ON DELETE SET NULL,
  food_id       INTEGER REFERENCES foods(id),      -- kept even if the item row is later deleted
  delta_quantity_text TEXT,               -- signed exact decimal in item unit
  canonical_delta INTEGER,                -- signed mg / µL / milli-each
  from_gauge    TEXT, to_gauge TEXT,
  from_have     INTEGER, to_have INTEGER, -- binary transitions, for undo
  reason        TEXT NOT NULL CHECK (reason IN
                  ('cook','manual_remove','spoiled','restock','stock_take','correction')),
  source        TEXT,                     -- 'cook' rows carry the batch context below
  cook_log_id   INTEGER REFERENCES cook_log(id) ON DELETE SET NULL, -- history survives cook-log cleanup
  batch_id      TEXT,                     -- groups one cook's deductions for one-tap Undo
  user_id       INTEGER REFERENCES users(id),
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Rules:
- Adding an item = autocomplete on `foods` + one tap on a location. Locations/foods/units creatable inline (Grocy's master-data detours are its #1 UI complaint).
- `out` gauge or canonical quantity below canonical minimum on a staple ⇒ auto-candidate for the shopping list.
- "Stock-take mode": walk one location, every item shows big tap targets (full/half/low/out or +/-), two taps max per item.
- **Remove / spoilage**: a quick action on any item sets exact→0 (or deletes) / gauge→`out`, writing a `pantry_adjustments` row with reason `manual_remove` or `spoiled`. Only `spoiled` is surfaced to the AI (waste patterns); the rest are silent.
- **Cook-through deduction** (`06-…` §2.1): the first cook writes a proposed batch and requires review. Only confirmed food/item mappings with compatible dimensions and unambiguous quantities qualify. Confirmed decisions are remembered per recipe; users may enable later auto-apply for that recipe. Every applied batch is reversible by `batch_id`.
- Pantry-aware matching: recipe ⇄ pantry via `food_id`; `to_taste`, unresolved package rounding, and approximate/unquantified items are excluded from "can I make this" scoring by default.

## 7. Shopping list

```sql
CREATE TABLE shopping_lists (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','completed','archived')),
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE shopping_list_items (
  id           INTEGER PRIMARY KEY,
  list_id      INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
  food_id      INTEGER REFERENCES foods(id),
  display_text TEXT NOT NULL,
  quantity_text TEXT,
  unit_id      INTEGER REFERENCES units(id),
  canonical_quantity INTEGER,
  category     TEXT,                        -- aisle grouping, from foods.category, user-overridable
  checked      INTEGER NOT NULL DEFAULT 0,
  checked_at   TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE shopping_item_sources (
  id             INTEGER PRIMARY KEY,
  item_id        INTEGER NOT NULL REFERENCES shopping_list_items(id) ON DELETE CASCADE,
  source_type    TEXT NOT NULL CHECK (source_type IN ('meal_plan','recipe','staple','manual')),
  recipe_id      INTEGER REFERENCES recipes(id),
  meal_plan_entry_id INTEGER REFERENCES meal_plan_entries(id),
  quantity_text  TEXT,
  unit_id        INTEGER REFERENCES units(id),
  label          TEXT
);
```

Aggregation math is a pure service using `Decimal` and canonical integers. Merge per `(food_id, dimension)`; bridge dimensions only through an explicit food conversion; emit separate lines when no bridge exists; approximate/unquantified items remain notes. Generation is idempotent and preserves manual items/check state instead of replacing the active list wholesale.

## 8. Meal planning

```sql
CREATE TABLE meal_plan_entries (
  id         INTEGER PRIMARY KEY,
  plan_date  TEXT NOT NULL,                 -- ISO date
  slot       TEXT NOT NULL DEFAULT 'dinner',-- free text, not a fixed enum (Mealie complaint)
  recipe_id  INTEGER REFERENCES recipes(id),
  note       TEXT,                          -- 'leftovers', 'pizza night' — recipe-less entries allowed
  servings   TEXT,                          -- exact decimal; drives shopping math
  created_by INTEGER REFERENCES users(id)
);

-- Reusable week templates ('Menus', Plan to Eat's beloved feature).
CREATE TABLE saved_menus (
  id      INTEGER PRIMARY KEY,
  name    TEXT NOT NULL,                    -- 'Standard week', 'Christmas menu'
  payload TEXT NOT NULL                     -- JSON of entries (day offsets + recipe ids + servings)
);

CREATE TABLE household_preferences (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  allergies_json TEXT NOT NULL DEFAULT '[]',       -- hard constraints, never optional to planner
  exclusions_json TEXT NOT NULL DEFAULT '[]',
  dislikes_json TEXT NOT NULL DEFAULT '[]',
  dietary_preferences_json TEXT NOT NULL DEFAULT '[]',
  weekday_time_limits_json TEXT NOT NULL DEFAULT '{}',
  planning_preferences_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## 9. Ingestion jobs

```sql
CREATE TABLE ingest_jobs (
  id            INTEGER PRIMARY KEY,
  url           TEXT,
  normalized_url TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  source_type   TEXT,                       -- detected: youtube | web | photo
  status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued','fetching','extracting','normalizing','done','failed')),
  stage         TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  started_at    TEXT,
  heartbeat_at  TEXT,
  error_code    TEXT,
  error_detail  TEXT,
  recipe_id     INTEGER REFERENCES recipes(id),
  submitted_by  INTEGER REFERENCES users(id),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at   TEXT
);

CREATE TABLE ingestion_artifacts (
  id            INTEGER PRIMARY KEY,
  job_id        INTEGER NOT NULL REFERENCES ingest_jobs(id) ON DELETE RESTRICT,
  kind          TEXT NOT NULL,              -- supplied_html, fetched_html, jsonld, transcript, description, comments
  storage_path  TEXT NOT NULL,              -- immutable compressed file beneath data/artifacts/
  sha256        TEXT NOT NULL,
  size_bytes    INTEGER NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (job_id, kind, sha256)
);

CREATE TABLE extraction_runs (
  id              INTEGER PRIMARY KEY,
  job_id          INTEGER REFERENCES ingest_jobs(id),
  recipe_id       INTEGER REFERENCES recipes(id),
  provider        TEXT,
  model           TEXT,
  extractor_version TEXT NOT NULL,
  prompt_version  TEXT,
  schema_version  TEXT NOT NULL,
  confidence      TEXT CHECK (confidence IN ('high','medium','thin')),
  result_json     TEXT NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('draft','accepted','rejected')),
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  accepted_at     TEXT,
  accepted_by     INTEGER REFERENCES users(id)
);
```

Duplicate detection uses `normalized_url` and the unique idempotency key. An explicit duplicate override generates a new key while retaining the relationship to the existing recipe. Worker stages are restartable; recipe creation and job completion are transactionally/idempotently linked.

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
  content         TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE ai_proposals (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
  message_id      INTEGER REFERENCES ai_messages(id) ON DELETE SET NULL,
  proposal_type   TEXT NOT NULL,            -- meal_plan, shopping_list, pantry_update, event_menu
  payload         TEXT NOT NULL,            -- validated versioned JSON
  schema_version  TEXT NOT NULL,
  provider        TEXT NOT NULL,
  model           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','accepted','dismissed','superseded')),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  decided_at      TEXT,
  decided_by      INTEGER REFERENCES users(id)
);
-- Every API call logged for cost visibility + a monthly cap guardrail (Tandoor's AI guardrail pattern).
CREATE TABLE ai_usage_log (
  id            INTEGER PRIMARY KEY,
  provider      TEXT NOT NULL,              -- 'anthropic' | 'openai'
  purpose       TEXT NOT NULL,              -- 'extract','tldr','chat','meal_plan','repair','embed'
  model         TEXT NOT NULL,
  input_tokens  INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER,
  est_cost_micro_usd INTEGER,
  request_id    TEXT,
  prompt_version TEXT,
  stored_remotely INTEGER NOT NULL DEFAULT 0, -- expected 0; OpenAI calls use store:false
  error_code    TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## 10a. Settings (user-facing app config)

```sql
-- Simple key/value store for user-facing toggles surfaced on the Settings page.
-- Secrets (ANTHROPIC_API_KEY, OPENAI_API_KEY) live in the root-owned env file, NOT here.
CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,                 -- JSON-encoded
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Seeded keys include: `pantry.default_deduct_mode` (`review`, with per-recipe trust enabling auto), `pantry.gauge_step_down_default` (false), `ai.routing`, `ai.spend_cap_usd` per provider, `ai.enabled_features`, and `shopping.aisle_order`. Keys remain in the env file.

## 11. Search

- **FTS5** contentless table keyed by `recipe_id`, containing a denormalized search document (title, TLDR, description, ingredient originals, tags, selected cook-log notes). The application updates it in the same transaction/event path as relevant edits and can deterministically rebuild it after imports or recovery. A conventional external-content table is not used because the indexed text spans several relational tables.
- Semantic search is post-v1 and ships only after logged FTS5 misses justify it. Its contract is fixed at **384 dimensions**: local MiniLM emits 384; OpenAI embeddings request `dimensions=384`. Store provider/model/dimension with each vector and rebuild the table on any incompatible change. Pin pre-1.0 sqlite-vec behind a small DAO.

## 12. Files on disk

```
data/
  recipecollater.db          # + -wal/-shm
  images/<recipe_id>/        # original + derived WEBP sizes
  artifacts/<job_id>/        # compressed immutable HTML/transcripts/extraction inputs
  backups/                   # staged DB + images + artifacts + checksum manifests
seed/                        # foods/units/conversions JSON
```

Every recipe is exportable as JSON and printable markdown (`GET /recipes/<slug>.json|.md`). Backup manifests cover the database, uploaded originals, images, and artifacts; a SQLite-only snapshot is not a complete backup.
