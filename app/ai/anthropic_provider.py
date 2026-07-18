"""Claude adapter: strict structured extraction via forced tool use.

The Anthropic client is injected (``__init__``) so tests exercise the parsing/validation logic
with a fake client and never need a live key; :meth:`from_settings` builds the real client for
production. The SDK is imported lazily there, keeping it out of the web process's import path
(CONVENTIONS 4). Receipt parsing sends the photo as a base64 image content block (Claude models
are vision-capable).
"""

from __future__ import annotations

import base64
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
_MAX_INPUT_CHARS = 60_000  # recipe pages/orders are small once reduced to text
_MAX_OUTPUT_TOKENS = 4096


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
        prompt = f"Source URL: {source_url}\n\nPage text:\n{content[:_MAX_INPUT_CHARS]}"
        return self._run_recipe(EXTRACT_SYSTEM, prompt)

    def draft(self, description: str) -> AIExtraction:
        return self._run_recipe(DRAFT_SYSTEM, description[:_MAX_INPUT_CHARS])

    def receipt(self, content: str, *, image_jpeg: bytes | None = None) -> AIReceipt:
        user_content: str | list[dict[str, Any]] = content[:_MAX_INPUT_CHARS]
        if image_jpeg is not None:
            encoded = base64.b64encode(image_jpeg).decode("ascii")
            user_content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64", "media_type": "image/jpeg", "data": encoded,
                    },
                },
                {"type": "text", "text": content[:_MAX_INPUT_CHARS]},
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
            description="Record the recipe found on the page.",
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
        """One forced tool call; returns (input dict, input_tokens, output_tokens, cost)."""
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                system=system,
                tools=[
                    {"name": tool_name, "description": description, "input_schema": schema}
                ],
                tool_choice={"type": "tool", "name": tool_name},
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # SDK raises many error types; treat all as a call failure
            raise AIError(f"Anthropic request failed: {exc}") from exc

        # The call was billed the moment it returned; capture its cost so a later parse/validation
        # failure still counts against the spend cap (a truncated tool call is a common trigger).
        usage = getattr(message, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        billed = cost_micros(self.model, input_tokens, output_tokens)

        payload = _tool_input(message, tool_name)
        if payload is None:
            raise AIError(
                f"model did not return the {tool_name} tool call",
                input_tokens=input_tokens, output_tokens=output_tokens, cost_micros=billed,
            )
        return payload, input_tokens, output_tokens, billed


def _tool_input(message: Any, tool_name: str) -> dict[str, Any] | None:
    """Pull the forced tool_use block's input dict out of a Claude message."""
    for block in getattr(message, "content", None) or []:
        is_target_tool = (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == tool_name
        )
        if is_target_tool:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data
    return None
