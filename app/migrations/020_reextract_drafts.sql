-- Migration 020 - Re-reading a source produces a comparison draft, never an overwrite.
--
-- docs/04 section 8 and CONVENTIONS section 9 have always promised that re-extraction creates a
-- comparison draft and never overwrites family edits, but nothing could re-extract: the pipeline
-- refuses a job that already has a recipe (that refusal IS its replay safety), and an
-- extraction_run had no way to say "proposed, not yet accepted". This migration adds both halves.
--
-- ingest_jobs gains an explicit re-read intent rather than overloading recipe_id, because
-- recipe_id is what makes a normal job replay-safe ("already produced a recipe - mark done and
-- stop"). A re-read job leaves recipe_id NULL and names its target in reextract_recipe_id
-- instead, so the normal path keeps its guarantee untouched and a re-read can never create or
-- reuse a recipe row. Its idempotency_key is 'reextract:<recipe>:<random>' - the URL is already
-- spoken for by the original job. `refetch` records whether the person asked to fetch the page
-- again (through the same SSRF-safe path) or to reuse the immutable artifacts already stored;
-- in the reuse case the stored artifact rows are linked to the new job (same sha256, same blob,
-- nothing rewritten). Deleting the recipe deletes its pending re-read jobs.
--
-- extraction_runs gains a lifecycle. Every pre-existing run is the accepted first reading, so
-- the default is 'accepted'. A re-read run starts as 'draft' and ends 'applied' (the person took
-- one or more sections - applied_sections lists them) or 'dismissed'. Only a run whose every
-- changed section was taken replaces recipes.current_extraction_run_id; a partial apply records
-- which run was consulted without claiming the recipe now matches it. At most one draft per
-- recipe is live: a newer reading supersedes (dismisses) an older unreviewed one.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

ALTER TABLE ingest_jobs
    ADD COLUMN reextract_recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE;
ALTER TABLE ingest_jobs ADD COLUMN refetch INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_ingest_jobs_reextract ON ingest_jobs(reextract_recipe_id)
    WHERE reextract_recipe_id IS NOT NULL;

ALTER TABLE extraction_runs ADD COLUMN state TEXT NOT NULL DEFAULT 'accepted'
    CHECK (state IN ('accepted', 'draft', 'applied', 'dismissed'));
ALTER TABLE extraction_runs ADD COLUMN reviewed_at TEXT;
ALTER TABLE extraction_runs ADD COLUMN reviewed_by INTEGER REFERENCES users(id);
ALTER TABLE extraction_runs ADD COLUMN applied_sections TEXT;   -- JSON list of section keys
CREATE INDEX idx_extraction_runs_draft ON extraction_runs(recipe_id) WHERE state = 'draft';
