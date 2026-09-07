"""Validating the ISO date strings this server accepts from a caller.

Every date argument this server takes ends up compared, as plain text,
against `Vienti.pvm` or `Tosite.pvm`, which are already zero-padded ISO text
(YYYY-MM-DD). A caller-supplied date that is not also zero-padded, such as
"2026-2-28", compares wrong under `<=` or `BETWEEN` without ever raising:
"2026-2-28" sorts after "2026-03-06" as a string, because "2" sorts after
"0". This module is the single point where every such date is checked
before it reaches SQL.
"""

from datetime import date

from .errors import DateFormatError


def parse_iso_date(value, field: str) -> str:
    """Validate a YYYY-MM-DD date and return it normalised for use in SQL.

    `date.fromisoformat` (Python 3.11+) accepts more than plain YYYY-MM-DD:
    it also accepts the basic format ("20260228") and ISO week dates
    ("2026-W09-2"). Comparing the parsed date's own isoformat() back against
    the original text rejects all of those, along with anything
    date.fromisoformat rejects outright, so only an already zero-padded
    YYYY-MM-DD string is ever accepted. A bare ValueError from
    date.fromisoformat never escapes; it becomes a DateFormatError instead.
    """
    text = str(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != text:
        raise DateFormatError(
            f"{field} is {value!r}, which is not a valid date. "
            "Use a zero-padded date like 2026-03-06."
        )
    return parsed.isoformat()
