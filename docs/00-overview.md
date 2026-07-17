# RecipeCollater — Plan Overview

*Planning completed July 2026, grounded in a multi-agent research pass over the self-hosted recipe ecosystem (Mealie, Tandoor, Grocy, KitchenOwl), commercial apps (Paprika, Crouton, Mela, ReciMe, Preplo, Samsung Food), extraction tooling, N95-class hardware benchmarks, Anthropic and OpenAI API capabilities, and iOS/Android PWA platform constraints. Key facts are recorded in `09-research-findings.md`.*

## The product in one paragraph

RecipeCollater is a family recipe platform on a home N95 mini PC. Anyone shares a YouTube video or recipe URL from their phone or PC; an AI pipeline builds a structured, beautiful recipe sheet (ingredients, steps, times, a plain-language TLDR of the method) into a Test Recipes inbox. Recipes the family likes get promoted — through an after-cook flow that captures the *real* cook time, what was *actually* used, and a rating — into the main cookbook, organized by tier (meal-prep / family / company) and tags. A pantry tab tracks what's on hand across user-defined locations with deliberately forgiving granularity. An AI assistant plans weeks, answers "what can I make in 30 minutes from what we have", builds big-event menus with prep timelines, and updates the pantry conversationally.

## Decision log

| # | Decision | Rationale anchor |
|---|---|---|
| D1 | FastAPI + Jinja2/htmx/Alpine + SQLite, single process each for web and worker, bare systemd | Ingestion libs are Python; htmx is the most LLM-buildable frontend; dockerd costs real idle watts on a 5 W box |
| D2 | Units carry dimensions + exact canonical integer factors (mg/µL/milli-each); all user-entered decimals are parsed with `Decimal`; generalized per-food unit bridges; hand-rolled table, not pint | Tandoor's #1 regret; scaling/pantry/shopping math must not accumulate binary floating-point drift |
| D3 | Ingredient parsing fully automatic at ingest (CRF parser → fuzzy matcher → batched LLM repair), new foods quarantined `pending` | Mealie's most-hated flaw + Tandoor's import-pollution complaint |
| D4 | Description-first YouTube pipeline (metadata → captions → one provider-neutral structured extraction call); comments fetched only when description/captions are thin; no local Whisper; ingestion stays on the home IP | Captions already exist; comments add latency and throttling risk; N95 CPU can't perform useful ASR |
| D5 | **Anthropic and OpenAI behind one small capability interface**, selected per task; no automatic cross-provider fallback in v1; OpenAI uses Responses with `store: false`; embeddings use a fixed 384-dimension contract | Either key can run the app without coupling the core to one vendor; deferring automatic failover avoids duplicate tool loops and hidden double-spend |
| D6 | iOS ingest = Apple Shortcut; Android = PWA share target; PC = paste box + bookmarklet; ingest endpoint returns 202 in <2s | iOS has no Web Share Target; Shortcuts time out ~25–30s |
| D7 | Auth = named users + 400-day device-session cookies via magic-link/QR + separate Bearer ingest tokens; no passwords | Immich/HA-proven; survives iOS PWA storage isolation via the 17.2+ cookie copy |
| D8 | Pantry: user-defined locations, graduated quantity modes, **review-first cook deductions that become auto-apply only after a recipe's mappings are trusted**, one-gesture remove/spoilage, stock-take mode, AI reconciliation | An incorrect silent deduction destroys trust faster than one lightweight confirmation; learned mappings remove repeat friction |
| D9 | V1 shopping is server-rendered with copy/share-as-text and a documented Apple Shortcut/native-list export; custom offline outbox/sync is post-v1 and built only if real use proves it necessary | The app is home-first; a sync protocol is disproportionate before the basic shopping workflow is validated |
| D10 | **$0 infrastructure, LAN-only**: plain HTTP at `recipes.local` (DHCP reservation + mDNS), uvicorn direct on port 80, no domain/certs/proxy/VPN; never port-forwarded | Owner requirement. Features needing a secure context get free fallbacks (silent-video screen-wake, copy-list-as-text); mkcert/Tailscale documented as optional free upgrades |
| D11 | AI writes only via proposal cards the user accepts | Trust: the assistant never silently mutates family data |
| D12 | Immutable ingestion artifacts and versioned extraction runs are retained; re-extraction creates a comparison draft and never overwrites family edits; JSON/markdown export remains first-class | Data portability + prompt improvements without destroying corrections or provenance |
| D13 | Migrations and tables land incrementally with the feature phase that owns them; no speculative full-final schema in phase 0 | Smaller reviewable changes, easier rollback, and less schema commitment before real use |
| D14 | AI proposes recipe IDs and structured artifacts; deterministic application services perform all scaling, conversion, pantry, shopping, and database mutation | Schema-valid model output is not necessarily numerically or factually correct |

## How to build from these docs

1. Read `01` (what) → `02` (how) → `03` (schema) in full before writing code.
2. Follow `08-roadmap.md` phase by phase; each phase has testable exit criteria — do not start phase N+1 with phase N's criteria red.
3. `04`–`07` are the per-subsystem specifications; consult them when a phase touches their area.
4. When a detail is unspecified, prefer: simpler, server-rendered, fewer dependencies, data preserved. When a spec conflicts with a measured reality, fix the spec in the same commit — these docs are living.

## Open questions (deliberately deferred, owner input wanted)

1. **Big-event reasoning budget** — which configured provider/model is sufficient for event menus; decide in Phase 5 with real examples rather than hardcoding a vendor.
2. **Nutrition** — schema carries `fdc_id` hooks; feature intentionally out of v1. Revisit if anyone actually wants it.
3. **TikTok/Instagram ingestion** — requires Whisper ASR (no caption API); revisit post-v1, possibly with a faster box or an ASR API.
4. **Barcode scanning** — polish, not core; the AI fuzzy matcher covers restock ergonomics first; also needs the optional HTTPS upgrade (camera API). Revisit after a month of pantry use.
5. **mkcert HTTPS upgrade** — free, one-time per device; only worth doing if the family misses the native wake-lock API, offline page re-opening, or Android share. Decide after living with v1.
6. **Offline shopping sync** — start with copy/share/native-list export. Add a localStorage outbox only if real shopping trips show the simpler path is inadequate.
