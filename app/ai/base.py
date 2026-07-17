"""Provider-agnostic types for AI recipe extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.extraction import ExtractedRecipe


class AIError(RuntimeError):
    """An AI provider call failed: network error, refusal, or output that failed validation."""


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
    """What the pipeline needs from any AI provider."""

    provider: str
    model: str

    def extract(self, content: str, *, source_url: str) -> AIExtraction: ...
