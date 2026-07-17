"""Model pricing in integer micro-USD (money stays integer, per CONVENTIONS 1).

Rates are Anthropic and OpenAI list prices per 1,000,000 tokens, expressed in micro-USD (so
$3.00/Mtok is 3_000_000). Prices change over time and are easy to update here; an unknown model
falls back to a deliberately high rate so a call's cost is never under-counted (a spend cap should
fail safe by over-estimating, never by silently counting zero).

Prefixes are matched with str.startswith, so a more specific id must precede a shorter one it
would otherwise shadow ("gpt-4o-mini" before "gpt-4o", "gpt-4.1-mini" before "gpt-4.1").
"""

from __future__ import annotations

# model-id prefix -> (input micro-USD per Mtok, output micro-USD per Mtok)
_PRICES: dict[str, tuple[int, int]] = {
    # Anthropic
    "claude-opus": (15_000_000, 75_000_000),
    "claude-sonnet": (3_000_000, 15_000_000),
    "claude-haiku": (800_000, 4_000_000),
    # OpenAI (specific ids first so startswith doesn't shadow them)
    "gpt-4o-mini": (150_000, 600_000),
    "gpt-4o": (2_500_000, 10_000_000),
    "gpt-4.1-nano": (100_000, 400_000),
    "gpt-4.1-mini": (400_000, 1_600_000),
    "gpt-4.1": (2_000_000, 8_000_000),
}
# Unknown model -> a high (Opus-class) rate so spend caps fail safe by over-estimating.
_DEFAULT: tuple[int, int] = (15_000_000, 75_000_000)


def _rate(model: str) -> tuple[int, int]:
    for prefix, rate in _PRICES.items():
        if model.startswith(prefix):
            return rate
    return _DEFAULT


def cost_micros(model: str, input_tokens: int, output_tokens: int) -> int:
    """Integer micro-USD cost of a call with the given token counts."""
    in_rate, out_rate = _rate(model)
    return (input_tokens * in_rate + output_tokens * out_rate) // 1_000_000
