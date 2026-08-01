"""Receipt/order capture (Phase 4.7): a grocery trip becomes pantry restocks in one review.

Flow: a receipt photo (or pasted Instacart/Costco order text) goes to the AI provider once, and
the parse is PERSISTED - it cost money and is not reproducible, so review round-trips never
re-call the model. The review screen shows every line with its proposed generic food name and
pantry action; apply is the only writer (AI proposes, deterministic services mutate -
CONVENTIONS 10).

Generalization is learned, not re-guessed: the model maps "KS ORG BLK BNS" to a short generic
kitchen name (anchored to the household's existing foods list, passed in the prompt), and every
APPLIED line writes its original receipt text into food_aliases. The next receipt from the same
store resolves those lines deterministically before the model gets a vote. Names generalize to
the level recipes speak at ("black beans", not "beans") - families stay in parent_food_id.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal

from app import ai
from app.ai import usage as ai_usage
from app.config import get_settings
from app.security import now_iso
from app.services import pantry, quantity, units

_OPERATION = "receipt"
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_EDGE = 1800  # long-edge cap keeps vision tokens (and upload of huge photos) bounded
_MAX_HOUSEHOLD_FOODS = 300


class ReceiptError(ValueError):
    """Invalid receipt input (no content, unknown receipt, or a bad image)."""


# --------------------------------------------------------------------------------------
# Capture: one provider call, persisted
# --------------------------------------------------------------------------------------


def _household_foods(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT name FROM foods ORDER BY name COLLATE NOCASE LIMIT ?", (_MAX_HOUSEHOLD_FOODS,)
    ).fetchall()
    return ", ".join(str(r["name"]) for r in rows)


def _normalize_alias(text: str) -> str | None:
    """A receipt line as a learnable alias: collapsed whitespace, trailing prices stripped."""
    clean = " ".join(text.split())
    clean = re.sub(r"[\s$]*\d+[.,]\d{2}\s*$", "", clean).strip()
    return clean if len(clean) >= 3 else None


def _match_food(conn: sqlite3.Connection, original_text: str, food_name: str | None) -> int | None:
    """Deterministic match: learned receipt alias first, then the generic name via alias/name."""
    alias = _normalize_alias(original_text)
    if alias:
        row = conn.execute("SELECT food_id FROM food_aliases WHERE alias = ?", (alias,)).fetchone()
        if row is not None:
            return int(row["food_id"])
    key = (food_name or "").strip()
    if not key:
        return None
    row = conn.execute("SELECT food_id FROM food_aliases WHERE alias = ?", (key,)).fetchone()
    if row is not None:
        return int(row["food_id"])
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (key,)).fetchone()
    return int(row["id"]) if row else None


def _prepare_image(raw: bytes) -> bytes:
    """Re-encode any uploaded photo as bounded JPEG (vision providers want jpeg/png/webp;
    a full-resolution phone photo wastes tokens). Pillow is imported lazily (CONVENTIONS 4)."""
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ReceiptError("that photo is too large - try a smaller one")
    import io

    from PIL import Image, ImageOps

    try:
        opened = Image.open(io.BytesIO(raw))
        upright = ImageOps.exif_transpose(opened)  # respect the phone's orientation flag
        rgb = upright.convert("RGB")
    except Exception as exc:
        raise ReceiptError("couldn't read that photo - JPEG/PNG/WEBP work best") from exc
    rgb.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE))
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=85)
    return out.getvalue()


@dataclass(frozen=True, slots=True)
class CaptureResult:
    receipt_id: int | None
    item_count: int = 0
    error: str | None = None


def capture(
    conn: sqlite3.Connection,
    *,
    text: str | None = None,
    image: bytes | None = None,
    user_id: int | None = None,
) -> CaptureResult:
    """Parse a receipt photo or pasted order once and persist the lines for review."""
    pasted = (text or "").strip()
    if not pasted and image is None:
        return CaptureResult(None, error="Snap the receipt or paste the order text first.")

    settings = get_settings()
    provider = ai.get_provider(settings)
    if provider is None:
        return CaptureResult(None, error="Receipt reading needs an AI key configured.")
    if not ai_usage.within_budget(conn, settings):
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, status="blocked", error="daily or monthly AI spend cap reached",
        )
        return CaptureResult(None, error="Today's AI spend limit has been reached.")

    household = _household_foods(conn)
    prompt = "HOUSEHOLD FOODS: " + (household or "(none yet)")
    image_jpeg: bytes | None = None
    if image is not None:
        image_jpeg = _prepare_image(image)
        prompt += "\n\nThe receipt is the attached photo."
    else:
        prompt += f"\n\nORDER TEXT:\n{pasted}"

    try:
        result = provider.receipt(prompt, image_jpeg=image_jpeg)
    except ai.AIError as exc:
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, input_tokens=exc.input_tokens, output_tokens=exc.output_tokens,
            cost_micros=exc.cost_micros, status="error", error=str(exc)[:500],
        )
        return CaptureResult(None, error="Couldn't read that - try a clearer photo or paste text.")
    ai_usage.log_usage(
        conn, provider=result.provider, model=result.model, operation=_OPERATION,
        job_id=None, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_micros=result.cost_micros, status="ok",
    )

    cur = conn.execute(
        "INSERT INTO receipts (source, raw_text, provider, model, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            "photo" if image_jpeg is not None else "paste",
            pasted or None, result.provider, result.model, user_id,
        ),
    )
    receipt_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    if image_jpeg is not None:
        receipts_dir = settings.data_dir / "receipts"
        receipts_dir.mkdir(parents=True, exist_ok=True)
        (receipts_dir / f"{receipt_id}.jpg").write_bytes(image_jpeg)
        conn.execute(
            "UPDATE receipts SET image_path = ? WHERE id = ?", (f"{receipt_id}.jpg", receipt_id)
        )
    for order, item in enumerate(result.receipt.items):
        conn.execute(
            """INSERT INTO receipt_lines
               (receipt_id, sort_order, original_text, product_name, food_name, food_id,
                quantity_text, size_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                receipt_id, order, item.original_text.strip() or "(item)",
                (item.name or "").strip() or None, (item.food or "").strip().lower() or None,
                _match_food(conn, item.original_text, item.food),
                (item.quantity_text or "").strip() or None,
                (item.size_text or "").strip() or None,
            ),
        )
    conn.commit()
    return CaptureResult(receipt_id, item_count=len(result.receipt.items))


