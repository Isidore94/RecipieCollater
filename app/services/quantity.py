"""Exact quantity math - the scaling/pantry/shopping foundation (CONVENTIONS section 1).

Every amount from a human, an AI provider, or an imported file enters as a *string* and is
parsed with ``decimal.Decimal``; a quantity is never passed through ``float``. Canonical
storage is integer micro-units - milligrams (mass), microlitres (volume), milli-each (count) -
so unit factors stay exact. Rounding is explicit and centralised here, never incidental.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction

# Canonical integer micro-unit per dimension (docs/03 section 3).
CANONICAL_MICRO_UNIT: dict[str, str] = {
    "mass": "milligram",
    "volume": "microlitre",
    "count": "milli-each",
}

# Kitchen-friendly fraction denominators (docs/03 section 4: halves, thirds, quarters, eighths).
_FRACTION_DENOMINATORS: tuple[int, ...] = (2, 3, 4, 8)
_FRACTION_TOLERANCE = Fraction(1, 100)

VALID_SCALING_MODES: frozenset[str] = frozenset(
    {"linear", "fixed", "to_taste", "round_to_package"}
)


class QuantityError(ValueError):
    """A quantity string could not be parsed as a non-negative exact amount."""


def parse_quantity(text: str) -> Decimal:
    """Parse an amount ('2', '2.5', '1/2', '1 1/2') into an exact, non-negative Decimal."""
    raw = " ".join(text.split())  # collapse and trim whitespace
    if not raw:
        raise QuantityError("empty quantity")
    try:
        whole_text, _, frac_text = raw.partition(" ")
        if frac_text:
            if "/" not in frac_text:
                raise QuantityError(f"not a mixed number: {text!r}")
            value = _decimal(whole_text) + _fraction(frac_text)
        elif "/" in raw:
            value = _fraction(raw)
        else:
            value = _decimal(raw)
    except (InvalidOperation, QuantityError) as exc:
        raise QuantityError(f"could not parse quantity: {text!r}") from exc
    if value < 0:
        raise QuantityError(f"quantity must not be negative: {text!r}")
    return value


def _decimal(token: str) -> Decimal:
    return Decimal(token)  # raises InvalidOperation on garbage


def _fraction(token: str) -> Decimal:
    num_text, sep, den_text = token.partition("/")
    if not sep:
        return _decimal(num_text)
    denominator = _decimal(den_text)
    if denominator == 0:
        raise QuantityError("division by zero in fraction")
    return _decimal(num_text) / denominator


def scale(
    value: Decimal, *, factor: Decimal, mode: str, package: Decimal | None = None
) -> Decimal | None:
    """Scale a base quantity by a serving ``factor`` per the ingredient's scaling mode.

    ``factor`` is target_servings / base_servings as a Decimal. Returns ``None`` for
    ``to_taste`` (the caller renders the original wording instead of a number).
    """
    if mode not in VALID_SCALING_MODES:
        raise QuantityError(f"unknown scaling mode: {mode!r}")
    if mode == "to_taste":
        return None
    if mode == "fixed":
        return value
    scaled = value * factor
    if mode == "round_to_package":
        if package is None or package <= 0:
            raise QuantityError("round_to_package requires a positive package size")
        return (scaled / package).to_integral_value(rounding=ROUND_CEILING) * package
    return scaled  # linear


def to_canonical(value: Decimal, factor_micro: int) -> int:
    """Convert an exact quantity to canonical integer micro-units (half-up rounding)."""
    if factor_micro <= 0:
        raise QuantityError("unit has no exact canonical factor (approximate unit?)")
    return int((value * factor_micro).to_integral_value(rounding=ROUND_HALF_UP))


def from_canonical(micro: int, factor_micro: int) -> Decimal:
    """Convert canonical integer micro-units back to an exact quantity in the unit."""
    if factor_micro <= 0:
        raise QuantityError("unit has no exact canonical factor (approximate unit?)")
    return Decimal(micro) / Decimal(factor_micro)


def convert(value: Decimal, from_factor_micro: int, to_factor_micro: int) -> Decimal:
    """Convert a quantity between two units of the SAME dimension via canonical micro-units."""
    return from_canonical(to_canonical(value, from_factor_micro), to_factor_micro)


def format_quantity(value: Decimal) -> str:
    """Render an exact quantity with kitchen fractions (1/2, 1/3, 3/8, ...) when close."""
    if value < 0:
        return "-" + format_quantity(-value)
    target = Fraction(value)
    best_error: Fraction | None = None
    best = target
    for denominator in _FRACTION_DENOMINATORS:
        candidate = Fraction(round(target * denominator), denominator)
        error = abs(candidate - target)
        if best_error is None or error < best_error:
            best_error, best = error, candidate
    if best_error is None or best_error > _FRACTION_TOLERANCE:
        return _plain_decimal(value)
    whole = best.numerator // best.denominator
    remainder = best - whole
    if remainder == 0:
        return str(whole)
    fraction_text = f"{remainder.numerator}/{remainder.denominator}"
    return f"{whole} {fraction_text}" if whole else fraction_text


def _plain_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")
