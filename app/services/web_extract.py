"""schema.org / JSON-LD recipe extraction via recipe-scrapers.

This is the ingestion fast-path (docs/04-ingestion-pipeline.md 3): most recipe sites embed a
``Recipe`` JSON-LD block, so no LLM call is needed. recipe-scrapers is a heavy import (BeautifulSoup
+ lxml), so it is imported lazily inside the worker (CONVENTIONS 4). It runs on an HTML *string* -
no network - which is what lets the whole pipeline be tested offline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from app.extraction import ExtractedIngredient, ExtractedRecipe, ExtractedStep

_T = TypeVar("_T")

# recipe-scrapers raises (SchemaOrgException, NotImplementedError, ElementNotFoundInHtml, ...)
# for any field a page does not provide; each accessor is wrapped so an absent field is just empty.


def _try(fn: Callable[[], _T], default: _T) -> _T:
    try:
        return fn()
    except Exception:
        return default


def _try_list(fn: Callable[[], list[_T]]) -> list[_T]:
    try:
        return fn()
    except Exception:
        return []


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def extract_from_html(html: str, url: str) -> ExtractedRecipe | None:
    """Extract a recipe from HTML via schema.org; return None if no usable recipe is present."""
    from recipe_scrapers import scrape_html  # lazy: bs4/lxml (CONVENTIONS 4)

    try:
        scraper = scrape_html(html, org_url=url, supported_only=False)
    except Exception:  # unparseable HTML / no recipe schema at all
        return None

    title = _try(scraper.title, "").strip()
    if not title:
        return None

    ingredients: list[ExtractedIngredient] = []
    for group in _try_list(scraper.ingredient_groups):
        section = getattr(group, "purpose", None) or None
        for line in getattr(group, "ingredients", []):
            text = str(line).strip()
            if text:
                ingredients.append(ExtractedIngredient(original_text=text, section=section))
    if not ingredients:  # some scrapers implement ingredients() but not ingredient_groups()
        for line in _try_list(scraper.ingredients):
            text = str(line).strip()
            if text:
                ingredients.append(ExtractedIngredient(original_text=text))

    steps = [
        ExtractedStep(instruction=text)
        for raw in _try_list(scraper.instructions_list)
        if (text := str(raw).strip())
    ]

    tags: list[str] = []
    category = _try(scraper.category, None)
    if isinstance(category, str) and category.strip():
        tags.append(category.strip())

    return ExtractedRecipe(
        title=title,
        description=(_try(scraper.description, None) or None),
        servings_text=(_try(scraper.yields, None) or None),
        prep_minutes=_positive_int(_try(scraper.prep_time, None)),
        cook_minutes=_positive_int(_try(scraper.cook_time, None)),
        total_minutes=_positive_int(_try(scraper.total_time, None)),
        image_url=(_try(scraper.image, None) or None),
        source_name=(_try(scraper.host, None) or None),
        ingredients=ingredients,
        steps=steps,
        tags=list(dict.fromkeys(tags)),  # dedupe, keep order
    )
