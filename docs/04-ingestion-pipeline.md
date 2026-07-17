# RecipeCollater — Ingestion Pipeline ("the bot")

The intake side must feel like magic: share a link from any device, and a minute later a clean recipe sheet is sitting in the Test Recipes inbox. This document specifies every entry point and the extraction pipeline behind them.

## 1. Entry points

### 1.1 iPhone / iPad — Apple Shortcut (the canonical family flow)

iOS PWAs **cannot** register as share targets (WebKit has never implemented Web Share Target — bug 194593). The Shortcut *is* the iOS path, and it's proven (Mealie/Tandoor communities use exactly this):

- Shortcut settings: "Show in Share Sheet", accepts **URLs** (and Safari web pages).
- One core action — **Get Contents of URL**:
  - `POST https://<server>/api/ingest`
  - Header `Authorization: Bearer <api_token>`
  - JSON body `{"url": <Shortcut Input>}`
- Followed by **Show Notification** ("Recipe queued ✓").
- Distributed to family as a single iCloud link, with **Import Questions** prompting for server URL + personal token on install.
- Works from the YouTube app, Safari, Chrome — anything that shares a URL.

Hard constraints this imposes:
- **`POST /api/ingest` must return `202 Accepted` + job id in <2s.** Get Contents of URL times out around 25–30s; a YouTube-transcript-plus-LLM parse takes longer. All parsing is async in the worker.
- First LAN request triggers iOS's one-time **Local Network permission** prompt for Shortcuts — the setup guide must say "tap Allow".

v2 enhancement: a second Shortcut variant that runs JavaScript-in-Safari to capture the rendered page HTML and POSTs `{"url", "html"}` — this defeats every bot-wall for free because the fetch happened in the user's real browser session (Mealie's newer shortcut does this).

### 1.2 Android — PWA Web Share Target

