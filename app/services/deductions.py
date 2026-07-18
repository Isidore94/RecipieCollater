"""Review-first cook deductions (Phase 4c): turn a cook into pantry decrements the family trusts.

The goal is not permanent confirmation fatigue - it is to earn trust once per recipe (docs/06 §2.1).
The first cook proposes deductions and the user reviews them; confirmed food/item mappings are
remembered and a recipe can be switched to auto-apply. A recipe edit that changes an ingredient's
food/unit/quantity/scaling/mapping revokes that line's trust (the signature stops matching) until it
is reviewed again.

What deducts, per ingredient:
- exact pantry items are decremented by the scaled amount, in canonical micro-units (never float);
- gauge (full/half/low/out) and binary (have/out) items are NOT silently arithmetic'd - they only
  step down when the item opted in (step_down_on_cook), otherwise they are offered for one tap;
- fixed, to_taste, round_to_package (a shopping concern), approximate-unit, unmatched, and
  deduction-disabled lines are skipped with a reason.

Auto-apply requires a CONFIRMED food (Sol review #6), a matched item, a trusted unchanged signature,
and - for exact - a compatible dimension. Applying writes one batch (batch_id) of reason='cook'
adjustments; Undo writes the compensating batch, never deleting history.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from app.security import now_iso
from app.services import pantry, quantity, recipes, units

_GAUGE_ORDER: tuple[str, ...] = ("full", "half", "low", "out")


class DeductionError(ValueError):
    """Invalid deduction input (unknown recipe/cook)."""


@dataclass(frozen=True, slots=True)
class ProposedLine:
    ingredient_id: int
    label: str
    food_id: int | None
    food_name: str | None
    food_confirmed: bool
    pantry_item_id: int | None
    pantry_item_name: str | None
    kind: str  # 'exact' | 'gauge' | 'binary' | 'skip'
    used_text: str | None  # human amount to deduct (exact) or the step target (gauge/binary)
    used_canonical: int | None
    reason: str | None  # skip reason / note
    trusted: bool
    eligible: bool  # safe to apply without review (confirmed + trusted + matched)

    @property
    def deductible(self) -> bool:
        return self.kind != "skip"


@dataclass(frozen=True, slots=True)
class DeductionProposal:
    recipe_id: int
    cook_log_id: int | None
    deduction_mode: str
    servings: str
    lines: list[ProposedLine] = field(default_factory=list)

    @property
    def deductible_lines(self) -> list[ProposedLine]:
        return [line for line in self.lines if line.deductible]

    @property
    def auto_ready(self) -> bool:
        """Auto-apply only when the recipe is trusted and every deductible line stays eligible."""
        lines = self.deductible_lines
        return (
            self.deduction_mode == "auto"
            and bool(lines)
            and all(line.eligible for line in lines)
        )


def trust_signature(
    food_id: int | None,
    unit_id: int | None,
    quantity_text: str | None,
    scaling_mode: str,
    pantry_item_hint: int | None,
) -> str:
    """Hash of the fields that make a deduction correct; any edit changes it, voiding trust."""
    payload = "|".join(
        str(part) for part in (food_id, unit_id, quantity_text, scaling_mode, pantry_item_hint)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _step_down(gauge: str | None) -> str:
    current = gauge if gauge in _GAUGE_ORDER else "full"
    return _GAUGE_ORDER[min(_GAUGE_ORDER.index(current) + 1, len(_GAUGE_ORDER) - 1)]


def _match_item(
    conn: sqlite3.Connection, food_id: int | None, hint: int | None
) -> tuple[pantry.PantryItem | None, str | None]:
    """Find the pantry item for an ingredient: a remembered hint wins, else the food's sole item."""
    if hint is not None:
        item = pantry.get_item(conn, hint)
        if item is not None:
            return item, None
    candidates = pantry.items_for_food(conn, food_id)
    if not candidates:
        return None, "not in the pantry"
    if len(candidates) > 1:
        return None, "several pantry items share this food - pick one to remember"
    return candidates[0], None


