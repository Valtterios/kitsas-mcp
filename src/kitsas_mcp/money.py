"""Money is integer cents. Floats never touch a stored amount."""

from decimal import Decimal, InvalidOperation

from .errors import AmountError

CENTS = Decimal("0.01")


def euros_to_cents(value) -> int:
    """Convert a euro amount to integer cents.

    Accepts a string (with an optional space thousands separator and a comma
    or dot decimal separator), an int, a float or a Decimal. Floats are routed
    through str() so that 1.15 becomes 115 rather than 114.
    """
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise AmountError(f"{value!r} is not an amount of money. Pass the amount as a string like '12.34'.") from None

    if amount != amount.quantize(CENTS):
        raise AmountError(f"{value!r} has more precision than one cent. Round it to two decimals first.")

    return int(amount.quantize(CENTS) * 100)


def cents_to_euros(cents: int) -> str:
    """Format integer cents as a plain euro string, for display only."""
    return f"{Decimal(cents) / 100:.2f}"
