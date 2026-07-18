"""Cookbook export (Phase 6): every recipe as portable JSON + Markdown on disk.

Data stays portable (CONVENTIONS): a nightly export means the family can read and re-import their
cookbook without this app. Deterministic and offline. Run via `python -m app.manage export-cookbook
<dir>` (cron/Scheduled Task for the nightly cadence).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path

from app.services import recipes


def _safe_name(slug: str) -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", slug.lower()).strip("-")
    return cleaned or "recipe"


def export_all(conn: sqlite3.Connection, dest: Path) -> int:
    """Write <dest>/json/<slug>.json and <dest>/markdown/<slug>.md for every recipe.

    Returns the number of recipes exported. Overwrites in place (an export is a fresh snapshot).
    """
    json_dir = dest / "json"
    md_dir = dest / "markdown"
    json_dir.mkdir(parents=True, exist_ok=True)
    md_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    index: list[dict[str, object]] = []
    for row in conn.execute("SELECT id FROM recipes ORDER BY id").fetchall():
        detail = recipes.get_recipe(conn, int(row["id"]))
        if detail is None:
            continue
        name = _safe_name(detail.slug)
        (json_dir / f"{name}.json").write_text(
            json.dumps(asdict(detail), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (md_dir / f"{name}.md").write_text(recipes.to_markdown(detail), encoding="utf-8")
        index.append({"slug": detail.slug, "title": detail.title, "status": detail.status})
        count += 1
    (dest / "index.json").write_text(
        json.dumps({"recipes": index}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return count
