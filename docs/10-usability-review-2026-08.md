# Usability review — August 2026

> **Status: all Tier 1 and Tier 2 items fixed**, plus the Tier 3 items on buttons, jargon, touch
> targets, empty states and `:active` feedback. What remains is listed under *Still open* at the
> bottom. Every page was re-checked at iPhone viewport afterwards: 21 screens, no page or network
> errors, no horizontal overflow, no interactive control under 40px.

Goal of this review: *"I want my wife to use this. It needs to be easy to use, quick to use, and enjoyable to use."*

Method: full code read of every template, router, and static asset, a docs-vs-code gap analysis,
and a live walkthrough of the running app at iPhone viewport (fresh DB → setup → add recipe by
URL → manual entry → recipe sheet → cook mode), screenshots taken along the way.

Verdict up front: the bones are genuinely good — the pairing flow, cook mode's structure,
stock-take ergonomics, the "assistant proposes, nothing changes until you Accept" model, and a
lot of the microcopy are better than most commercial recipe apps. But there is a tier of
issues that would make a non-technical daily user *stop using the app in week one*, and most
of them cluster in exactly the moments she'd use it: standing at the stove, standing in the
store, and waiting on the AI.

---

## Tier 1 — showstoppers (she will hit these in the first week)

### 1. The screen sleeps mid-cook, and timers silently fail — **FIXED**
`app/static/js/cook.js:185-194` implements only `navigator.wakeLock`, which requires a secure
context. The app's documented deployment is plain HTTP (`http://recipes.local`), so the wake
lock never activates — `wake()` returns immediately — and the phone sleeps mid-recipe with
batter on her hands. `docs/07-ui-ux.md:48` specifies the NoSleep looping-video fallback as the
*default* for exactly this reason, but it was never built (no `<video>`, no NoSleep, no asset
anywhere in the repo).

Compounding it: the timer alarm is a foreground `setInterval` beep (`cook.js:104-115,182`) and
`Notification` also doesn't work on iOS Safari over HTTP. So the 60-minute bake timer she set
**will not ring** once the screen sleeps. The headline cooking feature fails precisely when it's
being relied on, with no warning.

Fix: ship the NoSleep video fallback (small silent looping video, played on cook-mode entry,
wakeLock opportunistically on top), and surface a visible "screen will stay awake" / "couldn't
keep screen awake" state so failure is at least honest.

### 2. Every AI operation is a frozen page with no feedback — **FIXED**
Four synchronous LLM round-trips are plain `<form method=post>` with no spinner, no disabled
button, nothing: chat (`app/routers/assistant.py:68`), AI draft (`recipes.py:284`), photo draft
(`recipes.py:314`, easily 20s), receipt scan (`receipts.py:56`, 10–30s). The page just sits
there dead. This is the single most likely place she concludes "it's broken" — and double-taps,
double-billing the AI spend.

Worse, **assistant errors render in the green success banner**: `assistant.py:72` passes
`result.error` as `notice`, and `chat/index.html:10` styles `notice` as `banner-ok`. "No AI key
configured" appears as a success. And an error on Accept (`assistant.py:95-96`) redirects with
no message at all — the proposal just sits there.

Fix: htmx-ify these four forms with an `hx-indicator` spinner + disabled submit; route errors
to an error banner.

### 3. Manual entry rejects "3 bananas" and "2 eggs" — **FIXED**
Verified live: entering qty `3`, unit blank, ingredient `ripe bananas` — the only natural way a
person writes countable ingredients — hard-fails with *"'3 ripe bananas' has an amount but no
recognised unit"* (`app/services/recipes.py:248-253`). The `each` unit exists in the ontology,
but nothing defaults to it and the form gives no hint. The error appears as one banner at the
top of a form that is ~10,000px tall on a phone, far from the offending field.

Fix: blank unit + numeric qty should default to `each`. While in there: the form renders **six
blank rows × 6 fields** (36 stacked inputs on mobile, `recipes.py:31` + `app.css:204-206`),
each exposing a raw `linear / fixed / to_taste / round_to_package` enum select. Start with 2–3
rows, move scaling behind an "advanced" disclosure, add per-row remove.

