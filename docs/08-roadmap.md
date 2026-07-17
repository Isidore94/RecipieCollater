# RecipeCollater — Build Roadmap

Phased so that **every phase ends with something the family actually uses**. The builder agent (Claude Opus/Sonnet) should complete phases in order; each phase's exit criteria are testable. Keep a `CHANGELOG.md` from phase 1.

**Cost constraint (owner requirement): $0 infrastructure.** No domain, no certificates, no cloud services, no subscriptions — the only recurring cost is Claude API usage (~$3–8/month). LAN-only: plain HTTP at `http://recipes.local` (DHCP reservation + Avahi mDNS). Optional free upgrades (mkcert HTTPS, Tailscale) are documented in `deploy/` but never on the roadmap's critical path.

## Phase 0 — Skeleton & plumbing (foundation, no features)

- Repo layout, `pyproject.toml` (uv-managed venv, Python pinned 3.12), ruff + pytest wiring, minimal CI.
- `CONVENTIONS.md`: the drift-proofing rules for a multi-session AI builder (lazy-import rule, no-ORM SQL style, short worker transactions, one-cookie discipline, dependency pin/float split). Write it first; it governs every later phase.
- FastAPI app factory, Jinja2 + htmx base layout (nav shell, dark mode), static pipeline (Tailwind standalone CLI or vanilla CSS).
- SQLite connection factory with pragmas; numbered-SQL migration runner (refuses out-of-order files; `VACUUM INTO` snapshot before every apply); `schema_migrations` table. Huey queue in its own `queue.db`.
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
- **AI provider abstraction** (`app/ai/`): the two-implementation adapter (Anthropic + OpenAI) with task routing, cross-provider fallback, usage logging, and per-provider spend caps — built here because extraction is the first AI use. Designed for two providers from day one even if only one key is set; **golden-file tests per provider** lock the structured-output serializations.
- Web pipeline: httpx→curl_cffi fetch, recipe-scrapers fast path, LLM fallback (via the adapter), OpenGraph stub; `raw_extraction` stored; re-extract button.
- YouTube pipeline: yt-dlp metadata+comments, transcript fetch, single structuring call (via the adapter), thumbnail capture, per-step `video_seconds`.
- Ingredient normalization chain (ingredient-parser-nlp → matcher → batched LLM repair → pending-food quarantine chips).
- Apple Shortcuts (documented recipe + iCloud link template with Import Questions), **including the Safari HTML-capture variant** for bot-walled sites; paste box; bookmarklet.
- Heavy pipeline stages (yt-dlp, image processing, CRF parsing) run in short-lived subprocesses spawned by Huey tasks (full RAM release, hang isolation).
- Weekly yt-dlp self-update task; AI usage logging + monthly cap; Settings page shows per-provider spend and lets the owner pick which provider/model runs each task.
- **Exit criteria**: sharing a YouTube video from an iPhone yields a correct, pretty recipe sheet in <60s with zero further input; a schema.org site imports in <5s without an LLM call; extraction works with *either* an Anthropic-only or OpenAI-only key, and falls over cleanly when one provider is forced to error.

## Phase 3 — Cooking experience (why the family keeps it)

- Cook mode: full-screen steps, screen-wake via the silent-video technique (works on plain HTTP; native wakeLock tried opportunistically), tap-duration timers, ingredient checklist, embedded YouTube player with timestamp seek.
- After-cook capture: rating, actual time → `our_minutes`, per-ingredient actually-used, notes; promotion gate wiring.
- Cook log timeline on recipe sheets; "haven't made in a while" sort.
- Recipe Q&A drawer (Haiku, recipe-scoped).
- Home-screen app: manifest + apple-touch-icon + A2HS instructions page (works over plain HTTP). Service worker code written but self-disabling outside secure contexts.
- LAN setup: Avahi `recipes.local`, DHCP-reservation guide, port-80 systemd capability.
- **Exit criteria**: cook a real dinner phone-in-kitchen end-to-end; screen stays awake; timer fires; after-cook flow captures corrections and promotes the recipe.

## Phase 4 — Pantry & shopping

