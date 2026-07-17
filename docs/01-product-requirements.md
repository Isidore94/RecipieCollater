# RecipeCollater — Product Requirements

> **Status:** Planning complete, pre-implementation. This document is the source of truth for *what* we're building and *why*. See `02-architecture.md` for *how*.

## 1. Vision

A self-hosted, family-shared recipe platform running on a home mini PC (Intel N95). Anyone in the household can throw a YouTube cooking video or a recipe webpage at it — from an iPhone share sheet, or by pasting a link on a PC — and the system automatically parses it into a clean, structured, interactive recipe sheet. Recipes the family likes get promoted from a "test recipes" inbox into the main cookbook. A pantry tracker knows what's on hand and where it lives, and an AI assistant turns cookbook + pantry into meal plans, shopping lists, and answers to questions like *"what can I make in under 30 minutes tonight?"*

The product must be **equally good at three jobs**:
1. **Intake** — frictionless capture of recipes from video, web, and manual entry.
2. **Browse & cook** — beautiful, fast recipe sheets that work as well on a phone propped on the counter as on a PC.
3. **Plan** — pantry-aware meal planning and cookbook building, with strong AI integration.

## 2. Users & Access

- **Household members** on the home LAN only — the app is used at home, on the same Wi-Fi as the Ethernet-connected mini PC. Small trusted group; no public internet exposure; no remote access in scope (a free Tailscale upgrade path exists if that ever changes).
- Mixed devices: iPhones (share-sheet ingest via Apple Shortcuts, browsing, cook mode), desktop PCs (paste-link ingest, browsing, bulk editing).
- Auth: lightweight. Named profiles (no passwords required on LAN) attribute ratings/notes. Each Shortcut/device receives a separate revocable **ingest-only scoped token**; no shared household token and no admin capability in an ingest token.

## 3. Core Features

### 3.1 Recipe Ingestion ("the bot")

| Source | Entry point | Pipeline |
|---|---|---|
| YouTube video | Apple Shortcut share-sheet → `POST /api/ingest`; paste-box on site | Fetch metadata + description + transcript (+ pinned comment if available) → LLM extraction → structured recipe |
| Recipe website | Same endpoints | Fast path: schema.org/Recipe JSON-LD → deterministic parse. Fallback: readability-extracted text → LLM extraction |
| Manual | "New recipe" form | Structured editor with the same fields |
| Photo/screenshot (stretch) | Upload | Vision-model extraction from cookbook page / recipe card photos |

Ingestion requirements:
- **Fire-and-forget:** the phone/PC gets an immediate "got it" acknowledgment; parsing happens in a background job. New recipes appear in the Test Recipes inbox when ready (with a processing state visible in the UI).
- Every ingested recipe stores its **provenance**: normalized source URL, source type, thumbnail, channel/site name, immutable raw artifacts, extractor version, AI provider/model, prompt/schema version, and extraction confidence.
- Ingestion is **idempotent and crash-safe**. Resubmitting the same normalized URL or retrying a timed-out job cannot silently create a second recipe.
- Failures are visible and retryable from the UI (e.g., video had no captions; site blocked scraping).
- Duplicate detection warns and links to the existing recipe; an explicit "import another version" override is available.
- **Re-extract never overwrites edits.** It creates a field-by-field comparison draft that the user can accept selectively.

### 3.2 Recipe Sheet (the heart of the product)

