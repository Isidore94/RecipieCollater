# Phase 1–2 review and improvement plan

Reviewed against `docs/08-roadmap.md` at commit
`6555735c9becffae5be80e1cf0afdc3b53017cda` on 2026-07-17.

Automated baseline:

- `pytest -q`: passed (one Windows symlink test skipped)
- `ruff check app tests`: passed
- `mypy app`: passed

The implementation is a good functional prototype, but phases 1 and 2 should not yet be treated as
closed against their documented exit criteria. The highest-risk gaps are lossless recipe editing,
unit-aware package scaling, ingestion recovery/idempotency, ingredient normalization, immutable
provenance, and the incomplete SSRF boundary. These affect the pantry, shopping, and planning work
in phases 4–5, so the compatibility items below should be addressed before those later phases are
considered stable.

## Coordination with the phase 3–5 work

Do not rewrite or renumber migrations `004`–`009`, and do not claim the next migration numbers until
Claude's phase 3–5 migration series is known. Add remediation migrations after the highest migration
on the integration branch.

Keep the existing public service names where practical. If phase 4 is already calling the quantity,
recipe, or ingredient services, introduce corrected implementations behind the existing interfaces
or update both callers and contract tests in one change. Merge in narrow, independently testable
slices rather than one phase-wide patch.

## P0 — stabilize foundations consumed by phases 3–5

### 1. Make recipe editing lossless

Current evidence:

- The service model contains `description`, `servings_text`, step sections/minutes, original
  ingredient text, and package fields, but the HTML form does not expose all of them.
- Editing reconstructs ingredients from parsed columns, so imported `original_text` is lost.
- Editing flattens steps to instruction-only lines, so imported section and timing metadata is lost.
- `recipe_step_ingredients` exists in the schema but has no service or UI path for divided
  ingredients.
- Selecting `round_to_package` in the current form cannot succeed because the form provides no
  package quantity/unit inputs.

Plan:

1. Define one lossless `RecipeDraft`/edit contract shared by manual entry, ingestion review, export,
   revision snapshots, and future re-extraction.
2. Add form controls for description, servings text, ingredient original text, package quantity and
   unit, step section/minutes, and divided-ingredient links. Use progressive disclosure so ordinary
   recipes remain quick to enter.
3. Preserve source type and extraction provenance on normal edits. Never erase metadata merely
   because a form did not render it.
4. Add an optimistic concurrency token (`updated_at` or an explicit integer version) so two family
   members, or a user edit and re-extraction, cannot silently overwrite one another.
5. Add round-trip tests: ingest a fully populated recipe, open/edit/save without changes, and assert
   that every field and relationship is unchanged.

Acceptance:

- A phase-2 recipe can be edited without losing any imported field.
- Divided ingredients can be represented and exported.
- Concurrent stale edits receive a clear conflict instead of last-write-wins data loss.

### 2. Correct exact quantity and package conversion

Current evidence:

- Package scaling parses `package_quantity_text` but ignores `package_unit_id`. For example, an
  ingredient stored in grams with a package expressed in kilograms is rounded as though both
  numbers used the same unit.
- `quantity.convert()` accepts factors without checking dimensions.
- `food_unit_conversions` and food density fields exist but have no application service.

Plan:

1. Make conversion APIs accept typed units (including dimension) instead of unlabelled factors.
2. Convert package size into the ingredient's unit, or convert both values to canonical integers,
   before ceiling-to-package math.
3. Implement bidirectional per-food bridges and density bridges with explicit ambiguity handling.
   Never infer a mass/volume conversion without a confirmed food-specific bridge.
4. Validate package quantity is positive, package unit is present and compatible, minutes are
   non-negative, and all user/imported numeric strings are bounded in length and magnitude.
5. Extend property tests for cross-unit packages, reversible compatible conversions, mismatched
   dimensions, thirds, very large inputs, and pantry/shopping aggregation callers.

Acceptance:

- `300 g` scaled by `1.5` with a `1 kg` package produces `1 kg`, not `450 g`.
- Incompatible package dimensions fail validation with a useful message.
- Phase 4 pantry and shopping code uses the same tested canonical conversion service.

