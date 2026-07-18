"""Review-first cook deductions: proposal classification, exact math, trust, apply, and undo."""

from __future__ import annotations

import sqlite3

from app.services import cooking, deductions, pantry, recipes
from app.services.units import seed_core_units


def _ing(qty: str, unit: str, food: str) -> recipes.IngredientInput:
    return recipes.IngredientInput(quantity_text=qty, unit=unit, food=food)


def _recipe(conn: sqlite3.Connection, ingredients: list[recipes.IngredientInput]) -> int:
    return recipes.create_recipe(
        conn, recipes.RecipeInput(title="Dish", base_servings="4", ingredients=ingredients)
    )


def _exact_item(conn: sqlite3.Connection, loc: int, food: str, qty: str, unit: str) -> int:
    return pantry.add_item(
        conn,
        pantry.PantryItemInput(
            display_name=food.title(), location_id=loc, quantity_mode="exact",
            food=food, quantity_text=qty, unit=unit,
        ),
    )


def _line(proposal: deductions.DeductionProposal, food: str) -> deductions.ProposedLine:
    return next(line for line in proposal.lines if line.food_name == food)


def test_propose_and_apply_exact(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = _exact_item(migrated_db, loc, "flour", "1000", "grams")

    proposal = deductions.propose(migrated_db, rid)
    line = _line(proposal, "flour")
    assert line.kind == "exact"
    assert line.used_canonical == 200_000  # 200 g at base servings

    deductions.apply(migrated_db, rid, None, line_ids={line.ingredient_id})
    assert pantry.get_item(migrated_db, item).quantity_text == "800"  # 1000 - 200


def test_deduction_scales_with_servings(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = _exact_item(migrated_db, loc, "flour", "1000", "grams")

    line = _line(deductions.propose(migrated_db, rid, servings_made="8"), "flour")
    assert line.used_canonical == 400_000  # 4->8 servings doubles the 200 g
    deductions.apply(migrated_db, rid, None, line_ids={line.ingredient_id}, servings_made="8")
    assert pantry.get_item(migrated_db, item).quantity_text == "600"


def test_skips_fixed_to_taste_and_package(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    _exact_item(migrated_db, loc, "salt", "500", "grams")
    _exact_item(migrated_db, loc, "flour", "1000", "grams")
    rid = _recipe(
        migrated_db,
        [
            recipes.IngredientInput(
                quantity_text="1", unit="tsp", food="salt", scaling_mode="fixed"
            ),
            recipes.IngredientInput(
                quantity_text="300", unit="grams", food="flour", scaling_mode="round_to_package",
                package_quantity_text="1", package_unit="kg",
            ),
        ],
    )
    proposal = deductions.propose(migrated_db, rid)
    assert all(line.kind == "skip" for line in proposal.lines)
    assert "fixed" in _line(proposal, "salt").reason
    assert "package" in _line(proposal, "flour").reason


def test_skip_when_not_in_pantry(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("2", "each", "eggs")])
    line = _line(deductions.propose(migrated_db, rid), "eggs")
    assert line.kind == "skip" and "pantry" in line.reason


def test_dimension_mismatch_skipped(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "ml", "milk")])
    loc = pantry.create_location(migrated_db, "Fridge")
    _exact_item(migrated_db, loc, "milk", "1000", "grams")  # tracked by mass, recipe by volume
    line = _line(deductions.propose(migrated_db, rid), "milk")
    assert line.kind == "skip" and "match" in line.reason


def test_gauge_only_steps_when_opted_in(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("100", "grams", "rice")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="gauge", food="rice"
        ),
    )
    line = _line(deductions.propose(migrated_db, rid), "rice")
    assert line.kind == "gauge"
    assert line.eligible is False  # not opted into step-down -> not auto-eligible
    # applying it anyway (a manual review tap) steps the gauge down one level
    deductions.apply(migrated_db, rid, None, line_ids={line.ingredient_id})
    assert pantry.get_item(migrated_db, item).gauge == "half"


