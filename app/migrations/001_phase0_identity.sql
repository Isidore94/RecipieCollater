-- Migration 001 — Phase 0 identity & devices.
--
-- Contains ONLY Phase-0 tables (docs/08-roadmap.md: "Migration 001 contains only
-- phase-0 tables; later tables arrive with their owning phase"). Recipe, pantry,
-- shopping, ingestion, meal-plan, AI, and settings tables are intentionally absent
-- and land with the phases that own them.
--
-- The runner owns transactions: this file must not contain BEGIN/COMMIT/VACUUM.

CREATE TABLE users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per browser/device install. The opaque token lives in a single persistent
-- HttpOnly cookie (rc_session); only its SHA-256 hash is stored here. Sliding renewal
-- keeps the row alive; revoked_at makes lost-device revocation a one-row update.
CREATE TABLE device_sessions (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name  TEXT NOT NULL,
    issued_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE INDEX idx_device_sessions_user ON device_sessions(user_id);

-- Long-lived Bearer tokens for the Apple Shortcut / scripts. Deliberately separate
-- from browser identity and SCOPED: an ingest token may submit ingestion jobs and
-- nothing else (CONVENTIONS §6). Phase 0 defines the boundary and enforces it; the
-- ingest endpoint itself arrives in Phase 2.
--
-- NOTE (implementation-driven spec refinement): docs/03-data-model.md §2 originally
-- omitted `scope`, but requirements §2 and architecture §5 mandate ingest-only scoping.
-- The column is added here and documented in the data model in the same commit.
CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label        TEXT NOT NULL,
    scope        TEXT NOT NULL DEFAULT 'ingest' CHECK (scope IN ('ingest')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX idx_api_tokens_user ON api_tokens(user_id);

-- Single-use magic links / pairing codes for onboarding a new device. Short-lived
-- (~15 min). Consumed exactly once (used_at set); only the hash is stored.
CREATE TABLE onboarding_tokens (
    id          INTEGER PRIMARY KEY,
    token_hash  TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('magic_link', 'pairing_code')),
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name TEXT,
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);
CREATE INDEX idx_onboarding_tokens_user ON onboarding_tokens(user_id);
