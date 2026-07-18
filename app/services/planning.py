"""Meal planning (Phase 5a): the week board, plan->shopping, saved menus, and iCal export.

Deterministic and AI-free. An entry is a recipe or a free-text note ("leftovers", "pizza out");
each carries its own servings so the plan->shopping math scales per entry. Generating the list
reuses the Phase 4.6 trip builder (shopping.build_trip/apply_trip), so the whole store-language
story - purchase units, staple lane, pantry subtraction, provenance - applies to a planned week
for free. Dates are ISO YYYY-MM-DD; weeks run Monday..Sunday.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.security import now
from app.services import recipes, shopping

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class PlanningError(ValueError):
    """Invalid planning input (bad date, unknown recipe/menu)."""


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError) as exc:
        raise PlanningError(f"not a date: {value!r}") from exc


def week_start(for_date: date | None = None) -> date:
    """Monday of the week containing ``for_date`` (today in UTC when omitted)."""
    d = for_date or now().date()
    return d - timedelta(days=d.weekday())


def week_dates(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]


# --------------------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanEntry:
    id: int
    plan_date: str
    slot: str
    entry_type: str
    recipe_id: int | None
    recipe_slug: str | None
    recipe_title: str | None
    note_text: str | None
    servings_text: str | None

    @property
    def label(self) -> str:
        if self.entry_type == "note":
            return self.note_text or "(note)"
        return self.recipe_title or "(recipe)"


@dataclass(frozen=True, slots=True)
class DayColumn:
    plan_date: str
    weekday: str
    is_today: bool
    entries: list[PlanEntry] = field(default_factory=list)


def _next_sort(conn: sqlite3.Connection, plan_date: str, slot: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM meal_plan_entries "
        "WHERE plan_date = ? AND slot = ?",
        (plan_date, slot),
    ).fetchone()
    return int(row["n"])


def add_recipe_entry(
    conn: sqlite3.Connection,
    plan_date: str,
    recipe_id: int,
    *,
    slot: str = "dinner",
    servings_text: str | None = None,
    user_id: int | None = None,
    commit: bool = True,
) -> int:
    iso = _parse_date(plan_date).isoformat()
    recipe = recipes.get_recipe(conn, recipe_id)
    if recipe is None:
        raise PlanningError("recipe not found")
    slot_clean = (slot or "dinner").strip() or "dinner"
    servings = (servings_text or "").strip() or recipe.base_servings
    cur = conn.execute(
        """INSERT INTO meal_plan_entries
           (plan_date, slot, sort_order, entry_type, recipe_id, servings_text, created_by)
           VALUES (?, ?, ?, 'recipe', ?, ?, ?)""",
        (iso, slot_clean, _next_sort(conn, iso, slot_clean), recipe_id, servings, user_id),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def add_note_entry(
    conn: sqlite3.Connection,
    plan_date: str,
    note_text: str,
    *,
    slot: str = "dinner",
    user_id: int | None = None,
    commit: bool = True,
) -> int:
    iso = _parse_date(plan_date).isoformat()
    text = note_text.strip()
    if not text:
        raise PlanningError("a note entry needs text")
    slot_clean = (slot or "dinner").strip() or "dinner"
    cur = conn.execute(
        """INSERT INTO meal_plan_entries
           (plan_date, slot, sort_order, entry_type, note_text, created_by)
           VALUES (?, ?, ?, 'note', ?, ?)""",
        (iso, slot_clean, _next_sort(conn, iso, slot_clean), text, user_id),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid) if cur.lastrowid is not None else 0


def remove_entry(conn: sqlite3.Connection, entry_id: int, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM meal_plan_entries WHERE id = ?", (entry_id,))
    if commit:
        conn.commit()


def move_entry(
    conn: sqlite3.Connection, entry_id: int, plan_date: str, *, commit: bool = True
) -> None:
    """Reassign an entry to another day (tap-to-move / drag-drop target)."""
    iso = _parse_date(plan_date).isoformat()
    row = conn.execute(
        "SELECT slot FROM meal_plan_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        raise PlanningError("unknown entry")
    conn.execute(
        "UPDATE meal_plan_entries SET plan_date = ?, sort_order = ? WHERE id = ?",
        (iso, _next_sort(conn, iso, row["slot"]), entry_id),
    )
    if commit:
        conn.commit()


def set_entry_servings(
    conn: sqlite3.Connection, entry_id: int, servings_text: str, *, commit: bool = True
) -> None:
    clean = servings_text.strip()
    conn.execute(
        "UPDATE meal_plan_entries SET servings_text = ? WHERE id = ? AND entry_type = 'recipe'",
        (clean or None, entry_id),
    )
    if commit:
        conn.commit()


_ENTRY_SELECT = """
    SELECT e.id, e.plan_date, e.slot, e.entry_type, e.recipe_id, e.note_text, e.servings_text,
           r.slug AS recipe_slug, r.title AS recipe_title
    FROM meal_plan_entries e
    LEFT JOIN recipes r ON r.id = e.recipe_id
