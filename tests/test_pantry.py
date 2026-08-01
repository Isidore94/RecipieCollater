"""Pantry service: locations, the three quantity modes, adjustment history, staples/restock."""

from __future__ import annotations

import sqlite3

import pytest

from app.services import pantry
from app.services.units import seed_core_units


def _item(conn: sqlite3.Connection, item_id: int) -> pantry.PantryItem:
    """get_item, narrowed: these tests always address an item that must exist."""
    item = pantry.get_item(conn, item_id)
    assert item is not None
    return item



def _loc(conn: sqlite3.Connection, name: str = "Pantry", *, freezer: bool = False) -> int:
    return pantry.create_location(conn, name, is_freezer=freezer)


def _add(conn: sqlite3.Connection, name: str, loc: int, mode: str = "gauge", **kw: object) -> int:
    return pantry.add_item(
        conn, pantry.PantryItemInput(display_name=name, location_id=loc, quantity_mode=mode, **kw)  # type: ignore[arg-type]
    )


def _adjustments(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pantry_adjustments WHERE pantry_item_id = ? ORDER BY id", (item_id,)
    ).fetchall()


def test_create_location_is_idempotent(migrated_db: sqlite3.Connection) -> None:
    a = pantry.create_location(migrated_db, "Downstairs Freezer", is_freezer=True)
    b = pantry.create_location(migrated_db, "Downstairs Freezer")  # same name -> same row
    assert a == b
    locs = pantry.list_locations(migrated_db)
    assert len(locs) == 1
    assert locs[0].is_freezer is True


def test_add_exact_item_computes_canonical(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = _loc(migrated_db)
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Chopped tomatoes", location_id=loc, quantity_mode="exact",
            food="canned tomatoes", quantity_text="3", unit="each",
        ),
    )
    item = _item(migrated_db, item_id)
    assert item is not None
    assert item.quantity_mode == "exact"
    assert item.canonical_quantity == 3000  # 3 each -> 3000 milli-each
    assert item.food_id is not None  # resolved/created a food for pantry<->recipe matching
    # opening state recorded as a correction adjustment
    adj = _adjustments(migrated_db, item_id)
    assert len(adj) == 1 and adj[0]["reason"] == "correction"


def test_gauge_defaults_full_and_cycles(migrated_db: sqlite3.Connection) -> None:
    loc = _loc(migrated_db)
    item_id = _add(migrated_db, "Flour", loc)
    assert _item(migrated_db, item_id).gauge == "full"
    assert pantry.cycle_gauge(migrated_db, item_id) == "half"
    assert pantry.cycle_gauge(migrated_db, item_id) == "low"
    assert pantry.cycle_gauge(migrated_db, item_id) == "out"
    assert pantry.cycle_gauge(migrated_db, item_id) == "full"  # wraps
    # each cycle wrote history
    assert len(_adjustments(migrated_db, item_id)) == 5  # 1 opening + 4 cycles


def test_binary_toggle(migrated_db: sqlite3.Connection) -> None:
    loc = _loc(migrated_db)
    item_id = _add(migrated_db, "Ketchup", loc, "binary")
    assert _item(migrated_db, item_id).have == 1  # defaults to have
    assert pantry.toggle_have(migrated_db, item_id) is False
    assert _item(migrated_db, item_id).have == 0


def test_set_and_step_exact_track_delta(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = _loc(migrated_db)
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="exact",
            quantity_text="1000", unit="grams",
        ),
    )
    pantry.set_exact(migrated_db, item_id, "800")
    item = _item(migrated_db, item_id)
    assert item.quantity_text == "800"
    assert item.canonical_quantity == 800_000  # 800 g -> mg
    last = _adjustments(migrated_db, item_id)[-1]
    assert last["canonical_delta"] == -200_000  # dropped 200 g

    pantry.step_exact(migrated_db, item_id, "-1000")  # clamp at zero, not negative
    assert _item(migrated_db, item_id).quantity_text == "0"


def test_step_rejects_bad_amount(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = _loc(migrated_db)
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="exact",
            quantity_text="5", unit="each",
        ),
    )
    with pytest.raises(pantry.PantryError):
        pantry.step_exact(migrated_db, item_id, "lots")


def test_mode_mismatch_is_rejected(migrated_db: sqlite3.Connection) -> None:
    loc = _loc(migrated_db)
    gauge_id = _add(migrated_db, "Oil", loc)
    with pytest.raises(pantry.PantryError):
        pantry.set_exact(migrated_db, gauge_id, "3")  # gauge item can't take an exact set


def test_remove_spoiled_empties_and_logs(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = _loc(migrated_db)
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Spinach", location_id=loc, quantity_mode="exact",
            quantity_text="2", unit="each",
        ),
    )
    pantry.remove_item(migrated_db, item_id, reason="spoiled")
    item = _item(migrated_db, item_id)
    assert item is not None and item.quantity_text == "0"  # emptied, row kept
    assert _adjustments(migrated_db, item_id)[-1]["reason"] == "spoiled"