### 3. Finish the phase-2 ingredient normalization contract

Current evidence:

- Normalization is currently a conservative regex plus exact unit lookup.
- New foods created by ingestion become `confirmed`; the roadmap requires unknown foods to be
  quarantined as `pending`.
- There is no local parser → exact/alias/fuzzy → batched AI repair cascade.
- `parse_confidence` is documented as phase 2 but is absent from the schema and model.

Plan:

1. Add parse status/confidence and normalization provenance to recipe ingredients.
2. Separate manual food creation from ingestion: manual confirmed entries may remain confirmed;
   unfamiliar imported foods enter `pending`.
3. Implement the documented cascade with deterministic thresholds and fixtures. Keep ambiguous
   lines verbatim and exclude them from automatic pantry deductions until confirmed.
4. Batch unresolved lines into at most one configured-provider repair request per recipe, subject to
   spend limits. Store the proposal and let deterministic code apply it.
5. Add a small pending-food review screen before phase-4 pantry matching depends on food IDs.

Acceptance:

- Known aliases normalize deterministically without AI.
- Ambiguous/new foods do not silently pollute confirmed pantry vocabulary.
- Every structured imported ingredient records how it was parsed and at what confidence.

## P0 — make ingestion reliable and safe

### 4. Use a durable state machine with real retry and crash recovery

Current evidence:

- Huey has two retries, but expected fetch/provider failures return normally and are not retried.
- A failed duplicate cannot be resubmitted because URL uniqueness returns the old job and only new
  jobs are scheduled.
- If queue submission fails, the durable DB row remains queued but no reconciler schedules it.
- Stage updates act as one-time heartbeats; long network/model calls have no lease renewal.
- `apply_extraction()` commits recipe creation and extraction metadata separately, then downloads an
  image, then marks the job done. Its “whole apply is committed as one unit” comment is not true.
- A crash/replay can add duplicate extraction runs even when it avoids a duplicate recipe.

Plan:

1. Model job claiming as a lease with `next_attempt_at`, `lease_expires_at`, attempt limit, and
   transient/permanent error classification.
2. Add a periodic reconciler that schedules queued jobs and reclaims expired leases. Queue
   publication becomes at-least-once; DB state remains authoritative.
3. Add an authenticated Retry action that creates a new attempt/run for retryable failed jobs while
   preserving history.
4. Refactor recipe creation, extraction-run insertion, accepted-run pointer, and job completion into
   one short DB transaction. Network/image work happens outside it and is tracked as a separate
   best-effort stage.
5. Make service transaction ownership explicit; inner CRUD helpers used by the pipeline must not
   commit unexpectedly.
6. Add fault-injection tests after every durable write and queue handoff. Assert one accepted recipe,
   one accepted run, preserved attempt history, and eventual completion.

Acceptance:

- Killing the worker at every stage recovers without duplicate recipes or accepted runs.
- A transient provider failure is visible, automatically retried within bounds, and manually
  retryable.
- A queue outage cannot leave a job permanently stranded.

### 5. Close the complete SSRF and resource-limit boundary

Current evidence:

- `app/services/fetch.py` explicitly documents a DNS-rebinding gap even though phase 2 requires
  complete SSRF protection.
- Image downloading performs a preflight lookup and then an independent request, reads the complete
  body before enforcing its size cap, and does not share redirect/content checks.
- YouTube caption downloading uses a separate unrestricted request path.
- Supplied HTML/JSON and browser form bodies have no early request-size limit.

Plan:

1. Connect to a vetted resolved IP while retaining the original hostname for TLS/SNI and `Host`, or
   verify the actual connected peer against the vetted address set. Revalidate every redirect.
2. Centralize outbound HTTP in one streaming downloader used by pages, images, and other untrusted
   URLs. Enforce scheme, port policy, connect/read/total timeouts, redirect count, compressed and
   decompressed byte ceilings, content type, and peer address.
3. Reject URL credentials, malformed IPv6 authorities, unsafe schemes, and non-public literal IPs
   before queueing.
4. Add ASGI/server request limits for ingest JSON, captured HTML, and multipart image uploads so the
   acknowledgement path remains bounded.
