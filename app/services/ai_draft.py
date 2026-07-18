"""AI-assisted manual entry: draft a structured recipe from a cook's plain-language description.

Reuses the same provider adapters and spend accounting as ingestion, but returns the drafted
ExtractedRecipe for the manual-entry form to pre-fill - it does not create the recipe. Budget-gated
and logged to ai_usage_log (operation 'manual_draft') exactly like the ingestion AI calls.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app import ai
from app.ai import usage as ai_usage
from app.config import get_settings
from app.extraction import ExtractedRecipe

_OPERATION = "manual_draft"
_PHOTO_OPERATION = "recipe_photo"


@dataclass(frozen=True, slots=True)
class DraftResult:
    recipe: ExtractedRecipe | None
    error: str | None


def draft_from_photo(conn: sqlite3.Connection, image: bytes) -> DraftResult:
    """Transcribe a photographed recipe (cookbook page / card) into a draft for the form to
    prefill. Budget-gated and logged like every AI call; nothing is created until the user Saves."""
    from app.services import receipts  # reuse the bounded-JPEG normalizer (lazy: Pillow)

    settings = get_settings()
    provider = ai.get_provider(settings)
    if provider is None:
        return DraftResult(None, "Reading a recipe photo needs an API key on the server.")
    if not ai_usage.within_budget(conn, settings):
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_PHOTO_OPERATION,
            job_id=None, status="blocked", error="daily or monthly AI spend cap reached",
        )
        return DraftResult(None, "Today's AI spend limit has been reached - try again later.")
    try:
        image_jpeg = receipts._prepare_image(image)
    except receipts.ReceiptError as exc:
        return DraftResult(None, str(exc))
    try:
        result = provider.recipe_from_photo(image_jpeg)
    except ai.AIError as exc:
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_PHOTO_OPERATION,
            job_id=None, input_tokens=exc.input_tokens, output_tokens=exc.output_tokens,
            cost_micros=exc.cost_micros, status="error", error=str(exc)[:500],
        )
        return DraftResult(None, "Couldn't read that photo - try a clearer, straight-on shot.")
    ai_usage.log_usage(
        conn, provider=result.provider, model=result.model, operation=_PHOTO_OPERATION,
        job_id=None, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_micros=result.cost_micros, status="ok",
    )
    if not result.recipe.ingredients:
        return DraftResult(None, "No recipe found in that photo.")
    return DraftResult(result.recipe, None)


def draft_from_description(conn: sqlite3.Connection, description: str) -> DraftResult:
    """Turn a description into a recipe draft via the AI provider, or return why it couldn't."""
    text = description.strip()
    if not text:
        return DraftResult(None, "Describe your recipe first.")

    settings = get_settings()
    provider = ai.get_provider(settings)
    if provider is None:
        return DraftResult(None, "AI drafting needs an API key configured on the server.")

    if not ai_usage.within_budget(conn, settings):
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, status="blocked", error="daily or monthly AI spend cap reached",
        )
        return DraftResult(None, "Today's AI spend limit has been reached - try again later.")

    try:
        result = provider.draft(text)
    except ai.AIError as exc:
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, input_tokens=exc.input_tokens, output_tokens=exc.output_tokens,
            cost_micros=exc.cost_micros, status="error", error=str(exc)[:500],
        )
        return DraftResult(None, "The AI couldn't draft that - add a bit more detail and retry.")

    ai_usage.log_usage(
        conn, provider=result.provider, model=result.model, operation=_OPERATION,
        job_id=None, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_micros=result.cost_micros, status="ok",
    )
    return DraftResult(result.recipe, None)
