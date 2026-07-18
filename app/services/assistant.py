"""Meal-planning / pantry assistant (Phase 5c): deterministic retrieval, model proposes, we apply.

The load-bearing contract (docs/05 section 3): application code does the hard work FIRST -
it applies the household's hard constraints (allergies/exclusions), builds a compact ranked
candidate set, and summarizes the pantry - then the model reasons over that context and returns a
structured reply that MAY contain proposals. A proposal is a pending, versioned record; accepting
it is a separate, idempotent request that re-validates current data and applies one transaction
through deterministic services. The model never calculates authoritative quantities or writes to
the plan, pantry, or shopping tables.

v1 divergence from docs/05 (recorded per the docs-as-contract rule): this is a single structured
request/response per turn via the forced-tool adapter (the same pattern as extract/draft/receipt),
NOT the streaming SSE + SDK tool-runner loop. Server-rendered htmx, offline-testable adapters, one
bounded call per turn. Streaming and multi-tool loops are a post-v1 enhancement.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from app import ai
from app.ai import usage as ai_usage
from app.config import get_settings
from app.extraction import AssistantResponse
from app.security import now_iso
from app.services import matching, pantry, planning, preferences, quantity, recipes, units

_OPERATION = "assist"
_MAX_CANDIDATES = 40
_MAX_PANTRY = 60
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class AssistantError(ValueError):
    """Invalid assistant input (unknown conversation/proposal)."""


# --------------------------------------------------------------------------------------
# Conversations + messages
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    id: int
    role: str
    content: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Proposal:
    id: int
    kind: str
    status: str
    payload: dict[str, object]
    created_at: str

    @property
    def is_pending(self) -> bool:
        return self.status == "pending"


def start_conversation(
    conn: sqlite3.Connection, *, user_id: int | None = None, commit: bool = True
) -> int:
    cur = conn.execute("INSERT INTO ai_conversations (user_id) VALUES (?)", (user_id,))
    if commit:
        conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def latest_conversation(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM ai_conversations ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    return int(row["id"]) if row else None


def get_or_create_conversation(
    conn: sqlite3.Connection, *, user_id: int | None = None
) -> int:
    existing = latest_conversation(conn)
    return existing if existing is not None else start_conversation(conn, user_id=user_id)


def list_messages(conn: sqlite3.Connection, conversation_id: int) -> list[Message]:
    rows = conn.execute(
        "SELECT id, role, content, created_at FROM ai_messages "
        "WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    return [
        Message(id=int(r["id"]), role=r["role"], content=r["content"], created_at=r["created_at"])
        for r in rows
    ]


def list_proposals(conn: sqlite3.Connection, conversation_id: int) -> dict[int, list[Proposal]]:
    """message_id -> its proposals, for rendering cards under the assistant turn."""
    rows = conn.execute(
        "SELECT id, message_id, kind, status, payload, created_at FROM ai_proposals "
        "WHERE conversation_id = ? ORDER BY id",
        (conversation_id,),
    ).fetchall()
    out: dict[int, list[Proposal]] = {}
    for r in rows:
        prop = Proposal(
            id=int(r["id"]), kind=r["kind"], status=r["status"],
            payload=json.loads(r["payload"]), created_at=r["created_at"],
        )
        out.setdefault(int(r["message_id"] or 0), []).append(prop)
    return out


# --------------------------------------------------------------------------------------
# Deterministic context (hard filter + candidate set + pantry summary)
# --------------------------------------------------------------------------------------


def build_context(
    conn: sqlite3.Connection, message: str, *, week_start: date
) -> tuple[str, list[int]]:
    """Assemble the JSON context for one turn. Returns (json_text, candidate_recipe_ids).

    Candidates are cookbook recipes that pass the HARD constraints, ranked by pantry coverage
    then rating, capped. This is the deterministic retrieval the model reasons over."""
    prefs = preferences.load(conn)
    hard = prefs.hard_terms
    coverage = matching.batch_coverage(conn, status="cookbook")

    # (have, rating, recipe_id, candidate-dict) - the id is carried separately so it stays typed.
    scored: list[tuple[int, int, int, dict[str, object]]] = []
    for summary in recipes.list_recipes(conn, status="cookbook"):
        if hard and preferences.recipe_violates_hard(conn, summary.id, hard) is not None:
            continue
        cov = coverage.get(summary.id)
        detail_tags = recipes.get_recipe(conn, summary.id)
        tags = list(detail_tags.tags) if detail_tags else []
        scored.append((
            cov.have if cov else 0,
            summary.rating or 0,
            summary.id,
            {
                "id": summary.id,
                "title": summary.title,
                "tags": tags,
                "tier": summary.tier,
                "minutes": summary.total_minutes,
                "have": cov.have if cov else 0,
                "need": (cov.total - cov.have) if cov else 0,
            },
        ))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    top = scored[:_MAX_CANDIDATES]
    candidates = [c for _, _, _, c in top]
    candidate_ids = [rid for _, _, rid, _ in top]

    pantry_on_hand: list[str] = []
    for item in pantry.list_items(conn):
        if item.quantity_mode == "gauge" and item.gauge == "out":
            continue
        if item.quantity_mode == "binary" and not item.have:
            continue
        if item.quantity_mode == "exact" and not (item.canonical_quantity or 0) > 0:
            continue
        pantry_on_hand.append(item.display_name)
        if len(pantry_on_hand) >= _MAX_PANTRY:
            break

    expiring = [u.title for u in matching.use_it_up(conn, limit=5)]
    days = planning.week_dates(week_start)
    context = {
        "target_week": {
            "start": week_start.isoformat(),
            "days": [
                {"day_index": i, "weekday": _WEEKDAYS[i], "date": d.isoformat()}
                for i, d in enumerate(days)
            ],
        },
        "preferences": prefs.for_prompt(),
        "pantry_on_hand": pantry_on_hand,
        "recipes_that_use_expiring_items": expiring,
        "candidate_recipes": candidates,
        "user_message": message,
    }
    return json.dumps(context, ensure_ascii=False), candidate_ids


# --------------------------------------------------------------------------------------
# Ask (one turn)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AskResult:
    reply: str
    error: str | None = None
    proposal_ids: list[int] = field(default_factory=list)


def ask(
    conn: sqlite3.Connection,
    conversation_id: int,
    message: str,
    *,
    week_start: date | None = None,
    user_id: int | None = None,
) -> AskResult:
    """Record the user's message, call the assistant once, persist its reply + any proposals."""
    text = message.strip()
    if not text:
        return AskResult(reply="", error="Type a message first.")
    start = week_start or planning.week_start()

    settings = get_settings()
    provider = ai.get_provider(settings)
    if provider is None:
        return AskResult(reply="", error="The assistant needs an AI key configured on the server.")

    conn.execute(
        "INSERT INTO ai_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conversation_id, text),
    )
    conn.commit()

    if not ai_usage.within_budget(conn, settings):
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, status="blocked", error="daily or monthly AI spend cap reached",
        )
        return _record_assistant_message(
            conn, conversation_id, "Today's AI spend limit has been reached - try again later.",
            start, [], None,
        )

    content, candidate_ids = build_context(conn, text, week_start=start)
    try:
        result = provider.assist(content)
    except ai.AIError as exc:
        ai_usage.log_usage(
            conn, provider=provider.provider, model=provider.model, operation=_OPERATION,
            job_id=None, input_tokens=exc.input_tokens, output_tokens=exc.output_tokens,
            cost_micros=exc.cost_micros, status="error", error=str(exc)[:500],
        )
        return _record_assistant_message(
            conn, conversation_id, "Sorry - I couldn't put that together just now. Try rephrasing?",
            start, [], None,
        )
    ai_usage.log_usage(
        conn, provider=result.provider, model=result.model, operation=_OPERATION,
        job_id=None, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        cost_micros=result.cost_micros, status="ok",
    )
    return _record_assistant_message(
        conn, conversation_id, result.response.message or "Done.", start,
        candidate_ids, result.response,
    )


