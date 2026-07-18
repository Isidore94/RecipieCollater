"""AI tag backfill (Phase 4.6): give already-saved recipes the controlled-vocabulary tags.

New ingests get tags from the extraction prompts (app.ai.base.TAG_GUIDE); this service catches
the recipes saved before that existed. It reuses the extract() adapter on the recipe's own
Markdown export - only the returned tags are kept, everything else the model says is discarded,
so it can never rewrite a family-edited recipe. Budget-gated and logged like every AI call.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app import ai
from app.ai import usage as ai_usage
from app.config import get_settings
from app.services import recipes

_OPERATION = "tag_backfill"
_MAX_TAGS = 6


@dataclass(frozen=True, slots=True)
class TagResult:
    recipe_id: int
    title: str
    tags: list[str]
    error: str | None = None


def _normalize(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        clean = tag.strip().lower()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out[:_MAX_TAGS]


def suggest_tags(conn: sqlite3.Connection, recipe_id: int) -> TagResult:
    """Ask the provider for controlled-vocab tags for one recipe. Writes nothing."""
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        return TagResult(recipe_id, "?", [], error="recipe not found")

    settings = get_settings()
    provider = ai.get_provider(settings)
    if provider is None:
        return TagResult(recipe_id, detail.title, [], error="no AI provider configured")
    if not ai_usage.within_budget(conn, settings):
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, status="blocked", error="daily or monthly AI spend cap reached",
        )
        return TagResult(recipe_id, detail.title, [], error="AI spend cap reached")

    content = recipes.to_markdown(detail)
    try:
        result = provider.extract(content, source_url=detail.source_url or "manual://recipe")
    except ai.AIError as exc:
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, input_tokens=exc.input_tokens, output_tokens=exc.output_tokens,
            cost_micros=exc.cost_micros, status="error", error=str(exc)[:500],
        )
        return TagResult(recipe_id, detail.title, [], error=str(exc))

    ai_usage.log_usage(
        conn, provider=result.provider, model=result.model, operation=_OPERATION,
        job_id=None, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_micros=result.cost_micros, status="ok",
    )
    return TagResult(recipe_id, detail.title, _normalize(list(result.recipe.tags)))


def backfill(
    conn: sqlite3.Connection, *, only_untagged: bool = True, limit: int | None = None
) -> list[TagResult]:
    """Tag every (untagged) recipe via the provider and save the results. Stops early if the
    budget blocks or the provider is missing - partial progress is kept."""
    sql = "SELECT id FROM recipes"
    if only_untagged:
        sql += " WHERE NOT EXISTS (SELECT 1 FROM recipe_tags rt WHERE rt.recipe_id = recipes.id)"
    sql += " ORDER BY id"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    results: list[TagResult] = []
    for row in conn.execute(sql).fetchall():
        result = suggest_tags(conn, int(row["id"]))
        results.append(result)
        if result.error in ("no AI provider configured", "AI spend cap reached"):
            break
        if result.tags:
            recipes.add_tags(conn, result.recipe_id, result.tags)
    return results
