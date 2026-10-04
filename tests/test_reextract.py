"""Re-reading a recipe's source: the comparison draft (migration 020, docs/04 section 8).

Covers the pure diff, the pipeline's re-extract intent (a draft run, the recipe untouched, normal
replay safety intact), the compare screen, and apply/dismiss writing only what was chosen.
Fully offline: HTML comes from a fixture, fetches are monkeypatched, no AI provider is called.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.services import fetch, ingest, pipeline, recipes, reextract
from tests.conftest import SAME_ORIGIN

_FIXTURE = Path(__file__).parent / "fixtures" / "schema_org_recipe.html"
_URL = "https://example.test/carrot-soup"


@pytest.fixture(autouse=True)
def _no_image_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.images.store_image_from_url", lambda recipe_id, url: None)


def _ingest_recipe(conn: sqlite3.Connection) -> int:
    job, _ = ingest.enqueue_job(conn, _URL, html=_FIXTURE.read_text(encoding="utf-8"))
    pipeline.run_job(conn, job)
    done = ingest.get_job(conn, job.id)
    assert done is not None and done.recipe_id is not None
    return done.recipe_id


def _changed_html() -> str:
    """The same page after the site edited it: new title, a longer cook, one new step."""
    html = _FIXTURE.read_text(encoding="utf-8")
    html = html.replace('"name": "Cozy Carrot Soup"', '"name": "Silky Carrot Soup"')
    html = html.replace("PT25M", "PT30M").replace("PT35M", "PT40M")
    last = (
        '{"@type": "HowToStep", "text": "Pour in the broth, simmer 20 minutes, then blend smooth."}'
    )
    extra = '{"@type": "HowToStep", "text": "Swirl in cream and serve."}'
    return html.replace(last, f"{last},{extra}")


def _reread(
    conn: sqlite3.Connection,
    recipe_id: int,
    monkeypatch: pytest.MonkeyPatch,
    *,
    html: str | None = None,
) -> reextract.DraftRun:
    """Run a re-read to completion. With ``html`` it is a 'fetch again' returning that page."""
    if html is not None:
        monkeypatch.setattr(
            fetch,
            "fetch",
            lambda url: SimpleNamespace(html=html),
        )
    job, created = reextract.start_reread(
        conn, recipe_id, refetch=html is not None, submitted_by=None
    )
    assert created
    pipeline.run_job(conn, job)
    draft = reextract.pending_draft(conn, recipe_id)
    assert draft is not None
    return draft


# --------------------------------------------------------------------------------------
# Pure diff
# --------------------------------------------------------------------------------------


def _snap(**overrides: object) -> reextract.Snapshot:
    base: dict[str, object] = {
        "title": "Soup",
        "description": "Warm.",
        "servings_text": "4 servings",
        "prep_minutes": 10,
        "cook_minutes": 20,
        "total_minutes": 30,
        "ingredients": (
            reextract.IngredientLine("2 tbsp oil", "|2|tablespoon|oil||"),
            reextract.IngredientLine("1 tsp salt", "|1|teaspoon|salt||"),
        ),
        "steps": ("Heat.", "Simmer."),
        "tags": ("soup", "Dinner"),
    }
    base.update(overrides)
    return reextract.Snapshot(**base)  # type: ignore[arg-type]


def test_identical_snapshots_have_no_changed_sections() -> None:
    sections = reextract.diff_sections(_snap(), _snap())
    assert [s.key for s in sections] == list(reextract.SECTION_KEYS)
    assert reextract.changed_keys(sections) == []


def test_diff_ignores_case_whitespace_and_tag_order() -> None:
    draft = _snap(
        description="  warm. ",
        servings_text="4  Servings",
        steps=("heat.", "SIMMER.  "),
        tags=("dinner", "SOUP"),
    )
    assert reextract.changed_keys(reextract.diff_sections(_snap(), draft)) == []


@pytest.mark.parametrize(
    ("override", "key"),
    [
        ({"title": "Silky Soup"}, "title"),
        ({"description": "Rich."}, "description"),
        ({"cook_minutes": 25}, "times"),
        ({"servings_text": "6 servings"}, "servings"),
        ({"steps": ("Heat.", "Simmer.", "Serve.")}, "steps"),
        ({"tags": ("soup",)}, "tags"),
        (
            {"ingredients": (reextract.IngredientLine("2 tbsp oil", "|2|tablespoon|oil||"),)},
            "ingredients",
        ),
    ],
)
def test_each_section_reports_its_own_change(override: dict[str, object], key: str) -> None:
    sections = reextract.diff_sections(_snap(), _snap(**override))
    assert reextract.changed_keys(sections) == [key]


def test_missing_description_equals_blank_description() -> None:
    assert (
        reextract.changed_keys(
            reextract.diff_sections(_snap(description=None), _snap(description="  "))
        )
        == []
    )


def test_merge_sections_takes_only_named_sections() -> None:
    current = recipes.RecipeInput(
        title="Mine",
        tldr="Keep me",
        description="old",
        tier="family",
        base_servings="6",
        prep_minutes=1,
        cook_minutes=2,
        total_minutes=3,
        active_minutes=9,
        source_url="https://example.test/x",
        steps=[recipes.StepInput("old step")],
        tags=["a"],
    )
    draft = recipes.RecipeInput(
        title="Theirs",
        description="new",
        prep_minutes=10,
        cook_minutes=20,
        total_minutes=30,
        steps=[recipes.StepInput("new step")],
        tags=["b"],
    )
    merged = reextract.merge_sections(current, draft, ["title", "times"])
    assert merged.title == "Theirs"
    assert (merged.prep_minutes, merged.cook_minutes, merged.total_minutes) == (10, 20, 30)
    # Untouched: the sections not taken, and fields no section covers.
    assert merged.description == "old"
    assert [s.instruction for s in merged.steps] == ["old step"]
    assert merged.tags == ["a"]
    assert (merged.tldr, merged.tier, merged.base_servings) == ("Keep me", "family", "6")
    assert merged.active_minutes == 9
    assert merged.source_url == "https://example.test/x"


def test_snapshot_resolves_unit_text_so_aliases_do_not_look_changed() -> None:
    data = recipes.RecipeInput(
        title="T",
        ingredients=[recipes.IngredientInput(quantity_text="2", unit="tbsp", food="oil")],
    )
    snap = reextract.snapshot_from_input(
        data, unit_name=lambda t: {"tbsp": "tablespoon"}.get(t), food_name=lambda t: None
    )
    assert snap.ingredients[0].key == "|2|tablespoon|oil||"


# --------------------------------------------------------------------------------------
# Pipeline: the re-extract intent
# --------------------------------------------------------------------------------------


def test_reread_creates_draft_and_leaves_recipe_untouched(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    before = recipes.get_recipe(migrated_db, recipe_id)
    first_run = migrated_db.execute(
        "SELECT current_extraction_run_id AS r FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()["r"]

    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())

    assert recipes.get_recipe(migrated_db, recipe_id) == before
    assert migrated_db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1
    now = migrated_db.execute(
        "SELECT current_extraction_run_id AS r FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()["r"]
    assert now == first_run != draft.id
    assert draft.state == "draft" and draft.recipe_id == recipe_id
    # The first reading is still the accepted one.
    state = migrated_db.execute(
        "SELECT state FROM extraction_runs WHERE id = ?", (first_run,)
    ).fetchone()["state"]
    assert state == "accepted"
    # The re-read job finished without claiming to have produced a recipe.
    job = migrated_db.execute(
        "SELECT status, recipe_id, reextract_recipe_id, refetch FROM ingest_jobs "
        "WHERE reextract_recipe_id IS NOT NULL"
    ).fetchone()
    assert (job["status"], job["recipe_id"], job["reextract_recipe_id"], job["refetch"]) == (
        "done",
        None,
        recipe_id,
        1,
    )


def test_reread_without_refetch_reuses_stored_artifact_and_never_fetches(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)

    def boom(url: str) -> object:
        raise AssertionError("a reuse re-read must not touch the network")

    monkeypatch.setattr(fetch, "fetch", boom)
    job, created = reextract.start_reread(migrated_db, recipe_id, refetch=False, submitted_by=None)
    assert created and job.refetch is False
    assert ingest.read_artifact(migrated_db, job.id, "supplied_html") is not None
    pipeline.run_job(migrated_db, job)
    draft = reextract.pending_draft(migrated_db, recipe_id)
    assert draft is not None
    comparison = reextract.build_comparison(migrated_db, recipe_id, draft.id)
    assert comparison is not None
    # Same source, same stored page: nothing differs from the recipe as ingested.
    assert reextract.changed_keys(comparison.sections) == []


def test_reread_double_tap_returns_the_job_in_flight(
    migrated_db: sqlite3.Connection,
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    first, created_first = reextract.start_reread(
        migrated_db, recipe_id, refetch=False, submitted_by=None
    )
    second, created_second = reextract.start_reread(
        migrated_db, recipe_id, refetch=False, submitted_by=None
    )
    assert created_first and not created_second and first.id == second.id


def test_reread_refused_without_a_source_or_a_saved_copy(
    migrated_db: sqlite3.Connection,
) -> None:
    manual = recipes.create_recipe(migrated_db, recipes.RecipeInput(title="By hand"))
    with pytest.raises(reextract.ReextractError, match="no web link"):
        reextract.start_reread(migrated_db, manual, refetch=True, submitted_by=None)
    linked = recipes.create_recipe(
        migrated_db, recipes.RecipeInput(title="Linked", source_url=_URL)
    )
    with pytest.raises(reextract.ReextractError, match="fetch the page again"):
        reextract.start_reread(migrated_db, linked, refetch=False, submitted_by=None)


def test_normal_job_replay_safety_unchanged_by_reread(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = ingest.enqueue_job(migrated_db, _URL, html=_FIXTURE.read_text(encoding="utf-8"))
    pipeline.run_job(migrated_db, job)
    recipe_id = ingest.get_job(migrated_db, job.id).recipe_id  # type: ignore[union-attr]
    assert recipe_id is not None
    _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    # Replaying the ORIGINAL (stale) job still must not create or touch anything.
    pipeline.run_job(migrated_db, job)
    assert migrated_db.execute("SELECT COUNT(*) FROM recipes").fetchone()[0] == 1
    recipe = recipes.get_recipe(migrated_db, recipe_id)
    assert recipe is not None and recipe.title == "Cozy Carrot Soup"


def test_reread_job_replay_does_not_make_a_second_draft(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    monkeypatch.setattr(fetch, "fetch", lambda url: SimpleNamespace(html=_changed_html()))
    job, _ = reextract.start_reread(migrated_db, recipe_id, refetch=True, submitted_by=None)
    pipeline.run_job(migrated_db, job)
    pipeline.run_job(migrated_db, job)  # a worker retry
    drafts = migrated_db.execute(
        "SELECT COUNT(*) FROM extraction_runs WHERE state = 'draft'"
    ).fetchone()[0]
    assert drafts == 1


def test_a_newer_reading_supersedes_an_unreviewed_draft(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    first = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    second = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    assert second.id != first.id
    states = {
        r["id"]: r["state"]
        for r in migrated_db.execute("SELECT id, state FROM extraction_runs").fetchall()
    }
    assert states[first.id] == "dismissed" and states[second.id] == "draft"


# --------------------------------------------------------------------------------------
# Apply / dismiss
# --------------------------------------------------------------------------------------


def _family_edit(conn: sqlite3.Connection, recipe_id: int) -> None:
    """The family's own changes: a tag and a pantry decision on one ingredient line."""
    recipes.add_tags(conn, recipe_id, ["family-fave"])
    conn.execute(
        "UPDATE recipe_ingredients SET deduct_from_pantry = 0 "
        "WHERE recipe_id = ? AND original_text LIKE '%salt%'",
        (recipe_id,),
    )
    conn.commit()