def test_trust_enables_auto_and_edit_revokes(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    _exact_item(migrated_db, loc, "flour", "1000", "grams")

    line = _line(deductions.propose(migrated_db, rid), "flour")
    deductions.apply(migrated_db, rid, None, line_ids={line.ingredient_id}, trust=True, auto=True)

    proposal = deductions.propose(migrated_db, rid)
    assert proposal.deduction_mode == "auto"
    assert _line(proposal, "flour").trusted is True
    assert proposal.auto_ready is True

    # Editing the ingredient quantity changes the signature -> trust revoked.
    ing_id = _line(proposal, "flour").ingredient_id
    migrated_db.execute(
        "UPDATE recipe_ingredients SET quantity_text = '250' WHERE id = ?", (ing_id,)
    )
    migrated_db.commit()
    assert _line(deductions.propose(migrated_db, rid), "flour").trusted is False


def test_pending_food_not_auto_eligible(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    _exact_item(migrated_db, loc, "flour", "1000", "grams")
    migrated_db.execute("UPDATE foods SET status = 'pending' WHERE name = 'flour'")
    migrated_db.commit()

    line = _line(deductions.propose(migrated_db, rid), "flour")
    assert line.kind == "exact"  # still deductible in a manual review
    assert line.food_confirmed is False
    assert line.eligible is False  # but never auto-applied (Sol review #6)


def test_apply_then_undo_restores(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = _exact_item(migrated_db, loc, "flour", "1000", "grams")

    line = _line(deductions.propose(migrated_db, rid), "flour")
    result = deductions.apply(migrated_db, rid, None, line_ids={line.ingredient_id})
    assert pantry.get_item(migrated_db, item).quantity_text == "800"

    reversed_count = deductions.undo(migrated_db, result.batch_id)
    assert reversed_count == 1
    assert pantry.get_item(migrated_db, item).quantity_text == "1000"  # restored


def test_apply_ignores_lines_not_selected(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    rid = _recipe(
        migrated_db,
        [
            _ing("200", "grams", "flour"),
            _ing("100", "grams", "sugar"),
        ],
    )
    loc = pantry.create_location(migrated_db, "Pantry")
    flour = _exact_item(migrated_db, loc, "flour", "1000", "grams")
    sugar = _exact_item(migrated_db, loc, "sugar", "500", "grams")

    flour_line = _line(deductions.propose(migrated_db, rid), "flour")
    deductions.apply(migrated_db, rid, None, line_ids={flour_line.ingredient_id})  # only flour
    assert pantry.get_item(migrated_db, flour).quantity_text == "800"
    assert pantry.get_item(migrated_db, sugar).quantity_text == "500"  # untouched


# --- Regression tests for the Phase 4 adversarial review findings ---


def test_undo_is_idempotent(migrated_db: sqlite3.Connection) -> None:
    # Finding #1: replaying an Undo must NOT keep re-adding the deducted amount.
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = _exact_item(migrated_db, loc, "flour", "1000", "grams")
    line = _line(deductions.propose(migrated_db, rid), "flour")
    result = deductions.apply(migrated_db, rid, None, line_ids={line.ingredient_id})

    assert deductions.undo(migrated_db, result.batch_id) == 1
    assert pantry.get_item(migrated_db, item).quantity_text == "1000"
    assert deductions.undo(migrated_db, result.batch_id) == 0  # replay is a no-op
    assert pantry.get_item(migrated_db, item).quantity_text == "1000"  # not over-restored


def test_apply_is_idempotent_per_cook(migrated_db: sqlite3.Connection) -> None:
    # Finding #2: the same cook must not be deducted twice.
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("200", "grams", "flour")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = _exact_item(migrated_db, loc, "flour", "1000", "grams")
    cook_id = cooking.record_cook(migrated_db, rid, cooking.CookCaptureInput(servings_made="4"))
    line = _line(deductions.propose(migrated_db, rid, cook_log_id=cook_id), "flour")

    first = deductions.apply(migrated_db, rid, cook_id, line_ids={line.ingredient_id})
    second = deductions.apply(migrated_db, rid, cook_id, line_ids={line.ingredient_id})
    assert first.batch_id == second.batch_id  # same batch returned, not a new deduction
    assert pantry.get_item(migrated_db, item).quantity_text == "800"  # deducted once only


def test_gauge_item_stepped_once_per_cook(migrated_db: sqlite3.Connection) -> None:
    # Finding #5: a gauge item used by two recipe lines steps down once, not twice.
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("100", "grams", "rice"), _ing("50", "grams", "rice")])
    loc = pantry.create_location(migrated_db, "Pantry")
    item = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="gauge", food="rice"
        ),
    )
    ids = {ln.ingredient_id for ln in deductions.propose(migrated_db, rid).deductible_lines}
    deductions.apply(migrated_db, rid, None, line_ids=ids)
    assert pantry.get_item(migrated_db, item).gauge == "half"  # full -> half once, not full -> low


def test_cross_unit_deduction_display(migrated_db: sqlite3.Connection) -> None:
    # Finding #3: the shown amount uses the PANTRY item's unit, not the recipe's.
    seed_core_units(migrated_db)
    rid = _recipe(migrated_db, [_ing("2", "tbsp", "oil")])
    loc = pantry.create_location(migrated_db, "Pantry")
    _exact_item(migrated_db, loc, "oil", "500", "ml")  # pantry tracks oil in ml
    line = _line(deductions.propose(migrated_db, rid), "oil")
    assert "ml" in line.used_text and "tbsp" not in line.used_text
