# Changelog

All notable changes to RecipeCollater are recorded here. Phases refer to
`docs/08-roadmap.md`.

## [Unreleased]

### Phase 0 — Deployable foundation

The application skeleton the household can install, pair devices with, and operate —
before any recipe features exist.

**Project & tooling**
- `pyproject.toml` (uv-managed, Python pinned to 3.12, production deps pinned exactly;
  dev tools in a `dev` dependency group), `.python-version`, `.gitignore`, committed
  `uv.lock`.
- Ruff (lint + format), mypy (strict), pytest, and a minimal offline GitHub Actions CI
  running all four gates.
- Authoritative root `CONVENTIONS.md` (exact-math boundary, no-ORM, short transactions,
  lazy heavy imports, one-cookie discipline, scoped tokens, layered CSRF, htmx fragment
  conventions, artifact immutability, provider privacy, dependency/update policy,
  docs-as-contract).

**Web application**
- FastAPI application factory with lifespan (idempotent startup migration), structlog
  structured logging with per-request IDs, and an unauthenticated `/healthz` that checks
  the app DB, schema currency, and the separate queue DB.
- Server-rendered Jinja2 + htmx shell: responsive left-rail (desktop) / bottom tab-bar
  (mobile) navigation, five empty tab screens, auto/light/dark theme (per-device cookie),
  accessibility affordances, and self-hosted (checksum-pinned) htmx/Alpine — no runtime CDN.
- PWA manifest, apple-touch icons, home-screen metadata (no service worker on plain HTTP).

**Data layer**
- SQLite connection factory with project pragmas (WAL, busy_timeout, foreign_keys, …).
- Ordered, forward-only migration runner: `schema_migrations` bookkeeping, contiguity
  validation, checksum tamper / database-ahead detection, a `VACUUM INTO` snapshot before
  every apply, a trigger-aware SQL splitter, and atomic per-migration transactions.
- Migration `001` creates **only** Phase-0 tables: `users`, `device_sessions`,
  `api_tokens`, `onboarding_tokens`.

**Identity & onboarding**
- Named users; first-run admin bootstrap.
- One persistent HttpOnly SameSite=Lax device-session cookie with sliding renewal and
  per-device revocation; opaque tokens hashed at rest.
- Magic-link and typed pairing-code onboarding (single-use, expiring).
- Separate **scoped** ingest Bearer tokens (`api_tokens.scope`, ingest-only) — a spec
  refinement over `docs/03-data-model.md` §2, documented there in this commit.
- Layered CSRF (Fetch-Metadata + custom-header fallback atop SameSite=Lax).

**Worker & operations**
- Huey worker on a **separate** `data/queue.db`: liveness `ping`, hourly heartbeat, and a
  nightly backup task; systemd units for web + worker (port-80 capability, memory caps,
  hardening).
- Backup framework: `VACUUM INTO` snapshot + images/artifacts + checksum manifest +
  `PRAGMA integrity_check`, with verify / restore / prune and a management CLI
  (`python -m app.manage`).
- Staged install / update / rollback scripts (build isolated release → offline tests →
  verified backup → migration rehearsal on a copy → temp-port health check → atomic switch;
  separate application vs data rollback). Never mutates a live env in place.
- Deploy docs: `LAN.md` (DHCP + Avahi `recipes.local`), `UPDATES.md`, `RESTORE.md`,
  `env.example`.

**Tests (offline):** 67 tests covering the SQL splitter, migration ordering/failure/
rollback/snapshot-upgrade/tamper, session lifecycle & sliding renewal, the ingest-token
scope boundary (both directions) and its lack of app access, onboarding single-use/expiry,
CSRF, health, worker/queue separation, backup→verify→restore round trips, the management
CLI, and vendored-asset checksums.
