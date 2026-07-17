# RecipeCollater — Pantry, Shopping & Meal Planning

The pantry is the differentiator: neither Mealie nor Tandoor has real inventory (their users beg for Grocy integration), and Grocy itself — the reference implementation — is abandoned by most families within weeks. The documented failure evidence tells us exactly what to build and what to refuse to build.

## 1. Why pantry tracking fails (and our answers)

| Documented failure mode | Our design answer |
|---|---|
| Logging every fridge withdrawal is unrealistic; one midnight snack desyncs the ledger | **Cooking is the consumption event.** After-cook flow proposes decrements from "what I actually used" — confirm in bulk, one tap. No other consumption logging exists. |
| Exact quantities forced on everything (onions defeat unit modeling) | **Graduated granularity:** exact counts for discrete items (cans, jars), full/half/low/out gauge for bulk staples (flour, rice, oil), have/out binary for condiments & snacks. Don't track fresh produce unless a meal plan needs it. |
| Heavy master-data setup; can't create a location inline | Everything creatable inline, from the field where you need it. Setup = type your location names, done. |
| Drift makes the data worthless, which kills motivation | **Self-healing:** the pantry is explicitly approximate. Stock-take mode = walk one location, big tap targets, 1–2 taps per item, 5 minutes before the weekly shop. The AI reconciles conversationally ("we're out of eggs, used about half the rice"). |
| Whole-kitchen perfectionism burns people out in week one | Onboarding starts with **one high-value zone** — the freezer is the documented success case (few, expensive, invisible, long-lived items). Feature-flag the pantry entirely for households still forming the habit. |

## 2. Pantry UX specification

- **Location tabs/chips** across the top (user-defined: Kitchen Cupboards, Upstairs Pantry Fridge, Downstairs Pantry, Downstairs Freezer), then an item grid with name, gauge/count, expiry badge where set.
- **Add item**: one autocomplete field (searches foods + aliases; free-text allowed) → tap a location chip → done. Optional: quantity, expiry, staple toggle — never required.
- **Adjust**: tapping an item cycles its gauge (full → half → low → out) or shows +/- steppers for exact mode. Two taps max.
- **Stock-take mode**: full-screen per-location sweep, every item one row with giant gauge buttons; "haven't seen it? mark out". Designed for one-handed phone use standing in the pantry.
- **Restock flow**: checking off shopping-list items after a grocery run offers "add these back to pantry → pick locations" as one bulk screen.
- **Freezer location flag** exists in the schema for future expiry logic (freeze/thaw), but v1 keeps expiry manual and optional.
- Every recipe sheet shows **"you have 7 of 9 ingredients"** (matched via food ids; `approx`/non-scalable items like spices excluded from the score so recipes never look unmakeable for lack of a salt entry). A "cook from what we have" browse filter sorts the cookbook by match percentage.
- **"Use it up" rail** (Grocy's Due Score, reborn): recipes ranked by how many soon-to-expire or overstocked items they consume — also exposed to the AI planner as an objective.

## 3. Shopping list

Built from three sources, always visible as one list:
1. Meal-plan entries (date range → ingredients, scaled by per-entry servings) **minus** pantry on hand
2. Staples at/below threshold (`out` gauge or below min quantity)
3. Manual adds (type-ahead, or "add missing from this recipe" on any recipe sheet)

Mechanics:
- **Aggregation done right** (Grocy has open bugs here): merge per `(food, dimension)`, sum in canonical g/ml, bridge count↔mass only via per-food conversions, never merge `approx`/unquantified items — they append as notes. Each line shows its provenance: "5 onions — Pasta ×2, Curry, staple".
- **Aisle grouping** from `foods.category`, user-overridable, order persisted per household (Tandoor's most-loved shopping feature).
- **In-store mode**: big checkboxes, one-handed reach, checked items sink to the bottom, undo. This view is the app's **one client-rendered island** (see `02-architecture.md` §6): an Alpine store rendering from a local snapshot + pending-ops outbox in localStorage, flushed through one idempotent `POST /api/shopping/sync` batch endpoint. A dead spot in the store never blanks the list or loses a check-off. Full sync engines (Replicache/PowerSync/etc.) are deliberately out of scope — per-item last-writer-wins with UUID adds and tombstone deletes covers a family list completely.
- Copy-as-text export for anyone who wants it in Notes/Bring.

## 4. Meal planning

- **Week board**: 7 columns (or stacked cards on mobile), free-text slots (default "dinner" — no fixed meal-type enum; it's a top Mealie complaint), entries carry **per-entry servings** so guests scale the shopping math.
- Entries are recipes **or notes** ("leftovers", "pizza out") — leftovers are first-class plannable placeholders.
- Drag-drop on desktop; tap-to-assign on mobile.
- **Saved menus**: save any week as a reusable template ("standard week", "Christmas"), re-apply later (Plan to Eat's most-loved feature).
- **"Plan my week" AI button** seeds the board via the proposal pattern: constraints UI for tier mix, time budgets per weekday, dietary notes → assistant proposes → accept/edit.
- One tap from plan → generated shopping list for the date range.
- iCal export of the plan (cheap, loved in Tandoor).

## 5. Big-event mode

A meal plan variant for "company for N people":
- Pick/ask the AI for a multi-dish menu (Company-tier bias, make-ahead bias).
- Every dish scaled to N via the standard scaler.
- Combined shopping list across all dishes.
- AI-generated **prep timeline** rendered as a checklist grouped by "day before / morning of / last hour" — this is where multi-serving complex-meal planning pays off.
