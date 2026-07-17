"""OpenAI adapter: structured extraction to an ExtractedRecipe via a forced function call.

Mirrors the Anthropic adapter exactly. The client is injected (``__init__``) so tests exercise the
parse/validate/cost logic with a fake client and never need a key or network; :meth:`from_settings`
builds the real client for production. ``store=False`` asks OpenAI not to retain the request
(docs/05-ai-integration). The SDK is imported lazily, keeping it off the web process's import path
(CONVENTIONS 4).

This path targets chat-completions models with function calling (gpt-4o / gpt-4.1 families). The
model fills the tool arguments and we validate them with Pydantic - deliberately not OpenAI "strict"
mode, whose all-fields-required schema constraints fight optional fields.
"""

from __future__ import annotations

import json
from typing import Any

from app.ai.base import AIError, AIExtraction
from app.ai.pricing import cost_micros
from app.config import Settings
from app.extraction import ExtractedRecipe

_TOOL_NAME = "save_recipe"
_MAX_INPUT_CHARS = 60_000  # recipe pages/transcripts are small once reduced to text
_MAX_OUTPUT_TOKENS = 4096
_SYSTEM = (
    "You extract a single cooking recipe from the provided text, which may be a recipe web page "
    "or a cooking video's title, description, and transcript. "
    "Use only what the text states - never invent ingredients, steps, times, or yields. "
    "If a field is absent, omit it. Copy each ingredient line verbatim into original_text. "
    "If the text contains no recipe, return a title with empty ingredients and steps."
)


class OpenAIExtractor:
    provider = "openai"

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIExtractor:
        if not settings.openai_api_key:
            raise AIError("no OpenAI API key configured")
        try:
            from openai import OpenAI  # lazy: heavy SDK (CONVENTIONS 4)
        except ImportError as exc:  # pragma: no cover - dep is pinned in production
            raise AIError("the openai package is not installed") from exc
        client = OpenAI(api_key=settings.openai_api_key)
        return cls(client, settings.openai_model)

    def extract(self, content: str, *, source_url: str) -> AIExtraction:
        schema = ExtractedRecipe.model_json_schema()
        prompt = f"Source URL: {source_url}\n\nText:\n{content[:_MAX_INPUT_CHARS]}"
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_completion_tokens=_MAX_OUTPUT_TOKENS,
                store=False,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": _TOOL_NAME,
                            "description": "Record the recipe found in the text.",
                            "parameters": schema,
                        },
                    }
                ],
                tool_choice={"type": "function", "function": {"name": _TOOL_NAME}},
            )
        except Exception as exc:  # SDK raises many error types; treat all as a call failure
            raise AIError(f"OpenAI request failed: {exc}") from exc

        # The call was billed the moment it returned; capture its cost so a later parse/validation
        # failure still counts against the spend cap (a truncated tool call is a common trigger).
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        billed = cost_micros(self.model, input_tokens, output_tokens)

        payload = _tool_arguments(response)
        if payload is None:
            raise AIError(
                "model did not return the save_recipe function call",
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


def _tool_arguments(response: Any) -> dict[str, Any] | None:
    """Pull the forced function call's JSON arguments out of a chat-completions response."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if function is not None and getattr(function, "name", None) == _TOOL_NAME:
            raw = getattr(function, "arguments", None)
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return parsed
    return None
