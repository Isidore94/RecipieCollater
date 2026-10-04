"""Re-reading a recipe's source and reviewing the result as a comparison draft (docs/04 section 8).

The flow, end to end:

1. :func:`start_reread` records a re-read job (``ingest_jobs.reextract_recipe_id``, migration 020)
   and, unless the person asked to fetch again, links the newest stored immutable artifact for the
   recipe to it so the worker reuses it instead of going to the network.
2. The worker runs the normal extraction stages and ends in ``pipeline.record_draft``: a new
   ``extraction_runs`` row in state 'draft'. The recipe and its accepted run are untouched.
3. The family opens the comparison (:func:`build_comparison`), ticks the sections they want from the
   new reading, and :func:`apply_sections` writes ONLY those through ``recipes.update_recipe`` - the
   same service a manual edit uses, so revisions, FTS, quantity parsing and pantry-mapping
   carry-over on unchanged lines all behave identically. Or they :func:`dismiss_draft`.

The comparison itself is pure: :func:`diff_sections` takes two :class:`Snapshot` values and returns
one :class:`Section` per comparable part of the recipe. Everything database-shaped (resolving the
draft's unit/food text the way the sheet would show it, loading runs) lives in the thin functions
below it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from app.extraction import ExtractedRecipe
from app.security import now_iso
from app.services import ingest, recipes, units

# Section keys, in display order. The checkbox values on the compare screen are exactly these.
SECTION_KEYS: tuple[str, ...] = (
    "title",
    "description",
    "times",
    "servings",
    "ingredients",
    "steps",
    "tags",
)
_LABELS = {
    "title": "Title",
    "description": "Description",
    "times": "Times",
    "servings": "Servings",
    "ingredients": "Ingredients",
    "steps": "Steps",
    "tags": "Tags",
}

# Artifact kinds a re-read can reuse. The newest one tied to the recipe wins (a later "fetch
# again" is fresher than the page captured on day one).
_REUSABLE_KINDS: tuple[str, ...] = (
    "supplied_html",
    "fetched_html",
    "youtube_metadata",
    "instagram_metadata",
)


class ReextractError(ValueError):
    """A re-read could not be started or applied; the message is safe to show the family."""


# --------------------------------------------------------------------------------------
# Pure comparison
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngredientLine:
    """One ingredient as the sheet reads it, plus a key that ignores presentation noise."""

    text: str  # what the family reads (the line as written)
    key: str  # what decides "same line": section, amount, unit, food, note - casefolded


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The comparable parts of a recipe, from either side (current sheet or draft)."""

    title: str
    description: str | None
    servings_text: str | None
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None
    ingredients: tuple[IngredientLine, ...]
    steps: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Section:
    key: str
    label: str
    changed: bool
    current: tuple[str, ...]  # display lines, current recipe
    draft: tuple[str, ...]  # display lines, the new reading


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _ingredient_key(
    section: str | None,
    quantity: str | None,
    unit: str | None,
    food: str | None,
    note: str | None,
    original: str,
) -> str:
    # A line with no food at all (an unparsed one) can only be told apart by its own words.
    parts = [section, quantity, unit, food, note, original if not _norm(food) else ""]
    return "|".join(_norm(p) for p in parts)


def snapshot_from_detail(detail: recipes.RecipeDetail) -> Snapshot:
    """The current recipe, as it reads on the sheet."""
    return Snapshot(
        title=detail.title,
        description=detail.description,
        servings_text=detail.servings_text,
        prep_minutes=detail.prep_minutes,
        cook_minutes=detail.cook_minutes,
        total_minutes=detail.total_minutes,
        ingredients=tuple(
            IngredientLine(
                text=ing.original_text,
                key=_ingredient_key(
                    ing.section,
                    ing.quantity_text,
                    ing.unit_name,
                    ing.food_name,
                    ing.note,
                    ing.original_text,
                ),
            )
            for ing in detail.ingredients
        ),
        steps=tuple(step.instruction for step in detail.steps),
        tags=tuple(detail.tags),
    )


