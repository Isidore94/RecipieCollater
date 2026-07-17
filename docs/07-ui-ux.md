# RecipeCollater — UI / UX Specification

One responsive web app, mobile-first, installable as a PWA on both platforms. It must look like a product you'd pay for — warm, food-forward, image-led — while staying fast on a phone over Wi-Fi. Server-rendered pages with htmx partial swaps + Alpine.js for instant client-side niceties; no SPA framework, no build-step-heavy toolchain.

## 1. Design language

- **Warm minimalism**: generous whitespace, large recipe photography, a serif display face for recipe titles over a clean sans for UI, cream/paper light theme and a true dark theme (auto via `prefers-color-scheme`, manual toggle stored per device).
- **Cards everywhere**: the recipe card (photo, title, time chip, tier badge, "have 7/9" pantry chip, rating) is the core visual unit — grid on desktop, single/double column on mobile.
- Tier badges with consistent color + icon: 🥡 Meal Prep / 🍽 Family / ✨ Company.
- Micro-interactions via CSS + Alpine (checkbox satisfaction, gauge cycling, card hover) — no JS animation library.
- Tailwind CSS (standalone CLI binary — no Node toolchain on the server) or hand-rolled CSS custom properties; either way a single small stylesheet, far-future cached.
- Empty states that teach: an empty inbox shows "Share a YouTube video to get started" with per-device setup buttons.

## 2. Navigation

Bottom tab bar on mobile (thumb-reachable), left rail on desktop:

| Tab | Contents |
|---|---|
| **Inbox** (Test Recipes) | Triage queue: processing states, new arrivals, review flags. Badge with count. Paste box lives here. |
| **Cookbook** | The curated library: search, filters (tier, tags, max time, rating, "have ingredients", last-cooked), sort. |
| **Pantry** | Location chips + item grid, stock-take mode, staples view. |
| **Plan** | Week board + shopping list (segmented control between them), saved menus. |
| **Chat** | The AI assistant (persistent conversations). |

Global: instant-search overlay (FTS5, keystroke-fast on LAN), "+" action (paste URL / new recipe / add pantry item), settings (devices, tokens, AI spend, backups, export).

## 3. Key screens

### Recipe sheet (the heart)
- Hero image, title, source badge (YouTube channel avatar / site favicon → links to origin), tier badge, rating stars, time row (claimed vs *our kitchen* time when set — shown as "45 min · ours: 60"), TLDR in a highlighted "in short" block.
- **Serving scaler**: segmented 2 / 4 / 6 / 8 / custom, right above ingredients; quantities re-render as kitchen fractions instantly (htmx fragment swap; Alpine handles optimistic display). Non-scalable lines render as written with a subtle "as needed" tag. "Show original amounts" toggle. "Save as scaled copy" under overflow menu.
- **Ingredients list**: checkbox per line (mise-en-place state, local to device), quantity + unit + food + note; ingredient-group headers ("For the sauce"); low-confidence parses show a dotted underline inviting a one-tap fix; pending-food chips ask "is 'green onion' the same as scallion?".
- **Steps**: numbered cards; durations inside step text are auto-linked → tap spawns a named timer; steps with `video_seconds` show a tiny play glyph → seeks the embedded player.
- **Cook mode** button → full-screen step-by-step: one step per screen, huge type, swipe/tap to advance, persistent timer tray, ingredient amounts inline-expandable per step, screen wake-lock (`navigator.wakeLock` in try/catch, re-acquired on `visibilitychange` — iOS PWA quirk), embedded YouTube player collapsed at top for video recipes.
- **After-cook capture** (fires from "I made this" in cook mode or the sheet): rating, actual time (pre-filled with claimed, one slider), "what did you actually use?" quick-adjust list (pre-filled with scaled quantities), note field, then "update pantry?" bulk-confirm screen. This flow is also the **promotion gate**: for inbox recipes it ends with "Keep it? → Add to cookbook / Not for us (archive)".
- **Cook log** timeline at the bottom of the sheet: every make with who/when/rating/notes.
- Ask-AI drawer: recipe-scoped Q&A (substitutions, technique).

### Inbox
- Cards in arrival order with processing states (skeleton shimmer while extracting), extraction-confidence flags ("thin — needs review"), failure cards with retry, and duplicate warnings.
- Swipe/buttons: **Promote** (opens after-cook capture or straight promote), **Edit**, **Archive**.

### Cookbook
- Filter bar as horizontally scrollable chips on mobile; the "✨ what can I make now?" filter combines pantry match + max time.
- Collections come later; tiers + tags carry v1 organization.

### Editor
- Same form for manual entry and post-import fixes: drag-to-reorder steps/ingredients (SortableJS, the one small JS lib worth its bytes), inline food/unit creation, image upload/replace, raw-source viewer ("what the bot saw") with **Re-extract** button.

## 4. Platform integration

- **PWA**: manifest (`display: standalone`, 192/512 + maskable icons, `apple-touch-icon`), service worker with a *minimal* cache strategy — static assets cache-first; pages network-first with cached fallback; shopping list additionally cache-first-refresh so it opens instantly in-store. LAN server is the source of truth; iOS storage is treated as disposable.
- **iOS specifics**: Add-to-Home-Screen instructions shown on the device-onboarding success page; identity lives in the HttpOnly cookie (survives A2HS via the iOS 17.2+ one-time cookie copy; pairing-code fallback screen for cookie-less launches); the ingest path is the Shortcut, not the PWA.
- **Android**: install prompt via `beforeinstallprompt`; `share_target` in the manifest (see ingestion doc).
- **Desktop**: same responsive pages stretched to a grid + left rail; keyboard shortcuts (`/` search, `n` new) as low-cost polish.

## 5. Performance budget

- First contentful paint < 1s on LAN phone; page weight < 150 KB gzipped excluding images (htmx ~14 KB + Alpine ~15 KB + one stylesheet; zero framework runtime).
- Images always WEBP at the right size (300px cards / 1024px sheet / 2048px zoom) with `loading="lazy"` and explicit dimensions (no layout shift).
- Server responses <100 ms for reads (SQLite on NVMe/SSD at family scale is effectively instant; FTS5 queries are single-digit ms).
- Mealie's cautionary tale: a heavy Vue SPA reached ~10s first load with only 88 recipes. Server-rendered HTML makes that failure mode impossible.