5. Test DNS answer changes, redirects to private/mapped IPv6 addresses, oversized/chunked/compressed
   responses, slow reads, misleading content length/type, and malformed URLs.

Acceptance:

- The fetcher has no documented rebinding exception.
- Every untrusted download path uses the same verified network boundary.
- Oversized inputs fail early without buffering the entire payload.

### 6. Make artifacts and re-extraction genuinely immutable

Current evidence:

- `store_artifact()` uses `INSERT OR REPLACE` for `(job_id, kind)`, which mutates provenance.
- Artifact files are written directly rather than by atomic temp-file/rename.
- Captions are used in the AI prompt but the stored YouTube metadata records only
  `has_captions`, not the caption text.
- Re-extraction comparison/acceptance is not implemented.

Plan:

1. Make artifact records insert-only. Represent repeated captures with a new attempt/run ID or
   sequence rather than replacing a row.
2. Write content-addressed blobs atomically, verify hash/length when reading, and detect a missing or
   corrupt blob explicitly.
3. Persist all extraction inputs separately: fetched/supplied HTML, reduced page text or JSON-LD,
   YouTube metadata, captions/chapters, and exact provider input where privacy policy permits.
4. Re-extraction always creates a new extraction run and a comparison draft. Provide a field-level
   diff; accepting selected changes creates a normal recipe revision and atomically changes the
   accepted-run pointer.
5. Include artifact corruption and multiple re-extractions in backup/restore tests.

Acceptance:

- No code path updates or replaces an artifact record/blob.
- Re-extracting never changes family edits until a user explicitly accepts a diff.
- A restored recipe retains the inputs and versions needed to explain its extraction.

### 7. Align AI adapters, versioning, spend controls, and error UX

Current evidence:

- The OpenAI adapter uses Chat Completions with a non-strict function schema, while the architecture
  contract specifies Responses structured output/strict tools with `store: false`.
- Extraction runs currently receive no prompt version.
- Provider failures are collapsed into `no_recipe`, hiding whether the model failed, the cap was
  reached, or the source lacked a recipe.
- Spend checks are read-then-call and can race across concurrent jobs.
- Pricing has no “verified on” date even though the roadmap requires dated price metadata.

Plan:

1. Choose one explicit provider contract and update code, docs, and tests together. Recommended:
   keep the provider-neutral interface, use each provider's current strict structured-output
   mechanism, and retain a local Pydantic validation boundary.
2. Version system prompt, schema, normalization logic, and extractor independently; record all four
   on each run.
3. Preserve error categories (`provider_unavailable`, `provider_output_invalid`, `budget_blocked`,
   `no_recipe`) and expose safe actionable messages plus Retry.
4. Reserve budget atomically before calls, reconcile reservation with actual billed usage, and
   expire abandoned reservations. This bounds concurrent overspend.
5. Store pricing source/verification date and surface configured model capability mismatches at
   startup/admin rather than at the end of a job.
6. Contract-test both providers against the same golden recipes, malformed output, refusal,
   truncation, timeout, and billed failure cases.

Acceptance:

- Either provider works alone and produces the same internal schema.
- Every run records non-null prompt/schema/extractor/normalizer versions.
- A provider failure is distinguishable from “this page is not a recipe.”

### 8. Complete web and YouTube extraction features

Plan:

1. Add an explicit OpenGraph stub for thin pages so the inbox can show source/title/image and offer
   AI retry rather than only failing.
2. Preserve YouTube chapters/timestamps and add `recipe_steps.video_seconds`.
3. Fetch comments only when description/captions are objectively thin, with a small hard cap.
4. Store caption/chapter artifacts and enforce size/time limits.
5. Implement the controlled yt-dlp version-check signal in admin/update tooling; never self-update
   the live environment.
6. Correct provider-specific copy such as “Add an Anthropic key” when OpenAI alone is supported.

## P1 — security, UX, and data hygiene

### 9. Validate and sanitize recipe-facing content

1. Allow only `http`/`https` source links; never render `javascript:`, credentials, or malformed
   schemes into `href`.
