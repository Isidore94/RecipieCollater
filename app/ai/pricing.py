"""Model pricing in integer micro-USD (money stays integer, per CONVENTIONS 1).

Rates are Anthropic list prices per 1,000,000 tokens, expressed in micro-USD (so $3.00/Mtok is
3_000_000). Prices change over time and are easy to update here; an unknown model falls back to
Sonnet-class pricing so a call's cost is never silently counted as zero.
"""

from __future__ import annotations

# model-id prefix -> (input micro-USD per Mtok, output micro-USD per Mtok)
_PRICES: dict[str, tuple[int, int]] = {
    "claude-opus": (15_000_000, 75_000_000),
    "claude-sonnet": (3_000_000, 15_000_000),
    "claude-haiku": (800_000, 4_000_000),
}
_DEFAULT: tuple[int, int] = (3_000_000, 15_000_000)


def _rate(model: str) -> tuple[int, int]:
    for prefix, rate in _PRICES.items():
        if model.startswith(prefix):
            return rate
    return _DEFAULT


def cost_micros(model: str, input_tokens: int, output_tokens: int) -> int:
    """Integer micro-USD cost of a call with the given token counts."""
    in_rate, out_rate = _rate(model)
    return (input_tokens * in_rate + output_tokens * out_rate) // 1_000_000