# --------------------------------------------------------------------------------------
# Review: what applying would do
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewLine:
    line_id: int
    original_text: str
    product_name: str | None
    food_name: str  # editable generic name (matched food's name wins over the model's)
    food_id: int | None
    quantity_text: str | None
    size_text: str | None
    is_new_food: bool  # no deterministic match yet - applying creates/learns it
    pantry_item_name: str | None
    action_text: str | None  # 'mark full', '+2 kg', 'track new' - None = nothing to do
    add_canonical: int | None
    checks_off: str | None  # shopping-list line this purchase would check off


@dataclass(frozen=True, slots=True)
class ReceiptReview:
    receipt_id: int
    source: str
    status: str
    lines: list[ReviewLine] = field(default_factory=list)


def _size_canonical(conn: sqlite3.Connection, size_text: str | None) -> tuple[int, str] | None:
    """'15 oz' -> (canonical micro-units, dimension), when the unit resolves exactly."""
    raw = (size_text or "").strip()
    if not raw:
        return None
    match = re.match(r"^([\d./\s]+)\s*([a-zA-Z ]+)$", raw)
    if not match:
        return None
    try:
        amount = quantity.parse_quantity(match.group(1))
    except quantity.QuantityError:
        return None
    unit = units.resolve_unit(conn, match.group(2).strip())
    if unit is None or unit.to_canonical_microunits is None:
        return None
    return quantity.to_canonical(amount, unit.to_canonical_microunits), unit.dimension


