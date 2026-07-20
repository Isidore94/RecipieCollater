"""Receipt capture (Phase 4.7): adapters, generalization/alias learning, review, apply.

All offline (CONVENTIONS 15): providers are fakes; the "photo" is a Pillow-generated JPEG.
"""

from __future__ import annotations

import io
import sqlite3
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest
from fastapi.testclient import TestClient

from app.ai.anthropic_provider import AnthropicExtractor
from app.ai.base import AIReceipt
from app.ai.openai_provider import OpenAIExtractor
from app.extraction import ExtractedReceipt, ExtractedReceiptItem
from app.services import pantry, receipts, recipes, shopping
from app.services.units import seed_core_units
from tests.conftest import SAME_ORIGIN


def _jpeg_bytes() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (40, 60), (250, 245, 235)).save(out, format="JPEG")
    return out.getvalue()


# --------------------------------------------------------------------------------------
# Adapter-level: forced save_receipt tool + image content shape
# --------------------------------------------------------------------------------------


class _AnthropicFake:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.messages = self

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs

        class _Usage:
            input_tokens, output_tokens = 100, 50

        class _Block:
            type, name = "tool_use", "save_receipt"
            input: ClassVar[dict[str, Any]] = {
                "items": [{"original_text": "KS ORG BLK BNS", "food": "black beans"}]
            }

        class _Message:
            usage, content = _Usage(), [_Block()]

        return _Message()


def test_anthropic_receipt_sends_image_block() -> None:
    fake = _AnthropicFake()
    result = AnthropicExtractor(fake, "claude-sonnet-5").receipt(
        "HOUSEHOLD FOODS: black beans", image_jpeg=b"\xff\xd8fakejpeg"
    )
    assert isinstance(result, AIReceipt)
    assert result.receipt.items[0].food == "black beans"
    content = fake.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert fake.kwargs["tool_choice"] == {"type": "tool", "name": "save_receipt"}


class _OpenAIFake:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.chat = self
        self.completions = self

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs

        class _Fn:
            name = "save_receipt"
            arguments = '{"items": [{"original_text": "GV 2% MILK", "food": "milk"}]}'

        class _Call:
            function = _Fn()

        class _Msg:
            tool_calls: ClassVar[list[Any]] = [_Call()]

        class _Choice:
            message = _Msg()

        class _Usage:
            prompt_tokens, completion_tokens = 80, 40

        class _Resp:
            choices, usage = [_Choice()], _Usage()

        return _Resp()


def test_openai_receipt_sends_data_url() -> None:
    fake = _OpenAIFake()
    result = OpenAIExtractor(fake, "gpt-4o-mini").receipt(
        "HOUSEHOLD FOODS: milk", image_jpeg=b"\xff\xd8fakejpeg"
    )
    assert result.receipt.items[0].food == "milk"
    parts = fake.kwargs["messages"][1]["content"]
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert fake.kwargs["tool_choice"]["function"]["name"] == "save_receipt"


def test_text_receipt_stays_plain_string() -> None:
    fake = _OpenAIFake()
    OpenAIExtractor(fake, "gpt-4o-mini").receipt("ORDER TEXT:\n2% milk")
    assert isinstance(fake.kwargs["messages"][1]["content"], str)


# --------------------------------------------------------------------------------------
# Service-level: capture -> review -> apply, with a fake provider
# --------------------------------------------------------------------------------------


