"""Which partner a name means. One answer, shared by searching, history and writing.

Three modules ask this question and they must not answer it differently. The
model is told to call suggest_account before add_purchase_invoice, so if
history resolves "Hetzner" to the partner "Hetzner Online GmbH" and the write
path does not, the voucher lands on a second partner of its own, carrying none
of the history the model was just shown, and the supplier list in the book
splits in two. The rule lives here so all three read it from the same place.

The rule: trim and case-fold both sides, take an exact name match if there is
one, otherwise a substring match. One match is the answer, several is
AmbiguousSupplierError, none is None. Never pick between real partners in
someone's books: which supplier a bill belongs to is the bookkeeper's call, not
this server's.

Callers differ only in what they do with each outcome. read.find_supplier wants
the whole candidate list, so it uses the pattern helpers and not the resolver.
history returns no suggestions for None; write creates the partner.
"""

from typing import NamedTuple

from .errors import AmbiguousSupplierError

# Backslash, the conventional LIKE escape. SQL sees ESCAPE '\'; a Python
# literal needs it doubled.
LIKE_ESCAPE = "\\"
ESCAPE_CLAUSE = "ESCAPE '\\'"

_EXACT_SQL = "SELECT id, nimi FROM Kumppani WHERE lower(trim(nimi)) = lower(trim(?)) ORDER BY id"
_LIKE_SQL = (
    f"SELECT id, nimi FROM Kumppani WHERE lower(trim(nimi)) LIKE lower(?) {ESCAPE_CLAUSE} "
    "ORDER BY id"
)


def escape_like(text) -> str:
    """Make a caller's text a literal inside a LIKE pattern.

    Without this, '%' from the caller matches every partner in the book and
    '_e_zner' matches 'Hetzner'. In a search that is noise; in the resolver it
    is a wrong partner's booking history offered as the account to book to.
    The escape character is escaped first, or escaping the wildcards would
    escape their new backslashes again.
    """
    return (
        str(text)
        .replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def contains_pattern(text) -> str:
    """A LIKE pattern matching any name that contains this text literally."""
    return f"%{escape_like(text)}%"


class PartnerMatch(NamedTuple):
    """A partner the name resolved to, and whether the name was its own.

    `exact` is False when the name only appeared inside the partner's name. A
    caller that is about to write a voucher should say so rather than silently
    attaching a bill to a partner spelled differently from what was typed.
    """

    id: int
    name: str
    exact: bool


def ambiguous(name, rows, remedy: str) -> AmbiguousSupplierError:
    """One message for every ambiguous match. The remedy depends on the caller."""
    names = ", ".join(f"{row['nimi']} (id {row['id']})" for row in rows)
    return AmbiguousSupplierError(
        f"{name!r} matches {len(rows)} partners in this book ({names}). {remedy}"
    )


def resolve_partner(conn, name, *, remedy: str) -> PartnerMatch | None:
    """The one partner this name means, or None if the book has no such partner.

    An exact match wins over substring matches, so a partner whose full name
    was typed is never ambiguous against the longer names it is contained in.
    An exact match can still be several partners, because the comparison is
    case and whitespace insensitive: 'Hetzner' and 'HETZNER' are two rows in
    the book and two different partners on paper, and picking the lower id
    would attribute one partner's history to the other.

    `remedy` is the sentence that tells this caller's user how to get past an
    ambiguity, because what they can do about it differs: suggest_account
    takes a partner id, add_purchase_invoice takes only a name.
    """
    text = str(name).strip()
    if not text:
        # An empty name is not a partner. Left to the LIKE it would match every
        # partner in the book and be reported as an ambiguity over names the
        # caller never typed.
        return None

    rows = conn.execute(_EXACT_SQL, (text,)).fetchall()
    if len(rows) > 1:
        raise ambiguous(text, rows, remedy)
    if len(rows) == 1:
        return PartnerMatch(rows[0]["id"], rows[0]["nimi"], True)

    rows = conn.execute(_LIKE_SQL, (contains_pattern(text),)).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ambiguous(text, rows, remedy)
    return PartnerMatch(rows[0]["id"], rows[0]["nimi"], False)
