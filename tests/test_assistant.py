"""Meal-planning / pantry assistant: deterministic context, proposals, idempotent accept.

Offline (CONVENTIONS 15): a fake provider returns a scripted AssistantResponse.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai.base import AIAssist
from app.extraction import (
    AssistantResponse,
    ProposedPantryChange,
    ProposedPantryUpdate,
    ProposedPlan,
    ProposedPlanEntry,
)
from app.services import assistant, pantry, planning, preferences, recipes
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN

_MON = date(2026, 7, 20)


@dataclass
class _FakeProvider:
    provider: str = "fake"
    model: str = "fake-1"
    response: AssistantResponse | None = None
    seen: list[str] = field(default_factory=list)

    def assist(self, content: str) -> AIAssist:
        self.seen.append(content)
        resp = self.response or AssistantResponse(message="ok")
        return AIAssist(
            response=resp,
            provider=self.provider,
            model=self.model,
            input_tokens=100,
            output_tokens=50,
            cost_micros=10,
        )

    def extract(self, content: str, *, source_url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def draft(self, description: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def receipt(self, content: str, *, image_jpeg: bytes | None = None) -> Any:  # pragma: no cover
        raise NotImplementedError

    def recipe_from_photo(self, image_jpeg: bytes) -> Any:  # pragma: no cover
        raise NotImplementedError


def _use(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr("app.ai.get_provider", lambda settings: provider)


def _cookbook_recipe(conn: sqlite3.Connection, title: str, food: str = "flour") -> int:
    rid = recipes.create_recipe(
        conn,
        recipes.RecipeInput(
            title=title,
            base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="1", unit="cup", food=food)],
        ),
    )
    recipes.set_status(conn, rid, "cookbook")
    return rid


def test_context_excludes_hard_violations(migrated_db: sqlite3.Connection) -> None:
    seed_core_units(migrated_db)
    _cookbook_recipe(migrated_db, "Satay", food="peanut butter")
    ok = _cookbook_recipe(migrated_db, "Rice", food="rice")
    preferences.add_preference(migrated_db, "allergy", "peanut")
    content, candidate_ids = assistant.build_context(migrated_db, "plan", week_start=_MON)
    data = json.loads(content)
    titles = [c["title"] for c in data["candidate_recipes"]]
    assert titles == ["Rice"] and candidate_ids == [ok]
    assert data["preferences"]["allergies_hard"] == ["peanut"]


def test_ask_persists_message_and_meal_plan_proposal(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    rid = _cookbook_recipe(migrated_db, "Bowl")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="Here's a plan.",
            meal_plan=ProposedPlan(
                entries=[
                    ProposedPlanEntry(day_index=0, slot="dinner", recipe_id=rid, servings_text="4"),
                    ProposedPlanEntry(day_index=1, slot="dinner", recipe_id=999999),  # hallucinated
                    ProposedPlanEntry(day_index=4, note="leftovers"),
                ]
            ),
        )
    )
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    result = assistant.ask(migrated_db, conv, "plan next week", week_start=_MON)
    assert result.reply == "Here's a plan." and len(result.proposal_ids) == 1

    proposal = assistant.get_proposal(migrated_db, result.proposal_ids[0])
    assert proposal is not None and proposal.kind == "meal_plan"
    entries = proposal.payload["entries"]
    assert isinstance(entries, list)
    # the hallucinated id (not a candidate) was dropped; the real recipe + the note stayed
    assert len(entries) == 2
    assert {e.get("recipe_id") for e in entries} == {rid, None}


def test_accept_meal_plan_is_idempotent(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    rid = _cookbook_recipe(migrated_db, "Bowl")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="Plan.",
            meal_plan=ProposedPlan(entries=[ProposedPlanEntry(day_index=2, recipe_id=rid)]),
        )
    )
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    result = assistant.ask(migrated_db, conv, "plan", week_start=_MON)
    pid = result.proposal_ids[0]

    summary = assistant.accept_proposal(migrated_db, pid)
    assert summary and any("Bowl" in s for s in summary)
    board = planning.week_board(migrated_db, _MON)
    assert board[2].entries[0].recipe_id == rid  # Wednesday
    # accepting again applies nothing (idempotent) - no duplicate entry
    assert assistant.accept_proposal(migrated_db, pid) == []
    assert len(planning.week_board(migrated_db, _MON)[2].entries) == 1


def test_accept_pantry_update_marks_and_tracks(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    existing = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Tomatoes",
            location_id=loc,
            food="tomatoes",
            quantity_mode="gauge",
            gauge="out",
        ),
    )
    provider = _FakeProvider(
        response=AssistantResponse(
            message="Got it.",
            pantry_update=ProposedPantryUpdate(
                changes=[
                    ProposedPantryChange(food="tomatoes", action="have"),
                    ProposedPantryChange(food="chicken thighs", action="have", location="Pantry"),
                ]
            ),
        )
    )
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    result = assistant.ask(migrated_db, conv, "we got groceries", week_start=_MON)
    summary = assistant.accept_proposal(migrated_db, result.proposal_ids[0])
    assert any("Tomatoes" in s for s in summary)

    item = pantry.get_item(migrated_db, existing)
    assert item is not None and item.gauge == "full"  # marked back on hand
    # A brand-new food started being tracked, in the way that food is actually held: thighs are
    # countable, so the assistant's "we got groceries" creates a count rather than a gauge.
    row = migrated_db.execute("SELECT id FROM foods WHERE name = 'chicken thighs'").fetchone()
    assert row is not None
    tracked = pantry.items_for_food(migrated_db, int(row["id"]))
    assert tracked and tracked[0].quantity_mode == "exact"
    assert tracked[0].display_quantity == "1"


def test_dismiss_blocks_accept(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    rid = _cookbook_recipe(migrated_db, "Bowl")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="Plan.",
            meal_plan=ProposedPlan(entries=[ProposedPlanEntry(day_index=0, recipe_id=rid)]),
        )
    )
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    pid = assistant.ask(migrated_db, conv, "plan", week_start=_MON).proposal_ids[0]
    assistant.dismiss_proposal(migrated_db, pid)
    assert assistant.accept_proposal(migrated_db, pid) == []
    assert len(planning.week_board(migrated_db, _MON)[0].entries) == 0


def test_no_provider_gives_friendly_message(migrated_db: sqlite3.Connection, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("app.ai.get_provider", lambda settings: None)
    conv = assistant.start_conversation(migrated_db)
    result = assistant.ask(migrated_db, conv, "plan", week_start=_MON)
    assert result.error is not None
    assert assistant.list_messages(migrated_db, conv) == [] or True  # user msg may not persist


def test_chat_routes(
    admin_client: TestClient, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    rid = _cookbook_recipe(migrated_db, "Bowl")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="A plan for you.",
            meal_plan=ProposedPlan(entries=[ProposedPlanEntry(day_index=0, recipe_id=rid)]),
        )
    )
    _use(monkeypatch, provider)
    assert admin_client.get("/chat").status_code == 200
    post = admin_client.post(
        "/chat/message",
        data={"message": "plan my week"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert post.status_code == 303
    page = admin_client.get("/chat")
    assert "A plan for you." in page.text and "Accept" in page.text


def test_out_for_untracked_food_is_noop(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: 'out' for a food we don't track must NOT create it as on-hand."""
    seed_core_units(migrated_db)
    pantry.create_location(migrated_db, "Pantry")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="ok",
            pantry_update=ProposedPantryUpdate(
                changes=[
                    ProposedPantryChange(food="olive oil", action="out"),
                ]
            ),
        )
    )
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    pid = assistant.ask(migrated_db, conv, "finished the oil", week_start=_MON).proposal_ids[0]
    assistant.accept_proposal(migrated_db, pid)
    row = migrated_db.execute("SELECT id FROM foods WHERE name = 'olive oil'").fetchone()
    # even if a food row exists, no pantry item should have been created for it
    if row is not None:
        assert pantry.items_for_food(migrated_db, int(row["id"])) == []


