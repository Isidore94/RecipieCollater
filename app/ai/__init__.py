"""AI extraction: the provider-agnostic interface plus the Anthropic and OpenAI implementations.

The pipeline depends only on the :class:`RecipeExtractor` protocol and :func:`get_provider`;
which concrete provider (and whether one exists at all) is a config decision. With no API key
configured, get_provider returns None and the pipeline simply skips the LLM fallback.

Provider choice follows Settings.ai_provider: "anthropic" or "openai" forces that provider (and
requires its key), while "auto" (the default) uses whichever key is configured, preferring
Anthropic when both are set.
"""

from __future__ import annotations

from app.ai.base import AIBudgetError, AIError, AIExtraction, RecipeExtractor
from app.config import Settings

__all__ = ["AIBudgetError", "AIError", "AIExtraction", "RecipeExtractor", "get_provider"]


def _selected_provider(settings: Settings) -> str | None:
    """Resolve which provider to use given the preference and which keys are configured."""
    configured = {
        "anthropic": bool(settings.anthropic_api_key),
        "openai": bool(settings.openai_api_key),
    }
    preference = settings.ai_provider
    if preference in ("anthropic", "openai"):
        return preference if configured[preference] else None
    for name in ("anthropic", "openai"):  # "auto": first configured, Anthropic preferred
        if configured[name]:
            return name
    return None


def get_provider(settings: Settings) -> RecipeExtractor | None:
    """Return the configured recipe extractor, or None when AI extraction is disabled."""
    name = _selected_provider(settings)
    if name is None:
        return None
    try:
        if name == "openai":
            from app.ai.openai_provider import OpenAIExtractor  # lazy: SDK import (CONVENTIONS 4)

            return OpenAIExtractor.from_settings(settings)
        from app.ai.anthropic_provider import AnthropicExtractor  # lazy: SDK import (CONVENTIONS 4)

        return AnthropicExtractor.from_settings(settings)
    except AIError:
        return None
