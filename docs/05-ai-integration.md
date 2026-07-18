# RecipeCollater — AI Integration

AI is a first-class feature, not a bolt-on: it powers ingestion (extraction, TLDR, normalization), the meal-planning assistant, and conversational pantry updates. Everything runs through a hosted API — **no local LLM** (measured llama.cpp on the N95-equivalent N100: a 1.5B model degrades to ~1 tok/s with modest context; a 10K-token transcript extraction would take tens of minutes). Local compute is used only for embeddings, and only optionally.

## 1. Selectable providers behind one capability interface

The app supports **Anthropic (Claude) and OpenAI**, selected per task in settings. Either key can run extraction/chat when a capable model is configured; OpenAI additionally provides embeddings. V1 deliberately does **not** fail over automatically between providers. A timeout in a read-only extraction may safely be retried, but switching providers inside a streaming/tool loop risks duplicated calls, proposals, spend, and inconsistent conversation state. Automatic fallback is post-v1 and requires idempotency tests around every transition.

### Provider abstraction (`app/ai/`)

A thin interface with two implementations wrapping each vendor's native SDK — **not** a lowest-common-denominator hack. The three capabilities the app needs all exist on both providers, but their shapes differ and the adapter normalizes them:

| Capability | Anthropic | OpenAI | Normalized to |
|---|---|---|---|
| Structured extraction | Native structured-output API wrapped by adapter | **Responses API** with `text.format` strict JSON schema and `store: false` | `extract(schema, prompt) -> validated Pydantic object` |
| Streaming chat + tools | `messages.stream()`, tool-use blocks, SDK tool runner | streaming `responses` with function calling | `stream_chat(messages, tools) -> event stream` |
| Embeddings | *(none—use OpenAI, local, or off)* | `embeddings.create(dimensions=384)` | `embed(texts) -> list[vector[384]]` |

Provider wire shapes stay inside the adapters. The internal schema uses strings for decimal quantities and validates ranges in application code. Tool/function-call shapes map to one internal `ToolCall` type; tools are strict and read-only during generation. **Golden contract tests per provider** cover serialization, refusal, truncation, streaming events, usage accounting, and tool-call normalization.

### Task routing (settings, not code)

Config maps each task to one `provider/model`:

```
[ai.keys]
anthropic = "sk-ant-..."      # either or both
openai    = "sk-..."

[ai.routing]
extract = "anthropic/<verified-extraction-model>"
tldr    = "anthropic/<verified-fast-model>"
repair  = "anthropic/<verified-fast-model>"
chat    = "openai/<verified-reasoning-model>"
embed   = "openai/text-embedding-3-small"   # or "local/all-MiniLM-L6-v2" or "off"
```

Model IDs, capability metadata, context/output limits, and pricing are **versioned config, not scattered code**. Verify them at build/configuration time. The Settings UI offers only models known to support the requested capability, while an advanced override remains possible. A startup self-test validates configured extraction/chat models without spending more than a tiny bounded call.

### Cost posture & guardrails

| Task | Model class | Why |
|---|---|---|
| Recipe extraction, TLDR, ingredient repair | Configured fast model with strict structured output | Low cost/latency and sufficient transcript context |
| Meal-planning chat / big-event planning | Configured stronger reasoning model | Multi-constraint selection and scheduling need more reasoning |
| Embeddings (optional, post-v1) | OpenAI `text-embedding-3-small` at 384 dimensions or local MiniLM | One fixed vector contract |
| One-off backfills | Provider batch API where supported | Async/discount behavior verified at execution time |