def test_apply_writes_only_chosen_sections_and_keeps_edits_elsewhere(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    _family_edit(migrated_db, recipe_id)
    before = recipes.get_recipe(migrated_db, recipe_id)
    assert before is not None
    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    revisions_before = migrated_db.execute(
        "SELECT COUNT(*) FROM recipe_revisions WHERE recipe_id = ?", (recipe_id,)
    ).fetchone()[0]

    taken = reextract.apply_sections(
        migrated_db, recipe_id, draft.id, ["title", "times"], applied_by=None
    )
    assert taken == ["title", "times"]

    after = recipes.get_recipe(migrated_db, recipe_id)
    assert after is not None
    assert after.title == "Silky Carrot Soup"
    assert (after.cook_minutes, after.total_minutes) == (30, 40)
    # Not taken: the new step stays out, and the family's tag survives.
    assert [s.instruction for s in after.steps] == [s.instruction for s in before.steps]
    assert "family-fave" in after.tags
    assert [i.original_text for i in after.ingredients] == [
        i.original_text for i in before.ingredients
    ]
    # The family's pantry decision on the unchanged salt line survived the rebuild.
    salt = migrated_db.execute(
        "SELECT deduct_from_pantry FROM recipe_ingredients "
        "WHERE recipe_id = ? AND original_text LIKE '%salt%'",
        (recipe_id,),
    ).fetchone()
    assert salt["deduct_from_pantry"] == 0
    # It went through the normal edit service: a revision snapshot, and the search index.
    revisions_after = migrated_db.execute(
        "SELECT COUNT(*) FROM recipe_revisions WHERE recipe_id = ?", (recipe_id,)
    ).fetchone()[0]
    assert revisions_after == revisions_before + 1
    assert [r.id for r in recipes.list_recipes(migrated_db, query="Silky")] == [recipe_id]
    assert recipes.list_recipes(migrated_db, query="Cozy") == []


def test_apply_steps_section_replaces_steps_only(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    _family_edit(migrated_db, recipe_id)
    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    reextract.apply_sections(migrated_db, recipe_id, draft.id, ["steps"], applied_by=None)
    after = recipes.get_recipe(migrated_db, recipe_id)
    assert after is not None
    assert len(after.steps) == 4 and after.steps[-1].instruction == "Swirl in cream and serve."
    assert after.title == "Cozy Carrot Soup"
    salt = migrated_db.execute(
        "SELECT deduct_from_pantry FROM recipe_ingredients "
        "WHERE recipe_id = ? AND original_text LIKE '%salt%'",
        (recipe_id,),
    ).fetchone()
    assert salt["deduct_from_pantry"] == 0


def test_partial_apply_records_run_but_keeps_current_accepted_run(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    first_run = migrated_db.execute(
        "SELECT current_extraction_run_id AS r FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()["r"]
    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    reextract.apply_sections(migrated_db, recipe_id, draft.id, ["title"], applied_by=None)
    run = migrated_db.execute(
        "SELECT state, applied_sections, reviewed_at FROM extraction_runs WHERE id = ?",
        (draft.id,),
    ).fetchone()
    assert run["state"] == "applied" and run["applied_sections"] == '["title"]'
    assert run["reviewed_at"] is not None
    current = migrated_db.execute(
        "SELECT current_extraction_run_id AS r FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()["r"]
    assert current == first_run


def test_taking_every_changed_section_accepts_the_run(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    comparison = reextract.build_comparison(migrated_db, recipe_id, draft.id)
    assert comparison is not None
    keys = reextract.changed_keys(comparison.sections)
    assert {"title", "times", "steps"} <= set(keys)
    reextract.apply_sections(migrated_db, recipe_id, draft.id, keys, applied_by=None)
    current = migrated_db.execute(
        "SELECT current_extraction_run_id AS r FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()["r"]
    assert current == draft.id


def test_apply_is_single_shot_and_needs_a_changed_section(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    with pytest.raises(reextract.ReextractError, match="at least one"):
        reextract.apply_sections(migrated_db, recipe_id, draft.id, ["tags"], applied_by=None)
    reextract.apply_sections(migrated_db, recipe_id, draft.id, ["title"], applied_by=None)
    with pytest.raises(reextract.ReextractError, match="already been reviewed"):
        reextract.apply_sections(migrated_db, recipe_id, draft.id, ["title"], applied_by=None)


def test_dismiss_leaves_recipe_untouched(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id = _ingest_recipe(migrated_db)
    before = recipes.get_recipe(migrated_db, recipe_id)
    draft = _reread(migrated_db, recipe_id, monkeypatch, html=_changed_html())
    assert reextract.dismiss_draft(migrated_db, recipe_id, draft.id, dismissed_by=None)
    assert not reextract.dismiss_draft(migrated_db, recipe_id, draft.id, dismissed_by=None)
    assert recipes.get_recipe(migrated_db, recipe_id) == before
    assert reextract.pending_draft(migrated_db, recipe_id) is None


# --------------------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------------------


def _client_recipe(admin_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    """Ingest + re-read through the real DB behind the client; returns (recipe_id, run_id)."""
    from app import config, db

    conn = db.connect(config.get_settings().db_path)
    try:
        recipe_id = _ingest_recipe(conn)
        draft = _reread(conn, recipe_id, monkeypatch, html=_changed_html())
    finally:
        conn.close()
    return recipe_id, draft.id


def test_compare_screen_and_banners_render(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id, run_id = _client_recipe(admin_client, monkeypatch)

    sheet = admin_client.get("/recipes/cozy-carrot-soup")
    assert sheet.status_code == 200
    assert "A new reading is ready to compare" in sheet.text
    assert f"/recipes/{recipe_id}/compare/{run_id}" in sheet.text

    inbox = admin_client.get("/inbox")
    assert "A new reading is ready to compare" in inbox.text

    page = admin_client.get(f"/recipes/{recipe_id}/compare/{run_id}")
    assert page.status_code == 200
    assert "Cozy Carrot Soup" in page.text and "Silky Carrot Soup" in page.text
    assert "Swirl in cream and serve." in page.text
    assert 'name="take" value="title"' in page.text
    assert 'name="take" value="times"' in page.text
    assert 'name="take" value="steps"' in page.text
    assert 'name="take" value="tags"' not in page.text  # unchanged -> nothing to take


def test_apply_and_dismiss_routes(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id, run_id = _client_recipe(admin_client, monkeypatch)
    resp = admin_client.post(
        f"/recipes/{recipe_id}/compare/{run_id}/apply",
        data={"take": ["title"]},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    sheet = admin_client.get("/recipes/cozy-carrot-soup")
    assert "Silky Carrot Soup" in sheet.text
    assert "Swirl in cream" not in sheet.text
    assert "A new reading is ready to compare" not in sheet.text

    # Reviewed readings bounce back to the sheet instead of re-offering the form.
    again = admin_client.get(f"/recipes/{recipe_id}/compare/{run_id}", follow_redirects=False)
    assert again.status_code == 303


def test_apply_route_without_a_pick_explains_itself(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id, run_id = _client_recipe(admin_client, monkeypatch)
    resp = admin_client.post(
        f"/recipes/{recipe_id}/compare/{run_id}/apply",
        data={},
        headers=SAME_ORIGIN,
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "Pick at least one" in resp.text


def test_take_all_and_dismiss_routes(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe_id, run_id = _client_recipe(admin_client, monkeypatch)
    admin_client.post(
        f"/recipes/{recipe_id}/compare/{run_id}/apply",
        data={"take_all": "1"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    # The slug is stable across an edit, so the sheet is still at the original URL.
    sheet = admin_client.get("/recipes/cozy-carrot-soup")
    assert "Swirl in cream and serve." in sheet.text

    from app import config, db

    conn = db.connect(config.get_settings().db_path)
    try:
        draft = _reread(
            conn, recipe_id, monkeypatch, html=_changed_html().replace("Silky", "Velvet")
        )
    finally:
        conn.close()
    resp = admin_client.post(
        f"/recipes/{recipe_id}/compare/{draft.id}/dismiss",
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Silky Carrot Soup" in admin_client.get("/recipes/cozy-carrot-soup").text


def test_reread_route_enqueues_and_the_sheet_offers_it(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import config, db

    scheduled: list[int] = []
    monkeypatch.setattr("app.routers.reread.schedule_processing", scheduled.append)
    conn = db.connect(config.get_settings().db_path)
    try:
        recipe_id = _ingest_recipe(conn)
    finally:
        conn.close()
    sheet = admin_client.get("/recipes/cozy-carrot-soup")
    assert "Re-read source" in sheet.text

    resp = admin_client.post(
        f"/recipes/{recipe_id}/reread", data={}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert resp.status_code == 303 and len(scheduled) == 1
    # A second tap while it is still queued does not schedule another extraction.
    admin_client.post(
        f"/recipes/{recipe_id}/reread", data={}, headers=SAME_ORIGIN, follow_redirects=False
    )
    assert len(scheduled) == 1
    assert "Reading the source again" in admin_client.get("/recipes/cozy-carrot-soup").text


def test_reread_route_requires_same_site(
    admin_client: TestClient,
) -> None:
    resp = admin_client.post(
        "/recipes/1/reread",
        data={},
        headers={"sec-fetch-site": "cross-site"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