def _purchased_total(
    conn: sqlite3.Connection, line: sqlite3.Row, target: pantry.PantryItem
) -> int | None:
    """Bought amount in the pantry item's dimension: units x pack size, else units as 'each'."""
    item_unit = units.get_unit(conn, target.unit_id) if target.unit_id else None
    if item_unit is None or item_unit.to_canonical_microunits is None:
        return None
    try:
        count = quantity.parse_quantity(line["quantity_text"] or "1")
    except quantity.QuantityError:
        count = Decimal(1)
    size = _size_canonical(conn, line["size_text"])
    if size is not None and size[1] == item_unit.dimension:
        return int(count * size[0])
    if item_unit.dimension == "count":
        return quantity.to_canonical(count, 1_000)  # N units -> milli-each
    return None


def review(conn: sqlite3.Connection, receipt_id: int) -> ReceiptReview:
    head = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if head is None:
        raise ReceiptError("unknown receipt")
    lines: list[ReviewLine] = []
    for row in conn.execute(
        "SELECT * FROM receipt_lines WHERE receipt_id = ? ORDER BY sort_order", (receipt_id,)
    ).fetchall():
        food_id = row["food_id"]
        matched_name: str | None = None
        if food_id is not None:
            name_row = conn.execute(
                "SELECT name FROM foods WHERE id = ?", (food_id,)
            ).fetchone()
            matched_name = str(name_row["name"]) if name_row else None
        display_food = matched_name or row["food_name"] or row["product_name"] or ""

        target = None
        action_text: str | None = None
        add_canonical: int | None = None
        if food_id is not None:
            candidates = pantry.items_for_food(conn, food_id)
            target = candidates[0] if candidates else None
        if target is not None:
            if target.quantity_mode == "gauge":
                action_text = "mark full"
            elif target.quantity_mode == "binary":
                action_text = "mark have"
            else:
                add_canonical = _purchased_total(conn, row, target)
                if add_canonical is not None and target.unit_id is not None:
                    unit = units.get_unit(conn, target.unit_id)
                    if unit and unit.to_canonical_microunits:
                        amount = quantity.from_canonical(
                            add_canonical, unit.to_canonical_microunits
                        )
                        action_text = f"+{quantity.format_quantity(amount)} {unit.name}"

        checks_off: str | None = None
        if food_id is not None:
            shop_row = conn.execute(
                """SELECT sli.display_text FROM shopping_list_items sli
                   JOIN shopping_lists sl ON sl.id = sli.list_id
                   WHERE sl.status = 'active' AND sli.checked = 0 AND sli.food_id = ?
                   LIMIT 1""",
                (food_id,),
            ).fetchone()
            checks_off = str(shop_row["display_text"]) if shop_row else None

        lines.append(
            ReviewLine(
                line_id=int(row["id"]), original_text=row["original_text"],
                product_name=row["product_name"], food_name=display_food, food_id=food_id,
                quantity_text=row["quantity_text"], size_text=row["size_text"],
                is_new_food=(food_id is None),
                pantry_item_name=target.display_name if target else None,
                action_text=action_text, add_canonical=add_canonical,
                checks_off=checks_off,
            )
        )
    return ReceiptReview(
        receipt_id=receipt_id, source=head["source"], status=head["status"], lines=lines
    )


# --------------------------------------------------------------------------------------
# Apply: the only writer
# --------------------------------------------------------------------------------------


