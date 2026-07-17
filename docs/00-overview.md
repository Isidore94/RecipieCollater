# RecipeCollater — Plan Overview

*Planning completed July 2026, grounded in a multi-agent research pass over the self-hosted recipe ecosystem (Mealie, Tandoor, Grocy, KitchenOwl), commercial apps (Paprika, Crouton, Mela, ReciMe, Preplo, Samsung Food), extraction tooling, N95-class hardware benchmarks, Claude API capabilities, and iOS/Android PWA platform constraints. Key facts are recorded in `09-research-findings.md`.*

## The product in one paragraph

RecipeCollater is a family recipe platform on a home N95 mini PC. Anyone shares a YouTube video or recipe URL from their phone or PC; an AI pipeline builds a structured, beautiful recipe sheet (ingredients, steps, times, a plain-language TLDR of the method) into a Test Recipes inbox. Recipes the family likes get promoted — through an after-cook flow that captures the *real* cook time, what was *actually* used, and a rating — into the main cookbook, organized by tier (meal-prep / family / company) and tags. A pantry tab tracks what's on hand across user-defined locations with deliberately forgiving granularity. An AI assistant plans weeks, answers "what can I make in 30 minutes from what we have", builds big-event menus with prep timelines, and updates the pantry conversationally.

## Decision log

| # | Decision | Rationale anchor |
|---|---|---|
| D1 | FastAPI + Jinja2/htmx/Alpine + SQLite, single process each for web and worker, bare systemd | Ingestion libs are Python; htmx is the most LLM-buildable frontend; dockerd costs real idle watts on a 5 W box |
| D2 | Units carry dimensions + canonical g/ml factors; per-food conversion bridges; hand-rolled table, not pint | Tandoor's #1 regret; scaling/pantry/shopping math all depend on it |
| D3 | Ingredient parsing fully automatic at ingest (CRF parser → fuzzy matcher → batched LLM repair), new foods quarantined `pending` | Mealie's most-hated flaw + Tandoor's import-pollution complaint |
| D4 | Description-first YouTube pipeline (yt-dlp metadata → captions → one Claude call); no local Whisper; ingestion stays on the home IP | Captions already exist; datacenter IPs are ASN-blocked; N95 CPU can't ASR |
| D5 | **Two providers (Anthropic + OpenAI) behind one adapter**, routed per task with cross-provider fallback; default Haiku extraction / Sonnet chat / OpenAI embeddings; structured outputs; per-provider spend cap + usage log; no local LLM | Local generation measured non-viable (~1 tok/s w/ context); dual keys give resilience + first-party embeddings; either key alone suffices; $3–8/month realistic |
| D6 | iOS ingest = Apple Shortcut; Android = PWA share target; PC = paste box + bookmarklet; ingest endpoint returns 202 in <2s | iOS has no Web Share Target; Shortcuts time out ~25–30s |
| D7 | Auth = named users + 400-day device-session cookies via magic-link/QR + separate Bearer ingest tokens; no passwords | Immich/HA-proven; survives iOS PWA storage isolation via the 17.2+ cookie copy |
| D8 | Pantry: user-defined locations, graduated quantity modes, **cooking auto-deducts ingredients** (editable defaults, reversible), one-gesture remove/spoilage, stock-take mode, AI reconciliation | Grocy's documented failure modes are all consumption-side; auto-deduct-on-cook is the pattern families sustain |
| D9 | Shopping list is the app's one client-side island (outbox + LWW sync); everything else server-rendered | In-store dead spots are real; full sync engines are disproportionate |
| D10 | **$0 infrastructure, LAN-only**: plain HTTP at `recipes.local` (DHCP reservation + mDNS), uvicorn direct on port 80, no domain/certs/proxy/VPN; never port-forwarded | Owner requirement. Features needing a secure context get free fallbacks (silent-video screen-wake, copy-list-as-text); mkcert/Tailscale documented as optional free upgrades |
| D11 | AI writes only via proposal cards the user accepts | Trust: the assistant never silently mutates family data |
| D12 | Raw extraction payloads stored forever; re-extract without re-fetch; JSON/markdown export | Data portability + prompts improve over time |

## How to build from these docs

1. Read `01` (what) → `02` (how) → `03` (schema) in full before writing code.
2. Follow `08-roadmap.md` phase by phase; each phase has testable exit criteria — do not start phase N+1 with phase N's criteria red.
3. `04`–`07` are the per-subsystem specifications; consult them when a phase touches their area.
4. When a detail is unspecified, prefer: simpler, server-rendered, fewer dependencies, data preserved. When a spec conflicts with a measured reality, fix the spec in the same commit — these docs are living.

## Open questions (deliberately deferred, owner input wanted)

1. **Big-event thinking budget** — whether Sonnet with extended thinking or a plain call suffices for event menus; decide in Phase 5 with real examples.
2. **Nutrition** — schema carries `fdc_id` hooks; feature intentionally out of v1. Revisit if anyone actually wants it.
3. **TikTok/Instagram ingestion** — requires Whisper ASR (no caption API); revisit post-v1, possibly with a faster box or an ASR API.
4. **Barcode scanning** — polish, not core; the AI fuzzy matcher covers restock ergonomics first; also needs the optional HTTPS upgrade (camera API). Revisit after a month of pantry use.
5. **mkcert HTTPS upgrade** — free, one-time per device; only worth doing if the family misses the native wake-lock API, offline list re-opening, or Android share. Decide after living with v1.