def test_remove_delete_keeps_history(migrated_db: sqlite3.Connection) -> None:
    loc = _loc(migrated_db)
    item_id = _add(migrated_db, "Peas", loc, "binary")
    pantry.remove_item(migrated_db, item_id, reason="manual_remove", delete=True)
    assert pantry.get_item(migrated_db, item_id) is None  # row gone
    # adjustment survives with food_id, pantry_item_id nulled by the cascade
    rows = migrated_db.execute(
        "SELECT reason, pantry_item_id FROM pantry_adjustments WHERE reason = 'manual_remove'"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["pantry_item_id"] is None


def test_staple_thresholds_drive_shopping_candidates(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = _loc(migrated_db)
    # exact staple with a min threshold, currently above it
    rice = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="exact",
            quantity_text="1000", unit="grams",
        ),
    )
    pantry.set_staple(migrated_db, rice, is_staple=True, min_quantity_text="500")
    # gauge staple gone to out
    oil = _add(migrated_db, "Oil", loc)
    pantry.set_staple(migrated_db, oil, is_staple=True)
    pantry.set_gauge(migrated_db, oil, "out")

    assert _item(migrated_db, rice).needs_restock is False  # 1000 >= 500
    pantry.set_exact(migrated_db, rice, "400")  # now below threshold
    assert _item(migrated_db, rice).needs_restock is True

    names = {i.display_name for i in pantry.shopping_candidates(migrated_db)}
    assert names == {"Rice", "Oil"}


def test_list_items_by_location(migrated_db: sqlite3.Connection) -> None:
    pantry_loc = _loc(migrated_db, "Pantry")
    freezer = _loc(migrated_db, "Freezer", freezer=True)
    _add(migrated_db, "Flour", pantry_loc)
    _add(migrated_db, "Peas", freezer, "binary")
    assert [i.display_name for i in pantry.list_items(migrated_db, location_id=freezer)] == ["Peas"]
    assert len(pantry.list_items(migrated_db)) == 2


# --------------------------------------------------------------------------------------
# Undo of a single adjustment (the cook batch has its own undo in test_deductions)
# --------------------------------------------------------------------------------------


def _gauge_item(conn: sqlite3.Connection, name: str = "Olive oil") -> int:
    loc = pantry.create_location(conn, "Cupboard")
    return pantry.add_item(
        conn, pantry.PantryItemInput(display_name=name, location_id=loc, quantity_mode="gauge")
    )


def test_undo_restores_the_previous_gauge(migrated_db: sqlite3.Connection) -> None:
    item_id = _gauge_item(migrated_db)
    adjustment = pantry.set_gauge(migrated_db, item_id, "low")
    assert _item(migrated_db, item_id).gauge == "low"

    name = pantry.undo_adjustment(migrated_db, adjustment)
    assert name == "Olive oil"
    assert _item(migrated_db, item_id).gauge == "full"


def test_undo_is_single_shot(migrated_db: sqlite3.Connection) -> None:
    """A double tap or a reload must not reverse the same change twice and over-restore."""
    item_id = _gauge_item(migrated_db)
    adjustment = pantry.set_gauge(migrated_db, item_id, "out")
    pantry.undo_adjustment(migrated_db, adjustment)

    with pytest.raises(pantry.UndoUnavailable, match="already been undone"):
        pantry.undo_adjustment(migrated_db, adjustment)
    assert _item(migrated_db, item_id).gauge == "full"


def test_undo_restores_an_exact_amount(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Cupboard")
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Tinned tomatoes", location_id=loc, quantity_mode="exact",
            quantity_text="6", unit="each",
        ),
    )
    adjustment = pantry.step_exact(migrated_db, item_id, "-2")
    assert _item(migrated_db, item_id).quantity_text == "4"

    pantry.undo_adjustment(migrated_db, adjustment)
    assert _item(migrated_db, item_id).quantity_text == "6"


def test_undo_brings_back_a_deleted_item_with_its_settings(
    migrated_db: sqlite3.Connection,
) -> None:
    """A hard delete drops the row, so the transition columns cannot describe the way back."""
    loc = pantry.create_location(migrated_db, "Freezer", is_freezer=True)
    item_id = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Puff pastry", location_id=loc, quantity_mode="gauge",
            is_staple=True, expires_on="2026-12-01",
        ),
    )
    adjustment = pantry.remove_item(migrated_db, item_id, delete=True)
    assert pantry.get_item(migrated_db, item_id) is None

    pantry.undo_adjustment(migrated_db, adjustment)
    restored = [i for i in pantry.list_items(migrated_db) if i.display_name == "Puff pastry"]
    assert len(restored) == 1
    item = restored[0]
    assert item.location_id == loc
    assert item.quantity_mode == "gauge"
    assert item.is_staple is True
    assert item.expires_on == "2026-12-01"


def test_undo_of_an_unknown_adjustment_is_refused(migrated_db: sqlite3.Connection) -> None:
    with pytest.raises(pantry.UndoUnavailable, match="no longer available"):
        pantry.undo_adjustment(migrated_db, 9999)
