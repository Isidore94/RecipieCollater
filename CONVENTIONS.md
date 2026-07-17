# CONVENTIONS.md — RecipeCollater engineering contract

These rules are authoritative for every implementation session. They exist to stop a
multi-session builder from drifting. If a rule here conflicts with something a library
"makes easy", the rule wins. If measured reality forces a rule to change, change it **here,
in the same commit**, with the evidence — do not silently diverge.

The Markdown documents in `docs/` are the **product/architecture contract**. `docs/08-roadmap.md`
defines phase scope and exit criteria. Do not implement a later phase's feature or create a
later phase's table while working on an earlier phase.

---

## 1. Exact-quantity math boundary

- Every quantity that a human, an AI provider, or an imported file supplies enters the system
  **as a string** and is parsed with `decimal.Decimal`. Never `float()` a user/AI/imported
  quantity.
- Canonical storage is **integers**: milligrams (mass), microlitres (volume), milli-each (count).
  Unit factors are exact integers (`1 kg = 1_000_000 mg`, `1 dozen = 12_000 milli-each`).
- SQL never performs recipe, pantry, or shopping arithmetic with `REAL`. Aggregation, scaling,
  and deduction are pure Python services operating on `Decimal`/`int`, unit-tested with property
  tests. Rounding policy is explicit and centralised, never incidental.
- `parse_confidence` and similar are advisory scores only — never inputs to arithmetic.

*(Phase 0 ships no quantity math; this boundary governs Phase 1+ and is stated now so the
first math commit starts correct.)*

## 2. No ORM

- Data access is hand-written SQL through the `app.db` connection factory. No SQLAlchemy, no
  Alembic. Schema changes are ordered SQL files under `app/migrations/` tracked in
  `schema_migrations`.
- Keep SQL close to where it is used, but put anything reused or logic-bearing in `app/services/`
  as pure, testable functions.

## 3. Short transactions / worker never stalls reads

- Hold write transactions for the minimum time. Never open a transaction and then do network I/O,
  LLM calls, image processing, or `time.sleep` inside it.
- The worker must never stall recipe reads or pantry/shopping writes beyond the response budget
  (<100 ms p95 reads). There is a write-contention test; keep it green.
- The Huey queue lives in its **own** SQLite file (`data/queue.db`), separate from the app DB, so
  queue bookkeeping never contends with family-facing writes.

## 4. Lazy heavy imports

- Heavy libraries (yt-dlp, Pillow, ingredient-parser, provider SDKs, sqlite-vec, fastembed) are
  imported **inside the worker task function that uses them**, never at web-process module top level.
  This keeps the web process < ~90 MB RSS. This is how Mealie's web process ballooned to 400 MB;
  ours will not.
- Heavy ingestion stages run in a short-lived `job_runner.py` subprocess so imported-library RAM is
  fully returned to the OS after each burst (a thread-mode consumer never releases it).

## 5. Exactly one cookie

- Authentication is exactly **one** persistent HttpOnly cookie: `rc_session`, `SameSite=Lax`,
  `Path=/`, `Max-Age` 400 days, sliding renewal. It is **always persistent** — never a session
  cookie, and the app never mixes a session cookie with a persistent one (WebKit bug 272325 reverts
  cookies in iOS home-screen apps when strategies are mixed).
- `Secure` is added **only** when serving over HTTPS (the optional mkcert/Tailscale upgrade). On the
  default plain-HTTP LAN it is omitted, because a `Secure` cookie is dropped over HTTP.
- Never use Starlette `SessionMiddleware` (client-side signed blob: unrevocable). Identity authority
  is the `device_sessions` row keyed by the SHA-256 hash of the cookie value.
- Preferences such as theme use browser local storage or a server-side device record, never another
  cookie. `rc_session` is the only cookie the application creates.

## 6. Scoped, opaque, hashed tokens

- All tokens (`device_sessions`, `api_tokens`, `onboarding_tokens`) are opaque random values
  (`secrets.token_urlsafe(32)`), stored **only** as SHA-256 hashes, shown to the user exactly once,
  and revocable per row.
- **Ingest tokens are scoped.** An `api_tokens` row carries a `scope`; the ingest scope may submit
  ingestion jobs and nothing else. An ingest token must never read recipes, mutate pantry/shopping,
  reach admin, or act as a browser session. Browser session cookies must never authenticate the
  ingest API. There is a test asserting both boundaries; keep it green.

## 7. CSRF

- Every state-changing browser route (POST/PUT/PATCH/DELETE authenticated by the cookie) is guarded
  by `require_csrf`, layered so both htmx and no-JS form fallback are covered:
  1. **Fetch Metadata** — if `Sec-Fetch-Site` is present (all modern browsers incl. iOS Safari),
     allow only `same-origin`/`none`; reject `cross-site`/`same-site`. This browser-set header cannot
     be forged cross-site and protects plain `<form>` posts.
  2. **Legacy fallback** — browsers without Fetch Metadata must send a custom header a cross-site
     form cannot set (`HX-Request` from htmx, or `X-RC-CSRF`).
  `SameSite=Lax` withholds the auth cookie from cross-site POSTs underneath all of this.
