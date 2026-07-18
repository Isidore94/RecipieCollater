"""OpenAI adapter: structured extraction via a forced function call.

Mirrors the Anthropic adapter exactly. The client is injected (``__init__``) so tests exercise the
parse/validate/cost logic with a fake client and never need a key or network; :meth:`from_settings`
builds the real client for production. ``store=False`` asks OpenAI not to retain the request
(docs/05-ai-integration). The SDK is imported lazily, keeping it off the web process's import path
(CONVENTIONS 4).

This path targets chat-completions models with function calling (gpt-4o / gpt-4.1 families). The
model fills the tool arguments and we validate them with Pydantic - deliberately not OpenAI "strict"
mode, whose all-fields-required schema constraints fight optional fields. Receipt parsing sends the
photo as a base64 data URL (the same models are vision-capable).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from app.ai.base import (
    DRAFT_SYSTEM,
    EXTRACT_SYSTEM,
    RECEIPT_SYSTEM,
    AIError,
    AIExtraction,
    AIReceipt,
)
from app.ai.pricing import cost_micros
from app.config import Settings
from app.extraction import ExtractedReceipt, ExtractedRecipe

_RECIPE_TOOL = "save_recipe"
_RECEIPT_TOOL = "save_receipt"
_MAX_INPUT_CHARS = 60_000  # recipe pages/transcripts/orders are small once reduced to text
_MAX_OUTPUT_TOKENS = 4096


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
        prompt = f"Source URL: {source_url}\n\nText:\n{content[:_MAX_INPUT_CHARS]}"
        return self._run_recipe(EXTRACT_SYSTEM, prompt)

    def draft(self, description: str) -> AIExtraction:
        return self._run_recipe(DRAFT_SYSTEM, description[:_MAX_INPUT_CHARS])

    def receipt(self, content: str, *, image_jpeg: bytes | None = None) -> AIReceipt:
        user_content: str | list[dict[str, Any]] = content[:_MAX_INPUT_CHARS]
        if image_jpeg is not None:
            encoded = base64.b64encode(image_jpeg).decode("ascii")
            user_content = [
                {"type": "text", "text": content[:_MAX_INPUT_CHARS]},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                },
            ]
        payload, input_tokens, output_tokens, billed = self._call_tool(
            RECEIPT_SYSTEM, user_content, tool_name=_RECEIPT_TOOL,
            description="Record the grocery items bought on this receipt or order.",
            schema=ExtractedReceipt.model_json_schema(),
        )
        try:
            parsed = ExtractedReceipt.model_validate(payload)
        except Exception as exc:
            raise AIError(
                f"model output failed schema validation: {exc}",
                input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
            ) from exc
        return AIReceipt(
            receipt=parsed, provider=self.provider, model=self.model,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
        )

    def _run_recipe(self, system: str, content: str) -> AIExtraction:
        payload, input_tokens, output_tokens, billed = self._call_tool(
            system, content, tool_name=_RECIPE_TOOL,
            description="Record the recipe found in the text.",
            schema=ExtractedRecipe.model_json_schema(),
        )
        try:
            recipe = ExtractedRecipe.model_validate(payload)
        except Exception as exc:
            raise AIError(
                f"model output failed schema validation: {exc}",
                input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
            ) from exc
        return AIExtraction(
            recipe=recipe, provider=self.provider, model=self.model,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
        )

    def _call_tool(
        self,
        system: str,
        content: str | list[dict[str, Any]],
        *,
        tool_name: str,
        description: str,
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], int, int, int]:
        """One forced function call; returns (arguments, input_tokens, output_tokens, cost)."""
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_completion_tokens=_MAX_OUTPUT_TOKENS,
                store=False,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": description,
                            "parameters": schema,
                        },
                    }
                ],
                tool_choice={"type": "function", "function": {"name": tool_name}},
            )
        except Exception as exc:  # SDK raises many error types; treat all as a call failure
            raise AIError(f"OpenAI request failed: {exc}") from exc

        # The call was billed the moment it returned; capture its cost so a later parse/validation
        # failure still counts against the spend cap (a truncated tool call is a common trigger).
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        billed = cost_micros(self.model, input_tokens, output_tokens)

        payload = _tool_arguments(response, tool_name)
        if payload is None:
            raise AIError(
                f"model did not return the {tool_name} function call",
                input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
            )
        return payload, input_tokens, output_tokens, billed


def _tool_arguments(response: Any, tool_name: str) -> dict[str, Any] | None:
    """Pull the forced function call's JSON arguments out of a chat-completions response."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if function is not None and getattr(function, "name", None) == tool_name:
            raw = getattr(function, "arguments", None)
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return parsed
    return None
