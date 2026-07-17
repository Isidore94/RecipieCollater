"""The ingestion pipeline: turn a queued job into a saved recipe.

Orchestrates one job through fetch -> extract -> apply, driving the job lifecycle in
app.services.ingest and recording an extraction_run for provenance. Today only the schema.org
fast-path (recipe-scrapers) is wired in; the AI fallback (P2-3) and YouTube (P2-4) slot in at the
marked points. Runs entirely on an HTML string when the job carries supplied HTML, so the whole
path is testable offline (no network).

Replay safety: a job that already has a linked recipe is re-marked done and never re-extracted,
and applying an extraction reuses an existing recipe with the same source URL rather than creating
a duplicate. This keeps a worker retry or crash-replay from producing two recipes for one job.
"""

from __future__ import annotations

import sqlite3

from app import ai
from app.ai import usage as ai_usage
from app.config import get_settings
from app.extraction import SCHEMA_VERSION, ExtractedRecipe
from app.security import now_iso
from app.services import fetch, ingest, recipes, web_extract, youtube


def to_recipe_input(
    extracted: ExtractedRecipe, *, source_type: str, source_url: str | None = None
) -> recipes.RecipeInput:
    """Map the provider-neutral ExtractedRecipe onto the Phase-1 RecipeInput."""
    return recipes.RecipeInput(
        title=extracted.title,
        description=extracted.description,
        servings_text=extracted.servings_text,
        prep_minutes=extracted.prep_minutes,
        cook_minutes=extracted.cook_minutes,
        total_minutes=extracted.total_minutes,
        source_type=source_type,
        source_url=source_url,
        source_name=extracted.source_name,
        ingredients=[
            recipes.IngredientInput(
                original_text=item.original_text,
                section=item.section,
                quantity_text=item.quantity_text,
                unit=item.unit,
                food=item.food,
                note=item.note,
            )
            for item in extracted.ingredients
        ],
        steps=[
            recipes.StepInput(
                instruction=step.instruction, section=step.section, minutes=step.minutes
            )
            for step in extracted.steps
        ],
        tags=list(extracted.tags),
    )