2. Decide whether recipe prose is plain text or Markdown. If Markdown is retained, render a small
   allowlist and sanitize links/HTML; add stored-XSS fixtures. If plain text is preferred, correct
   the roadmap rather than claiming safe Markdown rendering.
3. Re-encode all uploaded images through the same bounded WEBP pipeline as downloaded images.
   Validate decoded dimensions/pixel count, strip metadata, use atomic writes, and remove superseded
   or deleted recipe images.
4. Return visible validation errors for invalid images and numeric times instead of silently
   ignoring them.

### 10. Improve cookbook browsing without adding a SPA

1. Add tier/tag/status filters and pagination or a bounded result limit.
2. Preserve query/filter state when navigating back from a recipe.
3. Show image thumbnails with explicit dimensions and responsive variants.
4. Add empty/loading/error states for ingestion with a clear stage, attempt count, and Retry action.
5. Keep htmx endpoints usable as full-page fallbacks and test core flows without JavaScript.

### 11. Reduce avoidable query/write amplification

1. Replace per-recipe detail fan-out where lists eventually need tags/images with bounded joined
   queries; measure before introducing caching.
2. Consolidate FTS refresh work during whole-recipe updates. The current delete/insert trigger cycle
   runs repeatedly for every child row.
3. Add a trigger or service invariant for future tag renames so FTS cannot drift.
4. Add indexes only from measured query plans, especially normalized source URL, stale job lease,
   extraction-run history, and pending foods.

## P2 — operational optimization and evidence

### 12. Enforce the worker memory model

The engineering contract requires heavy ingestion stages in a short-lived job-runner subprocess,
but the current Huey thread imports yt-dlp, parsers, Pillow, and provider SDKs into the long-lived
worker.

1. Add a small subprocess runner with a wall-clock timeout and structured result/error envelope.
2. Keep only queue/DB orchestration in the Huey process.
3. Measure idle and post-ingestion RSS on the actual N95; confirm memory returns after each job.
4. Add cancellation/termination behavior that leaves the DB lease recoverable.

### 13. Add phase-exit evidence

1. Add benchmark scripts for ingest acknowledgement, schema.org fast path, list/search reads, image
   processing, and worker peak/RSS recovery.
2. Add Playwright or equivalent browser coverage for create → edit → scale → search → export and
   submit → progress → retry → review.
3. Run the documented real iPhone Safari/A2HS/Shortcut checklist and N95 resource checks; record
   dates/results without substituting desktop automation.
4. Update README/CHANGELOG status so it no longer says only phase 0 is implemented, and keep branch
   names/release tags aligned with actual integration state.

## Recommended delivery order

1. Lossless edit contract and regression tests.
2. Correct unit/package/food-bridge service.
3. Ingredient parse status and pending-food quarantine.
4. Durable ingest leases, retry/reconciler, and transactional apply.
5. Unified SSRF-safe streaming downloader and request limits.
6. Immutable artifacts plus re-extraction comparison.
7. Provider contract/version/spend/error alignment.
8. YouTube/OpenGraph completion.
9. Content/image hardening and browsing polish.
10. Subprocess worker and measured phase-exit validation.

Items 1–3 are the minimum compatibility gate before phase 4 is trusted. Items 4–7 are the minimum
gate before phase 2 is declared complete.

## Design decisions to confirm

Recommended defaults are included so work can continue without blocking:

1. **Recipe prose:** allow a small Markdown subset (emphasis, lists, links) with sanitized output;
   disallow raw HTML.
2. **Ingredient editor:** keep compact rows, with an expandable “scaling/package/divided” section
   per row.
3. **Step editor:** use ordered step cards with optional section, minutes, linked ingredients, and
   video timestamp rather than a newline-only textarea.
4. **Re-extraction:** show a field-level diff with per-section acceptance; never provide a one-click
   silent overwrite.
5. **Retry policy:** two automatic retries for transient failures with backoff, then a visible manual
   Retry button; permanent validation/SSRF failures do not auto-retry.
6. **Food review:** keep unknown imported foods pending, but do not block saving/cooking the recipe;
   block only automatic pantry deductions until confirmed.
