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


def test_infinity_is_rejected_as_an_amount_error():
    # Decimal("Infinity") constructs happily and only fails in quantize, where
    # an unhandled InvalidOperation would reach the MCP client as a traceback.
    for value in ("Infinity", "-Infinity", "inf", "NaN"):
        with pytest.raises(AmountError):
            euros_to_cents(value)


def test_a_huge_exponent_is_rejected_as_an_amount_error():
    with pytest.raises(AmountError) as excinfo:
        euros_to_cents("1e1000")
    assert "1e1000" in str(excinfo.value)


def test_an_underscore_is_rejected_rather_than_read_as_a_separator():
    # Decimal("1_0") is ten. An invoice amount never looks like that.
    with pytest.raises(AmountError) as excinfo:
        euros_to_cents("1_0")
    assert "underscore" in str(excinfo.value)
    with pytest.raises(AmountError):
        euros_to_cents("1_000.00")
