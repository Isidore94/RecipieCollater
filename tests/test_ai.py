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
    assert pricing.cost_micros("claude-sonnet-5", 1_000_000, 0) == 3_000_000
    assert pricing.cost_micros("claude-opus-4-8", 0, 1_000_000) == 75_000_000
    assert pricing.cost_micros("claude-haiku-4-5", 1_000_000, 0) == 800_000
    # unknown model falls back to Sonnet-class pricing (never zero)
    assert pricing.cost_micros("mystery-model", 1_000_000, 0) == 3_000_000


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
    assert result.cost_micros == 1200 * 3 + 300 * 15  # sonnet rates, micro-USD


def test_extract_without_tool_call_raises() -> None:
    client = _Client(_Message([], _Usage(10, 0)))
    with pytest.raises(AIError):
        AnthropicExtractor(client, "claude-sonnet-5").extract("t", source_url="u")


def test_extract_invalid_payload_raises() -> None:
    client = _Client(_Message([_Block({"ingredients": []})], _Usage(1, 1)))  # no title
    with pytest.raises(AIError):
        AnthropicExtractor(client, "claude-sonnet-5").extract("t", source_url="u")


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
