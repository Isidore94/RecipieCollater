"""Tag filtering at scale, library paging, and the rename/merge/delete upkeep path.

The theme is the cookbook growing past the size where you can eyeball it: several filters at
once, a page at a time, and a way to correct a tag everywhere rather than recipe by recipe.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.ai.base import TAG_GUIDE
from app.routers.pages import library_url
from app.services import recipes, tag_vocabulary, tags
from tests.conftest import SAME_ORIGIN


def _recipe(
    conn: sqlite3.Connection, title: str, tag_list: list[str], *, status: str = "cookbook"
) -> int:
    rid = recipes.create_recipe(
        conn, recipes.RecipeInput(title=title, base_servings="4", tags=tag_list)
    )
    if status != "inbox":
        recipes.set_status(conn, rid, status)
    return rid


def _tag_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    assert row is not None, f"no tag {name!r}"
    return int(row["id"])


def _tags_of(conn: sqlite3.Connection, recipe_id: int) -> set[str]:
    detail = recipes.get_recipe(conn, recipe_id)
    assert detail is not None
    return set(detail.tags)


# --------------------------------------------------------------------------------------
# Filtering by more than one tag
# --------------------------------------------------------------------------------------


def test_two_tags_mean_both_not_either(migrated_db: sqlite3.Connection) -> None:
    """The whole point: at 300 recipes "chicken" alone is not a short enough list."""
    both = _recipe(migrated_db, "Weeknight Chicken", ["chicken", "weeknight"])
    _recipe(migrated_db, "Sunday Chicken", ["chicken", "project"])
    _recipe(migrated_db, "Weeknight Pasta", ["vegetarian", "weeknight"])

    listed = recipes.list_recipes(migrated_db, status="cookbook", tags=["chicken", "weeknight"])
    assert [r.id for r in listed] == [both]
    assert recipes.count_recipes(migrated_db, status="cookbook", tags=["chicken"]) == 2
    assert (
        recipes.count_recipes(migrated_db, status="cookbook", tags=["chicken", "weeknight"]) == 1
    )


def test_tag_filters_are_case_insensitive_and_deduplicated(
    migrated_db: sqlite3.Connection,
) -> None:
    rid = _recipe(migrated_db, "Chicken", ["chicken"])
    assert [r.id for r in recipes.list_recipes(migrated_db, tags=["CHICKEN"])] == [rid]
    # "chicken" twice must not become two ANDed conditions that still match, nor a no-op.
    assert [r.id for r in recipes.list_recipes(migrated_db, tags=["chicken", "Chicken"])] == [rid]


def test_tag_filters_compose_with_search_and_the_other_filters(
    migrated_db: sqlite3.Connection,
) -> None:
    keep = _recipe(migrated_db, "Chicken Cacciatore", ["chicken", "italian"])
    _recipe(migrated_db, "Chicken Curry", ["chicken", "thai"])
    listed = recipes.list_recipes(
        migrated_db, status="cookbook", query="chicken", tags=["italian"]
    )
    assert [r.id for r in listed] == [keep]


def test_an_unknown_tag_matches_nothing(migrated_db: sqlite3.Connection) -> None:
    _recipe(migrated_db, "Chicken", ["chicken"])
    assert recipes.list_recipes(migrated_db, tags=["nope"]) == []
    assert recipes.count_recipes(migrated_db, tags=["nope"]) == 0


def test_tag_filters_are_bounded(migrated_db: sqlite3.Connection) -> None:
    """A hand-written URL must not turn into an unbounded pile of subqueries."""
    asked = [f"tag{i}" for i in range(50)]
    assert len(recipes.normalize_tag_filters(asked)) == recipes.MAX_TAG_FILTERS
    assert recipes.normalize_tag_filters(["  ", "", " dinner "]) == ["dinner"]


# --------------------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------------------


def test_pages_cover_every_recipe_exactly_once(migrated_db: sqlite3.Connection) -> None:
    made = {_recipe(migrated_db, f"Recipe {i:03d}", ["dinner"]) for i in range(120)}
    size = recipes.PAGE_SIZE
    seen: list[int] = []
    for page in range(3):
        seen += [
            r.id
            for r in recipes.list_recipes(
                migrated_db, status="cookbook", limit=size, offset=page * size
            )
        ]
    assert len(seen) == len(made) == 120
    assert set(seen) == made
    assert len(seen) == len(set(seen)), "a recipe appeared on two pages"
    assert recipes.count_recipes(migrated_db, status="cookbook") == 120


def test_count_and_list_agree_under_the_same_filters(migrated_db: sqlite3.Connection) -> None:
    for i in range(5):
        _recipe(migrated_db, f"Chicken {i}", ["chicken", "weeknight"])
    for i in range(3):
        _recipe(migrated_db, f"Beef {i}", ["beef"])
    for kwargs in (
        {"tags": ["chicken"]},
        {"tags": ["chicken", "weeknight"]},
        {"query": "beef"},
        {"query": "chicken", "tags": ["weeknight"]},
    ):
        listed = recipes.list_recipes(migrated_db, status="cookbook", **kwargs)
        assert len(listed) == recipes.count_recipes(migrated_db, status="cookbook", **kwargs)


def test_callers_that_need_every_recipe_still_get_every_recipe(
    migrated_db: sqlite3.Connection,
) -> None:
    """The meal-plan/shopping pickers and the assistant read the unpaged list; a default page
    size here would silently hide recipes from them."""
    for i in range(60):
        _recipe(migrated_db, f"Recipe {i:03d}", ["dinner"])
    assert len(recipes.list_recipes(migrated_db, status="cookbook")) == 60


# --------------------------------------------------------------------------------------
# URL building (the filters have to survive every link on the page)
# --------------------------------------------------------------------------------------


def test_library_url_repeats_the_tag_parameter_and_keeps_the_rest() -> None:
    url = library_url(
        "/cookbook", query="pie", tags=["chicken", "weeknight"], rating="8", page=3
    )
    assert url.startswith("/cookbook?")
    assert "tag=chicken" in url and "tag=weeknight" in url
    assert "q=pie" in url and "rating=8" in url and "page=3" in url
    assert library_url("/cookbook") == "/cookbook"
    assert "page=" not in library_url("/cookbook", tags=["x"], page=1)


def test_every_paged_tab_actually_accepts_a_page(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    """browse.html renders Older/Newer links for whichever tab it is on. A tab that ignored
    ?page= would show the link and then serve page 1 again - a dead button, and everything
    past the first page unreachable."""
    for i in range(recipes.PAGE_SIZE + 5):
        _recipe(migrated_db, f"Cookbook {i:03d}", ["dinner"])
        _recipe(migrated_db, f"Inbox {i:03d}", [], status="inbox")
        rid = _recipe(migrated_db, f"Archived {i:03d}", [])
        recipes.set_status(migrated_db, rid, "archived")

    # The library lists newest first, so page 1 holds the last ones added and page 2 the first.
    for path, newest, oldest in (
        ("/cookbook", "Cookbook 052", "Cookbook 000"),
        ("/inbox", "Inbox 052", "Inbox 000"),
        ("/archive", "Archived 052", "Archived 000"),
    ):
        page_one = admin_client.get(path)
        page_two = admin_client.get(f"{path}?page=2")
        assert page_one.status_code == page_two.status_code == 200
        assert page_two.text != page_one.text, f"{path} ignored ?page="
        assert newest in page_one.text and oldest not in page_one.text
        assert oldest in page_two.text, f"{path} could not reach page 2"


def test_a_page_beyond_the_end_lands_on_the_last_page(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _recipe(migrated_db, "Only One", ["dinner"])
    for page in ("0", "-4", "9999"):
        resp = admin_client.get(f"/cookbook?page={page}")
        assert resp.status_code == 200
        assert "Only One" in resp.text


def test_library_url_escapes_what_it_carries() -> None:
    url = library_url("/cookbook", query="salt & pepper", tags=["slow-cooked"])
    assert "&amp;" not in url
    assert "q=salt+%26+pepper" in url or "q=salt%20%26%20pepper" in url


# --------------------------------------------------------------------------------------
# Rename
# --------------------------------------------------------------------------------------


def test_rename_fixes_the_tag_on_every_recipe_at_once(migrated_db: sqlite3.Connection) -> None:
    a = _recipe(migrated_db, "Carrot Soup", ["Soup"])
    b = _recipe(migrated_db, "Lentil Soup", ["Soup"])
    edit = tags.rename(migrated_db, _tag_id(migrated_db, "Soup"), "soup")
    assert edit.recipes_affected == 2
    assert _tags_of(migrated_db, a) == {"soup"}
    assert _tags_of(migrated_db, b) == {"soup"}


def test_renaming_only_the_capitalisation_is_not_a_collision(
    migrated_db: sqlite3.Connection,
) -> None:
    """The tag's own row matches itself case-insensitively; that must not block the fix."""
    _recipe(migrated_db, "Carrot Soup", ["Soup"])
    tags.rename(migrated_db, _tag_id(migrated_db, "Soup"), "soup")
    assert [t.name for t in recipes.list_tags(migrated_db, limit=None)] == ["soup"]


