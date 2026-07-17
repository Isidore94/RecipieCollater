"""Provider-agnostic types for AI recipe extraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.extraction import ExtractedRecipe


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
    """What the pipeline needs from any AI provider."""

    provider: str
    model: str

    def extract(self, content: str, *, source_url: str) -> AIExtraction: ...
