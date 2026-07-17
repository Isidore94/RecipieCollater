# RecipeCollater — Architecture

## 1. Decision summary

| Decision | Choice | Why (short) |
|---|---|---|
| Language/framework | **Python 3.12+ / FastAPI**, single uvicorn worker | The entire ingestion ecosystem (recipe-scrapers, yt-dlp, ingredient-parser-nlp) is Python-native; any other language shells out to it anyway |
| Frontend | **Server-rendered Jinja2 + htmx + Alpine.js** (+ one client-side island for the shopping list) | Fast on LAN, no build toolchain, no hydration bugs, the most reliably LLM-buildable frontend pattern; page weight <150 KB |
| Datastore | **SQLite** (WAL) — sole store, incl. FTS5 search and later sqlite-vec | Mealie officially recommends SQLite to ~20 users; zero ops; trivial backup |
| Background jobs | **Huey + SqliteHuey**, one worker process | Durable queue + cron-style periodic tasks, no Redis/Celery |
| Deployment | **Bare systemd units** (web, worker); uvicorn serves directly, no reverse proxy | dockerd adds ~100 MB RSS and ~2 W idle wakeups on a ~5 W box; systemd gives restart/journald/resource caps for free; nothing to proxy on a trusted LAN |
| Networking | **Plain HTTP on the LAN, $0**: DHCP-reserved IP + mDNS `recipes.local` (Avahi) | LAN-only by design; no domain, no certificates, no cloud dependency. Free HTTPS/remote upgrade paths documented but optional (§9) |
| Remote access | **None** — the app is used at home only; **no port forwarding, ever** | Owner's explicit choice; Tailscale (free tier) is the documented upgrade path if this ever changes |
| Auth | Named users, **device-session cookie** (400-day, HttpOnly, revocable rows) via magic-link/QR onboarding; separate long-lived **Bearer tokens** for ingest | No passwords for the family; lost phone = revoke one row; Immich/Home Assistant-proven pattern |
| AI | **Anthropic + OpenAI, pluggable per task** behind one adapter (default Haiku extract / Sonnet chat; OpenAI embeddings); structured outputs, SSE streaming, cross-provider fallback | Local LLM confirmed non-viable on N95 (~1 tok/s with context); two keys = resilience + first-party embeddings; API cost ≈ $3–8/month |
| Python env | **uv** + `pyproject.toml`, dependencies pinned | Reproducible, fast, no Docker needed |

An architecture panel (three independent proposals — minimal-footprint, modern-PWA, and agent-buildable — scored by three single-lens judges) settled this 2–1: two designers independently produced this exact FastAPI+htmx+SQLite shape, and the N95-performance and buildability judges both picked it. The family-UX judge preferred the SvelteKit PWA alternative for its richer in-hand feel; its winning ideas are grafted in rather than adopted wholesale: the instant client-side scaler preview, View-Transitions page morphs, the designed stock-take screen, and v1 Shortcut HTML-capture. Its disqualifying detail on this box: better-sqlite3's synchronous writes block Node's whole event loop under writer contention — a family-wide stall the single-language design can't have.

## 2. Process model on the N95

```
systemd
├── recipecollater-web.service      # uvicorn on port 80, 1 worker  (~60–90 MB RSS)
│     FastAPI: pages (Jinja2+htmx), /api/*, SSE chat relay,
│     static files (StaticFiles + far-future cache headers)
└── recipecollater-worker.service   # huey_consumer  (~50–80 MB)
      ingest jobs, image processing, LLM calls, periodic tasks
      (nightly backup + export, weekly yt-dlp update)

data/recipecollater.db  ← shared by web + worker (WAL; local disk only)
```

Port 80 via `AmbientCapabilities=CAP_NET_BIND_SERVICE` in the unit file so the family types `http://recipes.local` with no port suffix. No reverse proxy — there is nothing for one to do here.

