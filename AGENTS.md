# RecipeCollater — AI context index

Self-hosted family recipe platform on a home Intel N95 mini PC, LAN-only, $0
infrastructure. Anyone shares a YouTube video or recipe URL from a phone/PC; an AI
pipeline builds a structured recipe sheet into a Test Recipes inbox; keepers get
promoted into the family cookbook. A location-aware pantry, meal planning, shopping
lists, and an AI assistant (proposal cards only) sit on top. The GitHub repo name is
`recipiecollater`; the project is RecipeCollater.

## Core loop / data flow
- Web: `app/main.py` FastAPI + server-rendered Jinja2/htmx (Alpine only for local, non-authoritative niceties). Routers in `app/routers/` are thin; business logic lives as pure functions in `app/services/`.
- Worker: Huey consumer (`app/tasks.py`) with its own SQLite queue file (`data/queue.db`); heavy ingestion stages run in a short-lived `job_runner` subprocess; heavy libraries are imported lazily inside tasks.
- Ingest: `/add` paste box, bookmarklet, or Apple Shortcut (scoped Bearer ingest token) → 202 in <2 s → extraction cascade (recipe-scrapers schema.org fast path → provider-neutral LLM extraction). YouTube is description-first (metadata → captions, no local Whisper); Instagram via caption.
- Storage: SQLite through hand-written SQL (`app/db.py`), no ORM. Forward-only migrations `app/migrations/NNN_*.sql` with a `VACUUM INTO` snapshot before every apply. Immutable, content-addressed ingestion artifacts; FTS-indexed search.
- AI: `app/ai/` — Anthropic and OpenAI adapters behind one small capability interface, provider/model selected per task, no automatic cross-provider failover. The assistant writes only via proposal cards; deterministic services perform all math and mutations.
- Auth: passwordless magic-link/QR device onboarding (optional scrypt PIN) → the single `rc_session` cookie; opaque SHA-256-hashed tokens; ingest tokens can only ingest.
- Deploy: bare systemd units (`deploy/systemd/`), staged update flow (`deploy/UPDATES.md`), `http://recipes.local` with uvicorn directly on port 80, never port-forwarded.

## Hard invariants (CONVENTIONS.md is the binding contract; change it only with evidence, in the same commit)
- Quantities enter as strings and are parsed with `decimal.Decimal`; canonical storage is exact integers (mg/µL/milli-each). Never `float()` a quantity; no recipe/pantry/shopping arithmetic in SQL `REAL`.
- No ORM — no SQLAlchemy, no Alembic. Hand-written SQL + ordered migration files only.
- Exactly one cookie: `rc_session` (persistent, `SameSite=Lax`, the primary CSRF defense). Never Starlette SessionMiddleware, never a second cookie.
- LAN-only, $0 infra: plain HTTP, no domain/certs/proxy, no runtime CDN (vendored htmx/alpine), `APP_BASE_URL` is the single source of URLs; SSRF defense on ingest fetches.
- Web process stays < ~90 MB RSS (lazy imports); short write transactions; the worker never stalls reads (<100 ms p95 — keep the write-contention test green).
- AI proposes, deterministic services mutate; OpenAI Responses calls set `store: false`; keys live only in the root-owned EnvironmentFile.
- Immutable artifacts and versioned extraction runs; re-extraction never overwrites family edits.
- Phase discipline: no feature, table, or dependency from a later `docs/08-roadmap.md` phase; deps pinned exactly; updates only through the staged deploy flow.
- Backups count only when restore-tested. CI is fully offline; never claim N95/iPhone/home-network testing that didn't happen.

## Tech stack + key deps
- Python 3.12+, `uv` (+`uv.lock`), FastAPI + uvicorn, Jinja2, htmx + Alpine (vendored, pinned, SHA-256 recorded), SQLite (+FTS), Huey, structlog, pydantic, httpx.
- `recipe-scrapers` — schema.org extraction fast path; `yt-dlp` — YouTube; Pillow — images; `anthropic` + `openai` — provider adapters.
- Dev: pytest, ruff (broad select incl. security `S`), mypy `--strict`. Everything pinned exactly (CONVENTIONS §12).

## Commands
- Setup: `uv sync --frozen`. Run web: `RC_SETUP_TOKEN=... RC_ALLOWED_HOSTS=localhost,127.0.0.1 uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`; worker: `uv run huey_consumer app.tasks.huey -w 1 -k thread`.
- Gates before every commit (CI runs exactly these, offline): `uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` · `uv run pytest`.
- Deploy on the N95: `deploy/install.sh`; updates via `deploy/update.sh` / `deploy/rollback.sh` (staged flow in `deploy/UPDATES.md`).

## Where to read more
- `docs/00-overview.md` — product summary + the canonical decision log (D1–D14, each with rationale). Read first.
- `CONVENTIONS.md` — the authoritative engineering contract (§1–16), binding for every session.
- `docs/01`–`docs/07` — per-subsystem specs: requirements, architecture, data model, ingestion, AI integration, pantry/shopping/planning, UI/UX.
- `docs/08-roadmap.md` — phase order and exit criteria. `CHANGELOG.md` — what actually landed (well past Phase 6; the README's "Phase 0" status line is stale).
- `docs/09-research-findings.md` — the research backing the decisions; `docs/10-usability-review-2026-08.md` + `docs/reviews/` — review passes and fixes.
- `docs/decisions/` — one-page anchors into the canonical decisions; `00-overview.md` + `CONVENTIONS.md` remain the source of truth.
- `deploy/LAN.md`, `deploy/RESTORE.md`, `deploy/UPDATES.md` — LAN/mDNS setup, restore drills, staged updates.

`AGENTS.md` is a copy of this file (copy, not symlink, for Windows contributors) — edit CLAUDE.md, then re-copy.
