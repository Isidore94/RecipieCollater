"""OpenAI adapter: parse/validate/cost from a fake chat-completions response, and provider
selection. No API key or network - the fake client mimics the tool_calls shape the SDK returns."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app import ai, config
from app.ai.base import AIError
from app.ai.openai_provider import OpenAIExtractor

_VALID_PAYLOAD: dict[str, Any] = {
    "title": "AI Stew",
    "ingredients": [{"original_text": "3 potatoes"}, {"original_text": "1 onion"}],
    "steps": [{"instruction": "Chop everything."}, {"instruction": "Simmer for an hour."}],
}


class _Function:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, name: str, arguments: str) -> None:
        self.function = _Function(name, arguments)


class _Message:
    def __init__(self, tool_calls: list[Any]) -> None:
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, message: _Message) -> None:
        self.message = message


class _Usage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _Response:
    def __init__(self, choices: list[Any], usage: _Usage) -> None:
        self.choices = choices
        self.usage = usage


class _Client:
    """Mimics client.chat.completions.create(...)."""

    def __init__(self, response: _Response) -> None:
        self._response = response
        self.chat = self
        self.completions = self

    def create(self, **_: Any) -> _Response:
        return self._response


def _client_returning(
    payload: dict[str, Any] | str, prompt: int = 1000, completion: int = 250
) -> _Client:
    arguments = payload if isinstance(payload, str) else json.dumps(payload)
    message = _Message([_ToolCall("save_recipe", arguments)])
    return _Client(_Response([_Choice(message)], _Usage(prompt, completion)))


def test_extract_parses_function_call_and_prices_it() -> None:
    result = OpenAIExtractor(_client_returning(_VALID_PAYLOAD), "gpt-4o-mini").extract(
        "text", source_url="https://x/t"
    )
    assert result.recipe.title == "AI Stew"
    assert len(result.recipe.ingredients) == 2
    assert result.provider == "openai"
    assert result.input_tokens == 1000
    assert result.output_tokens == 250
    # gpt-4o-mini: (1000*150000 + 250*600000)//1_000_000
    assert result.cost_micros == (1000 * 150_000 + 250 * 600_000) // 1_000_000


def test_draft_uses_the_same_tool_path() -> None:
    result = OpenAIExtractor(_client_returning(_VALID_PAYLOAD), "gpt-4o-mini").draft("chili recipe")
    assert result.recipe.title == "AI Stew"
    assert result.provider == "openai"


def test_extract_without_tool_call_raises() -> None:
    client = _Client(_Response([_Choice(_Message([]))], _Usage(5, 0)))
    with pytest.raises(AIError):
        OpenAIExtractor(client, "gpt-4o-mini").extract("t", source_url="u")


def test_extract_with_malformed_arguments_raises() -> None:
    with pytest.raises(AIError):
        OpenAIExtractor(_client_returning("{not json"), "gpt-4o-mini").extract("t", source_url="u")


def test_extract_invalid_payload_records_billed_cost() -> None:
    with pytest.raises(AIError) as exc:
        OpenAIExtractor(_client_returning({"ingredients": []}), "gpt-4o-mini").extract(
            "t", source_url="u"
        )  # no title
    # the call was billed even though parsing failed -> cost is carried for the spend cap
    assert exc.value.cost_micros > 0


def test_extract_wraps_client_errors() -> None:
    class _Boom:
        def create(self, **_: Any) -> Any:
            raise RuntimeError("boom")

    class _BoomClient:
        def __init__(self) -> None:
            self.chat = self
            self.completions = _Boom()

    with pytest.raises(AIError):
        OpenAIExtractor(_BoomClient(), "gpt-4o-mini").extract("t", source_url="u")


def test_get_provider_selects_openai(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RC_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("RC_AI_PROVIDER", "openai")
    config.reset_settings_cache()
    provider = ai.get_provider(config.get_settings())
    assert provider is not None
    assert provider.provider == "openai"


def test_auto_uses_the_only_configured_key(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RC_OPENAI_API_KEY", "sk-test")  # only OpenAI configured; provider=auto
    config.reset_settings_cache()
    provider = ai.get_provider(config.get_settings())
    assert provider is not None and provider.provider == "openai"


def test_forced_openai_without_key_disables(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_AI_PROVIDER", "openai")  # forced, but no OpenAI key
    config.reset_settings_cache()
    assert ai.get_provider(config.get_settings()) is None
