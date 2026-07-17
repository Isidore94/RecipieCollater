# RecipeCollater — Pantry, Shopping & Meal Planning

The pantry is the differentiator: neither Mealie nor Tandoor has real inventory (their users beg for Grocy integration), and Grocy itself — the reference implementation — is abandoned by most families within weeks. The documented failure evidence tells us exactly what to build and what to refuse to build.

## 1. Why pantry tracking fails (and our answers)

| Documented failure mode | Our design answer |
|---|---|
| Logging every fridge withdrawal is unrealistic; one midnight snack desyncs the ledger | **Cooking is the main consumption event.** The first cook reviews proposed deductions; confirmed mappings are remembered and can become automatic per recipe. Off-recipe use is fixed with one-tap used-up/correction actions. |
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
- **Remove / used-up / spoiled**: every pantry item has a quick action to take it out — swipe-to-remove on mobile, a "⋯ → Remove" menu on desktop — with an optional one-tap reason (**used up** / **went bad** / **removing**). This is the escape valve for the pantry's approximate nature: things spoil, get finished off-recipe, or were miscounted, and fixing that must be effortless. Removal sets an exact item to 0 (or deletes the row), flips a gauge/binary item to `out`, and writes a `pantry_adjustments` row so history and undo work. A "went bad" reason is the only one surfaced back to the AI ("you've tossed spinach twice this month — buy less?") — everything else is silent.
- **Freezer location flag** exists in the schema for future expiry logic (freeze/thaw), but v1 keeps expiry manual and optional.

### 2.1 Trust-building cook-through deduction

When a recipe is first marked cooked, the app shows a compact proposed-deduction sheet. The goal is not permanent confirmation fatigue; it is to earn trust once per recipe. Confirmed item mappings, opt-outs, scaling behavior, and gauge choices are remembered. After a successful review the user may enable **auto-apply for this recipe**. Every applied batch still gets a quiet reversible summary.

**What gets deducted, per ingredient:**
- **Amount** = the "what I actually used" value if captured, else the deterministic scaled amount. `Decimal` input converts to canonical integer mg/µL/milli-each; no binary floats or model arithmetic.
- **Exact-quantity pantry items** are decremented by the converted amount. Crossing zero sets the item to 0 and (if it's a staple or below its threshold) drops it onto the shopping list.
- **Gauge items** (full/half/low/out bulk staples) and **binary items** (have/out) are **not** silently arithmetic'd — there's no amount to subtract from a "half". By default they're left untouched but listed in the summary as "used some?" chips so one tap steps a gauge down or flips a binary to out. (A per-item "step down when cooked" opt-in exists for things you burn through fast.)
- `fixed`, `to_taste`, unresolved `round_to_package`, approximate, alternative, divided-without-amount, and unmatched ingredients are skipped or explicitly reviewed. They never silently qualify for auto-apply.

**What the user controls (the bot's defaults are all editable):**
- **Global default is Review first.** Auto-apply is a per-recipe trust decision. A global "always review" option can forbid recipe-level auto mode.
- **The post-cook summary** always appears: "Deducted from pantry: rice −200 g, chicken −4 → Undo · Edit". **Edit** opens the line-by-line list; **Undo** reverses the entire deduction in one tap (it's a single `pantry_adjustments` batch keyed to the `cook_log` row).
- **Per-ingredient deduct toggle** on the recipe: tracked, unambiguous lines default to eligible; approximate, `to_taste`, alternative, and unresolved package lines default to "don't deduct." The recipe remembers confirmed choices.
- **Which pantry item** an ambiguous ingredient maps to is editable and remembered (e.g. "chicken" → "chicken thighs, downstairs freezer").
- Auto-apply eligibility requires a confirmed food and pantry item, compatible dimensions or an explicit food bridge, an unambiguous amount, and no unresolved alternative. Any later recipe edit that affects an eligible ingredient revokes trust for that line until reviewed again.

**Data**: proposal and application are separate. Applying writes one transaction containing the cook log, exact pantry updates, and a `batch_id`-grouped adjustment history. Undo is a compensating adjustment batch, not deletion of history.
- Every recipe sheet shows **"you have 7 of 9 ingredients"**; approximate, `to_taste`, and unquantified lines are excluded so a missing salt entry does not make a recipe look impossible.
- **"Use it up" rail** (Grocy's Due Score, reborn): recipes ranked by how many soon-to-expire or overstocked items they consume — also exposed to the AI planner as an objective.

## 3. Shopping list

Built from three sources, always visible as one list:
1. Meal-plan entries (date range → ingredients, scaled by per-entry servings) **minus** pantry on hand
2. Staples at/below threshold (`out` gauge or below min quantity)
3. Manual adds (type-ahead, or "add missing from this recipe" on any recipe sheet)

Mechanics:
- **Aggregation done right**: merge per `(food, dimension)`, sum canonical integer mg/µL/milli-each, bridge dimensions only through explicit food conversions, and leave approximate/unquantified items as notes. Each line shows provenance.
- **Aisle grouping** from `foods.category`, user-overridable, order persisted per household (Tandoor's most-loved shopping feature).
- **Away-from-home v1**: one tap copies/shares an aisle-grouped list; an optional Apple Shortcut fetches it while home and creates items in a native Reminders/Notes list. The local web list remains excellent while on Wi-Fi. Custom offline outbox/sync is postponed until real trips prove native export inadequate.
- If offline web synchronization is later approved, first write a versioned operation/conflict protocol and two-device test matrix. Do not improvise it inside page JavaScript.

## 4. Meal planning

- **Week board**: 7 columns (or stacked cards on mobile), free-text slots (default "dinner" — no fixed meal-type enum; it's a top Mealie complaint), entries carry **per-entry servings** so guests scale the shopping math.
- Entries are recipes **or notes** ("leftovers", "pizza out") — leftovers are first-class plannable placeholders.
- Drag-drop on desktop; tap-to-assign on mobile.
- **Saved menus**: save any week as a reusable template ("standard week", "Christmas"), re-apply later (Plan to Eat's most-loved feature).
- **"Plan my week" AI button** seeds the board via the proposal pattern: constraints UI for tier mix, time budgets per weekday, dietary notes → assistant proposes → accept/edit.
- Household preferences are structured inputs: allergies and exclusions are hard constraints; dislikes, diet, equipment, weekday active/elapsed time limits, leftovers, and tier mix are soft preferences. The deterministic filter runs before any model call.
- One tap from plan → generated shopping list for the date range.
- iCal export of the plan (cheap, loved in Tandoor).

## 5. Big-event mode

A meal plan variant for "company for N people":
- Pick/ask the AI for a multi-dish menu (Company-tier bias, make-ahead bias).
- Application services scale every dish to N using per-ingredient behavior; the model does not perform authoritative quantity math.
- The planner uses recipe metadata for max practical batch size, oven temperature/space, burner/equipment use, holding time, storage, and make-ahead windows. It flags impossible capacity instead of pretending serving multiplication solves it.
- Deterministic combined shopping list across all dishes.
- AI-drafted **prep timeline** rendered as a checklist grouped by "day before / morning of / last hour", then validated against recipe steps and capacity constraints before acceptance.
