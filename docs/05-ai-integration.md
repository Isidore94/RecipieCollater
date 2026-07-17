# RecipeCollater — AI Integration

AI is a first-class feature, not a bolt-on: it powers ingestion (extraction, TLDR, normalization), the meal-planning assistant, and conversational pantry updates. Everything runs through the Claude API — **no local LLM** (measured llama.cpp on the N95-equivalent N100: a 1.5B model degrades to ~1 tok/s with modest context; a 10K-token transcript extraction would take tens of minutes). Local compute is used only for embeddings, later.

## 1. Model split & cost posture

| Task | Model | Why |
|---|---|---|
| Recipe extraction, TLDR, ingredient repair | Claude Haiku (current: `claude-haiku-4-5`, $1/$5 per MTok) | Cheap, fast, 200K context swallows any transcript |
| Meal-planning chat / big-event planning | Claude Sonnet (current: `claude-sonnet-4-6` or Sonnet 5, $3/$15) | Needs actual reasoning over pantry + cookbook |
| One-off backfills (re-extract/re-embed everything) | Batches API | 50% off, 24h async |

Realistic family usage (≈30 extractions + 100 chat turns/month): **$3–8/month**. Extraction ≈ $0.01–0.03/recipe even with a 15K-token transcript.

Guardrails (Tandoor's AI pattern, worth copying):
- Every call logged to `ai_usage_log` (purpose, model, tokens, est. cost).
- Configurable monthly spend cap; when exceeded, ingestion falls back to schema.org-only parsing and chat politely declines.
- Settings page shows month-to-date spend.
- Model IDs live in config, not code — models improve; swapping should be a settings edit.

## 2. Extraction (see `04-ingestion-pipeline.md`)

- **Structured outputs** (`client.messages.parse()` with Pydantic models / `output_config.format` json_schema) — guaranteed schema-valid JSON. Schema rules: `additionalProperties: false` everywhere; no numeric min/max in the schema (validate quantities in app code).
- Temperature 0. Check `stop_reason` for `max_tokens` (truncated) and `refusal`.
- The SDK auto-retries 429/5xx — no custom retry code.

## 3. Meal-planning assistant

### Architecture: hybrid context + small tool set

The family's data is small enough to *stuff the summary and tool the detail*:

- **Prompt-cached system block** contains:
  - Stable instructions + tool definitions (breakpoint 1 — never changes, always a cache hit)
  - **Pantry snapshot** (~1–2K tokens: every item with location, quantity/gauge, staple flag, expiry)
  - **Cookbook index** (~40–60 tokens/recipe: id, title, tier, total & our-kitchen minutes, TLDR, key ingredients, last-cooked date, rating) (breakpoint 2 — invalidated only when pantry/cookbook change)
- **Tools** (strict mode, via the SDK tool runner — don't hand-roll the loop):
  - `get_recipe_details(recipe_id)` — full ingredients/steps for scaling math
  - `search_recipes(query, max_minutes?, tier?)` — FTS5 (later hybrid with vectors)
  - `propose_meal_plan(entries)` — returns a structured plan the UI renders as an accept/edit card
  - `propose_shopping_list(items)` — same pattern
  - `update_pantry(changes)` — **only ever invoked after explicit user confirmation in the UI**

Most queries ("what can I make in under 30 minutes with what we have?") answer in a single call with zero tool round-trips because the index + pantry are already in context. Tool descriptions written prescriptively ("Call this when the user asks...") — it measurably improves trigger rates.

### The proposal pattern (AI never silently mutates data)

Assistant responses may include structured proposal blocks (a meal plan, a shopping list, a scaled event menu). The UI renders them as cards with **Accept / Edit / Dismiss**. Accepting writes to the real tables and stamps the plan "AI-drafted, accepted by <user>". This keeps trust high and mistakes cheap.

### Flagship prompts to design for

- "Build next week's dinners: 3 meal-prep, 2 family, leftovers Friday, nothing over 45 minutes on weekdays."
- "What can I make tonight in under 30 minutes from what we have?"
- "What should I cook before it goes bad?" (uses expiry + the Due-Score idea from Grocy)
- "Company dinner for 8 on Saturday — impressive, mostly make-ahead; build the menu, scale everything, combined shopping list, and a prep timeline." (big-event mode: Sonnet, possibly with extended thinking; output = menu + per-recipe scaled quantities + day-before/morning-of/last-hour timeline)
- "We just got groceries: 2 cans of tomatoes into the downstairs pantry, chicken thighs in the freezer" (conversational pantry update → `update_pantry` proposal — this is the automation that fixes the documented reason pantry tracking fails)

### Streaming

SSE relay: browser POSTs the chat message → FastAPI `StreamingResponse(media_type="text/event-stream")` forwards `text_delta` chunks from `client.messages.stream()` → browser reads via `fetch()` + ReadableStream (EventSource is GET-only). SSE over WebSocket deliberately: unidirectional, auto-reconnect, plain HTTP, trivial on LAN. API key never leaves the server.

### Prompt-caching gotchas (encode in the implementation)

- Exact-prefix matching in tools → system → messages order; keep the tool list byte-stable.
- Minimum cacheable prefix: 4096 tokens (Haiku 4.5) / 2048 (Sonnet 4.6) — below that caching silently no-ops.
- Reads 0.1×, writes 1.25× (5-min TTL). Order: [stable system+tools | bp1] [pantry/cookbook snapshot | bp2] [conversation] so a pantry edit only invalidates from bp2.
- Never interpolate timestamps/UUIDs into the system prompt.
- Verify hits via `usage.cache_read_input_tokens` (logged to `ai_usage_log`).

## 4. Recipe Q&A in cook mode

"Can I substitute crème fraîche?" asked from within a recipe: one Haiku call with the full recipe as context. Cheap, high-delight. Include the user's cook-log history for that recipe ("last time you noted it needed more salt").

## 5. Semantic search (phase 3+)

- **fastembed** (Qdrant's ONNX runtime lib — no PyTorch; ~150–300 MB RSS only while embedding, $0 and offline) running `all-MiniLM-L6-v2` (384-dim).
- Vectors in **sqlite-vec** `vec0` table; brute-force KNN is sub-millisecond at family scale. Pin the pre-1.0 version; store model name with vectors.
- Embed `title + tldr + ingredient names + tags`; re-embed on edit via Huey.
- Honest scoping: FTS5 keyword search is probably sufficient below ~500 recipes — ship vectors only when "vibes" search ("something cozy and warming") is actually missed. Alternative: Voyage API embeddings (Anthropic's recommended provider) — first 200M tokens free.

## 6. Failure & offline behavior

- No internet ⇒ browsing, cooking, pantry, shopping lists all work (no AI in the read path). Ingestion queues and retries; chat shows a friendly offline notice.
- Anthropic outage ⇒ same degradation. schema.org fast-path ingestion still works fully.
- All AI features individually toggleable in settings (Mealie's `OPENAI_ENABLE_*` pattern).