def test_rename_onto_an_existing_tag_is_refused_not_silently_merged(
    migrated_db: sqlite3.Connection,
) -> None:
    souvlaki = _recipe(migrated_db, "Souvlaki", ["Entree"])
    chicken = _recipe(migrated_db, "Chicken", ["dinner"])
    with pytest.raises(tags.TagError, match="already exists"):
        tags.rename(migrated_db, _tag_id(migrated_db, "Entree"), "dinner")
    # Refused means nothing moved: both tags still exist, on the recipes they started on.
    assert _tags_of(migrated_db, souvlaki) == {"Entree"}
    assert _tags_of(migrated_db, chicken) == {"dinner"}
    assert {t.name for t in recipes.list_tags(migrated_db, limit=None)} == {"Entree", "dinner"}


def test_rename_reindexes_search(migrated_db: sqlite3.Connection) -> None:
    """Nothing triggers off the tags table, so a rename that skipped the index would leave
    search answering with the old word and not the new one."""
    rid = _recipe(migrated_db, "Souvlaki", ["Entree"])
    assert [r.id for r in recipes.list_recipes(migrated_db, query="entree")] == [rid]

    tags.rename(migrated_db, _tag_id(migrated_db, "Entree"), "maindish")
    assert [r.id for r in recipes.list_recipes(migrated_db, query="maindish")] == [rid]
    assert recipes.list_recipes(migrated_db, query="entree") == []


