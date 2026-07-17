-- Migration 007 - Phase 2: ingestion jobs, immutable artifacts, extraction runs,
-- and the recipe provenance columns deferred from Phase 1 (docs/03 section 4 note,
-- docs/04 ingestion pipeline).
--
-- ingest_jobs drive the queued -> fetching -> extracting -> normalizing -> done/failed
-- lifecycle with heartbeat/attempt metadata and an idempotency key so a resubmitted URL or
-- a worker replay cannot create a second recipe. artifacts are immutable, content-addressed
-- (sha256) captures of supplied/fetched HTML, JSON-LD, descriptions, and captions.
-- extraction_runs record extractor/provider/model/prompt/schema versions for provenance and
-- re-extraction diffs. The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE ingest_jobs (
    id                INTEGER PRIMARY KEY,
    url               TEXT NOT NULL,
    normalized_url    TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    source            TEXT NOT NULL DEFAULT 'api'
                        CHECK (source IN ('api', 'paste', 'share', 'manual')),
    has_html          INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'fetching', 'extracting', 'normalizing',
                                          'done', 'failed')),
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TEXT,
    error_category    TEXT,
    error_message     TEXT,
    recipe_id         INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    submitted_by      INTEGER REFERENCES users(id),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_ingest_jobs_status ON ingest_jobs(status);
CREATE INDEX idx_ingest_jobs_normalized ON ingest_jobs(normalized_url);

-- Immutable, content-addressed captures. One row per (job, kind); the bytes live under
-- data/artifacts/<sha256[:2]>/<sha256>.
CREATE TABLE artifacts (
    id         INTEGER PRIMARY KEY,
    job_id     INTEGER NOT NULL REFERENCES ingest_jobs(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    path       TEXT NOT NULL,
    bytes      INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (job_id, kind)
);
CREATE INDEX idx_artifacts_job ON artifacts(job_id);

CREATE TABLE extraction_runs (
    id             INTEGER PRIMARY KEY,
    recipe_id      INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
    job_id         INTEGER REFERENCES ingest_jobs(id) ON DELETE SET NULL,
    extractor      TEXT NOT NULL,   -- recipe_scrapers, llm_web, youtube, opengraph, manual
    provider       TEXT,
    model          TEXT,
    prompt_version TEXT,
    schema_version TEXT NOT NULL,
    confidence     TEXT CHECK (confidence IN ('high', 'medium', 'thin')),
    payload        TEXT NOT NULL,   -- JSON of the validated ExtractedRecipe
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_extraction_runs_recipe ON extraction_runs(recipe_id);

-- Recipe provenance columns (Phase 2 owners): normalized source URL for duplicate detection,
-- YouTube id for the embedded player / step deep-links, and the accepted extraction run.
ALTER TABLE recipes ADD COLUMN normalized_source_url TEXT;
ALTER TABLE recipes ADD COLUMN video_id TEXT;
ALTER TABLE recipes ADD COLUMN current_extraction_run_id INTEGER REFERENCES extraction_runs(id);
CREATE INDEX idx_recipes_normalized_source ON recipes(normalized_source_url);
