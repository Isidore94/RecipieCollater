# RecipeCollater — Build Roadmap

Build in vertical slices so every phase ends with something the household can use and test. Do not start phase N+1 while phase N's exit criteria are red. Keep `CHANGELOG.md`, architectural decision records, and schema/prompt versions from the first implementation commit.

**Owner constraints:** $0 hosting infrastructure; no domain, public exposure, certificates, cloud database, Redis, or container runtime. Default access is plain HTTP at `http://recipes.local` via DHCP reservation + Avahi/router DNS. Optional AI APIs are the only recurring cost. Optional mkcert/Tailscale upgrades are documented but never on the critical path.

## Coding-agent guidance

This plan is agent-neutral. Use the strongest available reasoning/coding model for architecture-sensitive work and a faster model for mechanical templates/CRUD only when the verification gates remain identical. No model choice substitutes for tests or review.

Use maximum care on:

1. exact quantity/unit/scaling/shopping/pantry math;
2. schema and migrations;
3. ingestion idempotency, artifacts, retries, and SSRF;
4. provider adapters, strict structured output, tool/proposal boundaries, and remote-data privacy;
5. auth, scoped tokens, cookies, and CSRF;
6. backup/restore and staged update/rollback;
7. any future offline synchronization protocol.

Before each phase:

- Read the relevant plan documents and record any implementation-driven spec correction in the same commit.
- Keep routers thin and business logic pure/testable.
- Do not silently broaden scope because a library or model makes an extra feature easy.
- Run a fresh review/verification pass before declaring the phase complete.

## Phase 0 — Deployable foundation

- Repository layout, `pyproject.toml`, Python pin, ruff, pytest, type checking, and minimal CI.
- Root `CONVENTIONS.md`: exact-math boundary, no ORM, short transactions, lazy heavy imports, one-cookie discipline, scoped tokens, htmx fragment conventions, artifact immutability, provider privacy, dependency pin/update policy, and Markdown docs as the contract.
- FastAPI factory, Jinja/htmx shell, responsive navigation, dark mode, `/healthz`, structured logging, and static assets.
- SQLite connection factory/pragmas and ordered migration runner. Migration 001 contains only phase-0 tables; later tables arrive with their owning phase. Test fresh install and upgrade from the previous snapshot.
- Huey + separate `queue.db`, worker lifecycle, systemd units, Avahi/DHCP guide.
- Named users, device sessions, pairing flow, and separate scoped ingest tokens.
- Versioned-release install/update/rollback skeleton: build new uv environment, offline tests, DB-copy migration rehearsal, temporary-port health check, atomic release switch. Never mutate the live environment with `pip install -U`.
- Backup framework and manifest format established even before recipe data exists.
- **Exit criteria:** clean install on the N95; onboard one iPhone and one PC; restart survives reboot; `/healthz` passes; migration/rollback rehearsal and an empty backup/restore smoke test pass; idle budgets measured.

## Phase 1 — Manual cookbook vertical slice

- Phase-owned migrations for units, foods, aliases/conversions, recipes, ingredients, steps, many-to-many step links, tags, revisions, and FTS5.
- Exact quantity service: decimal-string boundary, Python `Decimal`, canonical integer mg/µL/milli-each, generalized food bridges, fraction rendering, and property tests.
- Manual recipe CRUD with ingredient groups, divided ingredients, per-ingredient scaling modes (`linear`, `fixed`, `to_taste`, `round_to_package`), image upload, and safe Markdown rendering.
- Recipe sheet with TLDR, active vs elapsed time, ephemeral serving scaler, tier/tags, source field, print view, JSON/Markdown export, and responsive phone/desktop UI.
- Inbox/Cookbook/Archive status and FTS/filter browsing. No AI or pantry dependency.
- Image/original files included in backup manifests and restore tests.
- **Exit criteria:** manually enter a real pasta recipe, scale 4→6 exactly, render kitchen fractions, preserve fixed/to-taste/package behavior, find it instantly, cook from the iPhone view, export it, and restore it with images from backup.