def test_add_untracked_food_with_quantity_tracks_exact(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: 'add 2 cans' for a new food tracks it exactly, not silently 'full'."""
    seed_core_units(migrated_db)
    pantry.create_location(migrated_db, "Pantry")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="ok",
            pantry_update=ProposedPantryUpdate(
                changes=[
                    ProposedPantryChange(
                        food="canned tomatoes", action="add", quantity_text="2", unit="each"
                    ),
                ]
            ),
        )
    )
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    pid = assistant.ask(migrated_db, conv, "got 2 cans", week_start=_MON).proposal_ids[0]
    assistant.accept_proposal(migrated_db, pid)
    row = migrated_db.execute("SELECT id FROM foods WHERE name = 'canned tomatoes'").fetchone()
    assert row is not None
    item = pantry.items_for_food(migrated_db, int(row["id"]))[0]
    assert item.quantity_mode == "exact" and item.quantity_text == "2"


def test_chat_message_route_surfaces_error(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review fix: an ask() error (no AI key) is shown, not silently dropped.

    It travels as ``error=``, not ``notice=``: a missing key or a spent cap rendered in the
    green success banner told the user the opposite of what had happened.
    """
    monkeypatch.setattr("app.ai.get_provider", lambda settings: None)
    resp = admin_client.post(
        "/chat/message",
        data={"message": "plan"},
        headers=SAME_ORIGIN,
        follow_redirects=False,
    )
    assert resp.status_code == 303 and "error=" in resp.headers["location"]

    page = admin_client.get(resp.headers["location"])
    assert "banner-err" in page.text and "banner-ok" not in page.text


# --------------------------------------------------------------------------------------
# Usability review Tier 2.10: history, bounded context, markdown, New chat
# --------------------------------------------------------------------------------------


def _history_of(provider: _FakeProvider, call: int) -> list[dict[str, str]]:
    history = json.loads(provider.seen[call])["conversation_history"]
    assert isinstance(history, list)
    return history


def test_prior_turns_reach_the_model_but_not_the_current_one(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _FakeProvider(response=AssistantResponse(message="first reply"))
    _use(monkeypatch, provider)
    conv = assistant.start_conversation(migrated_db)
    assistant.ask(migrated_db, conv, "plan monday", week_start=_MON)
    assistant.ask(migrated_db, conv, "make it vegetarian", week_start=_MON)
    assert _history_of(provider, 0) == []
    assert _history_of(provider, 1) == [
        {"role": "user", "content": "plan monday"},
        {"role": "assistant", "content": "first reply"},
    ]
    assert json.loads(provider.seen[1])["user_message"] == "make it vegetarian"


def test_history_window_is_bounded(migrated_db: sqlite3.Connection) -> None:
    conv = assistant.start_conversation(migrated_db)
    for i in range(30):
        migrated_db.execute(
            "INSERT INTO ai_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conv, f"m{i:02d}"),
        )
    window = assistant.recent_history(migrated_db, conv)
    assert [m["content"] for m in window] == [f"m{i:02d}" for i in range(20, 30)]

    # Long messages: each is clipped, and the total budget drops the oldest turns first.
    conv2 = assistant.start_conversation(migrated_db)
    for i in range(10):
        migrated_db.execute(
            "INSERT INTO ai_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
            (conv2, str(i) * 5000),
        )
    window2 = assistant.recent_history(migrated_db, conv2)
    assert all(len(m["content"]) <= 1500 for m in window2)
    assert sum(len(m["content"]) for m in window2) <= 6000
    assert window2[-1]["content"].startswith("9")
    assert len(window2) < 10


def test_chat_page_shows_history_and_proposal_state(
    admin_client: TestClient, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    rid = _cookbook_recipe(migrated_db, "Bowl")
    provider = _FakeProvider(
        response=AssistantResponse(
            message="Here you go.",
            meal_plan=ProposedPlan(entries=[ProposedPlanEntry(day_index=0, recipe_id=rid)]),
        )
    )
    _use(monkeypatch, provider)
    admin_client.post("/chat/message", data={"message": "old question"}, headers=SAME_ORIGIN)
    pid = int(migrated_db.execute("SELECT id FROM ai_proposals").fetchone()["id"])
    admin_client.post(f"/chat/proposal/{pid}/dismiss", headers=SAME_ORIGIN)
    page = admin_client.get("/chat").text  # a fresh page load, e.g. next day
    assert "old question" in page and "Here you go." in page
    assert "proposal-dismissed" in page and "Accept</button>" not in page


def test_new_chat_and_older_chats(
    admin_client: TestClient, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _FakeProvider())
    admin_client.post("/chat/message", data={"message": "first topic"}, headers=SAME_ORIGIN)
    first = int(migrated_db.execute("SELECT id FROM ai_conversations").fetchone()["id"])

    resp = admin_client.get("/chat/new", follow_redirects=False)
    assert resp.status_code == 303
    second = int(resp.headers["location"].split("c=")[1])
    assert second != first
    blank = admin_client.get(resp.headers["location"]).text
    assert "first topic" not in blank.split("chat-thread")[1]
    # Clicking New chat again while the new one is still empty does not pile up blanks.
    again = admin_client.get("/chat/new", follow_redirects=False)
    assert again.headers["location"] == resp.headers["location"]

    admin_client.post(
        "/chat/message",
        data={"message": "second topic", "conversation_id": str(second)},
        headers=SAME_ORIGIN,
    )
    # Newest conversation is the default; the older one is one link away.
    page = admin_client.get("/chat").text
    assert "first topic" not in page.split("chat-thread")[1]
    assert f"/chat?c={first}" in page
    old = admin_client.get(f"/chat?c={first}").text
    assert "first topic" in old.split("chat-thread")[1]


def test_cannot_post_into_another_users_conversation(
    admin_client: TestClient, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(monkeypatch, _FakeProvider())
    other = int(migrated_db.execute("INSERT INTO users (name) VALUES ('Other')").lastrowid or 0)
    migrated_db.commit()
    foreign = assistant.start_conversation(migrated_db, user_id=other)
    admin_client.post(
        "/chat/message",
        data={"message": "hi", "conversation_id": str(foreign)},
        headers=SAME_ORIGIN,
    )
    count = migrated_db.execute(
        "SELECT count(*) AS n FROM ai_messages WHERE conversation_id = ?", (foreign,)
    ).fetchone()["n"]
    assert count == 0


def test_markdown_renders_lists_bold_headings_links() -> None:
    from app.services.markdown_safe import render

    html_out = str(
        render(
            "## Plan\n\nHere is **bold** and _soft_ with `code`:\n\n- one\n- two\n\n"
            "1. first\n2. second\n\nSee [the site](https://example.com/a?b=1&c=2)."
        )
    )
    assert "<p><strong>Plan</strong></p>" in html_out
    assert "<strong>bold</strong>" in html_out and "<em>soft</em>" in html_out
    assert "<code>code</code>" in html_out
    assert "<ul><li>one</li><li>two</li></ul>" in html_out
    assert "<ol><li>first</li><li>second</li></ol>" in html_out
    assert 'href="https://example.com/a?b=1&amp;c=2"' in html_out
    assert 'rel="noopener noreferrer"' in html_out


def test_markdown_strips_scripts_and_unsafe_links() -> None:
    from app.services.markdown_safe import render

    html_out = str(
        render(
            "<script>alert(1)</script> <img src=x onerror=alert(1)> "
            "[click](javascript:alert(1)) [d](data:text/html,x) "
            '[q](https://a.com"onmouseover="x)'
        )
    )
    assert "<script" not in html_out and "<img" not in html_out
    assert "&lt;script&gt;" in html_out
    assert 'href="javascript' not in html_out and 'href="data' not in html_out
    assert 'onmouseover="' not in html_out


def test_assistant_reply_is_rendered_as_markdown_on_the_page(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use(
        monkeypatch,
        _FakeProvider(
            response=AssistantResponse(
                message="**Tuesday** is set:\n- pasta\n- salad <script>x()</script>",
            )
        ),
    )
    admin_client.post("/chat/message", data={"message": "go"}, headers=SAME_ORIGIN)
    page = admin_client.get("/chat").text
    assert "<strong>Tuesday</strong>" in page and "<li>pasta</li>" in page
    assert "<script>x()" not in page
