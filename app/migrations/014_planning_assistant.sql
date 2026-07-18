-- Migration 014 - Phase 5: meal planning, household preferences, saved menus, and the
-- proposal-pattern assistant (AI proposes, deterministic services apply - docs/05 section 3).
--
-- The week board is deterministic and needs no AI: entries are a recipe OR a free-text note
-- ("leftovers", "pizza out"), each carrying its own servings so the plan->shopping math scales
-- per entry. Household preferences are structured: allergy/exclude are HARD constraints applied
-- by the deterministic filter BEFORE any model call; the rest are soft. The assistant persists
-- a conversation and any proposals it makes as PENDING records; acceptance re-validates current
-- data and applies one idempotent transaction, so the model never silently mutates the plan,
-- pantry, or shopping list.
--
-- shopping_item_sources.meal_plan_entry_id (a bare INTEGER since migration 011, FK deferred to
-- this phase) is now the parent for plan-generated shopping lines; SQLite can't add a FK to an
-- existing column, so it stays a plain INTEGER and the service enforces the reference.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

-- --------------------------------------------------------------------------------------
-- Week board
-- --------------------------------------------------------------------------------------

CREATE TABLE meal_plan_entries (
    id            INTEGER PRIMARY KEY,
    plan_date     TEXT NOT NULL,                 -- ISO YYYY-MM-DD
    slot          TEXT NOT NULL DEFAULT 'dinner',-- free text (no meal-type enum - Mealie complaint)
    sort_order    INTEGER NOT NULL DEFAULT 0,
    entry_type    TEXT NOT NULL DEFAULT 'recipe' CHECK (entry_type IN ('recipe','note')),
    recipe_id     INTEGER REFERENCES recipes(id) ON DELETE CASCADE,  -- recipe entries
    note_text     TEXT,                          -- note entries ('leftovers', 'pizza out')
    servings_text TEXT,                          -- per-entry servings for the shopping math
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_meal_plan_entries_date ON meal_plan_entries(plan_date);

-- --------------------------------------------------------------------------------------
-- Household preferences (the assistant's guardrails; also editable in settings)
-- --------------------------------------------------------------------------------------

-- Multi-valued lists. allergy + exclude are HARD (filter drops any recipe that hits them);
-- dislike/diet/equipment/cuisine_love are SOFT (ranking hints only).
CREATE TABLE household_preferences (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN
                 ('allergy','exclude','dislike','diet','equipment','cuisine_love')),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, value)
);

-- Scalar planning settings (weekday/weekend time budgets, default servings, tier-mix note).
CREATE TABLE planning_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- --------------------------------------------------------------------------------------
-- Saved menus (reusable week templates - Plan to Eat's most-loved feature)
-- --------------------------------------------------------------------------------------

CREATE TABLE saved_menus (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE saved_menu_entries (
    id            INTEGER PRIMARY KEY,
    menu_id       INTEGER NOT NULL REFERENCES saved_menus(id) ON DELETE CASCADE,
    day_index     INTEGER NOT NULL,              -- 0=Mon .. 6=Sun, relative to whatever week applied
    slot          TEXT NOT NULL DEFAULT 'dinner',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    entry_type    TEXT NOT NULL DEFAULT 'recipe' CHECK (entry_type IN ('recipe','note')),
    -- SET NULL (not CASCADE): a saved menu is a reusable template that must survive a recipe
    -- deletion - apply_menu_to_week then skips the now-orphaned recipe row gracefully.
    recipe_id     INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    note_text     TEXT,
    servings_text TEXT
);
CREATE INDEX idx_saved_menu_entries_menu ON saved_menu_entries(menu_id);

-- --------------------------------------------------------------------------------------
-- Assistant: conversations, messages, and versioned proposals
-- --------------------------------------------------------------------------------------

CREATE TABLE ai_conversations (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    title      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE ai_messages (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ai_messages_conversation ON ai_messages(conversation_id);

-- A proposal is a pending, versioned artifact the assistant produced; acceptance is a separate
-- idempotent request that re-validates and applies via deterministic services (docs/05 §3).
CREATE TABLE ai_proposals (
    id              INTEGER PRIMARY KEY,
    conversation_id INTEGER REFERENCES ai_conversations(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES ai_messages(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('meal_plan','pantry_update')),
    status          TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','accepted','dismissed','stale')),
    payload         TEXT NOT NULL,               -- validated JSON: entries / changes
    idempotency_key TEXT NOT NULL UNIQUE,        -- one acceptance ever, even on a double-click
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at     TEXT,
    resolved_by     INTEGER REFERENCES users(id)
);
CREATE INDEX idx_ai_proposals_conversation ON ai_proposals(conversation_id);
