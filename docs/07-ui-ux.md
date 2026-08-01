# RecipeCollater — UI / UX Specification

One responsive web app, mobile-first, installable as a PWA on both platforms. It must look like a product you'd pay for — warm, food-forward, image-led — while staying fast on a phone over Wi-Fi. Server-rendered pages with htmx partial swaps + Alpine.js for instant client-side niceties; no SPA framework, no build-step-heavy toolchain.

## 1. Design language

- **Warm minimalism**: generous whitespace, large recipe photography, a serif display face for recipe titles over a clean sans for UI, cream/paper light theme and a true dark theme (auto via `prefers-color-scheme`, manual toggle stored per device in local storage—not a second cookie).
- **Cards everywhere**: the recipe card (photo, title, time chip, tier badge, "have 7/9" pantry chip, rating) is the core visual unit — grid on desktop, single/double column on mobile.
- Tier badges with consistent color + icon: 🥡 Meal Prep / 🍽 Family / ✨ Company.
- Micro-interactions via CSS + Alpine (checkbox satisfaction, gauge cycling, card hover) — no JS animation library. Use the **View Transitions API** (htmx supports it natively; pure progressive enhancement) for page morphs and list reordering, which removes most of the "web-page-like vs app-like" gap the design panel flagged as htmx's UX ceiling.
- Tailwind CSS (standalone CLI binary — no Node toolchain on the server) or hand-rolled CSS custom properties; either way a single small stylesheet, far-future cached.
- Empty states that teach: an empty inbox shows "Share a YouTube video to get started" with per-device setup buttons.

## 2. Navigation

Bottom tab bar on mobile (thumb-reachable), left rail on desktop.

Interim tab set (Phase 4.6, until Phase 5 ships Plan/Chat):

| Tab | Contents |
|---|---|
| **Home** | Discovery: tonight's rested favourites, use-it-up, one-ingredient-away, new-to-try, "✨ what can I make?". |
| **Cookbook** | The curated library: search + filter chips (tier, tags, max time, rating), sorts, `/can-make`. |
| **Pantry** | Location chips + item grid, search, stock-take mode, staples view; links to `/foods` upkeep. |
| **Shopping** | The active list (badge with remaining count), trip builder, restock review. |
| **Inbox** | Triage queue: processing states, new arrivals, review flags. Paste box lives here. |

Final tab set once Phase 5 lands (Plan absorbs the shopping list behind a segmented control;
Chat returns):

| Tab | Contents |
|---|---|
| **Inbox** | Triage queue: processing states, new arrivals, review flags. Badge with count. Paste box lives here, and on `/add`. |
| **Cookbook** | The curated library: search, filters (tier, tags, max time, rating, "have ingredients", last-cooked), sort. |
| **Pantry** | Location chips + item grid, stock-take mode, staples view. |
| **Plan** | Week board + shopping list (segmented control between them), saved menus. |
| **Chat** | The AI assistant (persistent conversations). |

Global: instant-search overlay (FTS5, keystroke-fast on LAN), "+" action (paste URL / new recipe / add pantry item), settings (devices, tokens, AI spend, backups, export).

The "+" action's first half is built: **`+ Add a recipe`** sits above the rail (and in the mobile tools row) and leads to `/add` — paste, drop, or bookmarklet a link, with manual entry one step further in. The instant-search overlay and the pantry-item branch of "+" are still open.

## 3. Key screens

### Recipe sheet (the heart)
- Hero image, title, source badge, tier, rating, and a time row that distinguishes **active effort** from **elapsed time** and source claims from our-kitchen actuals (for example, "20 min active · 60 min elapsed · ours: 75"). TLDR sits in a highlighted "in short" block.
- **Serving scaler**: segmented 2 / 4 / 6 / 8 / custom, right above ingredients. An instant client-side preview may render from server-supplied exact decimal attributes, but the authoritative fragment comes from one tested Python service. Lines visibly distinguish `linear`, `fixed`, `to taste`, and package-rounded behavior and explain any rounding. "Show original amounts" is included; v1 does not create scaled-copy duplicates.
- **Ingredients list**: checkbox per line (mise-en-place state, local to device), quantity + unit + food + note; ingredient-group headers ("For the sauce"); low-confidence parses show a dotted underline inviting a one-tap fix; pending-food chips ask "is 'green onion' the same as scallion?".
- **Steps**: numbered cards; durations inside step text are auto-linked → tap spawns a named timer; steps with `video_seconds` show a tiny play glyph → seeks the embedded player.
- **Cook mode** button → full-screen step-by-step: one step per screen, huge type, swipe/tap to advance, persistent timer tray, ingredient amounts inline-expandable per step, embedded YouTube player collapsed at top for video recipes. **Screen wake**: the silent-looping-video technique (NoSleep pattern) is the default — it works over plain HTTP on iOS Safari; `navigator.wakeLock` is tried opportunistically in a try/catch (it needs a secure context, so it only activates if the optional HTTPS upgrade is installed) and re-acquired on `visibilitychange`.
- **After-cook capture**: rating, actual active + elapsed time, "what did you actually use?" quick-adjust list (per-line omitted / substituted / adjusted + free-text additions, pre-filled from cook-mode skip/sub quick marks stored in localStorage), and notes. Deviations are structured data: they render in the cook-log timeline, deductions honor them (an omitted line never deducts; an adjusted line deducts the actual amount), and a substitution can be remembered into the household's learned subs, which resurface on missing-ingredient coverage lines. Pantry deductions appear as a compact review on the first cook; confirmed mappings are remembered and the user may enable auto-apply for this recipe next time. Any skipped/ambiguous lines are explicit. This flow is also the inbox promotion gate.
- **Cook log** timeline at the bottom of the sheet: every make with who/when/rating/notes.
- Ask-AI drawer: recipe-scoped Q&A (substitutions, technique), clearly presented as advice rather than an automatic recipe edit.

