"""Recipe edits must not un-learn pantry knowledge; food status controls the review flow."""

from __future__ import annotations

import sqlite3

from app.services import recipes
from app.services.units import seed_core_units


def _input(qty: str = "200") -> recipes.RecipeInput:
    return recipes.RecipeInput(
        title="Bread", base_servings="4",
        ingredients=[
            recipes.IngredientInput(quantity_text=qty, unit="grams", food="flour"),
            recipes.IngredientInput(quantity_text="1", unit="each", food="egg"),
        ],
    )


def _flour_row(conn: sqlite3.Connection, recipe_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        """SELECT ri.* FROM recipe_ingredients ri JOIN foods f ON f.id = ri.food_id
           WHERE ri.recipe_id = ? AND f.name = 'flour'""",
        (recipe_id,),
    ).fetchone()
    assert row is not None
    return row


def _learn(conn: sqlite3.Connection, ingredient_id: int) -> None:
    conn.execute(
        "UPDATE recipe_ingredients SET pantry_item_hint = NULL, deduct_from_pantry = 0, "
        "deduction_trusted_at = '2026-01-01 00:00:00', deduction_trust_signature = 'sig' "
        "WHERE id = ?",
        (ingredient_id,),
    )
    conn.commit()


def test_edit_preserves_pantry_knowledge_on_unchanged_lines(
    migrated_db: sqlite3.Connection,
) -> None:
    seed_core_units(migrated_db)
    rid = recipes.create_recipe(migrated_db, _input())
    _learn(migrated_db, int(_flour_row(migrated_db, rid)["id"]))

    assert recipes.update_recipe(migrated_db, rid, _input()) is True  # same ingredients

    row = _flour_row(migrated_db, rid)
    assert row["deduct_from_pantry"] == 0  # the learned setting survived the edit
    assert row["deduction_trusted_at"] == "2026-01-01 00:00:00"
    assert row["deduction_trust_signature"] == "sig"


def test_edit_drops_knowledge_when_the_line_changed(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = recipes.create_recipe(migrated_db, _input())
    _learn(migrated_db, int(_flour_row(migrated_db, rid)["id"]))

    recipes.update_recipe(migrated_db, rid, _input(qty="300"))  # the flour amount changed

    row = _flour_row(migrated_db, rid)
    assert row["deduction_trusted_at"] is None  # trust correctly revoked
    assert row["deduct_from_pantry"] == 1  # back to the default


def test_food_status_pending_vs_confirmed(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    recipes.create_recipe(migrated_db, _input())  # manual entry -> confirmed
    row = migrated_db.execute("SELECT status FROM foods WHERE name = 'flour'").fetchone()
    assert row["status"] == "confirmed"
    recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Imported", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="1", unit="each", food="yuzu")],
        ),
        food_status="pending",
    )
    row = migrated_db.execute("SELECT status FROM foods WHERE name = 'yuzu'").fetchone()
    assert row["status"] == "pending"
    # an existing food is never demoted by a later pending-mode import
    recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Imported 2", base_servings="4",
            ingredients=[
                recipes.IngredientInput(quantity_text="1", unit="each", food="flour")
            ],
        ),
        food_status="pending",
    )
    row = migrated_db.execute("SELECT status FROM foods WHERE name = 'flour'").fetchone()
    assert row["status"] == "confirmed"


def test_add_tags_idempotent(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = recipes.create_recipe(migrated_db, _input())
    assert recipes.add_tags(migrated_db, rid, ["dinner", "italian"]) == 2
    assert recipes.add_tags(migrated_db, rid, ["dinner"]) == 0  # already linked
    detail = recipes.get_recipe(migrated_db, rid)
    assert detail is not None and set(detail.tags) == {"dinner", "italian"}