Installed PWA (Chrome, HTTPS with a trusted cert required) registers via the manifest:

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
- **Bookmarklet** (generated on the settings page): `javascript:window.open('https://<server>/new?url='+encodeURIComponent(document.URL))`.
- **Drag-and-drop** of a link onto the inbox page (`text/uri-list`).
- No browser extension — maintenance burden without real gain (Mealie's community reached the same conclusion).

### 1.4 Manual entry & photo import

- Structured "New recipe" form (same fields as the editor).
- Photo import (phase 2+): upload a photo of a cookbook page / recipe card → Claude vision → same extraction schema. No local OCR — Mealie removed Tesseract as unmaintainable; vision models replaced it.

## 2. Job lifecycle

```
POST /api/ingest {url[, html]}  ──►  202 {job_id}
        │
        ▼ (Huey worker, SQLite-backed queue)
  ingest_jobs: queued → fetching → extracting → done | failed
        │
        ▼
  recipes row (status='inbox') + images on disk + raw_extraction JSON
```

- The inbox page shows live processing state (htmx polling every ~2s on in-flight jobs only — zero idle traffic).
- Failures are visible in the inbox with a human-readable reason ("video has no captions or description recipe"; "site blocked us — try sharing from your phone with page capture") and a **Retry** button.
- Duplicate check on submit: normalized `source_url` (strip tracking params; canonicalize `youtu.be` → `youtube.com/watch`) already in DB ⇒ inline warning with a link to the existing recipe, and an override.

## 3. Web recipe extraction

Cascading strategy chain (Mealie's proven architecture, copied deliberately):

1. **Fetch** (skip if client supplied `html`): `httpx` with real-browser headers, 15s timeout, 5 MB size cap. On 403/challenge → retry with `curl_cffi` `impersonate="chrome"` (defeats TLS/JA3 fingerprinting; Mealie ships exactly this). Still blocked → fail with the "share from your phone" hint.
2. **Fast path — `recipe-scrapers`** (v15+, ~650 site scrapers + generic schema.org fallback): `scrape_html(html, url, supported_only=False)`. Accept iff ingredients AND instructions are non-empty. Handles all the schema.org polymorphism (ISO-8601 durations, `@graph` nesting, HowToStep/HowToSection). Keep `to_json()` in `raw_extraction`. Catch its typed exceptions (`NoSchemaFoundInWildMode` etc.) to fall through cleanly.
3. **LLM fallback — Claude Haiku**: strip HTML to readable text (BeautifulSoup/trafilatura), append any partial JSON-LD fragments + largest og:image URL → structured-output extraction (schema below), temperature 0.
4. **Last resort — OpenGraph stub**: title + image only, recipe lands in inbox flagged "needs manual completion".

## 4. YouTube extraction

**Description-first, transcript-second, vision-last** — and never local Whisper (YouTube already ran ASR; captions fetch in <1s, while Whisper-small on an N95 runs slower than realtime).

1. **One `yt-dlp` metadata call** — `YoutubeDL({'skip_download': True, 'getcomments': True}).extract_info(url)` with extractor-args `youtube:comment_sort=top;max_comments=20,20,0`:
   - `description` (many channels publish the full recipe here)
   - `chapters` (often step markers → seed `video_seconds` per step)
   - top comments — check `is_pinned` / `author_is_uploader` (creators pin recipes)
   - `title`, `uploader`, `duration`, `thumbnail`
2. **Captions**: `youtube-transcript-api` (prefers manual over auto tracks) with yt-dlp subtitle fetch as fallback — the two use different endpoints, so one usually survives when YouTube breaks the other. Use vtt/srt only (json3/srv* formats have documented bugs). Auto-captions have no punctuation and spelled-out quantities ("half a cup") — pass raw; the LLM normalizes reliably.
3. **One Claude call** structures `description + transcript + pinned comment + chapters` into the recipe schema. The prompt asks for per-step `video_seconds` when chapters/transcript timing allow — this powers tap-to-seek in cook mode.
4. **No-text videos** (rare: silent/on-screen-text cooking clips): flag the job "thin extraction — review me" in v1. Later phase: frame sampling (yt-dlp lowest-quality download → ffmpeg scene-change frames → Claude vision).
5. **Thumbnail**: fetch `i.ytimg.com/vi/<id>/maxresdefault.jpg` (fallback `hqdefault.jpg`), store locally — never hotlink.

Operational notes (load-bearing):
- **The home residential IP is an architectural asset.** YouTube blocks datacenter ASNs on the first request; home IPs are fine at family volume. Never route ingestion through a VPS or VPN egress.
- **yt-dlp is a hot dependency** — YouTube breaks it every few weeks. A weekly Huey periodic task runs `pip install -U yt-dlp` (with a pinned-floor version); surface the installed version on the admin page.
- Add `sleep_requests: 1` politeness. PO-token plumbing is unnecessary for metadata+captions from a home IP; if "Sign in to confirm you're not a bot" ever appears, `bgutil-ytdlp-pot-provider` is the documented escape hatch.

## 5. The extraction schema (one shape for all sources)

Claude structured outputs (`client.messages.parse()` with a Pydantic model — guaranteed schema-valid JSON, no prompt-engineered parsing):

```python
class ExtractedIngredient(BaseModel):
    original_text: str          # as written/spoken
    quantity: float | None
    unit: str | None            # free text here; normalized to units table downstream
    food: str                   # 'garlic'
    note: str | None            # 'minced'
    section: str | None         # 'For the sauce'
    scalable: bool              # False for 'to taste', 'for frying'

class ExtractedStep(BaseModel):
    instruction: str
    section: str | None
    minutes: int | None
    video_seconds: int | None   # YouTube only

class ExtractedRecipe(BaseModel):
    title: str
    tldr: str                   # 1–3 sentences, plain language, the whole method
    description: str | None
    base_servings: float        # default 4 if the source doesn't say
    servings_text: str | None
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None
    ingredients: list[ExtractedIngredient]
    steps: list[ExtractedStep]
    tags: list[str]             # cuisine, protein, course, equipment
    tier_guess: Literal['meal_prep','family','company'] | None
    confidence: Literal['high','medium','thin']   # 'thin' ⇒ inbox flags "review me"
```

The TLDR prompt instruction matches the product spec: *"Summarize the whole method in 1–3 casual sentences, e.g. 'Cut some aromatics, cook them down with some chilies, blend down some tomatoes, throw them in, reduce, then add butter and parmesan and serve with boiled pasta.'"*

## 6. Ingredient normalization (deterministic-first, LLM-second)

After extraction, every ingredient line is resolved against the foods/units vocabulary:

1. **Parse** each `original_text` with `ingredient-parser-nlp` (CRF, 95.6% sentence accuracy, milliseconds per line, MIT) → quantity/unit/food/prep + per-field confidence.
2. **Match** food & unit (Mealie's DataMatcher pattern): normalize (lowercase, unidecode) → exact match over names+plurals+aliases → `rapidfuzz.process.extractOne(fuzz.ratio)` with cutoff 85 (foods) / 70 (units). Require exact match for tokens ≤4 chars ("rice" vs "ice" scores 86!).
3. **LLM repair**: lines with parse confidence <0.8 plus all unmatched foods go into ONE batched Claude call, which must either pick from the top-10 fuzzy candidates or propose a new food entry. Proposed names are re-run through step 2 before insert (so "green onion" can never fork from "scallion").
4. **Quarantine**: LLM-proposed new foods insert with `status='pending'` and show as one-tap confirm/merge chips on the recipe's inbox card. Vocabulary hygiene is enforced at the boundary, not by cleanup.

This whole chain runs automatically at ingest — Mealie makes ingredient parsing a manual per-recipe chore and it is their single most complained-about flaw.

## 7. Post-extraction

- Generate WEBP image sizes (2048 / 1024 / 300 center-crop) into `data/images/<recipe_id>/`.
- Index into FTS5 (and queue embedding computation, once vectors ship).
- Recipe appears in the inbox with: hero image, TLDR, time, servings, tier guess, source badge (YouTube channel / site favicon), "who shared it" (from the api_token's user), and any pending-food confirmation chips.
