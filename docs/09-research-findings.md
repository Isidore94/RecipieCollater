# RecipeCollater — Competitive & Technical Research Findings

Condensed from a deep research pass (July 2026) across self-hosted recipe managers, pantry apps, commercial recipe apps, extraction tooling, and platform constraints. Full citations were gathered during planning; the load-bearing facts are recorded here so the builder agent doesn't re-litigate them.

## 1. The landscape, in one table

| Tool | What it proves | What it's missing (our opening) |
|---|---|---|
| **Mealie** (FastAPI+Vue, ~12.7k★) | URL import via recipe-scrapers + LLM fallback works; video import via yt-dlp shipped 2026; iOS-Shortcut-to-API ingest is the standard family flow; SQLite fine to ~20 users | No pantry at all; ingredient parsing is a hated manual chore; scaling is integer-multiplier only; 400 MB idle; ~10s first load (heavy SPA); no AI chat/planning |
| **Tandoor** (Django+Vue, ~8.5k★) | Step-linked ingredients, aisle-sorted shopping lists, keyword organization, import preview; AI cost caps + usage log | No real inventory (boolean "on hand" per food); units have no dimensions (their #1 open regret); power-user density scares families; Postgres+Redis+node stack idles ~300 MB |
| **Grocy** (PHP+SQLite) | The complete pantry data model: locations, min-stock, purchase/consume/inventory/transfer, recipe fulfillment checking, Due Score | Families abandon it within weeks — consumption logging is unsustainable, master-data setup is heavy. The model is right; the ergonomics are fatal |
| **KitchenOwl** (Flask+Flutter) | The ergonomic bar for family shopping lists: real-time sync, offline outbox replay, big tap targets; native apps won families that Grocy lost | Deliberately has no inventory, no AI, weak importer |
| **Paprika 3** | Cook mode (wake lock, tap-time→timer), pantry-staples-excluded-from-lists, ~95% scrape accuracy, beloved one-time pricing | No video import, no AI, closed |
| **Crouton / Mela / ReciMe / Pestle** | Share-sheet video import UX; Mela's RSS "inbox → review → save" flow validates our Test Recipes tab; Crouton's scale-by-anchor-ingredient | Caption-only parsing fails on video-only recipes; AI import universally paywalled ($50–60/yr) |
| **Preplo / Nutrola** (AI video apps) | Multi-signal extraction (transcript+description+OCR); **per-step video timestamps deep-linking into an embedded player in cook mode** — the standout pattern we copy | Subscription products, no self-hosting, no pantry |
| **Samsung Food** | AI meal plans + pantry-aware search at scale | Pantry has no quantities (top user complaint) — validates our quantity+location model |
| **Cooklist** | Automation is the only thing that makes full inventory work (auto-import from grocery loyalty accounts) | Closed, US-grocery-dependent; our equivalent automation is AI-assisted conversational updates |

**Positioning**: RecipeCollater = Mealie-quality ingestion + Grocy's data model with KitchenOwl's ergonomics + Paprika's cook mode + an AI layer none of them have (pantry-aware planning chat, conversational stock updates), on a footprint ~10× lighter than Mealie.

## 2. Load-bearing technical facts

**Extraction**
- `recipe-scrapers` v15+ (~650 sites, MIT, monthly releases) is parsing-only: fetch HTML yourself with httpx; on 403 retry via `curl_cffi` `impersonate="chrome"` (TLS-fingerprint blocks). 15s timeout, 5 MB cap.
- schema.org/Recipe polymorphism is real: instructions come as text | [text] | HowToStep[] | HowToSection[]; durations are ISO-8601; `@graph` nesting is common. The library normalizes all of it.
- `ingredient-parser-nlp` v2.7 (CRF, MIT, 4 MB, 95.6% sentence accuracy, ms per line) is the only maintained local ingredient parser; returns per-field confidence + pint units; optional USDA FDC matching.
- Client-side page capture (Shortcut runs JS in Safari, POSTs the DOM) bypasses every bot wall with zero server infrastructure — a home-LAN superpower commercial tools can't use. Keep headless Chromium out of the stack.

**YouTube**
- yt-dlp `extract_info(download=False)` returns description, chapters, thumbnails, and (with extractor-args) top/pinned comments in one call. Captions via `youtube-transcript-api` (<1s, prefers manual tracks) or yt-dlp subs — different endpoints, keep both.
- **Never local Whisper for YouTube** (captions already exist; N95 CPU is far sub-realtime beyond tiny models). Whisper only becomes relevant if TikTok/Instagram support is added.
- **Datacenter IPs are blocked by ASN on the first request; home residential IPs are fine at family volume.** Ingestion must run on the N95, never a VPS.
- yt-dlp breaks every few weeks — weekly auto-update task, version visible in admin.

**Platform**
- iOS Web Share Target: never implemented — Apple Shortcut is the only iOS share path.
- Android share target: URL arrives in `text` not `url`; PWA must be installed from Chrome over trusted HTTPS.
- Wake Lock API: iOS Safari 16.4+, but broken inside installed PWAs until iOS 18.4 — try/catch + re-acquire on visibilitychange.
- iOS 17.2+ copies Safari cookies into a newly installed home-screen PWA once at install — magic-link onboarding works natively. Installed PWAs are exempt from the 7-day storage eviction. WebKit bug 272325: never mix session + persistent cookies (use exactly one persistent cookie).
- Background Sync API: still unsupported on iOS — offline outbox must be hand-rolled and flushed from page code (mutation, `online`, `visibilitychange`, `pageshow`, 15s visible-interval).

**Stack / N95**
- Idle RSS: Go ~10–25 MB, FastAPI single worker ~60–130 MB, Django ~243 MB, Next.js 200–500 MB, Mealie ~400 MB (dependency bloat, not framework). Python wins on the ingestion ecosystem (recipe-scrapers, yt-dlp, ingredient-parser are all Python; anything else shells out to them anyway).
- dockerd/containerd add ~100 MB RSS and measured ~2 W idle wakeups on a ~5 W-idle box — bare systemd units are the right deployment.
- SQLite pragmas: WAL, busy_timeout=5000, synchronous=NORMAL, foreign_keys=ON per connection. FTS5 external-content tables + triggers. `VACUUM INTO` for online snapshots; Litestream optional.
- Huey + SqliteHuey: durable job queue + cron-style periodic tasks in one extra process, no Redis.
- Local LLM on N95: confirmed non-viable (~1 tok/s with context on a 1.5B model). Local embeddings viable: fastembed ONNX all-MiniLM-L6-v2 (no PyTorch).

**AI (verified pricing, July 2026)**
- Haiku 4.5 $1/$5 per MTok (extraction ≈ $0.01–0.03/recipe); Sonnet 4.6 $3/$15 (chat ≈ $0.02–0.05/turn with caching). Family total ≈ **$3–8/month**. Batches API 50% off for backfills.
- Structured outputs (`messages.parse()` + Pydantic) replace prompt-engineered JSON; `additionalProperties: false` required; no numeric constraints in schema.
- Prompt caching: prefix-exact, min 4096 tokens (Haiku)/2048 (Sonnet); order [stable system+tools][data snapshot][conversation].
- Anthropic has no embeddings API (recommends Voyage; voyage-3.5-lite $0.02/1M, first 200M tokens free).

**Seed data**
- TandoorRecipes/open-tandoor-data: 391 FDC-linked foods + 163 sourced per-food conversions + 14 dimension-typed units — a ready-made miniature of our ontology (ODbL/DBCL).
- Mealie en-US foods (2,689 names) for vocabulary; USDA FDC food_portion.csv (public domain) for gram weights; FAO/INFOODS for densities. Skip OpenFoodFacts (barcode-product granularity, share-alike).
- Don't build app math on pint (US-liquid cups, no pinch/dash, count-unit pain) — a ~30-row units table with static factors is deterministic and sufficient.

## 3. Feature ideas adopted from research (beyond the original spec)

1. **Per-step video timestamps** with embedded player tap-to-seek in cook mode (Preplo).
2. **Tap-a-duration-in-step-text → named timer**; multiple concurrent timers (Paprika/Crouton).
3. **After-cook capture flow** as the inbox→cookbook promotion gate (composite of the most-requested missing features across Paprika/Samsung Food).
4. **Cook-through pantry deduction** — cooking is the only consumption event (the one pattern families sustain).
5. **Graduated pantry granularity** (exact/gauge/binary) instead of forced quantities.
6. **Conversational pantry updates** via AI ("used half the rice, out of eggs") — automation is the documented fix for inventory decay.
7. **"Use it up" suggestions** ranked by expiring stock (Grocy's Due Score).
8. **Scale-by-anchor-ingredient** ("I only have 300 g of mince — rescale everything else", Crouton) — backlog.
9. **Saved menus** (reusable week templates, Plan to Eat).
10. **Leftovers as first-class plan entries** (gap in Samsung Food).
11. **Per-entry servings on meal plans** so guests scale shopping math (top Mealie complaint).
12. **Import preview/quarantine** of new foods/units (Tandoor's #1855, never shipped there).
13. **Duplicate detection at ingest** (missing in Mealie, users ask).
14. **Recipe export as JSON/Markdown + printable sheet** (data-portability complaint against Mealie).
15. **Magic-link/QR device onboarding with per-device revocation** (Immich's 400-day cookie pattern) — no passwords for the family, ever.
16. **AI spend cap + usage log** (Tandoor's guardrail).
17. **Re-extract button** — raw source stored forever means better prompts later improve old recipes without re-fetching.
18. **"What the bot saw" debug view** — trust through transparency when extraction goes wrong.
