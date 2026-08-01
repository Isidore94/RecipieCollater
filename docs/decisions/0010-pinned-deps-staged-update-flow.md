# 0010 — Exact-pinned dependencies with a staged, rehearsed update flow

Date: backfilled 2026-08-01 · Canonical: CONVENTIONS §12 §14, `deploy/UPDATES.md`

## Context
The N95 is a live family appliance; a bad dependency bump or migration must
never take the cookbook down or lose data.

## Decision
Production dependencies are pinned exactly in `pyproject.toml` (uv + `uv.lock`);
never `pip install -U` in a live environment. Updates go through
build → offline-test → migration-rehearsal → temp-port-healthcheck →
atomic-switch → rollback (`deploy/update.sh` / `deploy/rollback.sh`). Backups
count as healthy only after checksums, `PRAGMA integrity_check`, and a restore
smoke test.

## Rationale
Documented in §12/§14 and `deploy/UPDATES.md`: reproducible installs, hot
dependencies (yt-dlp) surfaced weekly but installed only deliberately, and
"file creation alone is not a backup."
