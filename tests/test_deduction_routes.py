"""Cook-through deductions over HTTP: after-cook routes to review, review applies, undo restores."""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import pantry, recipes
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _seed(conn: sqlite3.Connection) -> tuple[str, int, int]:
    """A recipe using 200 g flour + a pantry with 1000 g flour. Returns slug, ingredient, item."""
    seed_core_units(conn)
    rid = recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title="Bread", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="200", unit="grams", food="flour")],
        ),
    )
    detail = recipes.get_recipe(conn, rid)
    assert detail is not None
    loc = pantry.create_location(conn, "Pantry")
    item = pantry.add_item(
        conn,
        pantry.PantryItemInput(
            display_name="Flour", location_id=loc, quantity_mode="exact",
            food="flour", quantity_text="1000", unit="grams",
        ),
    )
    return detail.slug, detail.ingredients[0].id, item


def _last_cook_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT id FROM cook_log ORDER BY id DESC LIMIT 1").fetchone()["id"])


def test_after_cook_redirects_to_review(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    slug, _ing, _item = _seed(migrated_db)
    resp = admin_client.post(
        f"/recipes/{slug}/after-cook", data={"servings_made": "4"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/recipes/{slug}/deductions" in resp.headers["location"]


def test_review_page_lists_proposal(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    slug, _ing, _item = _seed(migrated_db)
    resp = admin_client.get(f"/recipes/{slug}/deductions?cook=1&servings=4")
    assert resp.status_code == 200
    assert "Flour" in resp.text
    assert "Deduct from pantry" in resp.text


def test_apply_deducts_and_undo_restores(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    slug, ing_id, item = _seed(migrated_db)
    # after-cook records the cook (a real cook_log the adjustment can reference), then review.
    admin_client.post(
        f"/recipes/{slug}/after-cook", data={"servings_made": "4"}, headers=SAME_ORIGIN
    )
    cook_id = _last_cook_id(migrated_db)

    apply = admin_client.post(
        f"/recipes/{slug}/deductions",
        data={"cook_log_id": str(cook_id), "servings": "4", "line": str(ing_id)},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert apply.status_code == 303 and "applied=" in apply.headers["location"]
    assert pantry.get_item(migrated_db, item).quantity_text == "800"

    batch = migrated_db.execute(
        "SELECT batch_id FROM pantry_adjustments WHERE reason = 'cook' LIMIT 1"
    ).fetchone()["batch_id"]
    undo = admin_client.post(
        f"/recipes/{slug}/deductions/undo", data={"batch_id": batch},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert undo.status_code == 303
    assert pantry.get_item(migrated_db, item).quantity_text == "1000"  # restored


def test_auto_apply_when_trusted(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    slug, ing_id, item = _seed(migrated_db)
    # First cook -> review; apply with trust + auto.
    admin_client.post(
        f"/recipes/{slug}/after-cook", data={"servings_made": "4"}, headers=SAME_ORIGIN
    )
    admin_client.post(
        f"/recipes/{slug}/deductions",
        data={
            "cook_log_id": str(_last_cook_id(migrated_db)), "servings": "4",
            "line": str(ing_id), "trust": "on", "auto": "on",
        },
        headers=SAME_ORIGIN,
    )
    assert pantry.get_item(migrated_db, item).quantity_text == "800"

    # Second cook -> auto-applies straight to the summary, no review step.
    resp = admin_client.post(
        f"/recipes/{slug}/after-cook", data={"servings_made": "4"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert "applied=" in resp.headers["location"]
    assert pantry.get_item(migrated_db, item).quantity_text == "600"  # deducted again automatically


def test_double_submit_apply_no_double_deduct(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    # Finding #2 over HTTP: re-POSTing the review form must not deduct twice.
    slug, ing_id, item = _seed(migrated_db)
    admin_client.post(
        f"/recipes/{slug}/after-cook", data={"servings_made": "4"}, headers=SAME_ORIGIN
    )
    cook_id = _last_cook_id(migrated_db)
    body = {"cook_log_id": str(cook_id), "servings": "4", "line": str(ing_id)}
    admin_client.post(f"/recipes/{slug}/deductions", data=body, headers=SAME_ORIGIN)
    admin_client.post(f"/recipes/{slug}/deductions", data=body, headers=SAME_ORIGIN)  # resubmit
    assert pantry.get_item(migrated_db, item).quantity_text == "800"  # deducted once


def test_bad_cook_log_id_does_not_500(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    # Finding #6: a forged cook_log_id must redirect, not violate the FK and 500.
    slug, ing_id, item = _seed(migrated_db)
    resp = admin_client.post(
        f"/recipes/{slug}/deductions",
        data={"cook_log_id": "99999", "servings": "4", "line": str(ing_id)},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert pantry.get_item(migrated_db, item).quantity_text == "1000"  # nothing deducted


def test_bad_servings_returns_400(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    # Finding #7: a non-numeric ?servings= is a 400, not a 500.
    slug, _ing, _item = _seed(migrated_db)
    resp = admin_client.get(f"/recipes/{slug}/deductions?cook=1&servings=abc")
    assert resp.status_code == 400
