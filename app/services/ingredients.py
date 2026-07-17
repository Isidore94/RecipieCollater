"""Ingredient-line normalization: turn a raw line into a structured RecipeInput ingredient.

Ingestion yields raw ingredient lines ("2 tablespoons olive oil, finely chopped"). This module
splits off a leading quantity, matches a unit against the ontology, and separates the food from a
trailing note, so the ingredient can be scaled (docs/04 normalization). It is deliberately
conservative: a line is only structured when the amount parses AND the unit resolves in the
ontology - otherwise the original text is kept verbatim (still shown, just not auto-scaled). This
avoids ever emitting an amount without a unit, which the recipe validator rejects, and keeps a
mis-parse from corrupting the recipe. The original_text is always preserved.

parse_quantity_prefix and split_food_note are pure and unit-tested; unit resolution needs the DB.
"""

from __future__ import annotations

import re
import sqlite3

from app.extraction import ExtractedIngredient
from app.services import quantity, recipes, units

# Unicode vulgar fractions -> ASCII, so quantity.parse_quantity (which speaks "1/2", "1 1/2") copes.
_UNICODE_FRACTIONS = {
    "¼": "1/4", "½": "1/2", "¾": "3/4",
    "⅓": "1/3", "⅔": "2/3",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
    "⅙": "1/6", "⅚": "5/6",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}

# A leading amount: a mixed number, a fraction, or a decimal/integer (ranges are left unstructured).
_QUANTITY_RE = re.compile(r"^\s*(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)\s*(.*)$", re.DOTALL)


def _normalize_unicode_fractions(text: str) -> str:
    out: list[str] = []
    for char in text:
        replacement = _UNICODE_FRACTIONS.get(char)
        if replacement is None:
            out.append(char)
            continue
        if out and out[-1].isdigit():  # "1½" -> "1 1/2"
            out.append(" ")
        out.append(replacement)
    return "".join(out)


def parse_quantity_prefix(text: str) -> tuple[str | None, str]:
    """Split a leading amount off an ingredient line -> (quantity_text | None, remainder)."""
    normalized = _normalize_unicode_fractions(text.strip())
    match = _QUANTITY_RE.match(normalized)
    if match is None:
        return None, text.strip()
    return match.group(1).strip(), match.group(2).strip()


def split_food_note(text: str) -> tuple[str | None, str | None]:
    """Separate a food name from a trailing note (a parenthetical or a ', ...' clause)."""
    note_parts: list[str] = []
    paren = re.search(r"\(([^)]*)\)", text)
    if paren is not None:
        inner = paren.group(1).strip()
        if inner:
            note_parts.append(inner)
        text = (text[: paren.start()] + text[paren.end() :]).strip()
    food = text
    if "," in text:
        food, _, trailing = text.partition(",")
        trailing = trailing.strip()
        if trailing:
            note_parts.append(trailing)
    return (food.strip() or None), (", ".join(note_parts) or None)


def _match_unit(conn: sqlite3.Connection, remainder: str) -> tuple[str, str] | None:
    """Match a 2- then 1-word unit at the start of ``remainder`` -> (unit_text, rest) or None."""
    words = remainder.split()
    for count in (2, 1):
        if len(words) > count:  # keep at least one word for the food
            candidate = " ".join(words[:count]).lower().strip(".,")
            if units.resolve_unit(conn, candidate) is not None:
                return candidate, " ".join(words[count:])
    return None


def normalize_ingredient(
    conn: sqlite3.Connection, item: ExtractedIngredient
) -> recipes.IngredientInput:
    """Structure one extracted ingredient; falls back to the raw line when it can't be parsed."""
    text = item.original_text

    # Trust the extractor's own structuring when the amount parses and the unit resolves.
    if (
        item.quantity_text
        and item.unit
        and units.resolve_unit(conn, item.unit.lower()) is not None
        and _amount_ok(item.quantity_text)
    ):
        return recipes.IngredientInput(
            original_text=text, section=item.section, quantity_text=item.quantity_text,
            unit=item.unit.lower(), food=(item.food or None), note=(item.note or None),
        )

    qty, remainder = parse_quantity_prefix(text)
    if qty is not None and _amount_ok(qty):
        matched = _match_unit(conn, remainder)
        if matched is not None:
            unit_text, rest = matched
            food, note = split_food_note(rest)
            return recipes.IngredientInput(
                original_text=text, section=item.section, quantity_text=qty,
                unit=unit_text, food=food, note=note,
            )

    # Unstructured: keep the line exactly as written (still usable, just not auto-scaled).
    return recipes.IngredientInput(
        original_text=text, section=item.section,
        food=(item.food or None), note=(item.note or None),
    )


def _amount_ok(text: str) -> bool:
    try:
        return quantity.parse_quantity(text) > 0
    except quantity.QuantityError:
        return False
