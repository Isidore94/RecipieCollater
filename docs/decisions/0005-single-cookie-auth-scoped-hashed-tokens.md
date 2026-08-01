# 0005 — Passwordless auth: one `rc_session` cookie, scoped opaque hashed tokens

Date: backfilled 2026-08-01 · Canonical: D7 (`docs/00-overview.md`), CONVENTIONS §5–§7

## Context
Family devices (iOS home-screen PWAs especially) must stay signed in for months
on a plain-HTTP LAN, without passwords and without iOS cookie-reversion bugs.

## Decision
Named users with 400-day device sessions via magic-link/QR pairing; exactly one
persistent HttpOnly `SameSite=Lax` cookie (`rc_session`), which is also the
primary CSRF defense. Optional per-user PIN sign-in (scrypt-hashed, per-user
lockout) mints the same cookie. All tokens are opaque, stored only as SHA-256
hashes; ingest tokens are scoped to ingestion and nothing else.

## Rationale
Documented in D7 and §5–§7: the Immich/Home-Assistant-proven pattern; mixing
cookie strategies triggers WebKit bug 272325 in iOS home-screen apps; Starlette
SessionMiddleware is unrevocable; the CSRF header requirement was dropped
2026-07-17 after a real iPhone couldn't pair (measured reality, recorded in §7).
