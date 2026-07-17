"""Exact-quantity math tests (CONVENTIONS section 1 - the highest-care area).

Example-based plus property-style loops asserting the invariants the roadmap calls out:
exact scaling with no binary-float drift, kitchen-fraction round-trips, canonical-integer
reversibility, and package rounding.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services import quantity as q


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2", "2"),
        ("2.5", "2.5"),
        ("1/2", "0.5"),
        ("3/4", "0.75"),
        ("1 1/2", "1.5"),
        ("10 3/8", "10.375"),
        ("  2  ", "2"),
        ("0", "0"),
    ],
)
def test_parse_valid(text: str, expected: str) -> None:
    assert q.parse_quantity(text) == Decimal(expected)


@pytest.mark.parametrize("bad", ["", "abc", "1/0", "-1", "1 2", "1/", "/2", "1 1", "1 1/", "x/2"])
def test_parse_invalid(bad: str) -> None:
    with pytest.raises(q.QuantityError):
        q.parse_quantity(bad)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2", "2"),
        ("0", "0"),
        ("1.5", "1 1/2"),
        ("0.5", "1/2"),
        ("0.25", "1/4"),
        ("0.75", "3/4"),
        ("0.375", "3/8"),
        ("2.25", "2 1/4"),
        ("0.7", "0.7"),  # not a kitchen fraction -> decimal fallback
    ],
)
def test_format(value: str, expected: str) -> None:
    assert q.format_quantity(Decimal(value)) == expected


def test_format_recognises_repeating_thirds() -> None:
    third = Decimal(1) / Decimal(3)
    assert q.format_quantity(third) == "1/3"
    assert q.format_quantity(Decimal(2) / Decimal(3)) == "2/3"
    assert q.format_quantity(Decimal(1) + third) == "1 1/3"


def test_format_parse_roundtrip_for_kitchen_fractions() -> None:
    fractions = [(1, 2), (1, 3), (2, 3), (1, 4), (3, 4), (1, 8), (3, 8), (5, 8), (7, 8)]
    for whole in range(0, 5):
        for numerator, denominator in fractions:
            value = Decimal(whole) + Decimal(numerator) / Decimal(denominator)
            rendered = q.format_quantity(value)
            # Re-parsing the rendered text lands back within a tolerance far tighter than any
            # cooking amount cares about.
            assert abs(q.parse_quantity(rendered) - value) <= Decimal("0.005")


def test_scale_linear_4_to_6_is_exact() -> None:
    factor = Decimal(6) / Decimal(4)  # 1.5 - the roadmap's exit-criterion scale
    assert q.scale(q.parse_quantity("2"), factor=factor, mode="linear") == Decimal("3")
    assert q.scale(q.parse_quantity("1 1/2"), factor=factor, mode="linear") == Decimal("2.25")


def test_scale_thirds_read_correctly_even_though_decimal_is_inexact() -> None:
    # 1/3 is not exactly representable in Decimal, so 1/3 * 3 == 0.999...9. What the cook sees
    # (the rendered fraction) and what the math stores (canonical integer) are still exact.
    tripled = q.scale(q.parse_quantity("1/3"), factor=Decimal(3), mode="linear")
    assert tripled is not None
    assert q.format_quantity(tripled) == "1"
    assert q.to_canonical(tripled, 236_588) == q.to_canonical(Decimal("1"), 236_588)


def test_scale_fixed_and_to_taste() -> None:
    assert q.scale(Decimal("3"), factor=Decimal(2), mode="fixed") == Decimal("3")
    assert q.scale(Decimal("1"), factor=Decimal(2), mode="to_taste") is None


def test_scale_identity_factor_is_a_noop() -> None:
    for text in ["2", "0.5", "1 1/2", "2.25", "10 3/8"]:
        value = q.parse_quantity(text)
        assert q.scale(value, factor=Decimal(1), mode="linear") == value


def test_round_to_package_rounds_up() -> None:
    def pkg(value: str, factor: str, package: str) -> Decimal | None:
        return q.scale(
            Decimal(value),
            factor=Decimal(factor),
            mode="round_to_package",
            package=Decimal(package),
        )

    assert pkg("1", "2", "1") == Decimal("2")  # 1 can doubled -> 2 whole cans
    assert pkg("1", "1.5", "1") == Decimal("2")  # 1.5 cans -> rounds up to 2 whole cans
    assert pkg("1", "1.5", "0.5") == Decimal("1.5")  # already on a half-can boundary


def test_round_to_package_requires_a_size() -> None:
    with pytest.raises(q.QuantityError):
        q.scale(Decimal("1"), factor=Decimal(2), mode="round_to_package")


def test_unknown_scaling_mode_raises() -> None:
    with pytest.raises(q.QuantityError):
        q.scale(Decimal("1"), factor=Decimal(1), mode="bogus")


def test_canonical_roundtrip_within_one_micro_unit() -> None:
    cup_microlitres = 236_588  # 1 US cup
    gram_milligrams = 1_000
    for factor in (cup_microlitres, gram_milligrams):
        for text in ["1", "2", "0.5", "1 1/2", "2.25", "0.375"]:
            value = q.parse_quantity(text)
            micro = q.to_canonical(value, factor)
            assert isinstance(micro, int)
            assert abs(q.from_canonical(micro, factor) - value) <= Decimal(1) / Decimal(factor)


def test_convert_same_dimension() -> None:
    tbsp = 14_787  # microlitres in 1 US tablespoon
    cup = 236_588
    # 1 cup == 16 tablespoons (within rounding).
    assert q.convert(Decimal("1"), cup, tbsp).to_integral_value() == Decimal("16")


def test_to_canonical_rejects_approximate_unit() -> None:
    with pytest.raises(q.QuantityError):
        q.to_canonical(Decimal("1"), 0)


def test_everything_stays_decimal_never_float() -> None:
    value = q.parse_quantity("1 1/2")
    assert isinstance(value, Decimal)
    scaled = q.scale(value, factor=Decimal("1.5"), mode="linear")
    assert isinstance(scaled, Decimal)
    assert isinstance(q.from_canonical(q.to_canonical(value, 1_000), 1_000), Decimal)
