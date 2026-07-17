"""Recipe FTS5 search (migration 006): indexing, trigger sync on edit, and removal."""

from __future__ import annotations

import sqlite3

from app.services import units


def _search(conn: sqlite3.Connection, query: str) -> set[int]:
    rows = conn.execute(
        "SELECT rowid FROM recipe_fts WHERE recipe_fts MATCH ?", (query,)
    ).fetchall()
    return {int(r["rowid"]) for r in rows}


def _seed_recipe(conn: sqlite3.Connection) -> int:
    units.seed_core_units(conn)
    cup = units.resolve_unit(conn, "cup")
    assert cup is not None
    recipe_id = conn.execute(
        "INSERT INTO recipes (slug, title, tldr, description) VALUES (?, ?, ?, ?)",
        (
            "tomato-pasta",
            "Tomato Pasta",
            "Simmer and reduce, then toss.",
            "A weeknight staple.",
        ),
    ).lastrowid
    assert recipe_id is not None
    conn.execute(
        "INSERT INTO recipe_ingredients "
        "(recipe_id, sort_order, original_text, quantity_text, unit_id) VALUES (?, ?, ?, ?, ?)",
        (recipe_id, 0, "2 cups San Marzano tomatoes", "2", cup.id),
    )
    tag_id = conn.execute("INSERT INTO tags (name) VALUES ('italian')").lastrowid
    conn.execute("INSERT INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)", (recipe_id, tag_id))
    conn.commit()
    return recipe_id


def test_fts_finds_by_title_ingredient_and_tag(migrated_db: sqlite3.Connection) -> None:
    recipe_id = _seed_recipe(migrated_db)
    assert _search(migrated_db, "pasta") == {recipe_id}  # title
    assert _search(migrated_db, "marzano") == {recipe_id}  # ingredient text
    assert _search(migrated_db, "italian") == {recipe_id}  # tag
    assert _search(migrated_db, "reduce") == {recipe_id}  # tldr
    assert _search(migrated_db, "chocolate") == set()  # no match


def test_fts_reflects_title_edit(migrated_db: sqlite3.Connection) -> None:
    recipe_id = _seed_recipe(migrated_db)
    migrated_db.execute("UPDATE recipes SET title = 'Arrabbiata' WHERE id = ?", (recipe_id,))
    migrated_db.commit()
    assert _search(migrated_db, "arrabbiata") == {recipe_id}
    assert _search(migrated_db, "pasta") == set()  # old title no longer indexed


def test_fts_reflects_ingredient_change(migrated_db: sqlite3.Connection) -> None:
    recipe_id = _seed_recipe(migrated_db)
    migrated_db.execute(
        "UPDATE recipe_ingredients SET original_text = 'fresh basil leaves' WHERE recipe_id = ?",
        (recipe_id,),
    )
    migrated_db.commit()
    assert _search(migrated_db, "basil") == {recipe_id}
    assert _search(migrated_db, "marzano") == set()


def test_fts_removes_on_delete(migrated_db: sqlite3.Connection) -> None:
    recipe_id = _seed_recipe(migrated_db)
    migrated_db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    migrated_db.commit()
    assert _search(migrated_db, "pasta") == set()
    assert migrated_db.execute("SELECT COUNT(*) FROM recipe_fts").fetchone()[0] == 0
