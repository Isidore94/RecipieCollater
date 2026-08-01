# Changelog

All notable changes to RecipeCollater are recorded here. Phases refer to
`docs/08-roadmap.md`.

## [Unreleased]

### Instagram reel ingestion (2026-07-27)

No schema change. The existing Apple Shortcut and existing tokens now import Instagram
reels and posts — the recipe is read from the post's caption.

- `app/services/instagram.py`: caption extraction from the public
  `/p/<code>/embed/captioned/` page, parsed defensively (inline `contextJSON` →
  rendered `.Caption` → truncated `og:description`), with trailing hashtag blocks stripped
  before the LLM prompt.
- **yt-dlp is deliberately not used.** Measured on the deploy target: yt-dlp 2026.07.04
  answers every Instagram post — real or invented shortcode — with "empty media response
  ... use --cookies", so anonymous access is impossible and the app stores no Instagram
  credential. Supplied Safari HTML from the Shortcut's JavaScript variant remains the
  path for private/followers-only posts, and now takes priority over any network fetch.
- `ingest.instagram_shortcode()` collapses `/reel/`, `/reels/`, `/p/`, and `/tv/` URLs for
  one post onto a single idempotency key, so sharing the same reel from the app and from
  the website no longer creates two recipes. Profile URLs still fall through to the web path.
- `source_type` stays `web` and `video_id` stays NULL (both avoid a `recipes` table rebuild;
  `video_id` is YouTube-specific and would render a broken cook-mode deep link). Provenance
  is `extraction_runs.extractor = 'instagram'`, spend logged as `extract_instagram`.
- Failure is actionable: private, removed, or caption-less posts report
  `instagram_unavailable` naming the Safari workaround, rather than a generic parse error.
- Test fix (pre-existing): `tests/conftest.py` now clears `RC_ANTHROPIC_API_KEY` /
  `RC_OPENAI_API_KEY`, which a developer shell commonly exports. Without it the
  "no AI key configured" paths silently stopped being exercised and
  `test_pipeline_youtube_needs_ai_key` failed on this machine.

### Phase 4.7 — Receipt capture (2026-07-18)

Schema v13 (migration 013). A grocery trip becomes pantry restocks in one review.

- Receipt photo (vision) or pasted Instacart/Costco order (text) parsed once by the AI
  provider into persisted lines; both adapters gained a forced `save_receipt` tool call,
  with the photo sent as a base64 image (uploads normalized to bounded JPEG via Pillow).
- Store-text generalization anchored to the household's own foods list, corrected inline at
  review, and LEARNED: applied lines write their receipt phrasing into `food_aliases`, so
  the same store resolves deterministically on the next trip.
- Apply (idempotent, review-first) restocks matched pantry items, optionally starts
  tracking new foods, and checks purchases off the shopping list. "Scan receipt" entry
  points on Pantry and Shopping.

### Phase 5 — Meal planning & assistant (2026-07-18)

Schema v14 (migration 014). The last unbuilt pillar of the product.