def propose(
    conn: sqlite3.Connection,
    recipe_id: int,
    *,
    servings_made: str | None = None,
    cook_log_id: int | None = None,
) -> DeductionProposal:
    """Build the per-ingredient deduction proposal for a cook at the given servings."""
    recipe = conn.execute(
        "SELECT base_servings, deduction_mode FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if recipe is None:
        raise DeductionError("recipe not found")
    servings = servings_made or recipe["base_servings"]
    factor = recipes.scale_factor(recipe["base_servings"], servings)

    rows = conn.execute(
        """SELECT ri.id, ri.original_text, ri.quantity_text, ri.unit_id, ri.food_id,
                  ri.scaling_mode, ri.deduct_from_pantry, ri.pantry_item_hint,
                  ri.deduction_trusted_at, ri.deduction_trust_signature,
                  u.dimension AS unit_dimension, u.to_canonical_microunits AS unit_factor,
                  fo.status AS food_status, fo.name AS food_name
           FROM recipe_ingredients ri
           LEFT JOIN units u ON u.id = ri.unit_id
           LEFT JOIN foods fo ON fo.id = ri.food_id
           WHERE ri.recipe_id = ? ORDER BY ri.sort_order""",
        (recipe_id,),
    ).fetchall()

    lines = [_propose_line(conn, r, factor) for r in rows]
    return DeductionProposal(
        recipe_id=recipe_id, cook_log_id=cook_log_id, deduction_mode=recipe["deduction_mode"],
        servings=servings, lines=lines,
    )


def _skip(row: sqlite3.Row, reason: str) -> ProposedLine:
    return ProposedLine(
        ingredient_id=int(row["id"]), label=row["original_text"], food_id=row["food_id"],
        food_name=row["food_name"], food_confirmed=(row["food_status"] == "confirmed"),
        pantry_item_id=None, pantry_item_name=None, kind="skip", used_text=None,
        used_canonical=None, reason=reason, trusted=False, eligible=False,
    )


def _propose_line(conn: sqlite3.Connection, row: sqlite3.Row, factor: Decimal) -> ProposedLine:
    if not row["deduct_from_pantry"]:
        return _skip(row, "deduction turned off for this ingredient")
    if row["scaling_mode"] in ("fixed", "to_taste"):
        return _skip(row, "amount is fixed / to taste")
    if row["scaling_mode"] == "round_to_package":
        return _skip(row, "package rounding is for shopping, not use")
    if row["quantity_text"] is None or row["unit_id"] is None:
        return _skip(row, "no measured amount")
    if row["unit_factor"] is None:
        return _skip(row, "approximate unit")
    if row["food_id"] is None:
        return _skip(row, "no food to match to the pantry")

    item, why = _match_item(conn, row["food_id"], row["pantry_item_hint"])
    if item is None:
        return _skip(row, why or "not in the pantry")

    confirmed = row["food_status"] == "confirmed"
    signature = trust_signature(
        row["food_id"], row["unit_id"], row["quantity_text"], row["scaling_mode"],
        row["pantry_item_hint"],
    )
    trusted = bool(row["deduction_trusted_at"]) and row["deduction_trust_signature"] == signature

    if item.quantity_mode == "exact":
        item_unit = units.get_unit(conn, item.unit_id) if item.unit_id else None
        if item_unit is None or item_unit.dimension != row["unit_dimension"]:
            return _skip(row, "recipe and pantry units don't match - needs a bridge")
        scaled = quantity.parse_quantity(row["quantity_text"]) * factor
        used_canonical = quantity.to_canonical(scaled, int(row["unit_factor"]))
        eligible = confirmed and trusted
        unit_label = item_unit.abbreviation or item_unit.name
        return ProposedLine(
            ingredient_id=int(row["id"]), label=row["original_text"], food_id=row["food_id"],
            food_name=row["food_name"], food_confirmed=confirmed, pantry_item_id=item.id,
            pantry_item_name=item.display_name, kind="exact",
            used_text=f"-{quantity.format_quantity(scaled)} {unit_label}",
            used_canonical=used_canonical, reason=None, trusted=trusted, eligible=eligible,
        )

    # gauge / binary: never silently arithmetic'd; only auto-step when the item opted in.
    kind = item.quantity_mode
    step_target = _step_down(item.gauge) if kind == "gauge" else "out"
    eligible = confirmed and trusted and item.step_down_on_cook
    return ProposedLine(
        ingredient_id=int(row["id"]), label=row["original_text"], food_id=row["food_id"],
        food_name=row["food_name"], food_confirmed=confirmed, pantry_item_id=item.id,
        pantry_item_name=item.display_name, kind=kind, used_text=step_target,
        used_canonical=None, reason=None if item.step_down_on_cook else "used some?",
        trusted=trusted, eligible=eligible,
    )


@dataclass(frozen=True, slots=True)
class ApplyResult:
    batch_id: str
    applied: list[str]  # human summary lines, e.g. "Rice -200 g"


def apply(
    conn: sqlite3.Connection,
    recipe_id: int,
    cook_log_id: int | None,
    *,
    line_ids: set[int],
    servings_made: str | None = None,
    trust: bool = False,
    auto: bool = False,
    user_id: int | None = None,
) -> ApplyResult:
    """Apply the chosen deductible lines in one batch. Re-proposes server-side, so a stale/hostile
    client can't deduct a line the recipe no longer supports. Optionally remembers trust + auto."""
    proposal = propose(conn, recipe_id, servings_made=servings_made, cook_log_id=cook_log_id)
    batch_id = pantry.new_batch_id()
    stamp = now_iso()
    applied: list[str] = []

    for line in proposal.deductible_lines:
        if line.ingredient_id not in line_ids or line.pantry_item_id is None:
            continue
        if line.kind == "exact" and line.used_canonical is not None:
            pantry.deduct_canonical(
                conn, line.pantry_item_id, line.used_canonical, cook_log_id=cook_log_id,
                batch_id=batch_id, user_id=user_id, commit=False,
            )
        elif line.kind == "gauge":
            pantry.set_gauge(
                conn, line.pantry_item_id, _step_down(_current_gauge(conn, line.pantry_item_id)),
                reason="cook", cook_log_id=cook_log_id, batch_id=batch_id, user_id=user_id,
                commit=False,
            )
        elif line.kind == "binary":
            pantry.set_have(
                conn, line.pantry_item_id, False, reason="cook", cook_log_id=cook_log_id,
                batch_id=batch_id, user_id=user_id, commit=False,
            )
        else:
            continue
        applied.append(f"{line.pantry_item_name} {line.used_text}")
        if trust:
            _mark_trusted(conn, line.ingredient_id, stamp)

    if auto:
        conn.execute("UPDATE recipes SET deduction_mode = 'auto' WHERE id = ?", (recipe_id,))
    conn.commit()
    return ApplyResult(batch_id=batch_id, applied=applied)


def _current_gauge(conn: sqlite3.Connection, item_id: int) -> str | None:
    item = pantry.get_item(conn, item_id)
    return item.gauge if item else None


def _mark_trusted(conn: sqlite3.Connection, ingredient_id: int, stamp: str) -> None:
    row = conn.execute(
        "SELECT food_id, unit_id, quantity_text, scaling_mode, pantry_item_hint "
        "FROM recipe_ingredients WHERE id = ?",
        (ingredient_id,),
    ).fetchone()
    if row is None:
        return
    signature = trust_signature(
        row["food_id"], row["unit_id"], row["quantity_text"], row["scaling_mode"],
        row["pantry_item_hint"],
    )
    conn.execute(
        "UPDATE recipe_ingredients SET deduction_trusted_at = ?, deduction_trust_signature = ? "
        "WHERE id = ?",
        (stamp, signature, ingredient_id),
    )


def set_mapping(
    conn: sqlite3.Connection, ingredient_id: int, pantry_item_id: int | None, *, commit: bool = True
) -> None:
    """Remember which pantry item an ambiguous ingredient maps to (and revoke its stale trust)."""
    conn.execute(
        "UPDATE recipe_ingredients SET pantry_item_hint = ?, deduction_trusted_at = NULL, "
        "deduction_trust_signature = NULL WHERE id = ?",
        (pantry_item_id, ingredient_id),
    )
    if commit:
        conn.commit()


def undo(conn: sqlite3.Connection, batch_id: str, *, user_id: int | None = None) -> int:
    """Reverse a cook's deduction batch with compensating adjustments. Returns lines reversed."""
    rows = conn.execute(
        "SELECT * FROM pantry_adjustments WHERE batch_id = ? AND reason = 'cook' ORDER BY id",
        (batch_id,),
    ).fetchall()
    reversed_count = 0
    for adj in rows:
        item_id = adj["pantry_item_id"]
        if item_id is None:  # the item was deleted since; nothing to restore
            continue
        item = pantry.get_item(conn, item_id)
        if item is None:
            continue
        if item.quantity_mode == "exact" and adj["canonical_delta"] is not None:
            # adj['canonical_delta'] was negative (a deduction); re-adding it restores the amount.
            pantry.deduct_canonical(
                conn, item_id, adj["canonical_delta"], reason="correction", user_id=user_id,
                commit=False,
            )
        elif item.quantity_mode == "gauge" and adj["from_gauge"] is not None:
            pantry.set_gauge(
                conn, item_id, adj["from_gauge"], reason="correction", user_id=user_id, commit=False
            )
        elif item.quantity_mode == "binary" and adj["from_have"] is not None:
            pantry.set_have(
                conn, item_id, bool(adj["from_have"]), reason="correction", user_id=user_id,
                commit=False,
            )
        else:
            continue
        reversed_count += 1
    conn.commit()
    return reversed_count


def batch_summary(conn: sqlite3.Connection, batch_id: str) -> list[str]:
    """Human summary of an applied batch (for the post-cook 'Deducted: ...' line + Undo)."""
    rows = conn.execute(
        """SELECT pa.delta_quantity_text, pa.to_gauge, pa.to_have,
                  COALESCE(pi.display_name, fo.name, 'item') AS name
           FROM pantry_adjustments pa
           LEFT JOIN pantry_items pi ON pi.id = pa.pantry_item_id
           LEFT JOIN foods fo ON fo.id = pa.food_id
           WHERE pa.batch_id = ? AND pa.reason = 'cook' ORDER BY pa.id""",
        (batch_id,),
    ).fetchall()
    summary: list[str] = []
    for r in rows:
        if r["delta_quantity_text"]:
            summary.append(f"{r['name']} {r['delta_quantity_text']}")
        elif r["to_gauge"]:
            summary.append(f"{r['name']} → {r['to_gauge']}")
        elif r["to_have"] is not None:
            summary.append(f"{r['name']} → {'have' if r['to_have'] else 'out'}")
    return summary


def batch_for_cook(conn: sqlite3.Connection, cook_log_id: int) -> str | None:
    """The deduction batch id written for a cook (for the Undo control), or None."""
    row = conn.execute(
        "SELECT batch_id FROM pantry_adjustments WHERE cook_log_id = ? AND reason = 'cook' "
        "AND batch_id IS NOT NULL LIMIT 1",
        (cook_log_id,),
    ).fetchone()
    return row["batch_id"] if row else None
