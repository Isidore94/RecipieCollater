-- Migration 002 — Phase 0 operational hardening.
--
-- Track the last session renewal independently from the immutable issue timestamp. This avoids
-- re-renewing and re-writing the session cookie on every request after its first 30 days.

ALTER TABLE device_sessions
    ADD COLUMN renewed_at TEXT NOT NULL DEFAULT '1970-01-01 00:00:00';
UPDATE device_sessions SET renewed_at = issued_at;
