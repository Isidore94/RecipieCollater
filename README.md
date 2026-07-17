# RecipeCollater 🍳

A self-hosted family recipe platform for a home mini PC. Share a YouTube cooking video or a recipe webpage from any device — an AI pipeline parses it into a clean, interactive recipe sheet. Triage new finds in a **Test Recipes inbox**, promote keepers into the **family cookbook**, track what's on hand in a location-aware **pantry**, and let the built-in **AI assistant** turn cookbook + pantry into meal plans and shopping lists.

> **Status: planning complete — implementation not started.** The `docs/` folder is the full build specification, written to be executed by an AI coding agent (Claude Opus/Sonnet) phase by phase.

## What it does (when built)

- **Ingest anything**: YouTube videos (description + transcript → structured recipe), recipe websites (schema.org fast path + LLM fallback), manual entry, photos later. From iPhone via an Apple Shortcut share sheet, from Android via PWA share target, from PC via paste box.
- **Recipe sheets that cook with you**: TLDR method summary, serving scaler with correct fraction math, per-step video seek, tap-to-start timers, cook mode with screen wake-lock, after-cook capture (real time-to-make, what you actually used, rating).
- **A pantry that survives real life**: user-defined locations (kitchen cupboards, upstairs pantry fridge, downstairs pantry, downstairs freezer), graduated quantities (counts / full-half-low / have-out), **cooking a recipe auto-deducts its ingredients** (editable defaults, one-tap undo), one-gesture remove for things that spoil, 5-minute stock-take mode.
- **Planning**: week board, per-entry servings, saved menus, shopping lists with correct cross-recipe aggregation and aisle grouping, in-store offline tolerance, big-event mode with prep timelines.
- **AI throughout**: extraction, TLDRs, "what can I make in 30 minutes from what we have?", plan-my-week proposals you accept or edit, conversational pantry updates. Works with **Anthropic (Claude) and/or OpenAI** — supply one or both keys and choose which model runs each task; with both, each is the other's fallback.
- **Featherweight and free to run**: single FastAPI process + SQLite + server-rendered htmx UI on an Intel N95 — target <200 MB RSS idle, ~0% idle CPU, <1s page loads on LAN. **$0 infrastructure** (no domain, certs, or cloud services); the only recurring cost is AI API usage (~$3–8/month at family volume).

## Documentation map

| Doc | Contents |
|---|---|
| [`docs/00-overview.md`](docs/00-overview.md) | Executive summary, decision log, how to build from these docs |
| [`docs/01-product-requirements.md`](docs/01-product-requirements.md) | Vision, features, non-functional requirements, success criteria |
| [`docs/02-architecture.md`](docs/02-architecture.md) | Stack decisions, process model, repo layout, security, backups |
| [`docs/03-data-model.md`](docs/03-data-model.md) | Complete SQLite schema with rationale |
| [`docs/04-ingestion-pipeline.md`](docs/04-ingestion-pipeline.md) | Share flows, extraction cascade, YouTube pipeline, normalization |
| [`docs/05-ai-integration.md`](docs/05-ai-integration.md) | Model split, structured outputs, assistant tools, caching, costs |
| [`docs/06-pantry-shopping-mealplan.md`](docs/06-pantry-shopping-mealplan.md) | Pantry UX, shopping aggregation, meal planning, big-event mode |
| [`docs/07-ui-ux.md`](docs/07-ui-ux.md) | Design language, screens, PWA integration, performance budget |
| [`docs/08-roadmap.md`](docs/08-roadmap.md) | Six build phases with testable exit criteria |
| [`docs/09-research-findings.md`](docs/09-research-findings.md) | Competitive landscape + load-bearing technical facts |

## Target environment

Intel N95 mini PC (4 cores), Linux, Ethernet-connected on the home LAN. Family access from iPhones and PCs on the same Wi-Fi at `http://recipes.local` — LAN-only by design, never exposed to the internet. Python 3.12+, SQLite, no Docker, no Redis, no Postgres, no Node toolchain, no domain or certificates.
