-- Migration 008 - Phase 2: AI usage accounting for the LLM extraction fallback.
-- Every provider call is logged (ok / error / blocked) with token counts and an integer
-- micro-USD cost, so daily and monthly spend caps can be enforced before the next call
-- (docs/05 ai-integration). Money is stored as integer micro-USD, never a float
-- (CONVENTIONS 1). The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE ai_usage_log (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    operation     TEXT NOT NULL,   -- extract_web, extract_youtube, normalize_ingredients, ...
    job_id        INTEGER REFERENCES ingest_jobs(id) ON DELETE SET NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_micros   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'error', 'blocked')),
    error         TEXT
);
CREATE INDEX idx_ai_usage_created ON ai_usage_log(created_at);