"""


def _to_entry(row: sqlite3.Row) -> PlanEntry:
    return PlanEntry(
        id=int(row["id"]), plan_date=row["plan_date"], slot=row["slot"],
        entry_type=row["entry_type"], recipe_id=row["recipe_id"],
        recipe_slug=row["recipe_slug"], recipe_title=row["recipe_title"],
        note_text=row["note_text"], servings_text=row["servings_text"],
    )


def list_entries(conn: sqlite3.Connection, start: date, end: date) -> list[PlanEntry]:
    rows = conn.execute(
        _ENTRY_SELECT + " WHERE e.plan_date >= ? AND e.plan_date <= ? "
        "ORDER BY e.plan_date, e.slot, e.sort_order",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [_to_entry(r) for r in rows]


def week_board(conn: sqlite3.Connection, start: date) -> list[DayColumn]:
    """The 7 day-columns for the week starting Monday ``start``."""
    days = week_dates(start)
    entries = list_entries(conn, days[0], days[-1])
    today = now().date().isoformat()
    by_day: dict[str, list[PlanEntry]] = {}
    for entry in entries:
        by_day.setdefault(entry.plan_date, []).append(entry)
    return [
        DayColumn(
            plan_date=d.isoformat(), weekday=_WEEKDAYS[i], is_today=(d.isoformat() == today),
            entries=by_day.get(d.isoformat(), []),
        )
        for i, d in enumerate(days)
    ]


# --------------------------------------------------------------------------------------
# Plan -> shopping (reuses the Phase 4.6 trip builder)
# --------------------------------------------------------------------------------------


def week_picks(conn: sqlite3.Connection, start: date) -> list[tuple[int, str | None]]:
    """(recipe_id, servings) for every recipe entry in the week - the trip builder's input."""
    picks: list[tuple[int, str | None]] = []
    for entry in list_entries(conn, start, start + timedelta(days=6)):
        if entry.entry_type == "recipe" and entry.recipe_id is not None:
            picks.append((entry.recipe_id, entry.servings_text))
    return picks


def plan_to_shopping(conn: sqlite3.Connection, start: date, *, commit: bool = True) -> int:
    """Add the week's recipes to the active shopping list (pantry-aware, aggregated once)."""
    picks = week_picks(conn, start)
    if not picks:
        return 0
    list_id = shopping.active_list(conn)
    return shopping.apply_trip(conn, list_id, picks, commit=commit)


# --------------------------------------------------------------------------------------
# Saved menus (reusable week templates)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SavedMenu:
    id: int
    name: str
    entry_count: int


def save_week_as_menu(
    conn: sqlite3.Connection, start: date, name: str, *, commit: bool = True
) -> int:
    clean = name.strip()
    if not clean:
        raise PlanningError("a saved menu needs a name")
    entries = list_entries(conn, start, start + timedelta(days=6))
    cur = conn.execute("INSERT INTO saved_menus (name) VALUES (?)", (clean,))
    menu_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
    start_iso = start
    for entry in entries:
        day_index = (_parse_date(entry.plan_date) - start_iso).days
        conn.execute(
            """INSERT INTO saved_menu_entries
               (menu_id, day_index, slot, sort_order, entry_type, recipe_id, note_text,
                servings_text)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?)""",
            (menu_id, day_index, entry.slot, entry.entry_type, entry.recipe_id,
             entry.note_text, entry.servings_text),
        )
    if commit:
        conn.commit()
    return menu_id


def list_menus(conn: sqlite3.Connection) -> list[SavedMenu]:
    rows = conn.execute(
        """SELECT m.id, m.name, COUNT(e.id) AS n
           FROM saved_menus m LEFT JOIN saved_menu_entries e ON e.menu_id = m.id
           GROUP BY m.id ORDER BY m.name COLLATE NOCASE""",
    ).fetchall()
    return [SavedMenu(id=int(r["id"]), name=r["name"], entry_count=int(r["n"])) for r in rows]


def apply_menu_to_week(
    conn: sqlite3.Connection,
    menu_id: int,
    start: date,
    *,
    user_id: int | None = None,
    commit: bool = True,
) -> int:
    """Lay a saved menu onto the week starting Monday ``start``. Skips recipe rows whose recipe
    was since deleted. Returns entries created."""
    rows = conn.execute(
        "SELECT * FROM saved_menu_entries WHERE menu_id = ? ORDER BY day_index, sort_order",
        (menu_id,),
    ).fetchall()
    if not rows:
        raise PlanningError("unknown or empty menu")
    added = 0
    for row in rows:
        target = (start + timedelta(days=int(row["day_index"]))).isoformat()
        if row["entry_type"] == "recipe":
            if row["recipe_id"] is None or recipes.get_recipe(conn, row["recipe_id"]) is None:
                continue
            add_recipe_entry(
                conn, target, int(row["recipe_id"]), slot=row["slot"],
                servings_text=row["servings_text"], user_id=user_id, commit=False,
            )
        else:
            add_note_entry(
                conn, target, row["note_text"] or "note", slot=row["slot"],
                user_id=user_id, commit=False,
            )
        added += 1
    if commit:
        conn.commit()
    return added


def delete_menu(conn: sqlite3.Connection, menu_id: int, *, commit: bool = True) -> None:
    conn.execute("DELETE FROM saved_menus WHERE id = ?", (menu_id,))
    if commit:
        conn.commit()


# --------------------------------------------------------------------------------------
# iCal export
# --------------------------------------------------------------------------------------


def _ical_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    )


def week_ical(conn: sqlite3.Connection, start: date, *, app_base_url: str = "") -> str:
    """The week's plan as an all-day-event iCalendar (imports into Apple/Google Calendar)."""
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RecipeCollater//Meal Plan//EN",
        "CALSCALE:GREGORIAN",
    ]
    for entry in list_entries(conn, start, start + timedelta(days=6)):
        d = _parse_date(entry.plan_date)
        dtstart = d.strftime("%Y%m%d")
        dtend = (d + timedelta(days=1)).strftime("%Y%m%d")
        summary = f"{entry.slot.title()}: {entry.label}"
        lines += [
            "BEGIN:VEVENT",
            f"UID:rc-plan-{entry.id}@recipecollater",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{dtstart}",
            f"DTEND;VALUE=DATE:{dtend}",
            f"SUMMARY:{_ical_escape(summary)}",
        ]
        if entry.recipe_slug and app_base_url:
            lines.append(f"URL:{app_base_url}/recipes/{entry.recipe_slug}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
