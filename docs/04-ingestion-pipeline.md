# RecipeCollater — Ingestion Pipeline ("the bot")

The intake side must feel like magic: share a link from any device, and a minute later a clean recipe sheet is sitting in the Test Recipes inbox. This document specifies every entry point and the extraction pipeline behind them.

## 1. Entry points

### 1.1 iPhone / iPad — Apple Shortcut (the canonical family flow)

iOS PWAs **cannot** register as share targets (WebKit has never implemented Web Share Target — bug 194593). The Shortcut *is* the iOS path, and it's proven (Mealie/Tandoor communities use exactly this):

- Shortcut settings: "Show in Share Sheet", accepts **URLs** (and Safari web pages).
- One core action — **Get Contents of URL**:
  - `POST <APP_BASE_URL>/api/ingest` (default `http://recipes.local/api/ingest`)
  - Header `Authorization: Bearer <api_token>`
  - JSON body `{"url": <Shortcut Input>}`
- Followed by **Show Notification** ("Recipe queued ✓").
- Distributed to family as a single iCloud link, with **Import Questions** prompting for server URL + personal token on install.
- Works from the YouTube app, Safari, Chrome — anything that shares a URL.

Hard constraints this imposes:
- **`POST /api/ingest` must return `202 Accepted` + job id in <2s.** Get Contents of URL times out around 25–30s; a YouTube-transcript-plus-LLM parse takes longer. All parsing is async in the worker.
- First LAN request triggers iOS's one-time **Local Network permission** prompt for Shortcuts — the setup guide must say "tap Allow".

**Ship in v1, not backlog**: a second Shortcut variant that runs JavaScript-in-Safari to capture the rendered page HTML and POSTs `{"url", "html"}` — this defeats every bot-wall for free because the fetch happened in the user's real browser session (Mealie's newer shortcut does this). The design panel's family-UX judge flagged a failed share-from-Safari as the single highest-friction moment the family will hit; don't defer the fix.

### 1.2 Android — PWA Web Share Target (dormant — optional HTTPS upgrade required)

The household is iPhone + PC, and the zero-cost LAN setup runs plain HTTP, so this path ships **disabled by default**: Android's share target requires an installed PWA served over trusted HTTPS. Android users on the LAN still have the paste box like any browser. If Android + the free mkcert upgrade (see `02-architecture.md` §9) ever land, the installed PWA (Chrome) registers via the manifest:

```json
"share_target": {
  "action": "/share",
  "method": "GET",
  "params": { "title": "title", "text": "text", "url": "url" }
}
```

Known quirk to handle: Android's share system usually delivers the URL in **`text`** (occasionally `title`), not `url` — the `/share` route must coalesce `url || text || title` and regex out the first URL. The route is a normal cookie-authenticated page: it queues the job, shows "Recipe queued", and deep-links to the inbox. Expect to fully uninstall/reinstall the PWA after manifest changes when testing (Android caches manifests aggressively).

### 1.3 PC — paste box, bookmarklet, drag-and-drop

