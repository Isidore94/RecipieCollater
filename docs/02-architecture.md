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
| AI | **Anthropic + OpenAI, selectable per task** behind a small capability interface; structured outputs, SSE streaming, OpenAI Responses with `store: false`; no automatic cross-provider fallback in v1 | Local LLM is non-viable on N95; either hosted provider can run core AI while selection remains explicit and testable |
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
- Short-transaction discipline in the worker is enforced by a test (simultaneous ingest + pantry/shopping writes must not stall recipe reads)—the 5s single-writer stall is an easy regression to reintroduce.
- Watchdog: `Restart=always`, `MemoryMax=512M` per unit as a leak backstop.

## 3. Repository layout (for the builder agent)

```
recipecollater/
├── pyproject.toml            # uv-managed; ruff + pytest config
├── app/
│   ├── main.py               # FastAPI factory, routers, startup
│   ├── db.py                 # connection factory (pragmas), migration runner
│   ├── migrations/           # small phase-owned SQL migrations, ordered
│   ├── auth.py               # cookie/session/token dependencies
│   ├── models.py             # Pydantic models (incl. extraction schemas)
│   ├── routers/              # recipes, inbox, pantry, shopping, plan, chat, ingest, admin, share
│   ├── services/             # scaler, exact quantities, matcher, aggregation, deduction, exporter (pure, tested)
│   ├── ingest/               # fetch.py, web.py, youtube.py, normalize.py, images.py
│   ├── ai/                   # provider.py (capability interface), anthropic.py, openai.py,
│   │                         # router.py (task→provider, usage log, spend cap),
│   │                         # extract.py, assistant.py (tools), embed.py
│   ├── tasks.py              # Huey task definitions
│   ├── templates/            # Jinja2; partials/ for htmx fragments
│   └── static/               # css, js (htmx, alpine, shopping-island.js), icons
├── seed/                     # foods.json, units.json, conversions.json, aliases.json
├── deploy/                   # systemd units, staged install/update/rollback, health check, LAN and backup/restore docs
├── shortcuts/                # Apple Shortcut definition + setup guide
├── tests/                    # unit + golden fixtures + Playwright smoke
└── docs/                     # these documents
```

Conventions: business logic in `services/` as pure functions (LLM-friendly to test); routers thin; every htmx fragment has a full-page fallback route (progressive enhancement, also makes Playwright trivial).

A root **`CONVENTIONS.md`** pins rules a multi-session builder must never drift from: exact-math boundaries, lazy heavy imports, no-ORM SQL style, short worker transactions, exactly-one-cookie discipline, htmx fragment naming, immutable artifacts, provider privacy, and the dependency update policy. Production dependencies are pinned. The app checks yt-dlp releases weekly but updates only through the staged test/health/rollback flow; recipe-scrapers, curl_cffi, and the ingredient-parser model change through deliberate reviewed bumps. Minimal CI includes lint/type checks, pytest, migrations, provider contracts, and offline ingestion fixtures.

Migration runner: refuses out-of-order files and takes a `VACUUM INTO` snapshot **before every migration apply**. Tables land with the phase that uses them rather than placing a speculative final schema in migration 001. Each migration is forward-only, transactional where SQLite permits it, and tested from both a fresh database and the prior released snapshot.

## 4. Request paths

**Page read** (cookbook, recipe sheet): cookie dependency → SQLite read → Jinja2 render (<100 ms). htmx swaps for scaler changes, filters, checkboxes.

**Ingest**: `POST /api/ingest` (Bearer or cookie) → insert `ingest_jobs` → Huey enqueue → 202 `{job_id}`. Worker walks the pipeline (see `04-ingestion-pipeline.md`), updates job status; the inbox partial polls only in-flight jobs.

**Chat**: `POST /chat/send` → SSE `StreamingResponse` relaying normalized provider events; read-only tools execute server-side; proposal records render as accept/edit/dismiss cards. Accepting a proposal is a separate idempotent application request—never a model-side write.

**Shopping (v1)**: normal server-rendered CRUD plus copy/share-as-text and a JSON/text export endpoint for an Apple Shortcut or native list. A custom `/api/shopping/sync` protocol is explicitly post-v1 and requires its own design/test gate if the simple export workflow proves inadequate.

## 5. Security posture

