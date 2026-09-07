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
        # Decimal accepts PEP 515 underscores, so "1_0" would be ten euros and
        # "1_000" a thousand. No invoice writes an amount that way; a string
        # with an underscore in it is a typo or a mangled field, not money.
        if "_" in text:
            raise AmountError(
                f"{value!r} has an underscore in it, which is not part of an amount. "
                "Pass the amount as a string like '12.34'."
            )
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise AmountError(f"{value!r} is not an amount of money. Pass the amount as a string like '12.34'.") from None

    # Infinity, NaN and an exponent past the Decimal context's precision all
    # construct without complaint and only fail here, in quantize. Left
    # unhandled that is a decimal.InvalidOperation traceback reaching the MCP
    # client instead of the {"error": ...} message every other bad amount gets.
    try:
        rounded = amount.quantize(CENTS)
    except InvalidOperation:
        raise AmountError(
            f"{value!r} is not a finite amount of money that can be written as cents. "
            "Pass the amount as a string like '12.34'."
        ) from None

    if amount != rounded:
        raise AmountError(f"{value!r} has more precision than one cent. Round it to two decimals first.")

    return int(rounded * 100)


def cents_to_euros(cents: int) -> str:
    """Format integer cents as a plain euro string, for display only."""
    return f"{Decimal(cents) / 100:.2f}"