@dataclass
class _FakeProvider:
    provider: str = "fake"
    model: str = "fake-1"
    items: list[ExtractedReceiptItem] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)

    def receipt(self, content: str, *, image_jpeg: bytes | None = None) -> AIReceipt:
        self.prompts.append(content)
        return AIReceipt(
            receipt=ExtractedReceipt(items=self.items), provider=self.provider,
            model=self.model, input_tokens=10, output_tokens=5, cost_micros=1,
        )

    def extract(self, content: str, *, source_url: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def draft(self, description: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def _use_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    monkeypatch.setattr("app.ai.get_provider", lambda settings: provider)


def test_capture_matches_known_foods_and_flags_new(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(display_name="Black beans", location_id=loc, food="black beans"),
    )
    provider = _FakeProvider(items=[
        ExtractedReceiptItem(original_text="KS ORG BLK BNS 8CT", food="black beans",
                             quantity_text="2", size_text="15 oz"),
        ExtractedReceiptItem(original_text="XYZ MYSTERY SNACK", food="mystery snack"),
    ])
    _use_provider(monkeypatch, provider)

    result = receipts.capture(migrated_db, text="pasted order")
    assert result.receipt_id is not None and result.item_count == 2
    assert "HOUSEHOLD FOODS" in provider.prompts[0]
    assert "black beans" in provider.prompts[0]  # the household vocabulary anchors the model

    data = receipts.review(migrated_db, result.receipt_id)
    beans = next(line for line in data.lines if line.food_name == "black beans")
    assert beans.is_new_food is False
    assert beans.action_text == "mark full"  # gauge pantry item
    mystery = next(line for line in data.lines if "mystery" in line.food_name)
    assert mystery.is_new_food is True


def test_apply_restocks_learns_and_checks_off(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    rice_item = pantry.add_item(
        migrated_db,
        pantry.PantryItemInput(
            display_name="Rice", location_id=loc, quantity_mode="exact",
            food="rice", quantity_text="100", unit="grams",
        ),
    )
    # a shopping line for rice that the purchase should check off
    rid = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="Fried rice", base_servings="4",
            ingredients=[recipes.IngredientInput(quantity_text="900", unit="grams", food="rice")],
        ),
    )
    lst = shopping.active_list(migrated_db)
    shopping.add_from_recipe(migrated_db, lst, rid)

    provider = _FakeProvider(items=[
        ExtractedReceiptItem(original_text="JASMINE RICE", food="rice",
                             quantity_text="1", size_text="2 kg"),
        ExtractedReceiptItem(original_text="KS ORG CHKPEAS", food="chickpeas"),
    ])
    _use_provider(monkeypatch, provider)
    captured = receipts.capture(migrated_db, text="order")
    assert captured.receipt_id is not None

    summary = receipts.apply(
        migrated_db, captured.receipt_id,
        included_line_ids={line.line_id for line in receipts.review(
            migrated_db, captured.receipt_id).lines},
        food_names={}, track_location_id=loc,
    )
    assert any("Rice" in s for s in summary)

    rice = pantry.get_item(migrated_db, rice_item)
    assert rice is not None and rice.quantity_text == "2100"  # 100 g + 1 x 2 kg
    # chickpeas: new food created confirmed + tracked gauge-full
    row = migrated_db.execute("SELECT id, status FROM foods WHERE name = 'chickpeas'").fetchone()
    assert row is not None and row["status"] == "confirmed"
    tracked = pantry.items_for_food(migrated_db, int(row["id"]))
    assert tracked and tracked[0].gauge == "full"
    # the store's phrasing was learned as an alias
    alias = migrated_db.execute(
        "SELECT food_id FROM food_aliases WHERE alias = 'KS ORG CHKPEAS'"
    ).fetchone()
    assert alias is not None and alias["food_id"] == row["id"]
    # rice got checked off the shopping list
    assert all(i.checked for i in shopping.list_items(migrated_db, lst) if i.food_id)
    # idempotent: a re-POST applies nothing
    assert receipts.apply(
        migrated_db, captured.receipt_id, included_line_ids={1}, food_names={},
        track_location_id=loc,
    ) == []


def test_apply_per_line_location_overrides_receipt_default(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    pantry_loc = pantry.create_location(migrated_db, "Pantry")
    freezer = pantry.create_location(migrated_db, "Freezer", is_freezer=True)
    provider = _FakeProvider(items=[
        ExtractedReceiptItem(original_text="KS ORG CHKPEAS", food="chickpeas"),
        ExtractedReceiptItem(original_text="GV PEAS FROZEN", food="frozen peas"),
    ])
    _use_provider(monkeypatch, provider)
    captured = receipts.capture(migrated_db, text="order")
    assert captured.receipt_id is not None
    lines = receipts.review(migrated_db, captured.receipt_id).lines
    peas_line = next(li.line_id for li in lines if li.food_name == "frozen peas")

    # default is the pantry; the frozen-peas line is overridden to the freezer
    receipts.apply(
        migrated_db, captured.receipt_id,
        included_line_ids={li.line_id for li in lines}, food_names={},
        track_location_id=pantry_loc, line_locations={peas_line: freezer},
    )

    chickpeas = pantry.items_for_food(
        migrated_db, _food_id_by_name(migrated_db, "chickpeas")
    )
    peas = pantry.items_for_food(migrated_db, _food_id_by_name(migrated_db, "frozen peas"))
    assert chickpeas and chickpeas[0].location_id == pantry_loc
    assert peas and peas[0].location_id == freezer


def _food_id_by_name(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM foods WHERE name = ?", (name,)).fetchone()
    assert row is not None
    return int(row["id"])


def test_learned_alias_matches_next_receipt_deterministically(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    loc = pantry.create_location(migrated_db, "Pantry")
    provider = _FakeProvider(items=[
        ExtractedReceiptItem(original_text="KS ORG BLK BNS", food="organic black beans canned"),
    ])
    _use_provider(monkeypatch, provider)
    first = receipts.capture(migrated_db, text="trip 1")
    assert first.receipt_id is not None
    # the user corrects the model's over-specific name to the household word at apply time
    line_id = receipts.review(migrated_db, first.receipt_id).lines[0].line_id
    receipts.apply(
        migrated_db, first.receipt_id, included_line_ids={line_id},
        food_names={line_id: "black beans"}, track_location_id=loc,
    )
    # trip 2: same store text, model now proposes something else entirely - the ALIAS wins
    provider.items = [
        ExtractedReceiptItem(original_text="KS ORG BLK BNS", food="beans, black, organic"),
    ]
    second = receipts.capture(migrated_db, text="trip 2")
    assert second.receipt_id is not None
    line = receipts.review(migrated_db, second.receipt_id).lines[0]
    assert line.is_new_food is False
    assert line.food_name == "black beans"  # deterministic, learned, household vocabulary


def test_capture_photo_stores_normalized_jpeg(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    provider = _FakeProvider(items=[ExtractedReceiptItem(original_text="EGGS", food="eggs")])
    _use_provider(monkeypatch, provider)
    result = receipts.capture(migrated_db, image=_jpeg_bytes())
    assert result.receipt_id is not None
    stored = get_settings().data_dir / "receipts" / f"{result.receipt_id}.jpg"
    assert stored.is_file() and stored.stat().st_size > 0
    row = migrated_db.execute(
        "SELECT source, image_path FROM receipts WHERE id = ?", (result.receipt_id,)
    ).fetchone()
    assert row["source"] == "photo" and row["image_path"] == f"{result.receipt_id}.jpg"


def test_capture_without_provider_or_content(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ai.get_provider", lambda settings: None)
    assert receipts.capture(migrated_db, text="x").error is not None
    provider = _FakeProvider()
    _use_provider(monkeypatch, provider)
    assert receipts.capture(migrated_db).error is not None  # nothing supplied


def test_receipt_routes_roundtrip(
    admin_client: TestClient, migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_core_units(migrated_db)
    pantry.create_location(migrated_db, "Pantry")
    provider = _FakeProvider(items=[
        ExtractedReceiptItem(original_text="BREAD WW", food="bread"),
    ])
    _use_provider(monkeypatch, provider)

    assert admin_client.get("/receipts/new").status_code == 200
    resp = admin_client.post(
        "/receipts", data={"order_text": "1 Whole Wheat Bread"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert resp.status_code == 303
    review_url = resp.headers["location"]
    page = admin_client.get(review_url)
    assert page.status_code == 200 and "BREAD WW" in page.text

    receipt_id = int(review_url.rstrip("/").split("/")[-1])
    line_id = receipts.review(migrated_db, receipt_id).lines[0].line_id
    apply_resp = admin_client.post(
        f"/receipts/{receipt_id}/apply",
        data={"line": str(line_id), f"food_{line_id}": "bread",
              "track_location": "1"},
        headers=SAME_ORIGIN, follow_redirects=False,
    )
    assert apply_resp.status_code == 303 and "/pantry" in apply_resp.headers["location"]
    row = migrated_db.execute("SELECT status FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    assert row["status"] == "applied"
