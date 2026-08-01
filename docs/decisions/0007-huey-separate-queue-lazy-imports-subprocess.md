# 0007 — Huey on its own SQLite file; lazy heavy imports; subprocess job runner

Date: backfilled 2026-08-01 · Canonical: CONVENTIONS §3–§4, D1

## Context
One N95 box runs both the family-facing web process and heavy ingestion
(yt-dlp, Pillow, provider SDKs) without letting either starve the other.

## Decision
The Huey queue lives in its own SQLite file (`data/queue.db`), separate from the
app DB. Heavy libraries are imported only inside the worker task that uses them,
never at web-process top level, and heavy ingestion stages run in a short-lived
`job_runner` subprocess. Write transactions stay short; a write-contention test
enforces <100 ms p95 reads.

## Rationale
Documented in §3–§4: queue bookkeeping must never contend with family-facing
writes; lazy imports keep the web process under ~90 MB RSS ("this is how
Mealie's web process ballooned to 400 MB; ours will not"); a thread-mode
consumer never returns imported-library RAM, a subprocess does.