- **Week board** (`/plan`, new Plan tab): recipe or free-text note entries with per-entry
  servings, prev/this/next-week navigation, and one-tap **plan → shopping list** (reuses the
  Phase 4.6 trip builder, so it's pantry-aware and package-rounded). **Saved menus** save a week
  as a reusable template and re-apply it to any week (survives recipe deletion, skips orphans).
  **iCal export** of the week for Apple/Google Calendar.
- **Household preferences** (`/preferences`): allergies + exclusions are hard constraints (the
  assistant never suggests a recipe that hits them); dislikes/diet/equipment/cuisines are soft;
  weekday/weekend time budgets and default servings are scalars.
- **Assistant** (`/chat`, new Chat tab): deterministic-first — application code builds a
  hard-filtered, coverage-ranked candidate set and pantry summary, the model reasons over that
  and returns one structured turn, and any meal-plan / pantry-update proposals are persisted as
  pending records. Accept/Dismiss cards; acceptance re-validates and applies via deterministic
  services in one idempotent transaction (the model never writes). Hallucinated recipe ids are
  dropped. v1 is one structured request/response per turn (no streaming yet — docs/05).
- Nav is now the full six: Home / Cookbook / Pantry / Shopping / Plan / Chat.

### Phase 6 — Resilience & polish (2026-07-18)

- **Admin dashboard** (`/admin/dashboard`): queue pending/failed, recent ingest failures, AI
  spend per provider vs caps, DB size, schema version, worker heartbeat, last healthy backup,
  yt-dlp version.
- **Cookbook export**: `python -m app.manage export-cookbook <dir>` writes every recipe as JSON
  and Markdown plus an index (cron/Scheduled-Task for the nightly cadence).
- **Cookbook photo import**: snap a cookbook page or recipe card on the New Recipe form → vision
  transcription prefills the form for review (reuses the 4.7 image pipeline; nothing saved until
  you Save).
- Evidence-gated items (semantic search, TLS, Tailscale, full a11y/perf checklist) deferred per
  the roadmap.

### YouTube ingestion + title fixes (2026-07-18)

- Inline recipe rename on the sheet (imported YouTube titles are clickbait); the slug/URL
  never changes.
- YouTube videos whose recipe lives in the description with a spoken-only method now import:
  the extraction prompt explicitly permits reconstructing steps from the transcript, and the
  YouTube path accepts ingredients-only recipes (the video carries the method — docs/04
  amendment).
- Re-pasting a failed URL requeues the job instead of returning the dead one.
- Full-form edits keep step metadata (video_seconds "Watch this step" links, minutes,
  sections) for unchanged step text.

### Phase 4.6 — GUI usability overhaul (2026-07-18)

Schema v12 (migration 012). The shopping list learns how food is bought, the cook log learns
what actually happened, and discovery makes the cookbook answer "what can I make?".

- **Shopping speaks "store"**: per-food purchase info (pack word + size, asked inline once);
  package ceiling on measured lines; gauge/binary staple-lane foods list without cooking
  amounts; unmeasurable lines flagged "check the amount" instead of silently dropped; fixed
  lines shop unscaled; per-line recipe provenance; Shopping in the nav with a count badge.
- **Trip builder** (`/shopping/plan`): multi-recipe preview with covered-by-pantry
  transparency and per-line opt-out; pantry subtracted once against the aggregate.
- **Restock review**: "Done shopping" turns checked lines into pantry restocks
  (gauge→full / have / +purchased amount / start tracking) before clearing them.
- **After-cook deviations**: omitted/substituted/adjusted per line + additions, quick-marked
  in cook mode and pre-filled; rendered on the cook-log timeline; deductions honor them;
  "remember this sub" feeds a learned `food_substitutes` table that resurfaces as
  suggestions on missing coverage lines.
- **Discovery**: Home screen at `/` (tonight's rested favourites, use-it-up,
  one-ingredient-away, new-to-try); `/can-make` shortfall groups; cookbook filter chips
  (tag/tier/rating/max-time) composing with search; clickable tags; photo/rating/coverage
  on recipe cards.
- **Food hygiene**: `/foods` upkeep screen (confirm pending imports, merge duplicates, set
  aisle/pack/family); ingested foods arrive `pending`; food families via `parent_food_id`;
  recipe edits carry pantry mappings/deduction trust onto unchanged lines.
- **AI tags**: controlled vocabulary (meal/protein/method/effort/cuisine) in both provider
  prompts + `python -m app.manage backfill-tags`.
- Pantry: post-creation expiry editing and a search box.

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
  (mobile) navigation, five empty tab screens, auto/light/dark theme (per-device local storage),
  accessibility affordances, and self-hosted (checksum-pinned) htmx/Alpine — no runtime CDN.
- PWA manifest, apple-touch icons, home-screen metadata (no service worker on plain HTTP).

**Data layer**
- SQLite connection factory with project pragmas (WAL, busy_timeout, foreign_keys, …).
- Ordered, forward-only migration runner: `schema_migrations` bookkeeping, contiguity
  validation, checksum tamper / database-ahead detection, a `VACUUM INTO` snapshot before
  every apply, a trigger-aware SQL splitter, and atomic per-migration transactions.
- Migration `001` creates **only** Phase-0 tables: `users`, `device_sessions`,
  `api_tokens`, `onboarding_tokens`.
- Migration `002` adds an independent session-renewal timestamp so reads are not forced to write
  or renew the cookie on every request after day 30.
- Migration `003` adds optional per-user PIN sign-in columns to `users` (see Identity below).
  Phase-1 cookbook tables therefore begin at migration `004`.

**Identity & onboarding**
- Named users; first-run admin bootstrap.
- Installer-generated first-run token, atomic bootstrap transaction, Host-header allowlist, and a
  root-run `recover-admin` pairing-code command prevent setup takeover and permanent lockout.
- One persistent HttpOnly SameSite=Lax device-session cookie with sliding renewal and
  per-device revocation; opaque tokens hashed at rest.
- Magic-link and typed pairing-code onboarding (single-use, expiring).
- Separate **scoped** ingest Bearer tokens (`api_tokens.scope`, ingest-only) — a spec
  refinement over `docs/03-data-model.md` §2, documented there in this commit.
- Layered CSRF (Fetch-Metadata + custom-header fallback atop SameSite=Lax).
- Optional per-user **PIN sign-in** (`/login`, migration 003): pick your name + a numeric PIN to
  mint a device session on a new device instead of an admin-issued code. PINs are scrypt-hashed
  (never plaintext) with per-user lockout (5 tries → 15 min); admins set/clear PINs on the Devices
  page. Added at the owner's request over the original passwordless design (D7 updated); pairing
  codes remain the no-typing fallback.

**Worker & operations**
- Huey worker on a **separate** `data/queue.db`: liveness `ping`, hourly heartbeat, and a
  nightly backup task; systemd units for web + worker (port-80 capability, memory caps,
  hardening).
- Backup framework: SQLite online snapshot + images/artifacts + complete checksum manifest +
  `PRAGMA integrity_check` + automatic scratch restore, with verify / restore / prune and a CLI
  (`python -m app.manage`).
- Staged install / update / rollback scripts (root-owned locked release → offline gates →
  consistent online snapshot rehearsal → temp-port health check → stopped-service external backup
  → migration and atomic switch;
  separate application vs data rollback). Never mutates a live env in place.
- Deploy docs: `LAN.md` (DHCP + Avahi `recipes.local`), `UPDATES.md`, `RESTORE.md`,
  `env.example`.

**Tests (offline):** 67 tests covering the SQL splitter, migration ordering/failure/
rollback/snapshot-upgrade/tamper, session lifecycle & sliding renewal, the ingest-token
scope boundary (both directions) and its lack of app access, onboarding single-use/expiry,
CSRF, health, worker/queue separation, backup→verify→restore round trips, the management
CLI, and vendored-asset checksums.
