"""AI extraction: pricing math, spend caps, and the Anthropic adapter's parse/validate logic.

The provider is exercised with a fake client (the tool_use message shape Claude returns), so no
API key or network is involved - only our parsing, validation, and cost accounting.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from app import ai, config
from app.ai import pricing
from app.ai import usage as ai_usage
from app.ai.anthropic_provider import AnthropicExtractor
from app.ai.base import AIError

_VALID_PAYLOAD: dict[str, Any] = {
    "title": "AI Soup",
    "ingredients": [{"original_text": "2 cups water"}, {"original_text": "1 tbsp salt"}],
    "steps": [{"instruction": "Boil the water."}, {"instruction": "Stir in the salt."}],
}


class _Block:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.type = "tool_use"
        self.name = "save_recipe"
        self.input = payload


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Message:
    def __init__(self, content: list[Any], usage: _Usage) -> None:
        self.content = content
        self.usage = usage


class _Client:
    def __init__(self, message: _Message) -> None:
        self._message = message
        self.messages = self

    def create(self, **_: Any) -> _Message:
        return self._message


# ---- pricing --------------------------------------------------------------------------


def test_pricing_known_and_unknown_models() -> None:
    assert pricing.cost_micros("claude-sonnet-5-5", 1_000_000, 1_000_000) == 12_000_000
    assert pricing.cost_micros("claude-sonnet-5", 1_000_000, 0) == 2_000_000
    assert pricing.cost_micros("claude-sonnet-4-6", 1_000_000, 0) == 3_000_000
    assert pricing.cost_micros("claude-opus-4-8", 0, 1_000_000) == 25_000_000
    assert pricing.cost_micros("claude-opus-5-5", 1_000_000, 1_000_000) == 24_000_000
    assert pricing.cost_micros("claude-opus-3-legacy", 1_000_000, 0) == 15_000_000
    assert pricing.cost_micros("claude-haiku-4-5", 1_000_000, 1_000_000) == 6_000_000
    # OpenAI: the specific prefix must win over the shorter one it would otherwise shadow.
    assert pricing.cost_micros("gpt-4o-mini", 1_000_000, 0) == 150_000
    assert pricing.cost_micros("gpt-4o", 1_000_000, 0) == 2_500_000
    # unknown model falls back to a high rate so spend caps over-estimate, never zero
    assert pricing.cost_micros("mystery-model", 1_000_000, 0) == 15_000_000


# ---- spend caps -----------------------------------------------------------------------


def test_within_budget_flips_when_daily_cap_reached(migrated_db: sqlite3.Connection) -> None:
    settings = config.get_settings()  # default daily cap $1.00 = 1_000_000 micro-USD
    assert ai_usage.within_budget(migrated_db, settings) is True
    ai_usage.log_usage(
        migrated_db, provider="anthropic", model="m", operation="extract_web",
        job_id=None, cost_micros=1_000_000, status="ok",
    )
    assert ai_usage.within_budget(migrated_db, settings) is False


# ---- provider parsing -----------------------------------------------------------------


def test_extract_parses_tool_use_and_prices_it() -> None:
    client = _Client(_Message([_Block(_VALID_PAYLOAD)], _Usage(1200, 300)))
    result = AnthropicExtractor(client, "claude-sonnet-5").extract("text", source_url="https://x/t")
    assert result.recipe.title == "AI Soup"
    assert len(result.recipe.ingredients) == 2
    assert result.input_tokens == 1200
    assert result.output_tokens == 300
    assert result.cost_micros == 1200 * 2 + 300 * 10  # sonnet 5 rates, micro-USD


def test_draft_uses_the_same_tool_path() -> None:
    client = _Client(_Message([_Block(_VALID_PAYLOAD)], _Usage(400, 100)))
    result = AnthropicExtractor(client, "claude-sonnet-5").draft("grandma's soup, simmer an hour")
    assert result.recipe.title == "AI Soup"
    assert result.provider == "anthropic"
    assert result.input_tokens == 400


def test_extract_without_tool_call_raises() -> None:
    client = _Client(_Message([], _Usage(10, 0)))
    with pytest.raises(AIError):
        AnthropicExtractor(client, "claude-sonnet-5").extract("t", source_url="u")


def test_extract_invalid_payload_records_billed_cost() -> None:
    client = _Client(_Message([_Block({"ingredients": []})], _Usage(1000, 200)))  # no title
    with pytest.raises(AIError) as exc:
        AnthropicExtractor(client, "claude-sonnet-5").extract("t", source_url="u")
    # the call was billed even though parsing failed -> cost is carried for the spend cap
    assert exc.value.cost_micros > 0


def test_extract_wraps_client_errors() -> None:
    class _Raises:
        def create(self, **_: Any) -> Any:
            raise RuntimeError("boom")

    class _BadClient:
        messages = _Raises()

    with pytest.raises(AIError):
        AnthropicExtractor(_BadClient(), "claude-sonnet-5").extract("t", source_url="u")


# ---- provider selection ---------------------------------------------------------------


def test_provider_disabled_without_api_key(data_dir: Path) -> None:
    settings = config.get_settings()
    assert settings.ai_enabled is False
    assert ai.get_provider(settings) is None


# ---- per-task model selection ---------------------------------------------------------


def test_model_defaults_are_fast_and_strong_tiers(data_dir: Path) -> None:
    settings = config.get_settings()
    assert settings.anthropic_model == "claude-sonnet-5-5"
    assert settings.anthropic_model_fast == "claude-haiku-4-5"
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.openai_model_fast == "gpt-4o-mini"


def test_model_env_overrides_and_blank_falls_back(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("RC_ANTHROPIC_MODEL_FAST", "claude-sonnet-5-5")
    monkeypatch.setenv("RC_OPENAI_MODEL", "strong-x")
    monkeypatch.setenv("RC_OPENAI_MODEL_FAST", "   ")  # blank -> default
    config.reset_settings_cache()
    settings = config.get_settings()
    assert (settings.anthropic_model, settings.anthropic_model_fast) == (
        "claude-opus-5-5", "claude-sonnet-5-5",
    )
    assert settings.openai_model == "strong-x"
    assert settings.openai_model_fast == "gpt-4o-mini"


def test_for_task_swaps_only_the_fast_tier(data_dir: Path) -> None:
    settings = config.get_settings()
    fast = settings.for_task(config.TASK_FAST)
    assert fast.anthropic_model == settings.anthropic_model_fast
    assert fast.openai_model == settings.openai_model_fast
    assert settings.for_task(config.TASK_STRONG) is settings
    assert settings.anthropic_model == "claude-sonnet-5-5"  # the original is untouched


def test_get_provider_builds_the_tiered_anthropic_model(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_ANTHROPIC_API_KEY", "sk-ant-test")
    config.reset_settings_cache()
    settings = config.get_settings()
    fast = ai.get_provider(settings.for_task(config.TASK_FAST))
    strong = ai.get_provider(settings.for_task(config.TASK_STRONG))
    assert fast is not None and strong is not None
    assert fast.model == "claude-haiku-4-5"
    assert strong.model == "claude-sonnet-5-5"


def test_each_operation_asks_for_its_tier(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Extraction/tagging/receipts/text drafts run fast; assistant and photo drafts run strong."""
    from app.services import ai_draft, assistant, receipts, recipes, tagging
    from app.services.units import seed_core_units

    monkeypatch.setenv("RC_ANTHROPIC_MODEL", "strong-model")
    monkeypatch.setenv("RC_ANTHROPIC_MODEL_FAST", "fast-model")
    config.reset_settings_cache()
    seen: list[str] = []

    def _record(settings: config.Settings) -> None:
        seen.append(settings.anthropic_model)
        return None

    monkeypatch.setattr("app.ai.get_provider", _record)
    seed_core_units(migrated_db)
    rid = recipes.create_recipe(
        migrated_db,
        recipes.RecipeInput(
            title="T", base_servings="4", tags=[],
            ingredients=[recipes.IngredientInput(quantity_text="1", unit="each", food="egg")],
        ),
    )

    tagging.suggest_tags(migrated_db, rid)
    receipts.capture(migrated_db, text="x")
    ai_draft.draft_from_description(migrated_db, "soup")
    assert seen == ["fast-model"] * 3

    seen.clear()
    ai_draft.draft_from_photo(migrated_db, b"jpeg")
    conv = assistant.start_conversation(migrated_db)
    assistant.ask(migrated_db, conv, "plan")
    assert seen == ["strong-model"] * 2


def test_pipeline_extraction_asks_for_the_fast_tier() -> None:
    import inspect

    from app.services import pipeline

    assert "for_task(TASK_FAST)" in inspect.getsource(pipeline._ai_extract_and_apply)
