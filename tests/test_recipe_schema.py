"""Migration 005 recipe schema: relationships, CHECK constraints, and cascade delete."""

from __future__ import annotations

import sqlite3

import pytest

from app.services import units


def _new_recipe(conn: sqlite3.Connection, slug: str = "pasta", title: str = "Pasta") -> int:
    row_id = conn.execute(
        "INSERT INTO recipes (slug, title) VALUES (?, ?)", (slug, title)
    ).lastrowid
    assert row_id is not None
    return row_id


def test_recipe_round_trip_and_cascade(migrated_db: sqlite3.Connection) -> None:
    units.seed_core_units(migrated_db)
    cup = units.resolve_unit(migrated_db, "cup")
    assert cup is not None

    recipe_id = _new_recipe(migrated_db)
    ingredient_id = migrated_db.execute(
        "INSERT INTO recipe_ingredients "
        "(recipe_id, sort_order, original_text, quantity_text, unit_id, scaling_mode) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recipe_id, 0, "2 cups flour", "2", cup.id, "linear"),
    ).lastrowid
    step_id = migrated_db.execute(
        "INSERT INTO recipe_steps (recipe_id, sort_order, instruction) VALUES (?, ?, ?)",
        (recipe_id, 0, "Mix the flour."),
    ).lastrowid
    migrated_db.execute(
        "INSERT INTO recipe_step_ingredients (step_id, ingredient_id, quantity_text) "
        "VALUES (?, ?, ?)",
        (step_id, ingredient_id, "2"),
    )
    tag_id = migrated_db.execute("INSERT INTO tags (name) VALUES ('italian')").lastrowid
    migrated_db.execute(
        "INSERT INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)", (recipe_id, tag_id)
    )
    migrated_db.commit()

    title = migrated_db.execute(
        "SELECT title FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()["title"]
    assert title == "Pasta"

    # Deleting the recipe cascades to its steps, ingredients, links, and tag links.
    migrated_db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    migrated_db.commit()
    def count(sql: str) -> int:
        return int(migrated_db.execute(sql, (recipe_id,)).fetchone()[0])

    assert count("SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = ?") == 0
    assert count("SELECT COUNT(*) FROM recipe_steps WHERE recipe_id = ?") == 0
    assert count("SELECT COUNT(*) FROM recipe_tags WHERE recipe_id = ?") == 0
    assert migrated_db.execute("SELECT COUNT(*) FROM recipe_step_ingredients").fetchone()[0] == 0
    # The shared tag vocabulary survives; only the link is removed.
    assert migrated_db.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 1


def test_quantity_requires_a_unit(migrated_db: sqlite3.Connection) -> None:
    recipe_id = _new_recipe(migrated_db, "x", "X")
    with pytest.raises(sqlite3.IntegrityError):
        migrated_db.execute(
            "INSERT INTO recipe_ingredients (recipe_id, sort_order, original_text, quantity_text) "
            "VALUES (?, ?, ?, ?)",
            (recipe_id, 0, "2 flour", "2"),  # quantity without a unit violates the CHECK
        )


def test_scaling_mode_is_constrained(migrated_db: sqlite3.Connection) -> None:
    recipe_id = _new_recipe(migrated_db, "y", "Y")
    with pytest.raises(sqlite3.IntegrityError):
        migrated_db.execute(
            "INSERT INTO recipe_ingredients (recipe_id, sort_order, original_text, scaling_mode) "
            "VALUES (?, ?, ?, ?)",
            (recipe_id, 0, "flour", "bogus-mode"),
        )


def test_status_and_tier_constrained(migrated_db: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        migrated_db.execute("INSERT INTO recipes (slug, title, status) VALUES ('z','Z','nope')")
    with pytest.raises(sqlite3.IntegrityError):
        migrated_db.execute("INSERT INTO recipes (slug, title, tier) VALUES ('z2','Z2','deluxe')")