- Locations CRUD (inline-creatable), pantry item CRUD with graduated quantity modes, stock-take mode, staples & thresholds.
- **Remove / used-up / spoiled** action on every item (swipe or ⋯ menu) writing `pantry_adjustments`; only `spoiled` surfaced to the AI.
- **Automatic cook-through deduction** (`06-…` §2.1): marking a recipe cooked deducts ingredients (canonical-unit math on exact items, one-tap step-downs for gauge/binary, skips approx/unmatched), auto-apply by default with a reversible summary (batch Undo via `batch_id`), a global auto/review toggle, and per-recipe editable "don't deduct" + pantry-item-mapping defaults that the recipe remembers. Property-test the deduction/conversion math (round-trips, zero-crossing, unit bridges).
- Recipe⇄pantry matching ("have 7/9", "cook from what we have" filter).
- Shopping list: generation (plan − pantry + low staples), aggregation math, aisle grouping, manual adds, provenance labels.
- In-store island: **write the sync-protocol spec (op shapes, LWW rules, tombstones, staleness cutoff) BEFORE generating any JS** — it's the one subsystem where an unspecified agent improvises divergently across sessions. Then: Alpine store + localStorage outbox + idempotent `POST /api/shopping/sync`, a pytest matrix over the sync endpoint (concurrent devices, replays, stale ops), and a defined degrade-to-online-only failure mode (never data loss). Post-shop "restock pantry" bulk flow.
- "Copy list as text" export on the shopping page (the free paper-backup for in-store use).
- **Exit criteria**: weekly shop runs off the app in the store, including one dead-signal aisle; cooking a recipe leaves the pantry correct automatically with a working one-tap Undo; a spoiled item is removed in one gesture; pantry survives a month of casual use without a full re-inventory.

## Phase 5 — Meal planning & AI assistant

- Week board (drag-drop/tap-assign), note entries, per-entry servings, saved menus, plan→shopping-list generation, iCal export.
- Chat assistant: SSE streaming, prompt-cached pantry+cookbook context, strict tools, proposal cards (meal plan / shopping list / pantry updates with accept-edit-dismiss).
- Conversational pantry updates; "use it up" rail.
- Big-event mode: menu proposal, N-scaling, combined list, prep timeline.
- **Exit criteria**: "plan next week's dinners" produces an accepted plan + shopping list in one short chat; "what can I make in 30 minutes" answers from real pantry state.

## Phase 6 — Polish & resilience

- Nightly `VACUUM INTO` backups to a different physical device + rotation + restore doc (tested!); nightly JSON/markdown export of the cookbook; optional Litestream.
- Admin dashboard: job health, AI spend, yt-dlp version, backup status, DB size.
- Photo import (Claude vision). Semantic search (fastembed + sqlite-vec) if FTS5 feels limiting.
- Family onboarding guide; optional-upgrades appendix in `deploy/` (mkcert HTTPS for wake-lock API/SW/Android share; Tailscale for remote access) — documented, not installed.
- Performance pass against budgets (FCP <1s LAN, reads <100ms); Lighthouse performance audit.
- **Backlog (post-v1)**: scale-by-anchor-ingredient, sub-recipes, collections, nutrition via FDC ids, TikTok/Instagram ingestion (needs Whisper — revisit hardware), barcode scanning (needs the HTTPS upgrade for camera access), Android `share_target` (ditto), Home Assistant hooks, multi-image recipes.

## Testing conventions

- pytest + httpx TestClient; golden-file tests for the extraction schema (recorded yt-dlp/scraper fixtures — never hit the network in tests); property tests for scaler & aggregation math (round-trips, fraction rendering); a **write-contention test** (simultaneous ingest + shopping sync must not stall the web process); Playwright smoke for cook mode/wake-lock and the shopping island (Chromium is fine; note real iOS quirks are hand-tested).
- Every LLM call mockable; CI runs fully offline.

## What "lightweight at idle" means, measurably

| Budget | Target |
|---|---|
| Infrastructure cost | $0 — Claude API is the only recurring cost |
| Idle RSS (web + worker) | < 200 MB total |
| Idle CPU | ~0% (no polling loops server-side; Huey periodic tasks only) |
| First contentful paint, phone on LAN | < 1s |
| Read endpoints | < 100 ms |
| Ingest job (YouTube, transcript path) | < 60 s end-to-end |