def test_reindex_produces_exactly_what_the_trigger_produces(
    migrated_db: sqlite3.Connection,
) -> None:
    """reindex_recipes restates migration 006's trigger body in Python. If the two ever
    disagree, a renamed tag's recipe gets a differently-shaped search document than every
    other recipe - so compare them directly."""
    rid = _recipe(migrated_db, "Souvlaki", ["dinner", "grilled"])
    columns = "title, tldr, description, ingredients, tags"
    from_trigger = migrated_db.execute(
        f"SELECT {columns} FROM recipe_fts WHERE rowid = ?", (rid,)  # noqa: S608 - fixed
    ).fetchone()

    recipes.reindex_recipes(migrated_db, [rid])
    from_reindex = migrated_db.execute(
        f"SELECT {columns} FROM recipe_fts WHERE rowid = ?", (rid,)  # noqa: S608 - fixed
    ).fetchone()
    assert tuple(from_reindex) == tuple(from_trigger)
    assert (
        migrated_db.execute(
            "SELECT COUNT(*) c FROM recipe_fts WHERE rowid = ?", (rid,)
        ).fetchone()["c"]
        == 1
    ), "reindexing left a duplicate row behind"


def test_rename_rejects_empty_and_overlong_names(migrated_db: sqlite3.Connection) -> None:
    _recipe(migrated_db, "Soup", ["soup"])
    tag_id = _tag_id(migrated_db, "soup")
    with pytest.raises(tags.TagError):
        tags.rename(migrated_db, tag_id, "   ")
    with pytest.raises(tags.TagError, match="at most"):
        tags.rename(migrated_db, tag_id, "x" * (tags.MAX_TAG_LENGTH + 1))


# --------------------------------------------------------------------------------------
# Merge + undo
# --------------------------------------------------------------------------------------


def test_merge_moves_every_recipe_and_removes_the_old_tag(
    migrated_db: sqlite3.Connection,
) -> None:
    a = _recipe(migrated_db, "Souvlaki", ["Entree"])
    b = _recipe(migrated_db, "Chicken", ["dinner"])
    edit = tags.merge(
        migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner")
    )
    assert edit.recipes_affected == 1
    assert _tags_of(migrated_db, a) == {"dinner"}
    assert _tags_of(migrated_db, b) == {"dinner"}
    assert [t.name for t in recipes.list_tags(migrated_db, limit=None)] == ["dinner"]