def snapshot_from_input(
    data: recipes.RecipeInput,
    *,
    unit_name: Callable[[str], str | None] = lambda text: text,
    food_name: Callable[[str], str | None] = lambda text: text,
) -> Snapshot:
    """A draft, as it WOULD read once saved.

    ``unit_name`` / ``food_name`` map free text ('tbsp', 'scallions') to the canonical name the
    sheet would store, so an unchanged re-read does not show every line as different merely
    because the draft says 'tbsp' where the sheet says 'tablespoon'. They default to identity
    (the function stays pure); :func:`build_comparison` passes read-only database lookups.
    """
    lines: list[IngredientLine] = []
    for ing in data.ingredients:
        has_qty = bool((ing.quantity_text or "").strip())
        unit_text = (ing.unit or "").strip() or ("each" if has_qty else "")
        unit = (unit_name(unit_text) or unit_text) if unit_text else None
        food_text = (ing.food or "").strip()
        food = (food_name(food_text) or food_text) if food_text else None
        original = (ing.original_text or "").strip() or " ".join(
            p for p in (ing.quantity_text, ing.unit, ing.food) if p
        )
        lines.append(
            IngredientLine(
                text=original,
                key=_ingredient_key(ing.section, ing.quantity_text, unit, food, ing.note, original),
            )
        )
    return Snapshot(
        title=data.title,
        description=data.description,
        servings_text=data.servings_text,
        prep_minutes=data.prep_minutes,
        cook_minutes=data.cook_minutes,
        total_minutes=data.total_minutes,
        ingredients=tuple(lines),
        steps=tuple(step.instruction for step in data.steps if step.instruction.strip()),
        tags=tuple(data.tags),
    )


def _times_lines(snap: Snapshot) -> tuple[str, ...]:
    pairs = (
        ("Prep", snap.prep_minutes),
        ("Cook", snap.cook_minutes),
        ("Total", snap.total_minutes),
    )
    return tuple(f"{name}: {value} min" for name, value in pairs if value is not None)


def diff_sections(current: Snapshot, draft: Snapshot) -> list[Section]:
    """Compare two snapshots one section at a time, in display order.

    Matching is deliberately forgiving about case and whitespace (a re-read rarely changes those
    on purpose) and about tag order, but strict about content: any real difference marks the
    section ``changed``, which is the only thing that offers a "take theirs" box.
    """

    def text_lines(value: str | None) -> tuple[str, ...]:
        return (value.strip(),) if value and value.strip() else ()

    sections = [
        Section(
            "title",
            _LABELS["title"],
            current.title.strip() != draft.title.strip(),
            text_lines(current.title),
            text_lines(draft.title),
        ),
        Section(
            "description",
            _LABELS["description"],
            _norm(current.description) != _norm(draft.description),
            text_lines(current.description),
            text_lines(draft.description),
        ),
        Section(
            "times",
            _LABELS["times"],
            (current.prep_minutes, current.cook_minutes, current.total_minutes)
            != (draft.prep_minutes, draft.cook_minutes, draft.total_minutes),
            _times_lines(current),
            _times_lines(draft),
        ),
        Section(
            "servings",
            _LABELS["servings"],
            _norm(current.servings_text) != _norm(draft.servings_text),
            text_lines(current.servings_text),
            text_lines(draft.servings_text),
        ),
        Section(
            "ingredients",
            _LABELS["ingredients"],
            [i.key for i in current.ingredients] != [i.key for i in draft.ingredients],
            tuple(i.text for i in current.ingredients),
            tuple(i.text for i in draft.ingredients),
        ),
        Section(
            "steps",
            _LABELS["steps"],
            [_norm(s) for s in current.steps] != [_norm(s) for s in draft.steps],
            current.steps,
            draft.steps,
        ),
        Section(
            "tags",
            _LABELS["tags"],
            {_norm(t) for t in current.tags} != {_norm(t) for t in draft.tags},
            tuple(current.tags),
            tuple(draft.tags),
        ),
    ]
    return sections


def labels_for(keys: Iterable[str]) -> list[str]:
    """Human labels for section keys, in display order (for the confirmation banner)."""
    wanted = set(keys)
    return [_LABELS[k] for k in SECTION_KEYS if k in wanted]


def changed_keys(sections: Iterable[Section]) -> list[str]:
    return [s.key for s in sections if s.changed]


