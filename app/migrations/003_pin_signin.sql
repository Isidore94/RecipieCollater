-- Migration 003 — Optional numeric PIN sign-in for named users.
--
-- Phase-0 identity refinement. The app defaults to passwordless device pairing; this adds
-- an OPTIONAL per-user PIN so a household member can sign in by name + PIN on a new device
-- instead of an admin-issued pairing code. The PIN is never stored in the clear: pin_hash
-- holds a scrypt-derived, salted, self-describing string (CONVENTIONS §6 now covers PINs).
-- A low-entropy numeric PIN is protected against brute force by per-user lockout.
--
-- The rc_session device cookie remains the single identity authority (CONVENTIONS §5);
-- PIN sign-in is just another way to MINT a device session. Documented in the same commit:
-- docs/00-overview.md (D7), CONVENTIONS §5/§6, docs/03-data-model.md. Phase-1 cookbook
-- tables begin at migration 004.
--
-- The runner owns transactions: this file must not contain BEGIN/COMMIT/VACUUM.

ALTER TABLE users ADD COLUMN pin_hash            TEXT;
ALTER TABLE users ADD COLUMN pin_set_at          TEXT;
ALTER TABLE users ADD COLUMN pin_failed_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN pin_locked_until    TEXT;