def apply_extraction(
    conn: sqlite3.Connection,
    job: ingest.IngestJob,
    extracted: ExtractedRecipe,
    *,
    extractor: str,
    provider: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    confidence: str = "high",
    source_type: str = "web",
) -> int:
    """Create (or reuse) the recipe, record an extraction_run, and link it to the job.

    The whole apply is committed as one unit and marks the job done, so a replay finds the recipe
    already linked and does nothing further.
    """
    existing = conn.execute("SELECT id FROM recipes WHERE source_url = ?", (job.url,)).fetchone()
    if existing is not None:
        recipe_id = int(existing["id"])
    else:
        recipe_id = recipes.create_recipe(
            conn, to_recipe_input(extracted, source_type=source_type, source_url=job.url),
            created_by=job.submitted_by,
        )

    cur = conn.execute(
        """INSERT INTO extraction_runs
           (recipe_id, job_id, extractor, provider, model, prompt_version, schema_version,
            confidence, payload)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (recipe_id, job.id, extractor, provider, model, prompt_version, SCHEMA_VERSION,
         confidence, extracted.model_dump_json()),
    )
    run_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    video_id = ingest.youtube_video_id(job.normalized_url)
    conn.execute(
        """UPDATE recipes
           SET normalized_source_url = ?, video_id = ?, current_extraction_run_id = ?,
               updated_at = ?
           WHERE id = ?""",
        (job.normalized_url, video_id, run_id, now_iso(), recipe_id),
    )
    conn.commit()
    ingest.set_status(conn, job.id, "done", recipe_id=recipe_id)
    return recipe_id


def _obtain_html(conn: sqlite3.Connection, job: ingest.IngestJob) -> str:
    """Return the job's HTML - supplied capture if present, otherwise an SSRF-safe fetch."""
    supplied = ingest.read_artifact(conn, job.id, "supplied_html")
    if supplied is not None:
        return supplied.decode("utf-8", errors="replace")
    ingest.set_status(conn, job.id, "fetching")
    result = fetch.fetch(job.normalized_url)
    ingest.store_artifact(conn, job.id, "fetched_html", result.html.encode("utf-8"))
    return result.html


def run_job(conn: sqlite3.Connection, job: ingest.IngestJob) -> None:
    """Process one ingest job to completion, recording failure categories for the inbox."""
    if job.recipe_id:  # already produced a recipe (crash-safe replay)
        ingest.set_status(conn, job.id, "done", recipe_id=job.recipe_id)
        return

    if ingest.youtube_video_id(job.normalized_url):
        _run_youtube(conn, job)
        return

    try:
        html = _obtain_html(conn, job)
    except fetch.FetchError as exc:
        ingest.set_status(
            conn, job.id, "failed", error_category=exc.category, error_message=exc.message
        )
        return

    ingest.set_status(conn, job.id, "extracting")
    extracted = web_extract.extract_from_html(html, job.normalized_url)
    if extracted is not None and extracted.is_complete():
        ingest.set_status(conn, job.id, "normalizing")
        apply_extraction(conn, job, extracted, extractor="recipe_scrapers", confidence="high")
        return

    if _ai_extract_and_apply(conn, job, _page_text(html), extractor="llm_web", source_type="web"):
        return

    ingest.set_status(
        conn, job.id, "failed", error_category="no_recipe",
        error_message="No recipe could be extracted from that page.",
    )


def _run_youtube(conn: sqlite3.Connection, job: ingest.IngestJob) -> None:
    """Ingest a YouTube video: description-first, captions as fallback, then LLM extraction."""
    if not get_settings().ai_enabled:
        ingest.set_status(
            conn, job.id, "failed", error_category="youtube_needs_ai",
            error_message="Add an Anthropic API key to import recipes from YouTube.",
        )
        return

    ingest.set_status(conn, job.id, "fetching")
    try:
        data = youtube.fetch(job.normalized_url)
    except youtube.YoutubeError as exc:
        ingest.set_status(
            conn, job.id, "failed", error_category="youtube_fetch", error_message=str(exc)[:400]
        )
        return
    ingest.store_artifact(conn, job.id, "youtube_metadata", data.to_json().encode("utf-8"))

    ingest.set_status(conn, job.id, "extracting")
    applied = _ai_extract_and_apply(
        conn, job, data.prompt_text(),
        extractor="youtube", source_type="youtube", operation="extract_youtube",
    )
    if not applied:
        ingest.set_status(
            conn, job.id, "failed", error_category="no_recipe",
            error_message="Couldn't find a recipe in that video's description or captions.",
        )


def _page_text(html: str) -> str:
    """Reduce HTML to visible text for the LLM prompt (scripts/styles stripped)."""
    from bs4 import BeautifulSoup  # lazy: bs4 comes with recipe-scrapers (CONVENTIONS 4)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _ai_extract_and_apply(
    conn: sqlite3.Connection,
    job: ingest.IngestJob,
    content: str,
    *,
    extractor: str,
    source_type: str,
    operation: str = "extract_web",
) -> bool:
    """Budget-gated LLM extraction that applies the recipe on success; always logs to ai_usage_log.

    Returns True iff a complete recipe was extracted and saved. Shared by the web fallback and the
    YouTube path so spend accounting and provenance stay identical across sources.
    """
    settings = get_settings()
    provider = ai.get_provider(settings)
    if provider is None:
        return False
    if not ai_usage.within_budget(conn, settings):
        ai_usage.log_usage(
            conn, provider="anthropic", model=settings.ai_model, operation=operation,
            job_id=job.id, status="blocked", error="daily or monthly AI spend cap reached",
        )
        return False
    try:
        result = provider.extract(content, source_url=job.normalized_url)
    except ai.AIError as exc:
        ai_usage.log_usage(
            conn, provider="anthropic", model=settings.ai_model, operation=operation,
            job_id=job.id, status="error", error=str(exc)[:500],
        )
        return False
    ai_usage.log_usage(
        conn, provider=result.provider, model=result.model, operation=operation,
        job_id=job.id, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_micros=result.cost_micros, status="ok",
    )
    if not result.recipe.is_complete():
        return False
    ingest.set_status(conn, job.id, "normalizing")
    apply_extraction(
        conn, job, result.recipe, extractor=extractor,
        provider=result.provider, model=result.model, confidence="medium", source_type=source_type,
    )
    return True
