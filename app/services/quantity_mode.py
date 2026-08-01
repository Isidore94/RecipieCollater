"""Deciding how a food should be tracked in the pantry: counted, gauged, or have/out.

The pantry supports three granularities (docs/06 section 1) and the right one is a property of
the food itself:

* ``exact``  - things that come in discrete units you would naturally count. Avocados, eggs,
  lemons, tins, jars, yoghurt pots. "Half an avocado" is not an amount anyone means; "3" is.
* ``gauge``  - loose or bulk staples where counting is meaningless and weighing is a chore.
  Flour, rice, oil, sugar. Full / half / low / out is exactly how people actually think here.
* ``binary`` - things you either have or you don't, where the amount never drives a decision.
  Spices, condiments, vinegar, baking powder.

Everything used to default to ``gauge``, which is right for a minority of a real pantry.

The rules below are deterministic and inspectable on purpose: this runs on every pantry add,
including the automatic ones from receipt import and restock, so it must be free, instant, and
explainable. It is also only ever a *suggestion* - ``foods.default_quantity_mode`` records what
a person actually chose, and that always wins (see ``suggest``).

Matching is on whole words, so "oil" does not match "boiled" and "can" does not match "pecan".
Longer phrases are checked before single words, so "coconut milk" (gauge) is not decided by
"coconut" (count).
"""

from __future__ import annotations

import re
import sqlite3

EXACT = "exact"
GAUGE = "gauge"
BINARY = "binary"

# Produce and packaged goods bought as discrete units. The plural is not listed separately;
# matching strips a trailing "s"/"es" first.
_COUNT_TERMS: frozenset[str] = frozenset({
    # Produce sold by the piece
    "avocado", "apple", "banana", "lemon", "lime", "orange", "grapefruit", "pear", "peach",
    "plum", "nectarine", "mango", "kiwi", "pineapple", "melon", "coconut", "pomegranate",
    "onion", "shallot", "garlic bulb", "potato", "sweet potato", "carrot", "parsnip", "turnip",
    "beetroot", "cucumber", "courgette", "zucchini", "aubergine", "eggplant", "pepper",
    "bell pepper", "chilli", "chili", "tomato", "corn", "corn on the cob", "cabbage",
    "cauliflower", "broccoli", "lettuce", "leek", "celery", "fennel", "artichoke", "squash",
    "pumpkin", "swede",
    # Everyday countables
    "egg", "lemon wedge", "bread", "loaf", "baguette", "bagel", "bun", "roll", "tortilla",
    "wrap", "pitta", "pita", "naan", "muffin", "croissant", "crumpet",
    # Packaged units
    "tin", "can", "jar", "packet", "pack", "bottle", "carton", "box", "tub", "pot", "sachet",
    "block", "bar", "tray", "punnet", "bag",
    # Proteins that arrive as pieces
    "chicken breast", "chicken thigh", "chicken leg", "drumstick", "steak", "chop", "sausage",
    "burger", "patty", "fillet", "cutlet", "rasher",
})

# Loose or bulk staples: measured by weight or volume, topped up rather than counted.
_GAUGE_TERMS: frozenset[str] = frozenset({
    "flour", "sugar", "rice", "pasta", "spaghetti", "noodle", "couscous", "quinoa", "bulgur",
    "lentil", "oat", "oatmeal", "porridge", "cereal", "muesli", "granola", "breadcrumb",
    "cornflour", "cornstarch", "semolina", "polenta",
    "oil", "olive oil", "vegetable oil", "sunflower oil", "butter", "margarine", "lard", "ghee",
    "milk", "cream", "yoghurt", "yogurt", "buttermilk", "coconut milk", "stock", "broth",
    "juice", "water", "wine", "beer",
    "salt", "cheese", "honey", "syrup", "maple syrup", "jam", "marmalade", "peanut butter",
    "chocolate", "cocoa", "coffee", "tea", "nut", "almond", "walnut", "cashew", "pecan",
    "raisin", "sultana", "date", "dried fruit", "seed", "spinach", "kale", "salad", "herb",
    "mince", "minced beef", "ground beef", "bacon", "ham",
})

# Things whose amount never drives a decision: you either have them or you have run out.
_BINARY_TERMS: frozenset[str] = frozenset({
    "pepper", "black pepper", "cumin", "coriander", "paprika", "turmeric", "cinnamon", "nutmeg",
    "ginger", "oregano", "thyme", "rosemary", "basil", "bay leaf", "chilli powder",
    "chili powder", "curry powder", "garam masala", "cayenne", "clove", "cardamom", "saffron",
    "vanilla", "vanilla extract", "spice", "seasoning", "stock cube", "bouillon",
    "baking powder", "baking soda", "bicarbonate of soda", "yeast", "gelatine", "gelatin",
    "vinegar", "balsamic", "soy sauce", "fish sauce", "worcestershire", "mustard", "ketchup",
    "mayonnaise", "mayo", "hot sauce", "tabasco", "sriracha", "tomato puree", "tomato paste",
    "food colouring", "food coloring",
})