def test_merge_does_not_duplicate_a_recipe_that_had_both(
    migrated_db: sqlite3.Connection,
) -> None:
    rid = _recipe(migrated_db, "Both", ["Entree", "dinner"])
    tags.merge(migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner"))
    assert _tags_of(migrated_db, rid) == {"dinner"}
    rows = migrated_db.execute(
        "SELECT COUNT(*) c FROM recipe_tags WHERE recipe_id = ?", (rid,)
    ).fetchone()["c"]
    assert rows == 1


def test_undo_a_merge_restores_exactly_what_moved(migrated_db: sqlite3.Connection) -> None:
    moved = _recipe(migrated_db, "Souvlaki", ["Entree"])
    had_both = _recipe(migrated_db, "Both", ["Entree", "dinner"])
    always = _recipe(migrated_db, "Chicken", ["dinner"])

    edit = tags.merge(
        migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner")
    )
    assert tags.undo(migrated_db, edit.edit_id) == "Entree"

    assert _tags_of(migrated_db, moved) == {"Entree"}, "the moved recipe lost its dinner tag"
    assert _tags_of(migrated_db, had_both) == {"Entree", "dinner"}, "a pre-existing tag was taken"
    assert _tags_of(migrated_db, always) == {"dinner"}, "an untouched recipe was changed"


def test_merge_undo_is_single_shot(migrated_db: sqlite3.Connection) -> None:
    """A double tap must not strip the target tag off those recipes a second time."""
    _recipe(migrated_db, "Souvlaki", ["Entree"])
    keep = _recipe(migrated_db, "Chicken", ["dinner"])
    edit = tags.merge(
        migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner")
    )
    tags.undo(migrated_db, edit.edit_id)
    with pytest.raises(tags.UndoUnavailable, match="already been undone"):
        tags.undo(migrated_db, edit.edit_id)
    assert _tags_of(migrated_db, keep) == {"dinner"}


def test_merge_into_itself_is_refused(migrated_db: sqlite3.Connection) -> None:
    _recipe(migrated_db, "Chicken", ["dinner"])
    tag_id = _tag_id(migrated_db, "dinner")
    with pytest.raises(tags.TagError, match="into itself"):
        tags.merge(migrated_db, tag_id, tag_id)


def test_merge_keeps_search_in_step(migrated_db: sqlite3.Connection) -> None:
    rid = _recipe(migrated_db, "Souvlaki", ["Entree"])
    _recipe(migrated_db, "Chicken", ["dinner"])
    tags.merge(migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner"))
    assert recipes.list_recipes(migrated_db, query="entree") == []
    # The merged recipe now answers to the target tag (alongside the one that always did).
    assert rid in [r.id for r in recipes.list_recipes(migrated_db, query="dinner")]


# --------------------------------------------------------------------------------------
# Delete + undo
# --------------------------------------------------------------------------------------


def test_delete_removes_the_tag_everywhere_and_undo_puts_it_back(
    migrated_db: sqlite3.Connection,
) -> None:
    a = _recipe(migrated_db, "One", ["meal prep", "dinner"])
    b = _recipe(migrated_db, "Two", ["meal prep"])
    edit = tags.delete(migrated_db, _tag_id(migrated_db, "meal prep"))
    assert edit.recipes_affected == 2
    assert _tags_of(migrated_db, a) == {"dinner"}
    assert _tags_of(migrated_db, b) == set()
    assert recipes.list_recipes(migrated_db, query="meal prep") == []

    assert tags.undo(migrated_db, edit.edit_id) == "meal prep"
    assert _tags_of(migrated_db, a) == {"meal prep", "dinner"}
    assert _tags_of(migrated_db, b) == {"meal prep"}


def test_undo_finds_the_target_even_after_it_was_renamed(
    migrated_db: sqlite3.Connection,
) -> None:
    """The receipt records the target's id as well as its name. Matching on the name alone
    would find nothing here, and the undo would leave both tags on the moved recipe."""
    moved = _recipe(migrated_db, "Souvlaki", ["Entree"])
    _recipe(migrated_db, "Chicken", ["dinner"])
    edit = tags.merge(
        migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner")
    )
    tags.rename(migrated_db, _tag_id(migrated_db, "dinner"), "supper")

    tags.undo(migrated_db, edit.edit_id)
    assert _tags_of(migrated_db, moved) == {"Entree"}


def test_undo_survives_a_recipe_deleted_since_the_merge(
    migrated_db: sqlite3.Connection,
) -> None:
    """recipe_tags has a foreign key to recipes; re-inserting a row for a recipe that is gone
    would fail the constraint and turn the undo button into a 500."""
    doomed = _recipe(migrated_db, "Souvlaki", ["Entree"])
    survivor = _recipe(migrated_db, "Gyros", ["Entree"])
    _recipe(migrated_db, "Chicken", ["dinner"])
    edit = tags.merge(
        migrated_db, _tag_id(migrated_db, "Entree"), _tag_id(migrated_db, "dinner")
    )
    recipes.delete_recipe(migrated_db, doomed)

    assert tags.undo(migrated_db, edit.edit_id) == "Entree"
    assert _tags_of(migrated_db, survivor) == {"Entree"}


def test_undo_of_an_unknown_edit_is_refused(migrated_db: sqlite3.Connection) -> None:
    with pytest.raises(tags.UndoUnavailable, match="no longer available"):
        tags.undo(migrated_db, 9999)


def test_undo_reuses_a_tag_name_that_came_back_meanwhile(
    migrated_db: sqlite3.Connection,
) -> None:
    """Someone retypes the deleted tag on a recipe before pressing undo; the UNIQUE index
    would otherwise turn the undo into a 500."""
    a = _recipe(migrated_db, "One", ["soup"])
    edit = tags.delete(migrated_db, _tag_id(migrated_db, "soup"))
    b = _recipe(migrated_db, "Two", ["soup"])
    tags.undo(migrated_db, edit.edit_id)
    assert _tags_of(migrated_db, a) == {"soup"}
    assert _tags_of(migrated_db, b) == {"soup"}


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


def test_every_vocabulary_word_appears_in_the_extraction_prompt() -> None:
    """Two statements of one list; this is what stops them drifting apart."""
    for group, words in tag_vocabulary.VOCABULARY.items():
        assert f"{group}:" in TAG_GUIDE, group
        for word in words:
            assert word in TAG_GUIDE, word
    for cuisine in tag_vocabulary.EXAMPLE_CUISINES:
        assert cuisine in TAG_GUIDE


def test_drift_flags_strays_but_leaves_cuisines_alone() -> None:
    """A banner that flagged every cuisine would list half the cookbook's tags, and a banner
    that is usually wrong gets ignored - so an off-vocabulary word is not by itself drift."""
    flagged = tag_vocabulary.drifted(
        ["dinner", "italian", "Soup", "chicken", "australian", "meal prep"]
    )
    assert flagged == ["Soup"]


def test_drift_flags_two_spellings_of_one_word() -> None:
    flagged = tag_vocabulary.drifted(["soup", "soups", "dinner"])
    assert set(flagged) == {"soup", "soups"}
    assert tag_vocabulary.drifted(["dinner", "chicken"]) == []


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------


def test_tags_screen_lists_tags_and_links_to_the_cookbook(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _recipe(migrated_db, "Chicken", ["chicken", "weeknight"])
    page = admin_client.get("/tags")
    assert page.status_code == 200
    assert "chicken" in page.text and "weeknight" in page.text
    assert "/cookbook?tag=chicken" in page.text


def test_cookbook_accepts_repeated_tag_parameters(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _recipe(migrated_db, "Weeknight Chicken", ["chicken", "weeknight"])
    _recipe(migrated_db, "Sunday Chicken", ["chicken", "project"])
    page = admin_client.get("/cookbook?tag=chicken&tag=weeknight")
    assert page.status_code == 200
    assert "Weeknight Chicken" in page.text
    assert "Sunday Chicken" not in page.text


def test_tag_routes_round_trip_through_the_screen(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    rid = _recipe(migrated_db, "Souvlaki", ["Entree"])
    entree = _tag_id(migrated_db, "Entree")
    _recipe(migrated_db, "Chicken", ["dinner"])
    dinner = _tag_id(migrated_db, "dinner")

    renamed = admin_client.post(
        f"/tags/{entree}/rename", data={"name": "entree"}, headers=SAME_ORIGIN
    )
    assert renamed.status_code == 200  # followed the redirect
    assert _tags_of(migrated_db, rid) == {"entree"}

    merged = admin_client.post(
        f"/tags/{entree}/merge", data={"target_id": str(dinner)}, headers=SAME_ORIGIN
    )
    assert merged.status_code == 200
    assert _tags_of(migrated_db, rid) == {"dinner"}

    edit_id = migrated_db.execute("SELECT MAX(id) m FROM tag_edits").fetchone()["m"]
    undone = admin_client.post(f"/tags/edits/{edit_id}/undo", headers=SAME_ORIGIN)
    assert undone.status_code == 200
    assert _tags_of(migrated_db, rid) == {"entree"}


def test_tag_mutations_require_csrf(
    admin_client: TestClient, migrated_db: sqlite3.Connection
) -> None:
    _recipe(migrated_db, "Chicken", ["dinner"])
    tag_id = _tag_id(migrated_db, "dinner")
    blocked = admin_client.post(
        f"/tags/{tag_id}/delete", headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert blocked.status_code == 403
    assert _tag_id(migrated_db, "dinner") == tag_id
