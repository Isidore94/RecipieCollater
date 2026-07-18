"""Cook mode + cook log: timer parsing, the cook-view builder, after-cook capture, and staleness."""

from __future__ import annotations

import sqlite3

import pytest

from app.services import cooking, recipes
from app.services.units import seed_core_units


def _recipe(conn: sqlite3.Connection, title: str = "Chili") -> int:
    seed_core_units(conn)
    return recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=title,
            base_servings="4",
            ingredients=[
                recipes.IngredientInput(
                    original_text="2 cups beans", quantity_text="2", unit="cups", food="beans"
                )
            ],
            steps=[
                recipes.StepInput(instruction="Simmer for 20 minutes, then rest 1 hour."),
                recipes.StepInput(instruction="Serve."),
            ],
        ),
    )


def test_find_timers() -> None:
    timers = cooking.find_timers("Boil 5 min, simmer 1.5 hours, rest 30 seconds; salt to taste.")
    assert [(t.seconds) for t in timers] == [300, 5400, 30]


def test_build_cook_view_scales_and_numbers(migrated_db: sqlite3.Connection) -> None:
    detail = recipes.get_recipe(migrated_db, _recipe(migrated_db))
    assert detail is not None
    view = cooking.build_cook_view(detail, "8")  # 4 -> 8 servings doubles it
    assert [s.number for s in view.steps] == [1, 2]
    assert view.steps[0].timers[0].seconds == 1200
    assert view.steps[0].seek_available is False  # no video
    assert len(view.ingredients) == 1
    assert "4 cups beans" in view.ingredients[0].display


def test_record_cook_logs_promotes_and_feeds_times(migrated_db: sqlite3.Connection) -> None:
    rid = _recipe(migrated_db)  # status defaults to inbox
    log_id = cooking.record_cook(
        migrated_db, rid,
        cooking.CookCaptureInput(
            rating=8, servings_made="4", active_minutes=25, elapsed_minutes=40,
            notes="kids loved it", promote=True,
        ),
        user_id=None,
    )
    assert log_id > 0
    detail = recipes.get_recipe(migrated_db, rid)
    assert detail is not None
    assert detail.rating == 8  # mirrored onto the recipe
    assert detail.our_minutes == 40 and detail.our_active_minutes == 25
    assert detail.status == "cookbook"  # promoted from inbox

    snapshot = migrated_db.execute(
        "SELECT count(*) FROM cook_log_ingredients WHERE cook_log_id = ?", (log_id,)
    ).fetchone()[0]
    assert snapshot == 1

    log = cooking.list_cook_log(migrated_db, rid)
    assert len(log) == 1
    assert log[0].rating == 8 and log[0].notes == "kids loved it" and log[0].servings_made == "4"


def test_record_cook_rejects_bad_servings(migrated_db: sqlite3.Connection) -> None:
    rid = _recipe(migrated_db)
    with pytest.raises(cooking.CookError):
        cooking.record_cook(
            migrated_db, rid, cooking.CookCaptureInput(servings_made="0"), user_id=None
        )
    # nothing written on rejection
    assert migrated_db.execute("SELECT count(*) FROM cook_log").fetchone()[0] == 0


def test_staleness_orders_never_cooked_first(migrated_db: sqlite3.Connection) -> None:
    alpha = _recipe(migrated_db, title="Alpha")
    beta = _recipe(migrated_db, title="Beta")
    recipes.set_status(migrated_db, alpha, "cookbook")
    recipes.set_status(migrated_db, beta, "cookbook")
    cooking.record_cook(migrated_db, alpha, cooking.CookCaptureInput(rating=5), user_id=None)

    order = [s.slug for s in cooking.list_recipes_by_staleness(migrated_db, status="cookbook")]
    assert order.index("beta") < order.index("alpha")  # never-cooked beta sorts before cooked alpha