Every recipe has:
- **Title, hero image/thumbnail, source link.**
- **TLDR** — a 1–3 sentence plain-language summary of the whole method, e.g. for a pasta: *"Cut some aromatics, cook them down with some chilies, blend down some tomatoes, throw them in, reduce, then add butter and parmesan and serve with boiled pasta."* Auto-generated at ingest; user-editable.
- **Ingredients** — each parsed into `quantity / unit / item / preparation note` (e.g. `2 | cloves | garlic | minced`), while always preserving the original text string as written.
- **Steps** — numbered, concise, with a many-to-many link to the ingredients they use (including divided quantities used in several steps).
- **Times** — claimed prep / cook / total plus **active effort vs elapsed time** and our-kitchen actuals. Auto-estimated at ingest, user-adjustable after cooking ("the video said 30 min, it really takes 50, but only 20 minutes are hands-on").
- **Servings + scaler** — recipes default to their stated yield. An interactive scaler (2/4/6/8/custom, including big events) recomputes quantities with exact decimal math, fraction-friendly display, unit awareness, and per-ingredient behavior: `linear`, `fixed`, `to_taste`, or `round_to_package`. Scaled views are ephemeral; v1 does not clone near-duplicate "scaled copies".
- **Tier** — `Meal Prep` (standard weekly staples) / `Family` (nice meals for us) / `Company` (the nicest meals I can make for guests). Plus free-form tags (cuisine, protein, course, equipment like "instant pot").
- **"What I actually used"** — per-ingredient, per-cook notes on real quantities used (planned vs actual), feeding pantry deduction and future scaling wisdom.
- **Cook log** — every time the recipe is made: date, who cooked, servings made, actual time, rating, free-text notes ("doubled the chilies, kids loved it"). This history is what makes it *our* cookbook.
- **Interactive cook mode** — step-by-step full-screen view, screen kept awake, checkable ingredient list, tap-to-advance steps, timers detected from step text ("simmer 20 minutes" → tappable timer).

### 3.3 Library Organization

- **Test Recipes (inbox)** — everything newly ingested lands here. From a test recipe you can: cook it, edit it, **promote to Cookbook**, or archive/delete. The inbox should feel like a queue to triage.
- **Main Cookbook** — the curated collection. All recipe data, full search/filter: by tier, tags, ingredient ("what do I make with leeks?"), max total time, rating, recently cooked / haven't made in a while.
- **Search** — instant full-text search across titles, ingredients, tags, notes; semantic "vibes" search via AI (stretch).
- **Collections** (stretch) — user-made groupings ("Christmas menu", "camping trip").

### 3.4 Pantry

- **Locations are user-defined**: e.g. *Kitchen Cupboards, Upstairs Pantry Fridge, Downstairs Pantry, Downstairs Freezer.* CRUD for locations; items belong to a location.
- **Items**: name, quantity + unit (loose — "2 cans", "about half a bag" must be representable), location, optional category (baking, spices, canned…), optional expiry, optional low-stock threshold, "staple" flag (things we always want on hand).
- **Ease of use is the make-or-break requirement.** Stock tracking fails when it's a chore. Design principles:
  - Adding an item is one search-with-autocomplete + one tap on a location.
  - Quantities are optional and fuzzy by default (`plenty / some / low / out` is a valid mode) with exact counts available where they matter (cans, boxes).
  - **Trust-building cook-through deduction**: the first cook of a recipe shows proposed deductions for review. Confirmed pantry mappings and opt-outs are remembered. Once a recipe is trusted, the user may enable auto-apply for that recipe; every batch remains summarized, editable, and reversible.
  - **Remove / used-up / spoiled**: any item can be taken out of the pantry in one gesture (things go bad, get finished off-recipe, or were miscounted), with an optional reason. This is the escape valve that keeps the approximate pantry honest without a chore.
  - Shopping list integration: `out`/below-threshold staples flow onto the shopping list automatically; checking items off the shopping list offers to add them back into pantry locations.
- **Pantry-aware recipe matching**: every recipe shows a "you have X of Y ingredients" indicator; a "cook from what we have" browse mode.

### 3.5 Meal Planning & Shopping

