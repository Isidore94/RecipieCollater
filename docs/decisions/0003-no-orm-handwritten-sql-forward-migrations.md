# 0003 — No ORM: hand-written SQL and forward-only incremental migrations

Date: backfilled 2026-08-01 · Canonical: CONVENTIONS §2 §13, D13 (`docs/00-overview.md`)

## Context
A single-box SQLite app built across many sessions needs schema changes that are
reviewable, rollback-friendly, and free of hidden query behavior.

## Decision
Data access is hand-written SQL through the `app.db` connection factory — no
SQLAlchemy, no Alembic. Schema changes are ordered `app/migrations/NNN_*.sql`
files tracked in `schema_migrations`, applied in-order in a transaction with a
`VACUUM INTO` snapshot before every apply; tables land with the phase that owns
them, never speculatively.

## Rationale
Documented in CONVENTIONS §2/§13 and D13: SQL stays visible and testable near
its use; incremental migrations mean smaller reviewable changes, easier
rollback, and less schema commitment before real use.
