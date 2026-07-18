"""Pantry over HTTP: page render, inline location/item add, adjustments, stock-take, auth gate.

admin_client and migrated_db share the same temp database (both derive from data_dir), so tests
seed items through the service to get known ids, then drive the routes and assert via the service.
"""

from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from app.services import pantry
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _add(conn: sqlite3.Connection, name: str, loc: int, mode: str = "gauge", **kw: object) -> int:
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(display_name=name, location_id=loc, quantity_mode=mode, **kw),  # type: ignore[arg-type]
    )


def test_pantry_page_and_add_location(admin_client: TestClient) -> None:
    assert admin_client.get("/pantry").status_code == 200
    resp = admin_client.post(
        "/pantry/locations", data={"name": "Downstairs Freezer", "is_freezer": "on"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Downstairs Freezer" in admin_client.get("/pantry").text


def test_add_item_via_form(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    loc = pantry.create_location(migrated_db, "Pantry")
    resp = admin_client.post(
        "/pantry/items",
        data={"display_name": "Flour", "location_id": str(loc), "quantity_mode": "gauge"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    items = pantry.list_items(migrated_db)
    assert [i.display_name for i in items] == ["Flour"]
    assert "Flour" in admin_client.get("/pantry").text


def test_adjust_gauge_and_cycle(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    loc = pantry.create_location(migrated_db, "Pantry")
    item = _add(migrated_db, "Rice", loc)
    admin_client.post(
        f"/pantry/items/{item}/adjust",
        data={"action": "gauge", "gauge": "out"}, headers=SAME_ORIGIN,
    )
    assert pantry.get_item(migrated_db, item).gauge == "out"
    admin_client.post(f"/pantry/items/{item}/adjust", data={"action": "cycle"}, headers=SAME_ORIGIN)
    assert pantry.get_item(migrated_db, item).gauge == "full"  # out -> wraps to full


def test_step_exact_item(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    item = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Cans", location_id=loc, quantity_mode="exact",
            quantity_text="3", unit="each",
        ),
    )
    admin_client.post(
        f"/pantry/items/{item}/adjust", data={"action": "step", "delta": "-1"}, headers=SAME_ORIGIN
    )
    assert pantry.get_item(migrated_db, item).quantity_text == "2"


def test_toggle_binary_item(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    loc = pantry.create_location(migrated_db, "Fridge")
    item = _add(migrated_db, "Ketchup", loc, "binary")
    admin_client.post(
        f"/pantry/items/{item}/adjust", data={"action": "toggle"}, headers=SAME_ORIGIN
    )
    assert pantry.get_item(migrated_db, item).have == 0


def test_remove_item_spoiled(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    loc = pantry.create_location(migrated_db, "Fridge")
    item = _add(migrated_db, "Spinach", loc, "binary")
    admin_client.post(
        f"/pantry/items/{item}/remove", data={"reason": "spoiled"}, headers=SAME_ORIGIN
    )
    assert pantry.get_item(migrated_db, item).have == 0
    reason = migrated_db.execute(
        "SELECT reason FROM pantry_adjustments ORDER BY id DESC LIMIT 1"
    ).fetchone()["reason"]
    assert reason == "spoiled"


def test_stock_take_page(admin_client: TestClient, migrated_db: sqlite3.Connection) -> None:
    loc = pantry.create_location(migrated_db, "Pantry")
    _add(migrated_db, "Oats", loc)
    resp = admin_client.get(f"/pantry/stock-take/{loc}")
    assert resp.status_code == 200
    assert "Oats" in resp.text
    assert "Stock-take" in resp.text


def test_pantry_requires_login(client: TestClient) -> None:
    resp = client.get("/pantry", follow_redirects=False)
    assert resp.status_code in (301, 302, 303, 307, 401)
