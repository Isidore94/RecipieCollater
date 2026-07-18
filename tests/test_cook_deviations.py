"""After-cook deviation capture: what actually happened feeds the log AND the pantry."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.services import cooking, deductions, foods, pantry, recipes
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _recipe(conn: sqlite3.Connection, ings: list[recipes.IngredientInput]) -> recipes.RecipeDetail:
    rid = recipes.create_recipe(
        conn, recipes.RecipeInput(title="Tacos", base_servings="4", ingredients=ings)
    )
    detail = recipes.get_recipe(conn, rid)
    assert detail is not None
    return detail


def _sour_cream_recipe(conn: sqlite3.Connection) -> recipes.RecipeDetail:
    return _recipe(
        conn,
        [
            recipes.IngredientInput(quantity_text="200", unit="grams", food="sour cream"),
            recipes.IngredientInput(quantity_text="1", unit="each", food="onion"),
        ],
    )


def test_deviations_recorded_and_rendered(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    detail = _sour_cream_recipe(migrated_db)
    sour_id, onion_id = detail.ingredients[0].id, detail.ingredients[1].id
    cooking.record_cook(
        migrated_db, detail.id,
        cooking.CookCaptureInput(
            rating=8,
            deviations={
                sour_id: cooking.DeviationInput(kind="substituted", text="greek yogurt"),
                onion_id: cooking.DeviationInput(kind="omitted"),
            },
            additions="a can of black beans",
        ),
    )
    entry = cooking.list_cook_log(migrated_db, detail.id)[0]
    displays = [d.display for d in entry.deviations]
    assert "used greek yogurt instead of sour cream" in displays
    assert "left out onion" in displays
    assert entry.additions == "a can of black beans"
    # once the sub is saved to food_substitutes, the log marks it remembered
    sub = next(d for d in entry.deviations if d.kind == "substituted")
    assert sub.remembered is False
    assert sub.food_id is not None
    foods.record_substitute(migrated_db, sub.food_id, "greek yogurt")
    entry = cooking.list_cook_log(migrated_db, detail.id)[0]
    assert next(d for d in entry.deviations if d.kind == "substituted").remembered is True


def test_invalid_deviation_kind_rejected(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    detail = _sour_cream_recipe(migrated_db)
    with pytest.raises(cooking.CookError):
        cooking.record_cook(
            migrated_db, detail.id,
            cooking.CookCaptureInput(
                deviations={detail.ingredients[0].id: cooking.DeviationInput(kind="exploded")}
            ),
        )


def test_adjusted_amount_stored_when_parseable(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    detail = _sour_cream_recipe(migrated_db)
    sour_id = detail.ingredients[0].id
    cook_id = cooking.record_cook(
        migrated_db, detail.id,
        cooking.CookCaptureInput(
            deviations={sour_id: cooking.DeviationInput(kind="adjusted", text="100")}
        ),
    )
    row = migrated_db.execute(
        "SELECT used_text, used_quantity_text FROM cook_log_ingredients "
        "WHERE cook_log_id = ? AND ingredient_id = ?",
        (cook_id, sour_id),
    ).fetchone()
    assert row["used_text"] == "100" and row["used_quantity_text"] == "100"


def test_deductions_honor_deviations(migrated_db: sqlite3.Connection) -> None:
    """Omitted lines don't deduct; adjusted lines deduct what was ACTUALLY used."""
    seed_core_units(migrated_db)
    detail = _sour_cream_recipe(migrated_db)
    sour_id, onion_id = detail.ingredients[0].id, detail.ingredients[1].id
    loc = pantry.create_location(migrated_db, "Fridge")
    sour_item = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Sour cream", location_id=loc, quantity_mode="exact",
            food="sour cream", quantity_text="500", unit="grams",
        ),
    )
    pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Onions", location_id=loc, quantity_mode="exact",
            food="onion", quantity_text="3", unit="each",
        ),
    )
    cook_id = cooking.record_cook(
        migrated_db, detail.id,
        cooking.CookCaptureInput(
            deviations={
                sour_id: cooking.DeviationInput(kind="adjusted", text="100"),
                onion_id: cooking.DeviationInput(kind="omitted"),
            }
        ),
    )
    proposal = deductions.propose(migrated_db, detail.id, cook_log_id=cook_id)
    by_label = {line.label: line for line in proposal.lines}
    onion_line = by_label["1 each onion"]
    assert onion_line.kind == "skip" and "left it out" in (onion_line.reason or "")
    deductions.apply(
        migrated_db, detail.id, cook_id,
        line_ids={line.ingredient_id for line in proposal.deductible_lines},
    )
    item = pantry.get_item(migrated_db, sour_item)
    assert item is not None and item.quantity_text == "400"  # 500 - 100 actually used, not -200


def test_remember_sub_route(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    detail = _sour_cream_recipe(migrated_db)
    food_id = detail.ingredients[0].food_id
    assert food_id is not None
    resp = admin_client.post(
        f"/recipes/{detail.slug}/remember-sub",
        data={"food_id": str(food_id), "text": "greek yogurt"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert foods.substitutes_for(migrated_db, food_id)[0].substitute_text == "greek yogurt"


def test_after_cook_form_posts_deviations(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    seed_core_units(migrated_db)
    detail = _sour_cream_recipe(migrated_db)
    sour_id = detail.ingredients[0].id
    resp = admin_client.post(
        f"/recipes/{detail.slug}/after-cook",
        data={
            "rating": "9", "servings_made": "4",
            f"dev_{sour_id}": "substituted", f"dev_text_{sour_id}": "greek yogurt",
            "additions": "extra lime",
        },
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    entry = cooking.list_cook_log(migrated_db, detail.id)[0]
    assert entry.additions == "extra lime"
    assert any("greek yogurt" in d.display for d in entry.deviations)