def _record_assistant_message(
    conn: sqlite3.Connection,
    conversation_id: int,
    message_text: str,
    week_start: date,
    candidate_ids: list[int],
    response: AssistantResponse | None,
) -> AskResult:
    cur = conn.execute(
        "INSERT INTO ai_messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
        (conversation_id, message_text),
    )
    message_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    conn.execute(
        "UPDATE ai_conversations SET updated_at = ? WHERE id = ?", (now_iso(), conversation_id)
    )
    proposal_ids: list[int] = []
    if response is not None:
        proposal_ids = _persist_proposals(
            conn, conversation_id, message_id, week_start, candidate_ids, response
        )
    conn.commit()
    return AskResult(reply=message_text, proposal_ids=proposal_ids)


def _persist_proposals(
    conn: sqlite3.Connection,
    conversation_id: int,
    message_id: int,
    week_start: date,
    candidate_ids: list[int],
    response: AssistantResponse,
) -> list[int]:
    """Validate and store any proposals as pending records. Recipe ids are constrained to the
    candidate set the model was shown - a hallucinated id is dropped, not persisted."""
    ids: list[int] = []
    allowed = set(candidate_ids)
    if response.meal_plan and response.meal_plan.entries:
        entries: list[dict[str, object]] = []
        for entry in response.meal_plan.entries:
            if entry.recipe_id is not None and entry.recipe_id not in allowed:
                continue  # hallucinated / non-candidate id
            if entry.recipe_id is None and not (entry.note or "").strip():
                continue
            title = None
            if entry.recipe_id is not None:
                detail = recipes.get_recipe(conn, entry.recipe_id)
                title = detail.title if detail else None
            entries.append({
                "day_index": max(0, min(6, entry.day_index)),
                "slot": (entry.slot or "dinner").strip() or "dinner",
                "recipe_id": entry.recipe_id,
                "title": title,
                "note": (entry.note or "").strip() or None,
                "servings_text": (entry.servings_text or "").strip() or None,
            })
        if entries:
            ids.append(_insert_proposal(
                conn, conversation_id, message_id, "meal_plan",
                {"week_start": week_start.isoformat(), "entries": entries},
            ))
    if response.pantry_update and response.pantry_update.changes:
        changes = [
            {
                "food": c.food.strip(),
                "action": c.action if c.action in ("have", "out", "add") else "have",
                "quantity_text": (c.quantity_text or "").strip() or None,
                "unit": (c.unit or "").strip() or None,
                "location": (c.location or "").strip() or None,
            }
            for c in response.pantry_update.changes
            if c.food.strip()
        ]
        if changes:
            ids.append(_insert_proposal(
                conn, conversation_id, message_id, "pantry_update", {"changes": changes},
            ))
    return ids