## Phase 2 — Reliable ingestion and both AI providers

- Phase-owned migrations for ingest jobs, immutable artifacts, extraction runs, current accepted run, and normalized URL/idempotency fields.
- `POST /api/ingest` returns 202 in <2 seconds; stage heartbeat, bounded retries, categorized errors, crash recovery, and concurrent duplicate protection.
- URL normalization and complete SSRF defense across DNS resolution and redirects; size/time/decompression limits.
- Web pipeline: deterministic JSON-LD/`recipe-scrapers` fast path, readable-text fallback, provider-neutral structured extraction, OpenGraph stub.
- AI capability interface with independently tested Anthropic and OpenAI adapters. OpenAI uses Responses `text.format`, strict tools where applicable, and `store: false`. One provider/model is selected per task; no automatic cross-provider fallback.
- Prompt/schema/extractor versioning, usage/error logging, dated price metadata, per-call output limits, and per-provider monthly caps.
- YouTube pipeline: lightweight metadata + captions/chapters first; comments only when inputs are thin; controlled yt-dlp version check/update path; thumbnail capture; timestamps.
- Ingredient normalization: local parser → exact/alias/fuzzy match → one batched configured-provider repair → pending-food quarantine.
- Apple Shortcuts generated from `APP_BASE_URL`, including Safari HTML capture; desktop paste box/bookmarklet.
- Re-extract creates a comparison draft and never overwrites family edits.
- **Exit criteria:** schema.org import in <5 seconds without AI; iPhone YouTube share produces a reviewable recipe in <60 seconds; each provider works alone in contract tests; a forced provider failure is visible/retryable; worker crashes at every stage do not duplicate recipes; SSRF suite passes.

## Phase 3 — Cooking and household history

- Phase-owned migrations for cook logs and immutable per-cook ingredient snapshots.
- Cook mode: large step view, ingredient checklist, multiple timers, local current-step/timer recovery, per-step video seek, and safe screen-wake fallback over HTTP.
- After-cook capture: rating, active + elapsed actual time, servings, actual quantities, and notes.
- Inbox promotion/archive decision and cook-log timeline; "haven't made in a while" sort.
- Recipe-scoped Q&A through the configured fast provider; advice cannot mutate the recipe.
- Manifest/icon/A2HS onboarding without claiming service-worker/offline support on default HTTP.
- **Exit criteria:** cook a real dinner end-to-end on an actual iPhone; refresh restores current step/timers; after-cook data and promotion are correct; a real Safari/A2HS/Shortcut test passes in addition to automation.

## Phase 4 — Pantry and practical shopping

- Phase-owned migrations for locations, pantry items, exact/gauge/binary modes, adjustment history, recipe mapping/trust state, shopping lists/items/source rows.
- Pantry CRUD, inline location creation, stock-take mode, staples/thresholds, one-gesture used-up/spoiled/correction, and restock flow.
- Review-first cook deductions: only confirmed compatible mappings qualify; exact canonical math; ambiguous/approximate/alternative lines are skipped or reviewed; confirmed decisions remembered; optional auto-apply per trusted recipe; edits revoke affected trust; Undo writes compensating adjustments.
- Pantry-aware recipe matching and "use it up" ranking.
- Deterministic shopping generation/aggregation that preserves manual lines/check state and shows provenance.
- Mobile home-Wi-Fi list plus **Take shopping list with me**: copy, share, print/text/JSON, and documented Apple Shortcut/native-list export.
- No custom offline outbox/sync in v1.
- **Exit criteria:** ten real recipes complete a first-cook review; repeated trusted cooks require no unnecessary confirmation; wrong mappings never auto-deduct; Undo/history work; pantry remains useful through a month of casual use; one weekly shop succeeds through native/text export.

## Phase 5 — Meal planning and assistant