def merge_sections(
    current: recipes.RecipeInput, draft: recipes.RecipeInput, take: Iterable[str]
) -> recipes.RecipeInput:
    """The recipe as it would be if only the ``take`` sections came from the draft.

    Everything not named - rating-adjacent fields, tier, base servings, TLDR, active/elapsed
    minutes, the source link - stays exactly as the family has it. Unknown keys are ignored.
    """
    chosen = set(take)
    merged = current
    if "title" in chosen:
        merged = replace(merged, title=draft.title)
    if "description" in chosen:
        merged = replace(merged, description=draft.description)
    if "times" in chosen:
        merged = replace(
            merged,
            prep_minutes=draft.prep_minutes,
            cook_minutes=draft.cook_minutes,
            total_minutes=draft.total_minutes,
        )
    if "servings" in chosen:
        merged = replace(merged, servings_text=draft.servings_text)
    if "ingredients" in chosen:
        merged = replace(merged, ingredients=list(draft.ingredients))
    if "steps" in chosen:
        merged = replace(merged, steps=list(draft.steps))
    if "tags" in chosen:
        merged = replace(merged, tags=list(draft.tags))
    return merged


# --------------------------------------------------------------------------------------
# Draft records
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftRun:
    id: int
    recipe_id: int
    extractor: str
    provider: str | None
    model: str | None
    confidence: str | None
    state: str
    created_at: str
    payload: str


@dataclass(frozen=True, slots=True)
class PendingDraft:
    """A recipe with a reading waiting to be compared (for the inbox and cookbook banner)."""

    recipe_id: int
    slug: str
    title: str
    run_id: int
    created_at: str


def _row_to_run(row: sqlite3.Row) -> DraftRun:
    return DraftRun(
        id=int(row["id"]),
        recipe_id=int(row["recipe_id"]),
        extractor=row["extractor"],
        provider=row["provider"],
        model=row["model"],
        confidence=row["confidence"],
        state=row["state"],
        created_at=row["created_at"],
        payload=row["payload"],
    )


def get_run(conn: sqlite3.Connection, recipe_id: int, run_id: int) -> DraftRun | None:
    row = conn.execute(
        "SELECT * FROM extraction_runs WHERE id = ? AND recipe_id = ?", (run_id, recipe_id)
    ).fetchone()
    return _row_to_run(row) if row else None


def pending_draft(conn: sqlite3.Connection, recipe_id: int) -> DraftRun | None:
    row = conn.execute(
        "SELECT * FROM extraction_runs WHERE recipe_id = ? AND state = 'draft' "
        "ORDER BY id DESC LIMIT 1",
        (recipe_id,),
    ).fetchone()
    return _row_to_run(row) if row else None


