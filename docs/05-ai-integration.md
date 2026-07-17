# RecipeCollater — AI Integration

AI is a first-class feature, not a bolt-on: it powers ingestion (extraction, TLDR, normalization), the meal-planning assistant, and conversational pantry updates. Everything runs through a hosted API — **no local LLM** (measured llama.cpp on the N95-equivalent N100: a 1.5B model degrades to ~1 tok/s with modest context; a 10K-token transcript extraction would take tens of minutes). Local compute is used only for embeddings, and only optionally.

## 1. Two providers behind one interface

The app supports **both Anthropic (Claude) and OpenAI**, chosen per task in settings. Either key alone is enough to run everything; with both configured, each becomes the other's automatic fallback. This is the Mealie/Tandoor pattern (a single client with a configurable provider + per-feature flags), and it buys three things: resilience when one provider has an outage or you hit its spend cap, freedom to put the cheapest capable model on each task, and a first-party **embeddings** option (Anthropic has no embeddings API; OpenAI's `text-embedding-3-small` fills that gap for semantic search — see §5).

### Provider abstraction (`app/ai/`)

A thin interface with two implementations wrapping each vendor's native SDK — **not** a lowest-common-denominator hack. The three capabilities the app needs all exist on both providers, but their shapes differ and the adapter normalizes them:

| Capability | Anthropic | OpenAI | Normalized to |
|---|---|---|---|
| Structured extraction | `messages.parse()` + Pydantic / `output_config.format` json_schema | `responses`/`chat.completions` with `response_format` json_schema, `strict: true` | `extract(schema, prompt) -> validated Pydantic object` |
| Streaming chat + tools | `messages.stream()`, tool-use blocks, SDK tool runner | streaming `responses` with function calling | `stream_chat(messages, tools) -> event stream` |
| Embeddings | *(none — falls back to OpenAI or local)* | `embeddings.create()` | `embed(texts) -> list[vector]` |

Both structured-output paths require `additionalProperties: false` on every object and reject numeric min/max constraints — so the same Pydantic schema serializes to both, and quantity validation stays in app code either way. Tool/function-call JSON shapes differ; the adapter maps them to one internal `ToolCall` type. **Golden-file tests per provider** guard the two serializations (this is the one place a dual-provider design can silently drift — see the model/effort note in the build plan).

### Task routing (settings, not code)

Config maps each task → `provider/model`, with an optional fallback:

```
[ai.keys]
anthropic = "sk-ant-..."      # either or both
openai    = "sk-..."

[ai.routing]                  # provider/model  (+ optional fallback)
extract   = "anthropic/claude-haiku-4-5"      fallback "openai/gpt-..."
tldr      = "anthropic/claude-haiku-4-5"
repair    = "anthropic/claude-haiku-4-5"
chat      = "anthropic/claude-sonnet-4-6"     fallback "openai/gpt-..."
embed     = "openai/text-embedding-3-small"   # or "local/all-MiniLM-L6-v2" or "off"
```

Model IDs and pricing are **config, not code** — verify current IDs/prices at build time (OpenAI's especially, since this plan didn't research them fresh); the app must not hardcode a model string anywhere. Default routing favors Claude for extraction and reasoning (best structured-output reliability in the research) and OpenAI for embeddings; a user with only one key gets that provider for everything embeddings-capable and local/off embeddings otherwise.

### Cost posture & guardrails

| Task | Default model | Why |
|---|---|---|
| Recipe extraction, TLDR, ingredient repair | Claude Haiku (`claude-haiku-4-5`, $1/$5 per MTok) | Cheap, fast, 200K context swallows any transcript |
| Meal-planning chat / big-event planning | Claude Sonnet (`claude-sonnet-4-6` or Sonnet 5, $3/$15) | Needs real reasoning over pantry + cookbook |
| Embeddings (optional) | OpenAI `text-embedding-3-small` ($0.02/1M) | First-party vectors; Anthropic has none |
| One-off backfills | provider Batch API | ~50% off, async |