- **Paste box** at the top of the Test Recipes inbox — primary desktop flow, zero install.
- **Bookmarklet** generated from the configured `APP_BASE_URL`; the default opens `http://recipes.local/new?url=…`. Protocol/host are never duplicated in a template.
- **Drag-and-drop** of a link onto the inbox page (`text/uri-list`).
- No browser extension — maintenance burden without real gain (Mealie's community reached the same conclusion).

### 1.4 Manual entry & photo import

- Structured "New recipe" form (same fields as the editor).
- Photo import (later phase): upload a photo of a cookbook page / recipe card → configured vision-capable provider → same extraction schema. No local OCR in v1.

## 2. Job lifecycle

```
POST /api/ingest {url[, html]}  ──►  202 {job_id}
        │
        ▼ (Huey worker, SQLite-backed queue)
  ingest_jobs: queued → fetching → extracting → normalizing → done | failed
        │
        ▼
  immutable artifacts + extraction_run draft → accepted recipe row + images
```

- The inbox page shows live processing state (htmx polling every ~2s on in-flight jobs only — zero idle traffic).
- Each stage writes heartbeat/attempt metadata. Retrying a crashed or timed-out job is idempotent: a repeated request or worker replay cannot create a second recipe accidentally.
- Failures are categorized and visible with a human-readable reason and **Retry** button. Retry reuses already captured artifacts when possible.
- Supplied HTML, fetched HTML/JSON-LD, descriptions, captions, and any fetched comments are immutable compressed artifacts with hashes. Extraction runs record extractor/provider/model/prompt/schema versions.
- Duplicate check uses a normalized URL plus idempotency key. It warns and links to the existing recipe; "import another version" is an explicit override rather than an accidental race.

## 3. Web recipe extraction

Cascading strategy chain (Mealie's proven architecture, copied deliberately):

1. **Fetch** (skip if client supplied `html`): `httpx` with real-browser headers, 15s timeout, 5 MB size cap. Before the initial request and every redirect, resolve and reject loopback/private/link-local/multicast/metadata-service addresses across IPv4 and IPv6; re-check the connected peer where supported. On 403/challenge → retry with `curl_cffi` `impersonate="chrome"`. Still blocked → fail with the "share from your phone" hint.
2. **Fast path — `recipe-scrapers`**: `scrape_html(html, url, supported_only=False)`. Accept iff ingredients AND instructions are non-empty. Preserve `to_json()` as an immutable extraction artifact. Catch typed exceptions and fall through cleanly.
3. **LLM fallback**: strip HTML to readable text (BeautifulSoup/trafilatura), append partial JSON-LD fragments + largest og:image URL, then call the configured extraction provider through the adapter with a strict structured schema.
4. **Last resort — OpenGraph stub**: title + image only, recipe lands in inbox flagged "needs manual completion".

## 4. YouTube extraction

**Description-first, transcript-second, vision-last** — and never local Whisper (YouTube already ran ASR; captions fetch in <1s, while Whisper-small on an N95 runs slower than realtime).

1. **One lightweight `yt-dlp` metadata call** — `YoutubeDL({'skip_download': True}).extract_info(url)`:
   - `description` (many channels publish the full recipe here)
   - `chapters` (often step markers → seed `video_seconds` per step)
   - `title`, `uploader`, `duration`, `thumbnail`
2. **Captions**: `youtube-transcript-api` (prefers manual over auto tracks) with yt-dlp subtitle fetch as fallback — the two use different endpoints, so one usually survives when YouTube breaks the other. Use vtt/srt only (json3/srv* formats have documented bugs). Auto-captions have no punctuation and spelled-out quantities ("half a cup") — pass raw; the LLM normalizes reliably.
3. **Conditional comments fallback**: only when description + captions are thin, make a second metadata request for a small number of top comments and prefer pinned/uploader-authored text. Comments are not fetched for every video.
4. **One configured-provider call** structures description + transcript + chapters (+ conditional pinned comment) into the recipe schema. The prompt asks for per-step `video_seconds` when timing allows.
5. **No-text videos**: flag "thin extraction — review me" in v1. Later: frame sampling → configured vision provider.
6. **Thumbnail**: fetch `i.ytimg.com/vi/<id>/maxresdefault.jpg` (fallback `hqdefault.jpg`), store locally—never hotlink.

Operational notes (load-bearing):
- **The home residential IP is an architectural asset.** YouTube blocks datacenter ASNs on the first request; home IPs are fine at family volume. Never route ingestion through a VPS or VPN egress.
- **yt-dlp is a hot dependency**. A weekly task checks and reports the latest compatible version; it never modifies the live environment. Updating uses the staged install, smoke-test, health-check, and rollback path in `02-architecture.md` §10.
- Add `sleep_requests: 1` politeness. PO-token plumbing is unnecessary for metadata+captions from a home IP; if "Sign in to confirm you're not a bot" ever appears, `bgutil-ytdlp-pot-provider` is the documented escape hatch.

## 5. The extraction schema (one shape for all sources)

The provider adapter returns a Pydantic-validated object. Anthropic and OpenAI have different wire formats; those remain inside their adapters. Schema validity does not imply factual correctness, so normalization and user review still apply.

```python
class ExtractedIngredient(BaseModel):
    original_text: str          # as written/spoken
    quantity: str | None       # decimal string; never a binary float
    unit: str | None            # free text here; normalized to units table downstream
    food: str                   # 'garlic'
    note: str | None            # 'minced'
    section: str | None         # 'For the sauce'
    scaling_mode: Literal['linear','fixed','to_taste','round_to_package']
    package_quantity: str | None
    package_unit: str | None

class ExtractedStep(BaseModel):
    instruction: str
    section: str | None
    minutes: int | None
    video_seconds: int | None   # YouTube only

class ExtractedRecipe(BaseModel):
    title: str
    tldr: str                   # 1–3 sentences, plain language, the whole method
    description: str | None
    base_servings: str          # exact decimal string; default 4 if absent
    servings_text: str | None
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None
    active_minutes: int | None
    elapsed_minutes: int | None
    ingredients: list[ExtractedIngredient]
    steps: list[ExtractedStep]
    tags: list[str]             # cuisine, protein, course, equipment
    tier_guess: Literal['meal_prep','family','company'] | None
    confidence: Literal['high','medium','thin']   # 'thin' ⇒ inbox flags "review me"
```

The TLDR prompt instruction matches the product spec: *"Summarize the whole method in 1–3 casual sentences, e.g. 'Cut some aromatics, cook them down with some chilies, blend down some tomatoes, throw them in, reduce, then add butter and parmesan and serve with boiled pasta.'"*

**Completeness (amended 2026-07-18):** a web extraction must produce both ingredients and steps to be saved. A **YouTube** extraction is accepted with ingredients alone — the dominant video format is an ingredient list in the description with the method spoken on camera, and the video itself carries the steps (the recipe keeps its link; cook mode still provides the ingredient checklist). The extraction prompt explicitly permits reconstructing steps from a spoken transcript (paraphrase is extraction, not invention), so steps are usually still produced when captions exist.

## 6. Ingredient normalization (deterministic-first, LLM-second)

After extraction, every ingredient line is resolved against the foods/units vocabulary:

1. **Parse** each `original_text` with `ingredient-parser-nlp` (CRF, 95.6% sentence accuracy, milliseconds per line, MIT) → quantity/unit/food/prep + per-field confidence.
2. **Match** food & unit (Mealie's DataMatcher pattern): normalize (lowercase, unidecode) → exact match over names+plurals+aliases → `rapidfuzz.process.extractOne(fuzz.ratio)` with cutoff 85 (foods) / 70 (units). Require exact match for tokens ≤4 chars ("rice" vs "ice" scores 86!).
3. **LLM repair**: low-confidence/unmatched lines go into one batched configured-provider call, which must either pick from top candidates or propose a new food. Proposed names are re-run through deterministic matching before insert.
4. **Quarantine**: LLM-proposed new foods insert with `status='pending'` and show as one-tap confirm/merge chips on the recipe's inbox card. Vocabulary hygiene is enforced at the boundary, not by cleanup.

This whole chain runs automatically at ingest — Mealie makes ingredient parsing a manual per-recipe chore and it is their single most complained-about flaw.

## 7. Post-extraction

- Generate WEBP image sizes (2048 / 1024 / 300 center-crop) into `data/images/<recipe_id>/`.
- Index into FTS5 (and queue embedding computation, once vectors ship).
- Recipe appears in the inbox with: hero image, TLDR, time, servings, tier guess, source badge (YouTube channel / site favicon), "who shared it" (from the api_token's user), and any pending-food confirmation chips.

## 8. Re-extraction and human edits

Re-extraction always creates a new `extraction_runs` draft from retained artifacts. The UI compares current and proposed title, times, servings, ingredients, steps, tags, and TLDR. Users accept individual fields or all changes; rejecting a run leaves the recipe untouched. Human edits, cook logs, pantry mappings, and ingredient deduction preferences are never overwritten implicitly.

## 9. Required ingestion tests

- URL normalization and concurrent duplicate submissions.
- Worker crash after artifact capture, after extraction, and after recipe insert; every retry remains idempotent.
- SSRF cases including redirects, DNS rebinding, IPv4-mapped IPv6, link-local, loopback, and metadata-service targets.
- Oversized/slow responses and decompression bombs.
- Recorded offline fixtures for JSON-LD, bot-wall HTML capture, captioned YouTube, thin YouTube, provider refusal/truncation, and schema-version migration.
