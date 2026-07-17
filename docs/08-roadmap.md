# RecipeCollater — Build Roadmap

Phased so that **every phase ends with something the family actually uses**. The builder agent (Claude Opus/Sonnet) should complete phases in order; each phase's exit criteria are testable. Keep a `CHANGELOG.md` from phase 1.

## Phase 0 — Skeleton & plumbing (foundation, no features)

- Repo layout, `pyproject.toml` (uv-managed venv), ruff + pytest wiring.
- FastAPI app factory, Jinja2 + htmx base layout (nav shell, dark mode), static pipeline (Tailwind standalone CLI or vanilla CSS).
- SQLite connection factory with pragmas; numbered-SQL migration runner; `schema_migrations` table.
- Migration 001: full schema from `03-data-model.md`.
- Seed loader: units, unit aliases, foods, food aliases, conversions from `seed/` JSON.
- Huey worker (SqliteHuey) + systemd unit files (`recipecollater-web.service`, `recipecollater-worker.service`) + install script.
- Auth: users, device sessions (cookie dependency), api tokens, magic-link + pairing-code flows, admin Devices page.
- **Exit criteria**: app boots on the N95, family devices onboarded via QR, empty tabs render, `pytest` green.

## Phase 1 — Recipes & manual entry (the cookbook exists)

- Recipe CRUD: editor (ingredients with inline food/unit creation, steps, sections, image upload), WEBP pipeline.
- Recipe sheet: full display incl. TLDR block, tier badges, tags, serving scaler (server-side math, kitchen fractions, non-scalable passthrough, original-amounts toggle).
- Inbox/Cookbook/Archive statuses + promotion actions; cookbook browse with FTS5 search + filters (tier, tags, max time).
- Recipe export (JSON + markdown/print view).
- **Exit criteria**: a manually entered pasta recipe scales 4→6 correctly ("1½ cups" not "1.5000"), searches instantly, prints cleanly.

## Phase 2 — Ingestion (the bot comes alive) ★ the magic moment

- `POST /api/ingest` (202 + job), ingest_jobs lifecycle, inbox processing states (htmx polling), retry, duplicate detection.
- Web pipeline: httpx→curl_cffi fetch, recipe-scrapers fast path, Claude fallback, OpenGraph stub; `raw_extraction` stored; re-extract button.
- YouTube pipeline: yt-dlp metadata+comments, transcript fetch, single Claude structuring call, thumbnail capture, per-step `video_seconds`.
- Ingredient normalization chain (ingredient-parser-nlp → matcher → batched LLM repair → pending-food quarantine chips).
- Apple Shortcut (documented recipe + iCloud link template with Import Questions); paste box; bookmarklet.
- Weekly yt-dlp self-update task; AI usage logging + monthly cap.
- **Exit criteria**: sharing a YouTube video from an iPhone yields a correct, pretty recipe sheet in <60s with zero further input; a schema.org site imports in <5s without an LLM call.

## Phase 3 — Cooking experience (why the family keeps it)

- Cook mode: full-screen steps, wake lock (with iOS fallbacks), tap-duration timers, ingredient checklist, embedded YouTube player with timestamp seek.
- After-cook capture: rating, actual time → `our_minutes`, per-ingredient actually-used, notes; promotion gate wiring.
- Cook log timeline on recipe sheets; "haven't made in a while" sort.
- Recipe Q&A drawer (Haiku, recipe-scoped).
- PWA: manifest, service worker (app-shell precache, network-first pages), A2HS instructions page, Android install prompt.
- **Exit criteria**: cook a real dinner phone-in-kitchen end-to-end; screen stays awake; timer fires; after-cook flow captures corrections and promotes the recipe.

## Phase 4 — Pantry & shopping

- Locations CRUD (inline-creatable), pantry item CRUD with graduated quantity modes, stock-take mode, staples & thresholds.
- Recipe⇄pantry matching ("have 7/9", "cook from what we have" filter).
- Shopping list: generation (plan − pantry + low staples), aggregation math, aisle grouping, manual adds, provenance labels.
- In-store island: Alpine store + localStorage outbox + idempotent `POST /api/shopping/sync` (LWW, UUID adds, tombstones, staleness cutoff); post-shop "restock pantry" bulk flow.
- Android `share_target` (now that the PWA is installed HTTPS).
- **Exit criteria**: weekly shop runs off the app in the store, including one dead-signal aisle; pantry survives a month of casual use without a full re-inventory.

## Phase 5 — Meal planning & AI assistant

- Week board (drag-drop/tap-assign), note entries, per-entry servings, saved menus, plan→shopping-list generation, iCal export.
- Chat assistant: SSE streaming, prompt-cached pantry+cookbook context, strict tools, proposal cards (meal plan / shopping list / pantry updates with accept-edit-dismiss).
- Conversational pantry updates; "use it up" rail.
- Big-event mode: menu proposal, N-scaling, combined list, prep timeline.
- **Exit criteria**: "plan next week's dinners" produces an accepted plan + shopping list in one short chat; "what can I make in 30 minutes" answers from real pantry state.

## Phase 6 — Polish & resilience

- Nightly `VACUUM INTO` backups + rotation + restore doc (tested!); optional Litestream.
- Admin dashboard: job health, AI spend, yt-dlp version, backup status, DB size.
- Photo import (Claude vision). Semantic search (fastembed + sqlite-vec) if FTS5 feels limiting.
- Caddy + domain + Tailscale subnet-router setup docs; onboarding guide for family.
- Performance pass against budgets (FCP <1s LAN, reads <100ms); Lighthouse PWA audit.
- **Backlog (post-v1)**: scale-by-anchor-ingredient, sub-recipes, collections, nutrition via FDC ids, TikTok/Instagram ingestion (needs Whisper — revisit hardware), barcode scanning, Home Assistant hooks, multi-image recipes.

## Testing conventions

- pytest + httpx TestClient; golden-file tests for the extraction schema (recorded yt-dlp/scraper fixtures — never hit the network in tests); property tests for scaler & aggregation math (round-trips, fraction rendering); Playwright smoke for cook mode/wake-lock and the shopping island (Chromium is fine; note real iOS quirks are hand-tested).
- Every LLM call mockable; CI runs fully offline.

## What "lightweight at idle" means, measurably

| Budget | Target |
|---|---|
| Idle RSS (web + worker + Caddy) | < 250 MB total |
| Idle CPU | ~0% (no polling loops server-side; Huey periodic tasks only) |
| First contentful paint, phone on LAN | < 1s |
| Read endpoints | < 100 ms |
| Ingest job (YouTube, transcript path) | < 60 s end-to-end |