def list_pending_drafts(conn: sqlite3.Connection) -> list[PendingDraft]:
    rows = conn.execute(
        """SELECT r.id AS recipe_id, r.slug, r.title, e.id AS run_id, e.created_at
           FROM extraction_runs e JOIN recipes r ON r.id = e.recipe_id
           WHERE e.state = 'draft' AND r.status != 'archived'
           ORDER BY e.id DESC"""
    ).fetchall()
    return [
        PendingDraft(
            recipe_id=int(r["recipe_id"]),
            slug=r["slug"],
            title=r["title"],
            run_id=int(r["run_id"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def active_reread(conn: sqlite3.Connection, recipe_id: int) -> ingest.IngestJob | None:
    """A re-read of this recipe that is still queued or running, if any."""
    placeholders = ",".join("?" for _ in ingest.ACTIVE_STATUSES)
    row = conn.execute(
        f"SELECT id FROM ingest_jobs WHERE reextract_recipe_id = ? "  # noqa: S608
        f"AND status IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        (recipe_id, *ingest.ACTIVE_STATUSES),
    ).fetchone()
    return ingest.get_job(conn, int(row["id"])) if row else None


# --------------------------------------------------------------------------------------
# Starting a re-read
# --------------------------------------------------------------------------------------


def _newest_stored_artifact(conn: sqlite3.Connection, recipe_id: int) -> int | None:
    """The id of the newest reusable artifact whose bytes are still on disk, or None."""
    from app.config import get_settings  # local: keeps this module cheap to import

    placeholders = ",".join("?" for _ in _REUSABLE_KINDS)
    rows = conn.execute(
        f"""SELECT a.id, a.path FROM artifacts a JOIN ingest_jobs j ON j.id = a.job_id
            WHERE (j.recipe_id = ? OR j.reextract_recipe_id = ?) AND a.kind IN ({placeholders})
            ORDER BY a.id DESC""",  # noqa: S608
        (recipe_id, recipe_id, *_REUSABLE_KINDS),
    ).fetchall()
    root = get_settings().artifacts_dir
    for row in rows:
        if (root / row["path"]).is_file():
            return int(row["id"])
    return None


def start_reread(
    conn: sqlite3.Connection,
    recipe_id: int,
    *,
    refetch: bool,
    submitted_by: int | None,
) -> tuple[ingest.IngestJob, bool]:
    """Record a re-read of ``recipe_id``'s source; returns ``(job, created)``.

    ``created`` is False when a re-read of this recipe is already in flight (a double tap must not
    pay for two extractions). The caller schedules processing only when it is True.

    Without ``refetch`` the newest stored artifact is linked to the job so the worker reuses it;
    if nothing usable is stored (a hand-typed recipe, or a pruned archive) that is refused with
    guidance rather than quietly going to the network the person did not ask for.
    """
    detail = recipes.get_recipe(conn, recipe_id)
    if detail is None:
        raise ReextractError("That recipe no longer exists.")
    source = (detail.source_url or "").strip()
    if not source or urlsplit(source).scheme.lower() not in ("http", "https"):
        raise ReextractError("This recipe has no web link to re-read.")
    try:
        normalized = ingest.normalize_url(source)
    except ingest.IngestError as exc:
        raise ReextractError("This recipe's link can't be read again.") from exc

    existing = active_reread(conn, recipe_id)
    if existing is not None:
        return existing, False

    artifact_id: int | None = None
    if not refetch:
        artifact_id = _newest_stored_artifact(conn, recipe_id)
        if artifact_id is None:
            raise ReextractError(
                "There is no saved copy of that page to re-read. Tick 'fetch the page again'."
            )
    job = ingest.create_reextract_job(
        conn,
        recipe_id=recipe_id,
        url=source,
        normalized_url=normalized,
        refetch=refetch,
        submitted_by=submitted_by,
    )
    if artifact_id is not None:
        ingest.link_artifact(conn, artifact_id, job.id)
    return job, True


# --------------------------------------------------------------------------------------
# Comparing and applying
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Comparison:
    recipe: recipes.RecipeDetail
    run: DraftRun
    sections: list[Section]
    draft_input: recipes.RecipeInput


def _draft_input(conn: sqlite3.Connection, run: DraftRun) -> recipes.RecipeInput:
    from app.services import pipeline  # lazy: pulls the AI package, which the web process avoids

    extracted = ExtractedRecipe.model_validate_json(run.payload)
    return pipeline.to_recipe_input(conn, extracted, source_type="web")


def _unit_lookup(conn: sqlite3.Connection) -> Callable[[str], str | None]:
    def lookup(text: str) -> str | None:
        unit = units.resolve_unit(conn, text)
        return unit.name if unit else None

    return lookup


def _food_lookup(conn: sqlite3.Connection) -> Callable[[str], str | None]:
    """Read-only twin of recipes._resolve_food_id: alias, then name, never creating a food."""

    def lookup(text: str) -> str | None:
        row = conn.execute(
            "SELECT f.name FROM food_aliases a JOIN foods f ON f.id = a.food_id WHERE a.alias = ?",
            (text,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT name FROM foods WHERE name = ? COLLATE NOCASE", (text,)
            ).fetchone()
        return str(row["name"]) if row else None

    return lookup


def build_comparison(conn: sqlite3.Connection, recipe_id: int, run_id: int) -> Comparison | None:
    """Load a run and diff it against the recipe as it is NOW (edits since the read count)."""
    detail = recipes.get_recipe(conn, recipe_id)
    run = get_run(conn, recipe_id, run_id)
    if detail is None or run is None:
        return None
    draft = _draft_input(conn, run)
    sections = diff_sections(
        snapshot_from_detail(detail),
        snapshot_from_input(draft, unit_name=_unit_lookup(conn), food_name=_food_lookup(conn)),
    )
    return Comparison(recipe=detail, run=run, sections=sections, draft_input=draft)


def _input_from_detail(
    conn: sqlite3.Connection, detail: recipes.RecipeDetail
) -> recipes.RecipeInput:
    """The recipe as an edit input, so sections NOT taken round-trip unchanged.

    Ingredients go back through their unit/food names (update_recipe resolves ids itself), and
    the package unit - which the views carry only as an id - is looked up so a round-to-package
    line keeps its package size.
    """

    def package_unit(unit_id: int | None) -> str | None:
        unit = units.get_unit(conn, unit_id) if unit_id is not None else None
        return unit.name if unit else None

    return recipes.RecipeInput(
        title=detail.title,
        tldr=detail.tldr,
        description=detail.description,
        tier=detail.tier,
        base_servings=detail.base_servings,
        servings_text=detail.servings_text,
        prep_minutes=detail.prep_minutes,
        cook_minutes=detail.cook_minutes,
        total_minutes=detail.total_minutes,
        active_minutes=detail.active_minutes,
        elapsed_minutes=detail.elapsed_minutes,
        source_type=detail.source_type,
        source_url=detail.source_url,
        source_name=detail.source_name,
        ingredients=[
            recipes.IngredientInput(
                original_text=ing.original_text,
                section=ing.section,
                quantity_text=ing.quantity_text,
                unit=ing.unit_name,
                food=ing.food_name,
                note=ing.note,
                scaling_mode=ing.scaling_mode,
                package_quantity_text=ing.package_quantity_text,
                package_unit=package_unit(ing.package_unit_id),
            )
            for ing in detail.ingredients
        ],
        steps=[
            recipes.StepInput(
                instruction=s.instruction,
                section=s.section,
                minutes=s.minutes,
                video_seconds=s.video_seconds,
            )
            for s in detail.steps
        ],
        tags=list(detail.tags),
    )


def apply_sections(
    conn: sqlite3.Connection,
    recipe_id: int,
    run_id: int,
    take: Sequence[str],
    *,
    applied_by: int | None,
) -> list[str]:
    """Write the chosen sections of a draft through the normal recipe update; return the keys.

    Only sections that actually differ are applied (ticking an unchanged one is a no-op). The
    run is claimed 'applied' first - atomically, WHERE state = 'draft' - so a double-submitted
    form cannot apply twice; a failure while writing releases the claim. The recipe's
    ``current_extraction_run_id`` moves to this run only when EVERY changed section was taken;
    a partial apply records the run as consulted without claiming the recipe now matches it.
    """
    comparison = build_comparison(conn, recipe_id, run_id)
    if comparison is None:
        raise ReextractError("That reading is no longer available.")
    if comparison.run.state != "draft":
        raise ReextractError("That reading has already been reviewed.")
    changed = changed_keys(comparison.sections)
    chosen = [key for key in SECTION_KEYS if key in set(take) and key in changed]
    if not chosen:
        raise ReextractError("Pick at least one changed section to take.")

    stamp = now_iso()
    claimed = conn.execute(
        """UPDATE extraction_runs
           SET state = 'applied', reviewed_at = ?, reviewed_by = ?, applied_sections = ?
           WHERE id = ? AND recipe_id = ? AND state = 'draft'""",
        (stamp, applied_by, json.dumps(chosen), run_id, recipe_id),
    )
    conn.commit()
    if claimed.rowcount == 0:
        raise ReextractError("That reading has already been reviewed.")

    merged = merge_sections(
        _input_from_detail(conn, comparison.recipe), comparison.draft_input, chosen
    )
    try:
        recipes.update_recipe(conn, recipe_id, merged, saved_by=applied_by, food_status="pending")
    except Exception:
        conn.rollback()
        conn.execute(
            """UPDATE extraction_runs SET state = 'draft', reviewed_at = NULL,
               reviewed_by = NULL, applied_sections = NULL WHERE id = ?""",
            (run_id,),
        )
        conn.commit()
        raise
    if set(chosen) == set(changed):
        conn.execute(
            "UPDATE recipes SET current_extraction_run_id = ? WHERE id = ?", (run_id, recipe_id)
        )
        conn.commit()
    return chosen


def dismiss_draft(
    conn: sqlite3.Connection, recipe_id: int, run_id: int, *, dismissed_by: int | None
) -> bool:
    """Reject a reading: the recipe stays exactly as it is. False if it was not a live draft."""
    cur = conn.execute(
        """UPDATE extraction_runs SET state = 'dismissed', reviewed_at = ?, reviewed_by = ?
           WHERE id = ? AND recipe_id = ? AND state = 'draft'""",
        (now_iso(), dismissed_by, run_id, recipe_id),
    )
    conn.commit()
    return cur.rowcount > 0