- Total idle budget: **< 200 MB RSS, ~0% CPU** (no server-side polling; htmx polls only while an ingest job is in flight in someone's open tab).
- Burst behavior: ingest jobs run in the worker at nice priority; the web process never blocks on them. LLM latency is Anthropic's, not ours.
- Lazy-import heavy libs (yt-dlp, PIL, ingredient-parser) inside worker tasks so the web process stays slim — this is precisely how Mealie's FastAPI app ballooned to 400 MB and ours won't. **Stated as a hard rule in `CONVENTIONS.md`.**
- **Subprocess-per-job for heavy work**: Huey tasks spawn a short-lived `job_runner.py` subprocess for yt-dlp/Pillow/CRF stages. A thread-mode consumer never returns imported-lib RAM to the OS — subprocesses guarantee full release after each burst and isolate a hung extractor from the long-lived worker.
- **The Huey queue lives in its own SQLite file** (`data/queue.db`), not the app DB, so queue polling/bookkeeping writes never contend with family-facing writes.
- Short-transaction discipline in the worker is enforced by a test (simultaneous ingest + shopping-list sync must not stall) — the 5s single-writer stall is the easiest regression for AI-generated code to reintroduce.
- Watchdog: `Restart=always`, `MemoryMax=512M` per unit as a leak backstop.

## 3. Repository layout (for the builder agent)

```
recipecollater/
├── pyproject.toml            # uv-managed; ruff + pytest config
├── app/
│   ├── main.py               # FastAPI factory, routers, startup
│   ├── db.py                 # connection factory (pragmas), migration runner
│   ├── migrations/           # 001_init.sql, 002_...  (plain SQL, ordered)
│   ├── auth.py               # cookie/session/token dependencies
│   ├── models.py             # Pydantic models (incl. extraction schemas)
│   ├── routers/              # recipes, inbox, pantry, shopping, plan, chat, ingest, admin, share
│   ├── services/             # scaler, matcher, aggregation, deduction, exporter  (pure functions, unit-tested)
│   ├── ingest/               # fetch.py, web.py, youtube.py, normalize.py, images.py
│   ├── ai/                   # provider.py (adapter interface), anthropic.py, openai.py,
│   │                         # router.py (task→provider, fallback, usage log, spend cap),
│   │                         # extract.py, assistant.py (tools), embed.py
│   ├── tasks.py              # Huey task definitions
│   ├── templates/            # Jinja2; partials/ for htmx fragments
│   └── static/               # css, js (htmx, alpine, shopping-island.js), icons
├── seed/                     # foods.json, units.json, conversions.json, aliases.json
├── deploy/                   # systemd units, install.sh, Avahi/LAN setup, backup/restore docs, optional-upgrades appendix
├── shortcuts/                # Apple Shortcut definition + setup guide
├── tests/                    # unit + golden fixtures + Playwright smoke
└── docs/                     # these documents
```

Conventions: business logic in `services/` as pure functions (LLM-friendly to test); routers thin; every htmx fragment has a full-page fallback route (progressive enhancement, also makes Playwright trivial).

A root **`CONVENTIONS.md`** pins the rules a multi-session AI builder must never drift from: lazy-import rule, no-ORM SQL style, short worker transactions, exactly-one-cookie discipline, htmx-fragment naming, and the dependency pin/float split — **yt-dlp deliberately floats** (weekly auto-update; it's a hot dependency), **recipe-scrapers deliberately pinned** (LLM fallback covers breakage between deliberate bumps), curl_cffi and the ingredient-parser CRF model treated as pinned artifacts with a documented rebuild path, Python pinned at 3.12 via uv. Minimal CI: ruff + pytest + the sync-protocol test matrix.

Migration runner: refuses out-of-order files, and takes a `VACUUM INTO` snapshot **before every migration apply**.

## 4. Request paths

**Page read** (cookbook, recipe sheet): cookie dependency → SQLite read → Jinja2 render (<100 ms). htmx swaps for scaler changes, filters, checkboxes.

**Ingest**: `POST /api/ingest` (Bearer or cookie) → insert `ingest_jobs` → Huey enqueue → 202 `{job_id}`. Worker walks the pipeline (see `04-ingestion-pipeline.md`), updates job status; the inbox partial polls only in-flight jobs.

**Chat**: `POST /chat/send` → SSE `StreamingResponse` relaying `text_delta`s from the Claude stream; tool calls executed server-side; proposal blocks rendered as accept/edit cards on completion.

**Shopping sync**: `POST /api/shopping/sync {device_id, ops[]}` → apply idempotent ops (UUID adds, LWW field updates, tombstone deletes, clamp client timestamps, 3-day staleness cutoff) → return full canonical snapshot (~200 rows max — no delta machinery).

## 5. Security posture

- Never exposed to the public internet: the app binds the LAN interface only; no router port-forwards, ever. The threat model is "trusted home network" — auth exists for attribution and lost-phone revocation, not to repel attackers.
- Exactly one persistent HttpOnly SameSite=Lax cookie (WebKit 272325 discipline; the `Secure` flag is added only if the optional HTTPS upgrade is installed). All tokens stored as SHA-256 hashes; opaque, revocable per device from the admin page.
- Ingest endpoints validate/normalize URLs (http/https only, no internal-network SSRF); fetches capped 15s/5 MB.
- `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` in `EnvironmentFile=/etc/recipecollater/env` (root-owned, 0600) — never in the repo or the browser. Either alone runs the app; both enable cross-provider fallback and first-party embeddings. Non-secret task routing lives in the `settings` table (editable in the UI); keys never do.
- CSRF: SameSite=Lax + custom-header check on state-changing htmx routes (one line, belt and braces).
- Backups tested by restoring: a backup that's never been restored is a hope, not a backup.

## 6. The one client-side island

Everything is server-rendered except the **shopping list**, which must tolerate leaving the house with the list open: an Alpine store renders from `localStorage` snapshot ⊕ pending-ops outbox; flushes on mutation, `online`, `visibilitychange→visible`, `pageshow`, and a 15 s visible-interval (`sendBeacon` last-gasp on hide). Open the list at home, check things off in the store with zero connectivity, and it reconciles automatically when the phone rejoins the home Wi-Fi. No Background Sync API (unsupported on iOS), no Workbox, no service-worker caching of `/api` responses (the two-sources-of-truth bug that broke Mealie's offline list). `localStorage` works fine over plain HTTP; only *re-opening a dead tab with no connectivity* needs the optional HTTPS upgrade (service workers require a secure context) — "copy list as text" covers that gap for free. Cook mode gets the same treatment *lite*: current step + timers persist to `localStorage` so a page reload restores exactly where you were.

## 7. Backups & recovery

- Nightly Huey task: `VACUUM INTO` snapshot, keep 14 daily + 8 weekly. Point the snapshot target at a **different physical device** — any spare USB stick plugged into the N95 does the job for $0; a second partition on the same SSD does not bound the disk-death failure mode.
- **Nightly export**: every cookbook recipe as plain JSON+markdown files (dead-man's portability guarantee, a second durability layer independent of SQLite itself).
- Optional Litestream for continuous off-box WAL replication.
- `deploy/RESTORE.md` documents the drill; admin page shows last-backup age with a red banner past 48 h.

## 8. Observability (right-sized)

- structlog → journald (`journalctl -u recipecollater-web`).
- Admin dashboard: job queue health, last-N failures with tracebacks, AI month-to-date spend, yt-dlp version + last self-update, DB size, backup age.
- No Prometheus/Grafana — the dashboard is the monitoring.

## 9. Zero-cost networking (and what plain HTTP costs in features)

**Default setup ($0, LAN-only):**
1. DHCP reservation for the N95 in the router (stable IP).
2. Avahi advertises `recipes.local` (mDNS — native on iPhone/iPad/Mac, works on Windows 10+; the IP always works as a fallback). Optionally add a router local-DNS entry.
3. Family devices bookmark / Add-to-Home-Screen `http://recipes.local`.
4. iOS Shortcuts POST to the same HTTP address (works fine from Shortcuts; the one-time Local Network permission prompt must be allowed).

**What insecure context (plain HTTP) disables, and the free answers:**

| Browser feature needing HTTPS | Impact | Free fallback |
|---|---|---|
| `navigator.wakeLock` | Cook-mode screen dimming | The silent-looping-video trick (NoSleep pattern) works over HTTP on iOS Safari — ship it as the default wake path, try the real API opportunistically |
| Service worker | No app-shell precache; a killed tab can't reopen offline | Shopping island's `localStorage` outbox still works while the page lives; "copy list as text" before leaving; nothing else in the app needs offline |
| Android PWA install + `share_target` | No Android share sheet | Household is iPhone + PC — dormant. Paste box works on any Android browser regardless |
| `Secure` cookies, camera APIs | Cosmetic here / barcode backlog | Cookie ships without `Secure` on HTTP; barcode scanning was backlog anyway |

**Optional free upgrades, documented in `deploy/` but never required:**
- **mkcert local CA** (free): one-time root-CA install per device → green-lock HTTPS on the LAN → unlocks everything in the table above.
- **Tailscale free tier**: remote access + a real `ts.net` Let's Encrypt cert via `tailscale cert`, if away-from-home use is ever wanted. No domain purchase in either path.