- **Weekly meal plan board** — assign recipes (or leftovers/eating-out placeholders) to days. Drag-drop on desktop, tap-assign on mobile. Structured household constraints include allergies/hard exclusions, dislikes, dietary preferences, available equipment, weekday time limits, and desired leftovers.
- **AI-assisted plan building** (see 3.6) seeded from pantry contents, tier balance ("3 meal-prep + 1 family nice meal"), and time budgets per weekday.
- **Shopping list** — generated from (meal plan ingredients) − (pantry on hand) + (low staples); deterministically aggregated across recipes; organized by aisle/category; manual add; one-tap copy/share and optional Apple Shortcut export to a native list. Custom offline reconciliation is post-v1.
- **Big-event mode** — plan a multi-dish menu for N guests: the AI proposes recipe IDs and a schedule; application code scales quantities and builds the combined list. The plan accounts for batch limits, oven/burner capacity, make-ahead suitability, holding, and storage—not only linear serving arithmetic.

### 3.6 AI Integration

A first-class assistant with access to cookbook and pantry tools, backed by **Anthropic (Claude) and/or OpenAI**. The owner selects a provider/model per task. V1 does not automatically cross providers during a failed tool loop; fallback is enabled later only after both adapters pass identical contract and idempotency tests:
- **Extraction**: video/webpage → structured recipe JSON (the ingest bot).
- **TLDR generation** and time estimation at ingest.
- **Chat**: "What can I make in under 30 minutes with what we have?", "Build me a meal plan for next week, two vegetarian nights", "What should I make for 8 guests Saturday — impressive but mostly make-ahead?"
- **Actions from chat**: the assistant can *propose* structured artifacts (a meal plan, a shopping list, a scaled menu) that the user accepts with one tap — never silently mutate data.
- **Deterministic authority**: the model selects and explains; application services own recipe filtering, quantity math, unit conversion, pantry deduction, shopping aggregation, and writes.
- **Recipe Q&A while cooking**: "can I substitute crème fraîche?", asked from within a recipe's cook mode with the recipe as context.
- Cost posture: cheap fast model for extraction/TLDR; smarter model for planning chat; optional first-party embeddings (OpenAI) for semantic search. Expected usage is low (a family), so monthly API cost should be trivial — but track it per provider, with a spend cap.

## 4. Non-Functional Requirements

- **Idle-light**: near-zero CPU at idle on the N95; total RSS well under ~500 MB idle. Burst CPU during ingest/AI calls is fine.
- **Fast**: server on LAN, page interactions should feel instant (<100 ms server responses for reads); no heavyweight client framework payloads on every page.
- **Responsive & beautiful**: one web app, mobile-first layouts that scale up to desktop; installable as a PWA (home-screen icon, standalone chrome). Dark mode. It should look like a product you'd pay for, not an admin panel.
- **Data is sacred**: SQLite with WAL + automated backups to a separate physical device. Backups cover the database, uploaded originals, recipe images, and ingestion artifacts; integrity checks and a restore drill are mandatory. Export everything (JSON + printable/markdown).
- **Simple ops**: two systemd units, auto-start on boot, painless updates. **Zero infrastructure cost**: no domain, no certificates, no cloud services — the only recurring cost is AI API usage (Anthropic and/or OpenAI, ~$3–8/month).
- **No cloud dependency for core browsing**: if the internet is down, browsing/cooking/pantry still work; only ingestion and AI chat need the network.

## 5. Explicitly Out of Scope (for now)

- Public multi-tenant hosting, federation, or accounts for people outside the household.
- Nutrition tracking / calorie math (schema leaves room; not a launch feature).
- Grocery-store price integration / online grocery ordering.
- Native iOS/Android apps — the PWA + Shortcuts covers it.
- Custom multi-device offline shopping synchronization — start with native copy/share/export and add sync only if real use justifies it.
- Semantic vector search before FTS5 has demonstrably failed real household queries.

## 6. Success Criteria

1. Sharing a YouTube video from an iPhone produces a correct, pretty recipe sheet in under ~a minute with zero further input.
2. A family member with no instructions can find a recipe and cook from their phone comfortably.
3. The pantry stays roughly accurate after a month of real use *because* updating it is nearly effortless.
4. "Plan next week's dinners" via AI produces a usable plan + shopping list in one short chat.
5. The N95 stays quiet: idle load unnoticeable next to whatever else the box runs.
