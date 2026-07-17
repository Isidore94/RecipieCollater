"""Claude adapter: strict structured extraction to an ExtractedRecipe via forced tool use.

The Anthropic client is injected (``__init__``) so tests exercise the parsing/validation logic
with a fake client and never need a live key; :meth:`from_settings` builds the real client for
production. The SDK is imported lazily there, keeping it out of the web process's import path
(CONVENTIONS 4).
"""

from __future__ import annotations

from typing import Any

from app.ai.base import AIError, AIExtraction
from app.ai.pricing import cost_micros
from app.config import Settings
from app.extraction import ExtractedRecipe

_TOOL_NAME = "save_recipe"
_MAX_INPUT_CHARS = 60_000  # recipe pages are small once reduced to text; keep the prompt bounded
_MAX_OUTPUT_TOKENS = 4096
_SYSTEM = (
    "You extract a single cooking recipe from the provided text, which may be a recipe web page "
    "or a cooking video's title, description, and transcript. "
    "Use only what the text states - never invent ingredients, steps, times, or yields. "
    "If a field is absent, omit it. Copy each ingredient line verbatim into original_text. "
    "If the text contains no recipe, return a title with empty ingredients and steps."
)


class AnthropicExtractor:
    provider = "anthropic"

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> AnthropicExtractor:
        if not settings.anthropic_api_key:
            raise AIError("no Anthropic API key configured")
        try:
            import anthropic  # lazy: heavy SDK (CONVENTIONS 4)
        except ImportError as exc:  # pragma: no cover - dep is pinned in production
            raise AIError("the anthropic package is not installed") from exc
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        return cls(client, settings.anthropic_model)

    def extract(self, content: str, *, source_url: str) -> AIExtraction:
        schema = ExtractedRecipe.model_json_schema()
        prompt = f"Source URL: {source_url}\n\nPage text:\n{content[:_MAX_INPUT_CHARS]}"
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=_SYSTEM,
                tools=[
                    {
                        "name": _TOOL_NAME,
                        "description": "Record the recipe found on the page.",
                        "input_schema": schema,
                    }
                ],
                tool_choice={"type": "tool", "name": _TOOL_NAME},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # SDK raises many error types; treat all as a call failure
            raise AIError(f"Anthropic request failed: {exc}") from exc

        # The call was billed the moment it returned; capture its cost so a later parse/validation
        # failure still counts against the spend cap (a truncated tool call is a common trigger).
        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        billed = cost_micros(self.model, input_tokens, output_tokens)

        payload = _tool_input(message)
        if payload is None:
            raise AIError(
                "model did not return the save_recipe tool call",
                input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
            )
        try:
            recipe = ExtractedRecipe.model_validate(payload)
        except Exception as exc:
            raise AIError(
                f"model output failed schema validation: {exc}",
                input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
            ) from exc

        return AIExtraction(
            recipe=recipe,
            provider=self.provider,
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micros=billed,
        )


def _tool_input(message: Any) -> dict[str, Any] | None:
    """Pull the forced tool_use block's input dict out of a Claude message."""
    for block in getattr(message, "content", None) or []:
        is_recipe_tool = (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == _TOOL_NAME
        )
        if is_recipe_tool:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None