### 4. The shopping list is at its worst in the store — **FIXED**
The check-off button is ~26px tall (`app.css:380`, vs. the app's own 44px baseline), only the
tiny ☐ is tappable (the item text is an inert span, `shopping/index.html:57`), and every check
is a **full page reload that scrolls back to the top** — mid-aisle, one-handed, with a cart.
This is the app's most-tapped control. Same reload-to-top pattern hits pantry gauge taps
(`pantry/index.html:101-107`) and every rating star (`view.html:30-41`).

Fix: htmx swap on the row, whole-row tap target, 44px minimum. This one change is probably the
biggest single quality-of-life win in the app.

### 5. Scaling doesn't follow through — **FIXED**
Scale a recipe 4 → 8 and:
- "Cook this →" carries no servings (`view.html:44`) — cook mode shows the 4-serving amounts
  (`cooking.py:51` falls back to base).
- "Add missing to shopping" posts a stale hidden `servings` input rendered server-side
  (`view.html:46-49`) — shops for 4.
- The header still says "serves 4" (`view.html:18`).

She scales for guests, cooks, and the numbers are silently wrong. Fix: make the scaler swap
update the hidden input and the Cook link (or store the chosen servings server-side).

### 6. Add-to-Home-Screen is broken on iPhone — **FIXED**
- Both icons are SVG only (`manifest.webmanifest:11-12`, `base.html:14`); iOS requires PNG for
  `apple-touch-icon`, so she gets a screenshot thumbnail instead of an app icon.
- `"start_url": "/inbox"` (`manifest.webmanifest:5`) — the installed app opens on the triage
  queue, not Home.
- The promised A2HS instructions (docs/07:82) don't exist anywhere in the UI; "Add to phone"
  in the header goes to the Shortcut setup page, which is developer-grade.

Fix: export 180/192/512 PNGs, `start_url: "/"`, and a friendly one-screen "add this to your
home screen" walkthrough linked from onboarding success.

---

## Tier 2 — big friction (she'll tolerate these, then quietly stop)

1. **~20 mutations swallow errors and reload unchanged** — `contextlib.suppress` around
   user-facing writes (`pantry.py:136,152,177,194,210`; `shopping.py:87,247,263`;
   `foods.py:66-111`; `preferences.py:52,78`; `planning.py:95-167`; `recipes.py:516,531`).
   From her seat: "I pressed the button and nothing happened."
2. **No way to move a planned meal to another day.** `POST /plan/entry/{id}/move`
   (`planning.py:114-126`) is implemented; no template calls it. She must delete and re-add.
3. **Ingest success is invisible; failure is raw and permanent.** — *Fixed:* finished jobs
   announce the recipe and link to it; failures offer Try again / dismiss. A finished job silently
   vanishes from the poll (`_ingest_jobs.html`) — no "Added: Banana Bread →". A failed job
   shows the verbatim error forever ("the site returned HTTP 402", verified live) with no
   retry and no dismiss.
4. **The add-a-recipe surface is hidden.** — *Fixed:* `/add` is a standing button on the rail
   and the primary button on every library tab, and it takes a pasted, dropped, or
   bookmarkleted link. The paste box exists only on Inbox, Inbox has no
   nav tab (`templating.py:50-57`), and the prominent "+ New recipe" leads to the intimidating
   manual form instead. Home only links "add your first recipe" when the whole app is empty.
5. **Pantry first-run is a dead end.** "+ Location" is a ~17px `<details>` summary
   (`app.css:307`); the prominent "+ Add an item" silently no-ops when no location exists
   (`pantry.py:120-121`).
6. **Destructive actions are uneven.** — *Partly fixed:* pantry item delete now confirms. Pantry item delete has *no* confirm
   (`pantry/index.html:147-152`); food merge is irreversible behind a generic confirm
   (`foods/index.html:76-88`); recipe Delete sits in the same button row as Promote
   (`view.html:177-205`). Meanwhile the one excellent undo (cook deductions,
   `deductions.html:17-24`) shows the pattern the rest of the app should copy — pantry
   adjustments even write the history rows for it (`services/pantry.py:8`), unused.
7. **Receipts can be stranded.** Cancel on review goes to /pantry leaving the receipt pending
   forever; there's no receipts index to find it again (`routers/receipts.py`).
8. **No logout / switch-user anywhere.** If her phone ends up on his session there's no
   recovery in the UI. Also: setting her PIN (`admin/devices.html:45-49`) has no confirm/reveal
   — a typo locks her out; and the pairing code/link has no copy button.
9. **Trip planner won't scale.** `/shopping/plan` renders every recipe as a checkbox with no
   search (`shopping.py:138-139`).
10. **Assistant has no memory affordance** — "New chat" discards the previous conversation
    irretrievably, and replies render markdown as literal `**bold**` in a `pre-wrap` bubble.

---

## Tier 3 — polish that separates "fine" from "enjoyable"

- **Primary buttons render as underlined link text on orange blocks** ("Cook this →" wraps to
  two lines, "Scan receipt" / "+ New recipe" / "What can I make?" all show as underlined text,
  screenshots confirm). Make `.btn-primary` anchors look like the buttons they are:
  no underline, `white-space: nowrap`, proper padding.
- **Developer vocabulary leaks everywhere a spouse would see it**: a "JSON" button in the main
  shopping toolbar; raw enums as UI text (`inbox` status chip, `(fixed)` /
  `(round_to_package)` in `_ingredients.html:12-13`, `pending` chip in deductions); "iCal";
  "Week of 2026-07-27" instead of "This week · Aug 1–7"; "Paste the page's HTML instead (for
  sites that block automated fetching)" on the primary add surface.
