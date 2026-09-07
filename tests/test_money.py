import pytest

from kitsas_mcp.errors import AmountError
from kitsas_mcp.money import cents_to_euros, euros_to_cents


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12.34", 1234),
        ("0.01", 1),
        ("1000", 100000),
        ("428.38", 42838),
        ("0.1", 10),
        ("1 234,56", 123456),
        ("-5.00", -500),
        (12.34, 1234),
        (0, 0),
    ],
)
def test_euros_to_cents(value, expected):
    assert euros_to_cents(value) == expected


def test_float_that_breaks_naive_conversion():
    # int(1.15 * 100) is 114 because 1.15 is not representable in binary.
    assert euros_to_cents("1.15") == 115
    assert euros_to_cents(1.15) == 115


def test_more_than_two_decimals_is_rejected():
    with pytest.raises(AmountError):
        euros_to_cents("1.234")


def test_non_numeric_is_rejected():
    with pytest.raises(AmountError):
        euros_to_cents("about ten euros")


@pytest.mark.parametrize("cents,expected", [(1234, "12.34"), (1, "0.01"), (0, "0.00"), (-500, "-5.00")])
def test_cents_to_euros(cents, expected):
    assert cents_to_euros(cents) == expected


def test_non_breaking_space_thousands_separator():
    assert euros_to_cents("1\xa0234,56") == 123456


def test_accepts_a_decimal():
    from decimal import Decimal

    assert euros_to_cents(Decimal("42.90")) == 4290
