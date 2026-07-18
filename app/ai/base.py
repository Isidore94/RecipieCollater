"""Provider-agnostic types for AI recipe and receipt extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.extraction import ExtractedReceipt, ExtractedRecipe

# Controlled tag vocabulary appended to every prompt. Free-text tag sprawl makes filter chips
# useless and the Phase-5 assistant's hard-filters unreliable; a small fixed vocabulary keeps
# "I have chicken, plan me dinner" answerable (docs/07 section 2 Cookbook filters).
TAG_GUIDE = (
    " Also fill tags (lowercase) with AT MOST one from each group that clearly applies: "
    "meal: breakfast, lunch, dinner, side, dessert, snack, drink; "
    "protein: chicken, beef, pork, seafood, lamb, turkey, vegetarian; "
    "method: baked, grilled, stovetop, slow-cooked, instant-pot, air-fryer, no-cook; "
    "effort: weeknight (under ~45 minutes hands-off-ish) or project (a weekend cook); "
    "plus the cuisine as one lowercase word (e.g. italian, mexican, thai) when obvious. "
    "Never more than 6 tags; omit a group rather than guess."
)

# System prompt for extracting a recipe from source text (a web page or a video transcript).
EXTRACT_SYSTEM = (
    "You extract a single cooking recipe from the provided text, which may be a recipe web page "
    "or a cooking video's title, description, and transcript. "
    "Use only what the text states - never invent ingredients, steps, times, or yields. "
    "If a field is absent, omit it. Copy each ingredient line verbatim into original_text. "
    "If the text contains no recipe, return a title with empty ingredients and steps."
    + TAG_GUIDE
)

# System prompt for drafting a recipe from a cook's plain-language description (manual entry).
DRAFT_SYSTEM = (
    "You help a home cook turn a plain-language description into a clean, structured recipe. "
    "Build the title, ingredients, and numbered steps from what they describe. You may fill in "
    "conventional details they clearly imply (typical amounts, obvious prep steps, usual times), "
    "but do not invent a different dish or ingredients they did not mention. For each ingredient "
    "fill quantity_text, unit, and food separately (e.g. '2', 'cups', 'flour') and also put the "
    "whole line in original_text. If the description is vague, still return a best-effort recipe."
    + TAG_GUIDE
)


# System prompt for reading a grocery receipt photo or a pasted online order (Phase 4.7).
# The generalization rule is the heart of it: receipts speak in store abbreviations and brands;
# recipes and the pantry speak in short generic food names. The household's existing food list is
# supplied in the user content so the model converges on THIS kitchen's vocabulary.
RECEIPT_SYSTEM = (
    "You read a grocery receipt (photographed) or a pasted online grocery order and list the "
    "FOOD items that were bought. For each item fill: original_text = the item's line exactly as "
    "printed, without the price; name = the product with abbreviations expanded "
    "('KS ORG CHKN BRST' -> 'Kirkland organic chicken breast'); food = the short, generic, "
    "lowercase kitchen name a recipe would use, stripped of brand, size, packaging and marketing "
    "words ('black beans', 'chicken breast', 'olive oil') - when a HOUSEHOLD FOODS list is "
    "provided and one of its names fits the item, use that exact name; quantity_text = how many "
    "units were bought ('2', default '1'); size_text = one unit's pack size when shown "
    "('15 oz', '2 kg'). Skip tax, deposits, refunds, coupons, bag fees, loyalty/points lines and "
    "clearly non-food items (detergent, batteries). If nothing is a grocery item, return an "
    "empty items list."
)


class AIError(RuntimeError):
    """An AI provider call failed: network error, refusal, or output that failed validation.

    Carries the billed token usage when the *failing* call was still charged (e.g. the model
    returned a truncated/unparseable response), so the pipeline can count that spend against the
    cap. Zero when the call never reached the provider (so nothing was billed).
    """

    def __init__(
        self,
        message: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_micros: int = 0,
    ) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_micros = cost_micros


class AIBudgetError(AIError):
    """The configured spend cap would be exceeded; no call was made."""


@dataclass(frozen=True, slots=True)
class AIExtraction:
    """A successful extraction plus the accounting needed to log spend."""

    recipe: ExtractedRecipe
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_micros: int


@dataclass(frozen=True, slots=True)
class AIReceipt:
    """A successful receipt/order parse plus the accounting needed to log spend."""

    receipt: ExtractedReceipt
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_micros: int


class RecipeExtractor(Protocol):
    """What the pipeline, manual-draft, and receipt paths need from any AI provider."""

    provider: str
    model: str

    def extract(self, content: str, *, source_url: str) -> AIExtraction: ...

    def draft(self, description: str) -> AIExtraction: ...

    def receipt(self, content: str, *, image_jpeg: bytes | None = None) -> AIReceipt: ...