Realistic family usage (≈30 extractions + 100 chat turns/month): **$3–8/month**, whichever provider mix. Extraction ≈ $0.01–0.03/recipe even with a 15K-token transcript. Guardrails (Tandoor's pattern):
- Every call logged to `ai_usage_log` (provider, model, purpose, tokens, est. cost).
- Configurable monthly spend cap **per provider**; when exceeded, route falls to the other provider if configured, else ingestion drops to schema.org-only parsing and chat politely declines.
- Settings page shows month-to-date spend per provider.
- Per-feature and per-provider enable flags (Mealie's `*_ENABLE_*` switches).

## 2. Extraction (see `04-ingestion-pipeline.md`)

- **Structured outputs** via the provider abstraction's `extract(schema, prompt)` — guaranteed schema-valid JSON on either provider (Claude `messages.parse()` / OpenAI `response_format` strict json_schema). Schema rules that satisfy both: `additionalProperties: false` everywhere; no numeric min/max in the schema (validate quantities in app code).
- Temperature 0. Handle truncation/refusal stop reasons uniformly in the adapter.
- Both SDKs auto-retry 429/5xx — no custom retry code; the adapter adds the cross-provider fallback on hard failure or spend-cap trip.

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

SSE relay: browser POSTs the chat message → FastAPI `StreamingResponse(media_type="text/event-stream")` forwards text-delta chunks from the provider's stream (Claude `messages.stream()` or OpenAI streaming responses, normalized by the adapter to a single delta event) → browser reads via `fetch()` + ReadableStream (EventSource is GET-only). SSE over WebSocket deliberately: unidirectional, auto-reconnect, plain HTTP, trivial on LAN. API keys never leave the server.

### Prompt-caching gotchas (encode in the implementation)

- Exact-prefix matching in tools → system → messages order; keep the tool list byte-stable.
- Minimum cacheable prefix: 4096 tokens (Haiku 4.5) / 2048 (Sonnet 4.6) — below that caching silently no-ops.
- Reads 0.1×, writes 1.25× (5-min TTL). Order: [stable system+tools | bp1] [pantry/cookbook snapshot | bp2] [conversation] so a pantry edit only invalidates from bp2.
- Never interpolate timestamps/UUIDs into the system prompt.
- Verify hits via `usage.cache_read_input_tokens` (logged to `ai_usage_log`).

## 4. Recipe Q&A in cook mode

"Can I substitute crème fraîche?" asked from within a recipe: one Haiku call with the full recipe as context. Cheap, high-delight. Include the user's cook-log history for that recipe ("last time you noted it needed more salt").

## 5. Semantic search (phase 3+)

Three embedding sources, selected by the `embed` route:
- **OpenAI `text-embedding-3-small`** ($0.02/1M, 1536-dim) — the natural choice once an OpenAI key is present; near-free at this scale.
- **Local `all-MiniLM-L6-v2`** via **fastembed** (Qdrant's ONNX runtime lib — no PyTorch; ~150–300 MB RSS only while embedding, $0 and offline, 384-dim) — the zero-key, zero-cost path.
- **`off`** — FTS5 keyword search only.
- Vectors in **sqlite-vec** `vec0` table; brute-force KNN is sub-millisecond at family scale. Pin the pre-1.0 version; **store the embedding model name with each vector** so a provider/model swap triggers a clean re-embed (dimensions differ: 1536 vs 384).
- Embed `title + tldr + ingredient names + tags`; re-embed on edit via Huey.
- Honest scoping: FTS5 is probably sufficient below ~500 recipes — ship vectors only when "vibes" search ("something cozy and warming") is actually missed.

## 6. Failure & offline behavior

- No internet ⇒ browsing, cooking, pantry, shopping lists all work (no AI in the read path). Ingestion queues and retries; chat shows a friendly offline notice.
- Anthropic outage ⇒ same degradation. schema.org fast-path ingestion still works fully.
- All AI features individually toggleable in settings (Mealie's `OPENAI_ENABLE_*` pattern).
