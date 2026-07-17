"""AI extraction: the provider-agnostic interface plus the Anthropic implementation.

The pipeline depends only on the :class:`RecipeExtractor` protocol and :func:`get_provider`;
which concrete provider (and whether one exists at all) is a config decision. With no API key
configured, get_provider returns None and the pipeline simply skips the LLM fallback.
"""

from __future__ import annotations

from app.ai.base import AIBudgetError, AIError, AIExtraction, RecipeExtractor
from app.config import Settings

__all__ = ["AIBudgetError", "AIError", "AIExtraction", "RecipeExtractor", "get_provider"]


def get_provider(settings: Settings) -> RecipeExtractor | None:
    """Return the configured recipe extractor, or None when AI extraction is disabled."""
    if not settings.ai_enabled:
        return None
    from app.ai.anthropic_provider import AnthropicExtractor  # lazy: SDK import (CONVENTIONS 4)

    try:
        return AnthropicExtractor.from_settings(settings)
    except AIError:
        return None