- Token-authenticated API endpoints (ingest) are exempt from the header check because they are not
  cookie-authenticated and are not reachable from a victim's browser session.

## 8. htmx / templates

- V1 is server-rendered Jinja2. Routers are thin; business logic is in `app/services/`.
- Every htmx fragment endpoint has a **full-page fallback** so the app degrades without JS and so
  Playwright/HTTP tests can drive it. Fragment templates live in `templates/partials/`.
- Alpine.js is allowed only for local, non-authoritative niceties (scaler preview, timers,
  checklists, theme toggle). The authoritative value always comes from a tested server service.
- Vendored `htmx`/`alpine` are self-hosted under `app/static/vendor/` with a pinned version and a
  recorded SHA-256 (`app/static/vendor/VENDOR.md`). No third-party CDN at runtime (LAN-offline).

## 9. Immutable artifacts & provenance

- Ingestion artifacts (supplied/fetched HTML, transcripts, extraction inputs) are written once,
  content-addressed by SHA-256, never mutated or deleted outside backup rotation.
- Extraction runs are versioned records; re-extraction creates a comparison draft and never
  overwrites family edits. *(Phase 2 concern; stated now so the first artifact commit is correct.)*

## 10. Provider privacy & AI boundaries

- Provider API keys live only in the root-owned `EnvironmentFile`, never in the repo, DB, logs, or
  browser. Adapters redact secrets from logs and set output/tool limits.
- OpenAI Responses calls set `store: false`. No automatic cross-provider fallback in v1.
- AI proposes IDs/structured artifacts; **deterministic application services perform all scaling,
  conversion, pantry, shopping, and DB mutation.** Accepting an AI proposal is a separate idempotent
  application request, never a model-side write. *(Phase 2/5 concern.)*

## 11. Networking / URLs

- `APP_BASE_URL` (default `http://recipes.local`) is the single source of truth for links, Shortcut
  templates, bookmarklets, and callbacks. Never hardcode `http`/`https` or an IP elsewhere.
- The app binds the LAN interface only. No reverse proxy, no port-forwarding, ever.
- Validate `Host` against `RC_ALLOWED_HOSTS`; LAN-only is not a substitute for DNS-rebinding
  protection. First-run bootstrap requires the installer-generated `RC_SETUP_TOKEN`.
- Ingest fetches enforce SSRF defence (http/https only; reject loopback/private/link-local/multicast/
  IPv4-mapped/IPv6-local/cloud-metadata; 15 s / 5 MB caps; re-resolve after redirects). *(Phase 2.)*

## 12. Dependency & update policy

- Production dependencies are **pinned exactly** in `pyproject.toml`. Dev/CI tools are in the `dev`
  dependency group.
- No `pip install -U` in a live environment, ever. Updates go through the staged
  build→offline-test→migration-rehearsal→temp-port-healthcheck→atomic-switch→rollback flow
  (`deploy/UPDATES.md`).
- Hot dependencies (yt-dlp) are checked for new releases weekly and surfaced in admin, but installed
  only through the staged flow. `recipe-scrapers`, `curl_cffi`, and the ingredient-parser model
  change only through deliberate reviewed bumps.
- Do not add a dependency to enable a feature outside the current phase's scope.

## 13. Migrations

- Migration files are ordered (`NNN_description.sql`), forward-only, and applied in order inside a
  transaction. The runner refuses out-of-order/unknown files and takes a `VACUUM INTO` snapshot
  **before every apply**.
- Tables land with the phase that owns them. Migration `001` contains only Phase-0 tables. Do not
  place a speculative final schema in `001`.
- Every migration is tested from a fresh database **and** from the prior released snapshot.

## 14. Backups mean restore-tested

- A backup is "healthy" only after: complete manifest (DB snapshot + images + originals + artifacts),
  verified checksums, `PRAGMA integrity_check`, **and** a successful restore smoke test. File
  creation alone is not a backup.
- If `RC_BACKUP_DIR` is configured, it must already exist on a different mounted filesystem from
  `RC_DATA_DIR`; never create a missing mountpoint and silently write a supposed external backup to
  the system disk.

## 15. Testing & honesty

- Tests are meaningful, not placeholders. CI runs fully offline (no live web/YouTube/provider calls;
  provider contracts use recorded payloads).
- Never claim something was tested on the N95, a real iPhone, or the home network unless it actually
  was. Automated Chromium does not validate iOS networking/cookie/wake/home-screen behaviour; those
  are hand-test gates at phase exits.

## 16. Scope discipline

- Do not broaden scope because a library or model makes an extra feature easy. Server-rendered,
  fewer dependencies, data preserved — that is the tie-breaker when a detail is unspecified.