- **Almost no success feedback**: notes, ratings, rename, promote, preferences all reload
  silently. A tiny toast/banner ("Saved ✓") covers it. There is zero motion in the app — no
  check-off animation, no swap transitions (`app.css:182` guards animations that don't exist).
- **The designed empty state is dead code** — `.empty-state`/`.empty-emoji` (`app.css:124-127`)
  is referenced only by the orphaned Phase-0 `tab_empty.html`; every live empty state is a bare
  muted paragraph. Delete `tab_empty.html`, use the styled component everywhere.
- **Touch targets below the app's own 44px baseline**: shopping check ~26px, cook-mode
  skip/sub ~22px, timer cancel ~20px, rating stars ~25px, "+ Location" ~17px, rename ✏ ~15px.
- **No personalization** — `user.name` is available in every template and Home never says
  "What's cooking, Sarah?". Cheap delight, currently unused.
- **Images**: no width/height on any `<img>` (layout shift), one size serves card and hero,
  manual uploads stored raw (`recipes.py:41-56` bypasses the WEBP pipeline in
  `services/images.py`).

## What's already good (keep and lean into)

- Device pairing screen is clear and friendly; PIN input avoids iOS zoom correctly.
- Cook mode's one-step-at-a-time layout, localStorage step recovery, timer chips, and the
  "Watch this step" YouTube deep-link.
- Stock-take mode's 60px targets and reason-coded adjustments.
- The proposal pattern ("nothing changes until you Accept") and its explanatory copy.
- Deduction undo with compensating batches — the model for undo app-wide.
- Empty states that tell you what to do next (Home, Pantry, Shopping).
- Clipboard fallback for non-secure contexts in `shopping.js` is careful, correct work.

## What was fixed

All six Tier 1 items, plus the ingest-feedback and pantry-delete items from Tier 2 and the
button-styling, jargon, touch-target and `:active` items from Tier 3. Verified in a browser at
iPhone viewport, with regression tests covering the count-unit default, the scaling
follow-through, the inline check-off, and ingest retry/dismiss.

Two bugs surfaced while fixing these and were fixed with them: a `display:` rule outranks
`[hidden]`'s UA `display: none`, which would have left the new AI spinner and timer banner
permanently on screen and the ingredient rows' "more" fields permanently expanded; and the
wakeLock branch could throw without falling through to the video.

## Then fixed (second pass)

1. **Swallowed errors.** ~20 mutations wrapped their service call in `contextlib.suppress` and
   redirected regardless, so a rejection was indistinguishable from success. All of them now
   report through a shared flash helper (a query parameter, since the app has exactly one
   persistent cookie by contract). Two specific dead ends went with them: adding a pantry item
   before any location exists, and "Done shopping" with nothing to put away.
2. **Moving a planned meal.** `POST /plan/entry/{id}/move` existed with no caller; each entry
   now has a move control. The free-text `slot` beside it became a select, so a typo can no
   longer invent a new meal slot.
3. **Logout, and PIN confirmation.** There was no way out of a session anywhere; the name in
   both navs is now a Sign out form that revokes server-side as well as clearing the cookie.
   Setting a PIN asks twice, because a PIN is typed blind and a slip locked someone out.
4. **Empty states.** The designed `.empty-state` component was dead code, referenced only by an
   orphaned `tab_empty.html` that still said "you're on the Phase 0 foundation build". That file
   is deleted; the component now backs a shared partial used across cookbook, inbox, no-match
   search, pantry and shopping.
5. **Success feedback.** Ratings, notes, rename, promote, preferences and pantry edits all
   confirm what they did instead of reloading silently.

## Then fixed (third pass)

1. **Undo for pantry changes.** The history rows existed and nothing read them back, so "Used
   up", "Went bad" and Delete were one-way. Undo is offered on the confirmation banner itself
   and is single-shot, so a double tap cannot over-restore. Migration 015 adds `undone_at` to
   enforce that for a lone adjustment, and `undo_payload` to snapshot a deleted item — deleting
   drops the row, so without the snapshot a wrong delete is unrecoverable.
2. **Quantity mode chosen by what the food is.** Everything defaulted to the gauge, so
   countable things arrived as "half an avocado". `app/services/quantity_mode.py` infers from
   the name (longest whole-word phrase wins, so "coconut milk" gauges while "coconut" counts),
   with the aisle as a weaker fallback and gauge as the safe default. Migration 016 remembers
   what a person actually chose per food, which always beats the inference. An item's tracking
   can also be changed in place, carrying its level across.
3. **The hidden add-a-recipe surface** (Tier 2.4). The paste box lived only on Inbox, which has
   no nav tab, and the one prominent button led to the manual form — so the way nearly every
   recipe actually arrives was the hardest thing to find. `/add` now owns it: paste, drop, or
   bookmarklet a YouTube/recipe/Instagram link, with the job list underneath. It is a standing
   button at the top of the desktop rail, in the mobile tools row, and the primary button on
   every library tab; the manual form is one step further in, from `/add`. The bookmarklet
   (docs/04 §1.3, never built) is the PC counterpart of the Apple Shortcut: one click on any
   recipe page opens `/add?url=…` prefilled, then waits, because a GET must not queue a job.
   Re-pasting a known link now says "Already added: *title*" instead of reloading unchanged —
   the URL is its own idempotency key, so a resubmit produced no job and, past the inbox's
   two-minute recently-done window, no visible trace either.

## Then fixed (fourth pass) — nothing destructive is one-way any more

1. **Deleting a recipe.** Deleting cascaded ingredients, steps, tags, revisions, cook log and
   plan entries away, and `recipe_revisions` could not help because it cascades too — the
   snapshots kept "for cheap undo" are destroyed by the operation someone would want to undo.
   Migration 017 archives the recipe as JSON outside its foreign-key graph; restore rebuilds it
   through the normal create path, so ingredients, steps, tags and the FTS entry all come back,
   along with status, rating, notes and photo. The cook log does not come back — those rows
   referenced an id that no longer exists — so the confirmation says so when there is a log to
   lose, and points at Archive.
2. **Merging two foods.** A merge rewrites references across seven tables and deletes the
   source, after which nothing distinguishes the target's own references from inherited ones —
   so it was genuinely irreversible, on a screen whose target is picked from a list of every
   food. Migration 018 records a receipt of exactly which rows moved, which is the only thing
   that makes an undo possible; the confirmation now also states how many recipes and pantry
   items will move.

Both undos are single-shot, like the pantry and cook-batch ones: a replayed request is refused
rather than applied twice.

## Still open
- **Trip planner does not scale** (Tier 2.9) — every recipe as a checkbox, no search.
- **Receipts can still be stranded** (Tier 2.7) — no receipts index to find a pending one.
- **Assistant has no conversation history** (Tier 2.10), and replies render markdown literally.
- **No global search or "+" affordance**, and no motion anywhere (Tier 3).
- From the docs-vs-code gap analysis: big-event mode, re-extract comparison, the embedded
  per-step video player, and structured after-cook quantities remain unbuilt.