- Phase-owned migrations for meal plans, saved menus, household preferences, AI conversations, versioned proposals, and usage records.
- Structured household constraints: allergies/exclusions (hard), dislikes/diet/equipment/time/leftover/tier preferences (soft).
- Week board, recipe/note/leftover entries, per-entry servings, saved menus, deterministic plan→shopping generation, and iCal export.
- Assistant uses deterministic hard filtering and a compact candidate set before model reasoning; full details are fetched through strict read-only tools.
- Versioned proposal cards for meal plans, pantry updates, and event menus. Acceptance revalidates current data and uses deterministic services in one idempotent transaction.
- Big-event mode: per-ingredient scaling behavior, max batch size, oven/burner/equipment capacity, make-ahead/holding/storage metadata, deterministic combined list, and AI-drafted timeline validated against recipe steps/capacity.
- **Exit criteria:** "plan next week's dinners" respects a deliberately conflicting hard allergy and soft preference, produces an accepted plan + correct shopping list, and remains deterministic on quantities; a company meal exposes an intentional oven-capacity conflict rather than producing an impossible schedule.

## Phase 6 — Resilience and evidence-driven polish

- Complete backup sets: `VACUUM INTO` DB + images/originals + artifacts + manifest/checksums to a separate physical device; 14 daily + 8 weekly; `PRAGMA integrity_check`; automated restore smoke test; bare-machine restore document.
- Nightly JSON/Markdown cookbook export.
- Admin dashboard: queue health, categorized failures, selected provider/model capabilities, spend/caps, yt-dlp installed/latest-known version, DB/data size, last healthy backup, last restore test.
- Performance and accessibility pass against budgets; real iPhone + desktop regression checklist.
- Photo import through the selected vision-capable provider.
- Only if evidence justifies them: semantic search (fixed 384 dimensions), optional trusted HTTPS, or Tailscale.
- **Exit criteria:** restore onto a clean test directory and browse/cook with images; failed staged update rolls back safely; budgets and accessibility gates pass.

## Post-v1 proposals requiring a new decision

- **Automatic cross-provider fallback:** only after tool-loop/idempotency/double-spend tests define safe transition points.
- **Custom offline shopping sync:** only after native/text export proves inadequate; write the operation/conflict/tombstone/staleness protocol and two-device matrix before JS.
- **Semantic search:** only after logged FTS5 misses justify ONNX/sqlite-vec complexity.
- Scale-by-anchor ingredient, sub-recipes, collections, nutrition/FDC, TikTok/Instagram ingestion, barcode scanning, Android share target, Home Assistant hooks, and multi-image recipes.

## Verification conventions

- pytest + HTTP client tests; property tests for exact quantity conversion, scaling, package rounding, aggregation, and deduction/undo.
- Golden provider contract tests with recorded payloads; all CI model calls mocked/offline.
- Recorded ingestion fixtures; never depend on live websites/YouTube in CI.
- Migration tests from fresh and every retained release snapshot.
- Write-contention test: worker activity cannot stall recipe reads or pantry/shopping writes beyond the response budget.
- Playwright for core browser flows plus mandatory hand testing on real iOS Safari/A2HS/Shortcuts at phase exits.
- Backup success means verified complete manifest + integrity check + successful restore smoke test, not merely file creation.

## Measurable budgets

| Budget | Target |
|---|---|
| Hosting infrastructure | $0; optional AI API usage only |
| Idle RSS (web + worker) | <200 MB total target; investigate before accepting >250 MB |
| Idle CPU | ~0%; no server-side polling loops |
| First contentful paint, phone on LAN | <1 second |
| Read endpoints | <100 ms p95 on the N95 at family-scale fixtures |
| Ingest acknowledgment | <2 seconds |
| YouTube transcript-path ingest | <60 seconds target, with visible stage/retry when upstream is slow |
| Backup freshness | <48 hours and last restore smoke test <7 days |