def _insert_proposal(
    conn: sqlite3.Connection,
    conversation_id: int,
    message_id: int,
    kind: str,
    payload: dict[str, object],
) -> int:
    cur = conn.execute(
        "INSERT INTO ai_proposals (conversation_id, message_id, kind, payload, idempotency_key) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversation_id, message_id, kind, json.dumps(payload), uuid.uuid4().hex),
    )
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


# --------------------------------------------------------------------------------------
# Accept / dismiss (deterministic, idempotent)
# --------------------------------------------------------------------------------------


def get_proposal(conn: sqlite3.Connection, proposal_id: int) -> Proposal | None:
    row = conn.execute(
        "SELECT id, kind, status, payload, created_at FROM ai_proposals WHERE id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        return None
    return Proposal(
        id=int(row["id"]), kind=row["kind"], status=row["status"],
        payload=json.loads(row["payload"]), created_at=row["created_at"],
    )


def accept_proposal(
    conn: sqlite3.Connection, proposal_id: int, *, user_id: int | None = None
) -> list[str]:
    """Apply a pending proposal in one transaction. Idempotent: a resolved proposal is a no-op."""
    proposal = get_proposal(conn, proposal_id)
    if proposal is None:
        raise AssistantError("unknown proposal")
    if proposal.status != "pending":
        return []  # already accepted/dismissed - a double-click applies nothing
    if proposal.kind == "meal_plan":
        summary = _apply_meal_plan(conn, proposal.payload, user_id)
    else:
        summary = _apply_pantry_update(conn, proposal.payload, user_id)
    conn.execute(
        "UPDATE ai_proposals SET status = 'accepted', resolved_at = ?, resolved_by = ? "
        "WHERE id = ? AND status = 'pending'",
        (now_iso(), user_id, proposal_id),
    )
    conn.commit()
    return summary


def dismiss_proposal(conn: sqlite3.Connection, proposal_id: int, *, commit: bool = True) -> None:
    conn.execute(
        "UPDATE ai_proposals SET status = 'dismissed', resolved_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (now_iso(), proposal_id),
    )
    if commit:
        conn.commit()


def _apply_meal_plan(
    conn: sqlite3.Connection, payload: dict[str, object], user_id: int | None
) -> list[str]:
    start = date.fromisoformat(str(payload["week_start"]))
    raw_entries = payload.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    summary: list[str] = []
    for raw in entries:
        entry = raw if isinstance(raw, dict) else {}
        target = (start + timedelta(days=int(entry.get("day_index", 0)))).isoformat()
        slot = str(entry.get("slot") or "dinner")
        recipe_id = entry.get("recipe_id")
        if recipe_id is not None:
            # Re-validate at accept time: the recipe may have been deleted since the proposal.
            if recipes.get_recipe(conn, int(recipe_id)) is None:
                continue
            planning.add_recipe_entry(
                conn, target, int(recipe_id), slot=slot,
                servings_text=entry.get("servings_text"), user_id=user_id, commit=False,
            )
            detail = recipes.get_recipe(conn, int(recipe_id))
            summary.append(f"{slot.title()} {target[5:]}: {detail.title if detail else 'recipe'}")
        elif entry.get("note"):
            planning.add_note_entry(
                conn, target, str(entry["note"]), slot=slot, user_id=user_id, commit=False
            )
            summary.append(f"{slot.title()} {target[5:]}: {entry['note']}")
    return summary


def _apply_pantry_update(
    conn: sqlite3.Connection, payload: dict[str, object], user_id: int | None
) -> list[str]:
    """Apply conversational pantry changes deterministically (mirrors the receipt applier):
    match the food, update an existing item, or start tracking a new one in a sensible location."""
    raw_changes = payload.get("changes")
    changes = raw_changes if isinstance(raw_changes, list) else []
    default_location = _default_location_id(conn)
    summary: list[str] = []
    for raw in changes:
        change = raw if isinstance(raw, dict) else {}
        food_name = str(change.get("food") or "").strip()
        if not food_name:
            continue
        action = str(change.get("action") or "have")
        food_id = _resolve_food(conn, food_name)
        items = pantry.items_for_food(conn, food_id) if food_id else []
        target = items[0] if items else None

        if action == "out":
            if target is not None:
                pantry.remove_item(conn, target.id, reason="manual_remove", user_id=user_id,
                                   commit=False)
                summary.append(f"{target.display_name} → out")
            # An 'out' for a food we don't track is a no-op: never CREATE it as on-hand
            # (that recorded the opposite of what the user said - review finding).
            continue
        if target is not None:
            if target.quantity_mode == "gauge":
                pantry.set_gauge(conn, target.id, "full", reason="restock", user_id=user_id,
                                 commit=False)
                summary.append(f"{target.display_name} → full")
            elif target.quantity_mode == "binary":
                pantry.set_have(conn, target.id, True, reason="restock", user_id=user_id,
                                commit=False)
                summary.append(f"{target.display_name} → have")
            else:
                added = _add_exact(conn, target, change, user_id)
                summary.append(f"{target.display_name} {added}")
        elif default_location is not None:
            location_id = _location_by_name(conn, change.get("location")) or default_location
            new_item = _new_tracked_item(food_name, location_id, change)
            pantry.add_item(conn, new_item, user_id=user_id, commit=False)
            if new_item.quantity_mode == "exact":
                summary.append(f"{food_name} → tracked ({new_item.quantity_text} {new_item.unit})")
            else:
                summary.append(f"{food_name} → tracked (full)")
    return summary


def _new_tracked_item(
    food_name: str, location_id: int, change: dict[str, object]
) -> pantry.PantryItemInput:
    """Start tracking a new food. When the change carries a countable quantity+unit, track it
    exactly (so 'add 2 cans' isn't silently reduced to 'full'); otherwise gauge-full."""
    qty = str(change.get("quantity_text") or "").strip()
    unit = str(change.get("unit") or "").strip()
    if change.get("action") == "add" and qty and unit:
        try:
            quantity.parse_quantity(qty)
        except quantity.QuantityError:
            qty = ""
        if qty:
            return pantry.PantryItemInput(
                display_name=food_name, location_id=location_id, quantity_mode="exact",
                food=food_name, quantity_text=qty, unit=unit,
            )
    return pantry.PantryItemInput(
        display_name=food_name, location_id=location_id, quantity_mode="gauge",
        gauge="full", food=food_name,
    )


def _add_exact(
    conn: sqlite3.Connection,
    target: pantry.PantryItem,
    change: dict[str, object],
    user_id: int | None,
) -> str:
    """Add a counted amount to an exact item, in the item's own unit when the change resolves."""
    unit = units.get_unit(conn, target.unit_id) if target.unit_id else None
    if unit is None or unit.to_canonical_microunits is None:
        return "(no change)"
    factor = unit.to_canonical_microunits
    try:
        amount = quantity.parse_quantity(str(change.get("quantity_text") or "1"))
    except quantity.QuantityError:
        amount = quantity.parse_quantity("1")
    change_unit = (
        units.resolve_unit(conn, str(change.get("unit") or "")) if change.get("unit") else None
    )
    if (
        change_unit is not None
        and change_unit.to_canonical_microunits is not None
        and change_unit.dimension == unit.dimension
    ):
        add_canonical = quantity.to_canonical(amount, change_unit.to_canonical_microunits)
    else:
        add_canonical = quantity.to_canonical(amount, factor)
    total = (target.canonical_quantity or 0) + add_canonical
    new_text = quantity.plain_str(quantity.from_canonical(total, factor))
    pantry.set_exact(conn, target.id, new_text, reason="restock", user_id=user_id, commit=False)
    added = quantity.format_quantity(quantity.from_canonical(add_canonical, factor))
    return f"+{added} {unit.name}"


def _resolve_food(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT food_id FROM food_aliases WHERE alias = ?", (name,)).fetchone()
    if row is not None:
        return int(row["food_id"])
    row = conn.execute("SELECT id FROM foods WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    return int(row["id"]) if row else None


def _location_by_name(conn: sqlite3.Connection, name: object) -> int | None:
    text = str(name or "").strip()
    if not text:
        return None
    row = conn.execute(
        "SELECT id FROM locations WHERE name = ? COLLATE NOCASE", (text,)
    ).fetchone()
    return int(row["id"]) if row else None


def _default_location_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM locations ORDER BY sort_order, id LIMIT 1").fetchone()
    return int(row["id"]) if row else None
