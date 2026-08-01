# RecipeCollater 🍳

A self-hosted family recipe platform for a home mini PC. Share a YouTube cooking video or a recipe webpage from any device — an AI pipeline parses it into a clean, interactive recipe sheet. Triage new finds in a **Test Recipes inbox**, promote keepers into the **family cookbook**, track what's on hand in a location-aware **pantry**, and let the built-in **AI assistant** turn cookbook + pantry into meal plans and shopping lists.

> **Status: Phase 0 foundation implemented; N95/iPhone acceptance testing remains.** The app
> shell, migrations, device onboarding, scoped tokens, worker, backups, and staged deployment are
> built. Recipe features begin in Phase 1 only after the real-hardware Phase 0 checklist passes.

## What it does (when built)

- **Ingest anything**: YouTube videos (description + transcript → structured recipe), recipe websites (schema.org fast path + LLM fallback), manual entry, photos later. From iPhone via an Apple Shortcut, from any phone/PC via **Add a recipe** (`/add` — paste a link, drop a link, or one-click it with the bookmarklet), and optionally from Android's share target after a future trusted-HTTPS upgrade.
- **Recipe sheets that cook with you**: TLDR method summary, active vs elapsed time, serving scaler with deterministic decimal/fraction math and per-ingredient scaling behavior, per-step video seek, tap-to-start timers, cook mode with screen wake support, after-cook capture (real time-to-make, what you actually used, rating).
- **A pantry that survives real life**: user-defined locations (kitchen cupboards, upstairs pantry fridge, downstairs pantry, downstairs freezer), graduated quantities (counts / full-half-low / have-out), review-first cook-through deductions that learn trusted mappings per recipe, one-tap undo, one-gesture remove for things that spoil, and a 5-minute stock-take mode.
- **Planning**: week board, household constraints, per-entry servings, saved menus, shopping lists with deterministic cross-recipe aggregation and aisle grouping, one-tap text/native-list export, and big-event mode with batch and equipment-capacity-aware prep timelines.
- **AI throughout**: extraction, TLDRs, "what can I make in 30 minutes from what we have?", plan-my-week proposals you accept or edit, and conversational pantry updates. Works with **Anthropic (Claude) and/or OpenAI** — supply either or both keys and choose which provider/model runs each task. V1 does not automatically fail over mid-task; provider fallback is added only after both adapters pass the same contract tests.
- **Featherweight and free to host**: FastAPI + SQLite + server-rendered htmx UI on an Intel N95 — target <200 MB RSS idle, ~0% idle CPU, <1s page loads on LAN. **$0 infrastructure** (no domain, certificates, or hosted services); the only recurring cost is optional AI API usage, with current prices verified when models are configured.

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

## Phase 0 local development

Set `RC_SETUP_TOKEN` and allow the development client's host before first-run setup:

```sh
export RC_SETUP_TOKEN=local-development-only
export RC_ALLOWED_HOSTS=localhost,127.0.0.1
uv sync --frozen
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# In another terminal, so the default /healthz also proves worker liveness:
uv run huey_consumer app.tasks.huey -w 1 -k thread
```

Production installation generates a random setup token automatically. See `deploy/LAN.md`.