### Inbox
- Cards in arrival order with stage/attempt state, extraction-confidence flags, categorized failure cards with retry, and duplicate warnings.
- Swipe/buttons: **Promote** (opens after-cook capture or straight promote), **Edit**, **Archive**.
- Re-extract opens a comparison view with current/proposed columns and per-field accept controls; it never overwrites the current recipe on click.

### Cookbook
- Filter bar as horizontally scrollable chips on mobile; the "✨ what can I make now?" filter combines pantry match + max time.
- Collections come later; tiers + tags carry v1 organization.
- **Chips stack, they do not replace.** Every chip toggles itself and keeps the others, and
  multiple tags mean *all* of them (`?tag=chicken&tag=weeknight`). One tag stops narrowing
  anything once it covers a third of the cookbook, which is the size this is built for.
- **The list is paged** (`recipes.PAGE_SIZE`), with a count and Newer/Older links that carry
  every active filter. Applies to Cookbook, Inbox and Archive alike. The two curated views
  (`sort=stale`, `sort=useitup`) are prompts rather than catalogues and are capped instead.
- **`/tags`** is the cookbook's counterpart to `/foods`: rename, merge and delete a tag across
  every recipe at once, each undoable from the banner that confirms it. It also lists every
  tag, so a tag outside the filter bar's top handful is still reachable.

### Editor
- Same form for manual entry and post-import fixes: drag-to-reorder steps/ingredients, many-to-many step ingredient links (including divided amounts), per-ingredient scaling behavior, inline food/unit creation, image upload/replace, immutable raw-artifact viewer ("what the bot saw"), and **Re-extract to comparison**.

### Pantry deduction review
- Mobile-first checklist grouped as **will deduct / needs a choice / skipped**.
- Each editable mapping shows recipe ingredient → pantry item/location, converted amount, and why it is or is not eligible for auto mode.
- Confirming remembers mappings. "Trust this recipe next time" is explicit and revoked for affected lines when ingredient quantities, food, unit, or scaling behavior changes.
- Applied summary always offers Undo; Undo writes a compensating history entry.

### Shopping list
- V1 web list is server-rendered with big checkboxes, aisle groups, provenance, manual add, and checked-items-at-bottom behavior while on home Wi-Fi.
- **The list speaks "store", not "recipe"** (Phase 4.6): purchase info (pack word + size) lives on foods and is asked for inline the first time a food hits the list; measured lines ceiling to packages ("Flour — 2 bags (need 3 cups)"); foods tracked as gauge/binary never show cooking amounts — either the pantry covers them (reported, collapsed) or they land as a quantity-less "1 bag" line; unmeasurable ingredient lines land flagged "check the amount" — nothing is ever silently dropped.
- **Trip builder** (`/shopping/plan`): pick recipes + servings → one preview with to-buy by aisle, a covered-by-pantry section, per-line opt-out; pantry stock is subtracted once against the aggregate need; applying recomputes server-side.
- **Done shopping → restock review**: checked lines propose pantry updates (gauge→full, have, +purchased amount, optionally start tracking new items) so cooking's deductions and shopping's restocks close the loop.
- A prominent **Take shopping list with me** action offers copy, system share, printable text, and the documented Apple Shortcut/native-list export.
- Do not imply offline web sync. If later built, its UI must expose last-synced time and conflicts rather than pretending a stale local list is current.

## 4. Platform integration

- **Home-screen app over plain HTTP**: iOS Add-to-Home-Screen supplies an icon/standalone presentation. There is no service worker in the default setup. Shopping is exported to a native/text list before leaving; no UI promises that a killed web tab will work away from home. Optional trusted HTTPS may later enable a minimal app-shell service worker, never an API cache.
- **iOS specifics**: A2HS instructions shown on the device-onboarding success page; identity lives in the HttpOnly cookie (survives A2HS via the iOS 17.2+ one-time cookie copy; pairing-code fallback screen for cookie-less launches); the ingest path is the Shortcut, not the PWA.
- **Android** (dormant — household is iPhone+PC): paste box works in any browser today; install prompt + `share_target` activate only with the HTTPS upgrade (see ingestion doc).
- **Desktop**: same responsive pages stretched to a grid + left rail, with `+ Add a recipe` standing above the rail links and a bookmarklet on `/add` as the share-sheet equivalent; keyboard shortcuts (`/` search, `n` new) as low-cost polish.

## 5. Performance budget

- First contentful paint < 1s on LAN phone; page weight < 150 KB gzipped excluding images (htmx ~14 KB + Alpine ~15 KB + one stylesheet; zero framework runtime).
- Images always WEBP at the right size (300px cards / 1024px sheet / 2048px zoom) with `loading="lazy"` and explicit dimensions (no layout shift).
- Server responses <100 ms for reads (SQLite on NVMe/SSD at family scale is effectively instant; FTS5 queries are single-digit ms).
- Mealie's cautionary tale: a heavy Vue SPA reached ~10s first load with only 88 recipes. Server-rendered HTML makes that failure mode impossible.

## 6. Accessibility and real-device gates

- WCAG-minded contrast, visible focus, semantic labels, reduced-motion support, and 44×44 px minimum touch targets on primary cook/pantry controls.
- All core flows work with keyboard only and without drag-and-drop; drag is enhancement, never the sole interaction.
- Phase exits require a real iPhone Safari/A2HS/Shortcut pass plus a desktop browser pass. Chromium automation does not validate iOS networking, cookie, wake, or home-screen behavior.
- Recipe and pantry states are never conveyed by color alone.