def apply(
    conn: sqlite3.Connection,
    receipt_id: int,
    *,
    included_line_ids: set[int],
    food_names: dict[int, str],  # line_id -> the (possibly edited) generic name
    track_location_id: int | None,
    line_locations: dict[int, int] | None = None,  # line_id -> per-line location override
    user_id: int | None = None,
) -> list[str]:
    """Apply the reviewed lines: learn aliases, restock matched pantry items, optionally start
    tracking new foods, and check bought items off the shopping list. Idempotent per receipt.

    A new food lands in its per-line location when one was chosen, else the receipt-wide
    ``track_location_id`` default; with neither it stays untracked (a food + alias are still
    learned)."""
    head = conn.execute("SELECT status FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if head is None:
        raise ReceiptError("unknown receipt")
    if head["status"] != "pending":
        return []  # a re-POST (back button / double-click) never restocks twice

    summary: list[str] = []
    for row in conn.execute(
        "SELECT * FROM receipt_lines WHERE receipt_id = ? ORDER BY sort_order", (receipt_id,)
    ).fetchall():
        line_id = int(row["id"])
        if line_id not in included_line_ids:
            continue
        edited = (food_names.get(line_id) or "").strip().lower()
        name = edited or (row["food_name"] or "").strip().lower()
        if not name:
            continue
        # Resolve or create the food. Created CONFIRMED: the name on this line was just
        # reviewed (and possibly corrected) by a human on the apply form.
        alias_row = conn.execute(
            "SELECT food_id FROM food_aliases WHERE alias = ?", (name,)
        ).fetchone()
        food_id = int(alias_row["food_id"]) if alias_row else None
        if food_id is None:
            name_row = conn.execute(
                "SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            food_id = int(name_row["id"]) if name_row else None
        if food_id is None:
            cur = conn.execute(
                "INSERT INTO foods (name, status) VALUES (?, 'confirmed')", (name,)
            )
            food_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        conn.execute(
            "UPDATE receipt_lines SET food_id = ?, food_name = ? WHERE id = ?",
            (food_id, name, line_id),
        )
        # Learn the store's phrasing so the next receipt matches without the model.
        alias = _normalize_alias(row["original_text"])
        if alias:
            conn.execute(
                "INSERT OR IGNORE INTO food_aliases (alias, food_id) VALUES (?, ?)",
                (alias, food_id),
            )

        candidates = pantry.items_for_food(conn, food_id)
        target = candidates[0] if candidates else None
        if target is not None:
            if target.quantity_mode == "gauge":
                pantry.set_gauge(
                    conn, target.id, "full", reason="restock", user_id=user_id, commit=False
                )
                summary.append(f"{target.display_name} → full")
            elif target.quantity_mode == "binary":
                pantry.set_have(
                    conn, target.id, True, reason="restock", user_id=user_id, commit=False
                )
                summary.append(f"{target.display_name} → have")
            else:
                bought = _purchased_total(conn, row, target)
                unit = units.get_unit(conn, target.unit_id) if target.unit_id else None
                if bought is not None and unit and unit.to_canonical_microunits:
                    total = (target.canonical_quantity or 0) + bought
                    pantry.set_exact(
                        conn, target.id,
                        quantity.plain_str(
                            quantity.from_canonical(total, unit.to_canonical_microunits)
                        ),
                        reason="restock", user_id=user_id, commit=False,
                    )
                    summary.append(f"{target.display_name} restocked")
        elif (loc_id := (line_locations or {}).get(line_id) or track_location_id) is not None:
            new_id = pantry.add_item(
                conn,
                pantry.PantryItemInput(
                    display_name=name, location_id=loc_id,
                    quantity_mode=pantry.AUTO_MODE, gauge="full", food=name,
                ),
                user_id=user_id, commit=False,
            )
            if new_id:
                summary.append(f"{name} → now tracked")

        # Check the purchase off the shopping list (it was bought, after all).
        checked = conn.execute(
            """UPDATE shopping_list_items SET checked = 1, checked_at = ?
               WHERE checked = 0 AND food_id = ? AND list_id IN
                 (SELECT id FROM shopping_lists WHERE status = 'active')""",
            (now_iso(), food_id),
        )
        if checked.rowcount:
            summary.append(f"{name} checked off the list")

    conn.execute(
        "UPDATE receipts SET status = 'applied', applied_at = ? WHERE id = ?",
        (now_iso(), receipt_id),
    )
    conn.commit()
    return summary


def discard(conn: sqlite3.Connection, receipt_id: int) -> None:
    conn.execute(
        "UPDATE receipts SET status = 'discarded' WHERE id = ? AND status = 'pending'",
        (receipt_id,),
    )
    conn.commit()