- Never exposed to the public internet: the app binds the LAN interface only; no router port-forwards, ever. The threat model is "trusted home network" — auth exists for attribution and lost-phone revocation, not to repel attackers.
- Exactly one persistent HttpOnly SameSite=Lax cookie (WebKit 272325 discipline; the `Secure` flag is added only if the optional HTTPS upgrade is installed). All tokens are opaque, hashed at rest, revocable per device, and scoped; Shortcut tokens can ingest but cannot read recipes or mutate pantry/admin data.
- Ingest endpoints normalize URLs and defend against SSRF across initial resolution **and every redirect**: allow only http/https; reject loopback, private, link-local, multicast, IPv4-mapped, IPv6 local, and cloud-metadata ranges; cap fetches at 15s/5 MB; re-resolve and re-check to limit DNS rebinding.
- `APP_BASE_URL=http://recipes.local` is the single URL source for Shortcut templates, bookmarklets, links, and callbacks—never hardcode `http` or `https` elsewhere.
- `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` live in `EnvironmentFile=/etc/recipecollater/env` (root-owned, 0600), never in the repo/browser. Either alone runs supported tasks. Non-secret routing lives in `settings`. OpenAI Responses calls set `store: false`; both provider adapters enforce output/tool limits and redact secrets from logs.
- CSRF: SameSite=Lax + custom-header check on state-changing htmx routes (one line, belt and braces).
- Backups tested by restoring: a backup that's never been restored is a hope, not a backup.

## 6. Client-side code budget

V1 is server-rendered. Small Alpine modules are allowed for instant scaler preview, timers, checklists, drag/reorder affordances, and persistence of the current cook step in `localStorage`. The authoritative value always comes from a tested server service.

The shopping list deliberately starts simpler: server-rendered list at home, plus copy/share-as-text and native-list export before leaving. Do not build a localStorage outbox, last-writer-wins protocol, tombstones, Background Sync, or a service-worker API cache in v1. If real trips prove native export inadequate, write the sync protocol and conflict tests as a separate post-v1 design before generating client code.

## 7. Backups & recovery

- Nightly Huey task creates a staged backup set: `VACUUM INTO` database snapshot plus `data/images/`, uploaded originals, immutable ingestion artifacts, and a small manifest of checksums/schema version. Keep 14 daily + 8 weekly on a **different physical device**—a second partition on the same SSD does not cover disk death.
- **Nightly export**: every cookbook recipe as plain JSON+markdown files (dead-man's portability guarantee, a second durability layer independent of SQLite itself).
- Every backup runs `PRAGMA integrity_check`, verifies manifest checksums, and is considered healthy only after a scheduled restore smoke test. `deploy/RESTORE.md` documents bare-machine recovery; admin shows last healthy backup and last restore-test age.
- Optional Litestream may replicate SQLite but does not replace image/artifact backups.

## 8. Observability (right-sized)

- structlog → journald (`journalctl -u recipecollater-web`).
- Admin dashboard: job queue health, last-N categorized failures with tracebacks, AI month-to-date spend, configured model capabilities, yt-dlp installed/latest-known version, DB/data size, backup age, and restore-test age.
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
| Service worker | No app-shell precache; a killed tab can't reopen offline | Copy/share/export the shopping list to a native app before leaving; nothing in the home-first core needs offline |
| Android PWA install + `share_target` | No Android share sheet | Household is iPhone + PC — dormant. Paste box works on any Android browser regardless |
| `Secure` cookies, camera APIs | Cosmetic here / barcode backlog | Cookie ships without `Secure` on HTTP; barcode scanning was backlog anyway |

**Optional free upgrades, documented in `deploy/` but never required:**
- **mkcert local CA** (free): one-time root-CA install per device → green-lock HTTPS on the LAN → unlocks everything in the table above.
- **Tailscale free tier**: remote access + a real `ts.net` Let's Encrypt cert via `tailscale cert`, if away-from-home use is ever wanted. No domain purchase in either path.

## 10. Installation, updates, and rollback

- Install into versioned releases (for example `/opt/recipecollater/releases/<commit>/`) with a dedicated uv environment per release and a `current` symlink.
- An update builds the new environment, runs offline tests, snapshots data, applies migrations against a copy, starts on a temporary port, and checks `/healthz` before switching `current` and restarting systemd.
- Never run `pip install -U` inside the live environment. Hot dependencies such as yt-dlp are checked weekly and shown in admin; installation is explicit or uses the same staged smoke-test/rollback path.
- Application rollback and data rollback are separate documented actions. A release that contains a forward-only schema migration cannot be rolled back by switching code alone.
