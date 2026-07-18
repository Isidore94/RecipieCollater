"""Provider-agnostic types for AI recipe extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.extraction import ExtractedRecipe

# System prompt for extracting a recipe from source text (a web page or a video transcript).
EXTRACT_SYSTEM = (
    "You extract a single cooking recipe from the provided text, which may be a recipe web page "
    "or a cooking video's title, description, and transcript. "
    "Use only what the text states - never invent ingredients, steps, times, or yields. "
    "If a field is absent, omit it. Copy each ingredient line verbatim into original_text. "
    "If the text contains no recipe, return a title with empty ingredients and steps."
)

# System prompt for drafting a recipe from a cook's plain-language description (manual entry).
DRAFT_SYSTEM = (
    "You help a home cook turn a plain-language description into a clean, structured recipe. "
    "Build the title, ingredients, and numbered steps from what they describe. You may fill in "
    "conventional details they clearly imply (typical amounts, obvious prep steps, usual times), "
    "but do not invent a different dish or ingredients they did not mention. For each ingredient "
    "fill quantity_text, unit, and food separately (e.g. '2', 'cups', 'flour') and also put the "
    "whole line in original_text. If the description is vague, still return a best-effort recipe."
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


class RecipeExtractor(Protocol):
    """What the pipeline and manual-draft path need from any AI provider."""

    provider: str
    model: str

    def extract(self, content: str, *, source_url: str) -> AIExtraction: ...

    def draft(self, description: str) -> AIExtraction: ...