Family usage should be inexpensive, but estimates are not guarantees and model prices change. Show a live estimate from versioned pricing config and label it with its last-verified date. Guardrails:
- Every call logged to `ai_usage_log` (provider, model, purpose, tokens, est. cost).
- Configurable monthly spend cap **per provider** checked before calls, plus per-call `max_output_tokens`/equivalent limits. When exceeded, schema.org ingestion still works and AI-dependent work pauses with a clear retry/configure action.
- Settings page shows month-to-date spend per provider.
- Per-feature and per-provider enable flags (Mealie's `*_ENABLE_*` switches).
- Usage logging happens on success and failure. Cost uses integer micro-USD, not binary floating point.

## 2. Extraction (see `04-ingestion-pipeline.md`)

- **Structured outputs** via `extract(schema, prompt)`. OpenAI uses Responses `text.format` and `store: false`; Anthropic uses its native structured-output path. Parse to the same Pydantic object and run semantic validation afterward.
- Use deterministic settings where supported. Handle truncation, refusal, timeout, and invalid semantic values uniformly.
- Retry only idempotent calls with bounded exponential backoff and jitter. Respect provider retry hints. No automatic cross-provider retry in v1.
- Store provider/model/request id, prompt/schema version, token use, and artifact hashes with every extraction run.

## 3. Meal-planning assistant

### Architecture: hybrid context + small tool set

Use **deterministic retrieval first, model reasoning second**:

- Stable instructions and tool definitions form the cacheable prefix where the provider supports it.
- For ordinary questions, application code first applies hard filters (allergies/exclusions, time, equipment, tier, pantry coverage) and supplies a compact ranked candidate set rather than the entire cookbook.
- Pantry summaries include only fields relevant to the question; exact rows remain available through tools.
- **Tools** (strict mode, via the SDK tool runner — don't hand-roll the loop):
  - `get_recipe_details(recipe_id)` — full ingredients/steps for scaling math
  - `search_recipes(query, max_minutes?, tier?)` — FTS5 (later hybrid with vectors)
  - `create_meal_plan_proposal(entries)` — validates recipe IDs/constraints and writes a pending proposal record, not the real plan
  - `create_pantry_update_proposal(changes)` — pending proposal only

The model never calculates authoritative ingredient totals or mutates pantry/plan/shopping tables. On proposal acceptance, deterministic services re-validate current data, calculate quantities, and apply one idempotent transaction. A stale proposal is refreshed instead of forced through.

### The proposal pattern (AI never silently mutates data)

Assistant responses may create separate versioned proposal records (meal plan, pantry update, event menu). The UI renders **Accept / Edit / Dismiss**. The chat message contains presentation text, not embedded authoritative JSON. Acceptance records who/when/model and uses an idempotency key.

### Flagship prompts to design for

- "Build next week's dinners: 3 meal-prep, 2 family, leftovers Friday, nothing over 45 minutes on weekdays."
- "What can I make tonight in under 30 minutes from what we have?"
- "What should I cook before it goes bad?" (uses expiry + the Due-Score idea from Grocy)
- "Company dinner for 8 on Saturday — impressive, mostly make-ahead; build the menu, scale everything, combined shopping list, and a prep timeline." (big-event mode: Sonnet, possibly with extended thinking; output = menu + per-recipe scaled quantities + day-before/morning-of/last-hour timeline)
- "We just got groceries: 2 cans of tomatoes into the downstairs pantry, chicken thighs in the freezer" (conversational pantry update → `update_pantry` proposal — this is the automation that fixes the documented reason pantry tracking fails)

### Streaming

**v1 status (Phase 5c, 2026-07-18, recorded per the docs-as-contract rule):** the assistant ships
as a **single structured request/response per turn** via the forced-tool adapter (the same offline-
testable pattern as extract/draft/receipt), NOT the streaming SSE + SDK tool-runner loop below. The
load-bearing contract is preserved — application code builds the hard-filtered candidate set and
pantry summary first (`assistant.build_context`), the model reasons over that and returns a
validated `AssistantResponse` (message + optional meal-plan / pantry-update proposals), and
acceptance re-validates and applies through deterministic services in one idempotent transaction.
Server-rendered htmx, no SSE. Streaming and multi-tool loops are a post-v1 enhancement (they need
idempotency tests around every mid-stream tool call and provider transition; not worth the risk at
family scale for a one-shot planning turn).

Planned streaming design (post-v1): SSE relay: browser POSTs the chat message → FastAPI `StreamingResponse(media_type="text/event-stream")` forwards text-delta chunks from the provider's stream (Claude `messages.stream()` or OpenAI streaming responses, normalized by the adapter to a single delta event) → browser reads via `fetch()` + ReadableStream (EventSource is GET-only). SSE over WebSocket deliberately: unidirectional, auto-reconnect, plain HTTP, trivial on LAN. API keys never leave the server.

### Prompt-caching gotchas (encode in the implementation)

- Exact-prefix matching in tools → system → messages order; keep the tool list byte-stable.
- Minimum cacheable prefix: 4096 tokens (Haiku 4.5) / 2048 (Sonnet 4.6) — below that caching silently no-ops.
- Reads 0.1×, writes 1.25× (5-min TTL). Order: [stable system+tools | bp1] [pantry/cookbook snapshot | bp2] [conversation] so a pantry edit only invalidates from bp2.
- Never interpolate timestamps/UUIDs into the system prompt.
- Verify hits via `usage.cache_read_input_tokens` (logged to `ai_usage_log`).

## 4. Recipe Q&A in cook mode

"Can I substitute crème fraîche?" uses the configured fast Q&A model with the full recipe and only relevant cook-log notes. The response is advice, never a silent recipe edit.

## 5. Semantic search (phase 3+)

Three embedding sources, selected by the `embed` route:
- **OpenAI `text-embedding-3-small` with `dimensions=384`** — matches the local vector contract.
- **Local `all-MiniLM-L6-v2`** via **fastembed** (Qdrant's ONNX runtime lib — no PyTorch; ~150–300 MB RSS only while embedding, $0 and offline, 384-dim) — the zero-key, zero-cost path.
- **`off`** — FTS5 keyword search only.
- Vectors use a fixed 384-dimensional sqlite-vec table. Store provider/model/dimension with each vector and rebuild atomically on a model change. Never mix incompatible dimensions in one table.
- Embed `title + tldr + ingredient names + tags`; re-embed on edit via Huey.
- Honest scoping: FTS5 is likely sufficient at family scale. Ship vectors only after logged real queries show meaningful misses such as "something cozy and warming."

## 6. Failure & offline behavior

- No internet ⇒ browsing, cooking, pantry, shopping lists all work (no AI in the read path). Ingestion queues and retries; chat shows a friendly offline notice.
- Selected provider outage ⇒ schema.org fast-path ingestion still works; AI-dependent jobs remain retryable and visible rather than silently switching provider.
- All AI features and providers are individually toggleable in settings.

## 7. Privacy and remote retention

- API keys remain server-side in the root-owned environment file.
- OpenAI Responses calls explicitly set `store: false`. Provider request bodies exclude authentication tokens, private operational logs, and unrelated household data.
- Send the minimum recipe/pantry context needed for the task. Household allergies are necessary planning constraints; names, device labels, and storage locations usually are not.
- Raw provider responses are not treated as the source of truth. Store only the validated result, request metadata, and enough redacted diagnostics to reproduce a failure from local artifacts.
