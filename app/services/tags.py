"""Tag upkeep: rename, merge and delete a tag across the whole cookbook, reversibly.

Tags are what makes a large cookbook navigable - they are the filter chips, they feed FTS,
and the Phase-5 assistant's hard filters read them. They also arrive from three directions
(extraction prompts, the AI backfill, a free-text form field), so they drift: a capital, a
plural, a synonym. This module is the correction path, so a drifted tag is a ten-second fix
instead of an edit of every recipe that carries it.

Merges and deletes write a receipt (migration 019) recording exactly which recipes moved, so
undo puts back precisely what the operation took - the same reasoning as food merges.

FTS: the migration-006 triggers cover recipe_tags writes, so merge and delete stay in sync on
their own. A rename does not touch recipe_tags, so it reindexes explicitly - see
recipes.reindex_recipes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from app.security import now_iso
from app.services import recipes
from app.services.tag_vocabulary import EXAMPLE_CUISINES, VOCABULARY, drifted

__all__ = [
    "EXAMPLE_CUISINES",
    "MAX_TAG_LENGTH",
    "VOCABULARY",
    "TagEdit",
    "TagError",
    "UndoUnavailable",
    "delete",
    "drifted",
    "merge",
    "rename",
    "undo",
]


class TagError(Exception):
    """A tag operation the caller should be told about, in words a cook would use."""


class UndoUnavailable(TagError):
    """This tag edit cannot be reversed (unknown, or already undone)."""


@dataclass(frozen=True, slots=True)
class TagEdit:
    """What an edit did, so the caller can describe it and offer to undo it."""

    edit_id: int
    source_name: str
    target_name: str
    recipes_affected: int


def _clean(name: str) -> str:
    cleaned = " ".join(str(name).split())
    if not cleaned:
        raise TagError("a tag needs a name")
    if len(cleaned) > MAX_TAG_LENGTH:
        raise TagError(f"a tag can be at most {MAX_TAG_LENGTH} characters")
    return cleaned


# Long enough for "slow-cooked" and a two-word cuisine, short enough that a pasted paragraph
# cannot become a chip that breaks the filter bar.
MAX_TAG_LENGTH = 40


def _row(conn: sqlite3.Connection, tag_id: int) -> sqlite3.Row:
    row: sqlite3.Row | None = conn.execute(
        "SELECT id, name FROM tags WHERE id = ?", (tag_id,)
    ).fetchone()
    if row is None:
        raise TagError("that tag no longer exists")
    return row


def _recipe_ids(conn: sqlite3.Connection, tag_id: int) -> list[int]:
    return [
        int(r["recipe_id"])
        for r in conn.execute(
            "SELECT recipe_id FROM recipe_tags WHERE tag_id = ? ORDER BY recipe_id", (tag_id,)
        ).fetchall()
    ]


def rename(
    conn: sqlite3.Connection, tag_id: int, new_name: str, *, commit: bool = True
) -> TagEdit:
    """Rename a tag everywhere at once.

    Renaming onto a name another tag already holds is refused rather than quietly merged:
    merging discards the distinction between the two, which is a different decision and has
    its own undoable operation. Changing only the capitalisation of the tag's own name is a
    rename, not a collision - that is the "Soup" -> "soup" fix.
    """
    row = _row(conn, tag_id)
    clean = _clean(new_name)
    clash = conn.execute(
        "SELECT id FROM tags WHERE name = ? COLLATE NOCASE AND id <> ?", (clean, tag_id)
    ).fetchone()
    if clash is not None:
        raise TagError(f"a tag called “{clean}” already exists - merge them instead")

    affected = _recipe_ids(conn, tag_id)
    conn.execute("UPDATE tags SET name = ? WHERE id = ?", (clean, tag_id))
    recipes.reindex_recipes(conn, affected)
    if commit:
        conn.commit()
    return TagEdit(
        edit_id=0, source_name=str(row["name"]), target_name=clean,
        recipes_affected=len(affected),
    )


def merge(
    conn: sqlite3.Connection, source_id: int, target_id: int, *,
    edited_by: int | None = None, commit: bool = True,
) -> TagEdit:
    """Fold one tag into another: every recipe tagged source ends up tagged target.

    Returns a receipt recording which recipes carried the source and which of them gained the
    target, which is the only way this can be undone: afterwards the target's own recipes are
    indistinguishable from the ones it inherited.
    """
    if source_id == target_id:
        raise TagError("cannot merge a tag into itself")
    source = _row(conn, source_id)
    target = _row(conn, target_id)

    unlinked = _recipe_ids(conn, source_id)
    already = {
        int(r["recipe_id"])
        for r in conn.execute(
            "SELECT recipe_id FROM recipe_tags WHERE tag_id = ?", (target_id,)
        ).fetchall()
    }
    linked = [rid for rid in unlinked if rid not in already]

    receipt = conn.execute(
        "INSERT INTO tag_edits (kind, source_name, target_id, target_name, payload, edited_by) "
        "VALUES ('merge', ?, ?, ?, ?, ?)",
        (
            str(source["name"]), target_id, str(target["name"]),
            json.dumps({"unlinked": unlinked, "linked": linked}), edited_by,
        ),
    )
    for recipe_id in linked:
        conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag_id) VALUES (?, ?)",
            (recipe_id, target_id),
        )
    # Drop the links before the tag row so the recipe_tags triggers fire on rows we chose,
    # rather than relying on what a foreign-key cascade does or does not trigger.
    conn.execute("DELETE FROM recipe_tags WHERE tag_id = ?", (source_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (source_id,))
    if commit:
        conn.commit()
    return TagEdit(
        edit_id=int(receipt.lastrowid or 0),
        source_name=str(source["name"]), target_name=str(target["name"]),
        recipes_affected=len(unlinked),
    )


def delete(
    conn: sqlite3.Connection, tag_id: int, *, edited_by: int | None = None, commit: bool = True
) -> TagEdit:
    """Remove a tag from the vocabulary and from every recipe carrying it."""
    row = _row(conn, tag_id)
    unlinked = _recipe_ids(conn, tag_id)
    receipt = conn.execute(
        "INSERT INTO tag_edits (kind, source_name, target_id, target_name, payload, edited_by) "
        "VALUES ('delete', ?, NULL, NULL, ?, ?)",
        (str(row["name"]), json.dumps({"unlinked": unlinked, "linked": []}), edited_by),
    )
    conn.execute("DELETE FROM recipe_tags WHERE tag_id = ?", (tag_id,))
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    if commit:
        conn.commit()
    return TagEdit(
        edit_id=int(receipt.lastrowid or 0), source_name=str(row["name"]), target_name="",
        recipes_affected=len(unlinked),
    )


def undo(conn: sqlite3.Connection, edit_id: int, *, commit: bool = True) -> str:
    """Reverse a merge or a delete, returning the tag name that came back.

    Single-shot: a replay is refused, so a double tap or a reload cannot strip the target tag
    off recipes a second time.
    """
    row = conn.execute("SELECT * FROM tag_edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None:
        raise UndoUnavailable("that tag change is no longer available to undo")
    if row["undone_at"] is not None:
        raise UndoUnavailable("that tag change has already been undone")

    payload = json.loads(row["payload"])
    unlinked: list[int] = [int(r) for r in payload.get("unlinked") or []]
    linked: list[int] = [int(r) for r in payload.get("linked") or []]
    source_name = str(row["source_name"])

    # The source name may have been taken by a new tag since; reuse that row rather than
    # failing on the UNIQUE, so the recipes still get their tag back.
    existing = conn.execute(
        "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (source_name,)
    ).fetchone()
    if existing is None:
        restored = conn.execute("INSERT INTO tags (name) VALUES (?)", (source_name,))
        source_id = int(restored.lastrowid or 0)
    else:
        source_id = int(existing["id"])

    for recipe_id in unlinked:
        # INSERT ... SELECT rather than VALUES: a recipe deleted since the merge has no row to
        # point at, and a plain insert would fail the foreign key and turn undo into a 500.
        conn.execute(
            "INSERT OR IGNORE INTO recipe_tags (recipe_id, tag_id) "
            "SELECT id, ? FROM recipes WHERE id = ?",
            (source_id, recipe_id),
        )
    if row["kind"] == "merge" and linked:
        # By id, not by name: the target may have been renamed since the merge, and matching
        # on the old name would silently skip this half, leaving both tags on those recipes.
        target_id = row["target_id"]
        if target_id is None:
            found = conn.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (row["target_name"],)
            ).fetchone()
            target_id = found["id"] if found is not None else None
        if target_id is not None:
            marks = ",".join("?" for _ in linked)
            conn.execute(
                "DELETE FROM recipe_tags WHERE tag_id = ? "  # noqa: S608 - marks are ? only
                f"AND recipe_id IN ({marks})",
                [int(target_id), *linked],
            )

    conn.execute("UPDATE tag_edits SET undone_at = ? WHERE id = ?", (now_iso(), edit_id))
    if commit:
        conn.commit()
    return source_name