# The shopping aisle is a weaker signal than the name, used only when no term matches.
_CATEGORY_DEFAULTS: dict[str, str] = {
    "produce": EXACT,
    "fruit": EXACT,
    "vegetables": EXACT,
    "bakery": EXACT,
    "tins": EXACT,
    "canned": EXACT,
    "tinned": EXACT,
    "spices": BINARY,
    "herbs": BINARY,
    "condiments": BINARY,
    "sauces": BINARY,
    "baking": GAUGE,
    "dry goods": GAUGE,
    "grains": GAUGE,
    "dairy": GAUGE,
    "frozen": GAUGE,
}

# A pantry is more bulk staple than anything else, and a gauge is the least annoying thing to be
# wrong about: it needs no number and no decision.
_FALLBACK = GAUGE

_WORD_SPLIT = re.compile(r"[^a-z]+")

# "2 avocados", "500g flour", "a tin of tomatoes" — the amount is not part of the food's identity.
_LEADING_QUANTITY = re.compile(r"^\s*[\d./]+\s*[a-z]{0,4}\s+(?:of\s+)?", re.IGNORECASE)


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"          # berries -> berry
    if word.endswith("oes") and len(word) > 4:
        return word[:-2]                # potatoes -> potato, tomatoes -> tomato
    if word.endswith("es") and len(word) > 3 and word[-3] in "sxzh":
        return word[:-2]                # peaches -> peach, dishes -> dish
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]                # apples -> apple, but not "glass"
    return word


def _normalise(name: str) -> str:
    """Lower-case, drop a leading amount, and singularise each word for matching."""
    text = _LEADING_QUANTITY.sub("", name.strip().lower())
    words = [w for w in _WORD_SPLIT.split(text) if w]
    return " ".join(_singular(w) for w in words)


def _matches(normalised: str, terms: frozenset[str]) -> str | None:
    """The longest term that appears as a whole word (or phrase) in the name, if any."""
    padded = f" {normalised} "
    best: str | None = None
    for term in terms:
        if f" {term} " in padded and (best is None or len(term) > len(best)):
            best = term
    return best


def infer(name: str, *, category: str | None = None) -> str:
    """Suggest a tracking mode for a food name, ignoring anything already recorded for it.

    Whichever list contributes the *longest* matching phrase wins, so "coconut milk" is gauged
    even though "coconut" alone is counted, and "chilli powder" is a have/out even though
    "chilli" alone is counted.
    """
    normalised = _normalise(name)
    if not normalised:
        return _FALLBACK

    candidates = [
        (_matches(normalised, _COUNT_TERMS), EXACT),
        (_matches(normalised, _GAUGE_TERMS), GAUGE),
        (_matches(normalised, _BINARY_TERMS), BINARY),
    ]
    matched = [(term, mode) for term, mode in candidates if term is not None]
    if matched:
        # Ties are impossible between equal-length terms in different lists in practice; sorting
        # by length keeps the choice deterministic if one is ever added.
        matched.sort(key=lambda pair: (-len(pair[0]), pair[1]))
        return matched[0][1]

    if category:
        key = category.strip().lower()
        if key in _CATEGORY_DEFAULTS:
            return _CATEGORY_DEFAULTS[key]

    return _FALLBACK


def suggest(conn: sqlite3.Connection, name: str, *, food_id: int | None = None) -> str:
    """The mode to use for a food: what someone chose before, else what the name implies.

    A recorded choice always wins. Inference is a convenience for the first time a food is seen;
    it must never override a household that has said "we count our rice".
    """
    row = None
    if food_id is not None:
        row = conn.execute(
            "SELECT default_quantity_mode, category FROM foods WHERE id = ?", (food_id,)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT f.default_quantity_mode, f.category FROM foods f "
            "LEFT JOIN food_aliases a ON a.food_id = f.id "
            "WHERE f.name = ? COLLATE NOCASE OR a.alias = ? COLLATE NOCASE LIMIT 1",
            (name.strip(), name.strip()),
        ).fetchone()

    if row is not None and row["default_quantity_mode"]:
        return str(row["default_quantity_mode"])
    return infer(name, category=row["category"] if row is not None else None)


def remember(
    conn: sqlite3.Connection, food_id: int, mode: str, *, commit: bool = True
) -> None:
    """Record that this food is tracked this way, so nothing has to guess again."""
    if mode not in (EXACT, GAUGE, BINARY):
        raise ValueError(f"unknown quantity mode: {mode!r}")
    conn.execute("UPDATE foods SET default_quantity_mode = ? WHERE id = ?", (mode, food_id))
    if commit:
        conn.commit()
