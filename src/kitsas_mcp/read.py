"""Read-only queries over a Kitsas book."""

import json
from datetime import date

from .constants import TILA_KIRJANPIDOSSA
from .dates import parse_iso_date
from .errors import LedgerVoucherError, NoFiscalYearError
from .money import cents_to_euros
from .partners import ESCAPE_CLAUSE, contains_pattern, folded, unaccented

# Every fiscal year carries "confirmed_unknown" beside "confirmed": true when
# Tilikausi.json could not be read as an object, so whether the year is
# confirmed is genuinely unknown rather than "no". An unreadable confirmation
# date must never read as unconfirmed, because that is exactly the field that
# stops a write into a closed year (see _check_fiscal_year in write.py):
# "confirmed": null on its own would silently reopen a fiscal year that might
# really be closed. It is a separate boolean rather than a word in
# "confirmed" so that "confirmed" stays what it says it is, an ISO date or
# null, for a client that compares it against other dates.
CONFIRMED_UNKNOWN_KEY = "confirmed_unknown"


def no_such_voucher_error(voucher_id: int) -> LedgerVoucherError:
    return LedgerVoucherError(
        f"There is no voucher {voucher_id} in this book. "
        "Use list_vouchers to find the id of the voucher you meant."
    )


def list_fiscal_years(book) -> list[dict]:
    today = date.today().isoformat()
    with book.connect_read() as conn:
        rows = conn.execute("SELECT alkaa, loppuu, json FROM Tilikausi ORDER BY alkaa").fetchall()
    years = []
    for row in rows:
        try:
            data = json.loads(row["json"] or "{}")
        except json.JSONDecodeError:
            data = None
        unknown = not isinstance(data, dict)
        years.append(
            {
                "starts": row["alkaa"],
                "ends": row["loppuu"],
                "confirmed": None if unknown else data.get("vahvistettu"),
                CONFIRMED_UNKNOWN_KEY: unknown,
                "current": row["alkaa"] <= today <= row["loppuu"],
            }
        )
    return years


def fiscal_year_for(book, when: str) -> dict:
    when = parse_iso_date(when, "when")
    for year in list_fiscal_years(book):
        if year["starts"] <= when <= year["ends"]:
            return year
    raise NoFiscalYearError(
        f"No fiscal year in this book covers {when}. Create the fiscal year in Kitsas first."
    )


# Built once, and outside the f-string in find_supplier: nesting the same
# quote character inside an f-string expression only became legal in Python
# 3.12, and this package supports 3.11.
_NAME = folded("k.nimi")
_UNACCENTED_NAME = unaccented("k.nimi")
_BUSINESS_ID = folded("coalesce(k.alvtunnus,'')")
_IBAN = folded("coalesce(i.iban,'')")
_TYPED = folded()
_UNACCENTED_TYPED = unaccented()
FIND_SUPPLIER_SQL = (
    "SELECT k.id, k.nimi, k.alvtunnus FROM Kumppani k "
    "LEFT JOIN KumppaniIban i ON i.kumppani = k.id "
    f"WHERE {_NAME} LIKE {_TYPED} {ESCAPE_CLAUSE} "
    f"OR {_UNACCENTED_NAME} LIKE {_UNACCENTED_TYPED} {ESCAPE_CLAUSE} "
    f"OR {_BUSINESS_ID} = {_TYPED} "
    f"OR replace({_IBAN},' ','') = replace({_TYPED},' ','') "
    "GROUP BY k.id ORDER BY k.nimi"
)

# Every IBAN of each partner found, read separately rather than off the join
# above: that join is filtered by the WHERE clause, so a search for one of a
# partner's IBANs would otherwise report that partner as having only that one.
PARTNER_IBANS_SQL = "SELECT kumppani, iban FROM KumppaniIban WHERE kumppani IN ({}) ORDER BY iban"


def find_supplier(book, query: str) -> list[dict]:
    """Every partner whose name contains the query, or whose business id or IBAN is it.

    A browsing tool: it returns a list, and finding several partners is a
    result, not an error. The name matching is the same rule partners.py
    resolves a single supplier with, so what this lists and what
    suggest_account and add_purchase_invoice pick are never at odds. That
    includes the trim: partners.resolve_partner strips the caller's text
    before matching, so without the strip here find_supplier("  Hetzner  ")
    found nothing where the other two resolved the partner, and the one
    tool a bookkeeper reaches for to check the other two disagreed with
    them. Both sides are case-folded by casefold(), not by SQLite's
    ASCII-only lower(); see partners.py.

    A name is also matched with its accents ignored, which the resolver does
    not do: searching 'Karkkainen' has to show the 'Kärkkäinen Lahti' that is
    already in the book, since the whole point of looking a supplier up before
    billing it is to find out whether it is there. This is the tool where
    showing more is right, because it decides nothing; both spellings are in
    the list, and which of them the bill belongs to stays the reader's call.

    Each partner's IBANs come back with it. Without them there was no way to
    see, before add_purchase_invoice refused the bill, that an IBAN read off
    an invoice already belongs to a different partner, even though this tool
    matches on IBANs.
    """
    query = str(query).strip()
    with book.connect_read() as conn:
        rows = conn.execute(
            FIND_SUPPLIER_SQL,
            (contains_pattern(query), contains_pattern(query), query, query),
        ).fetchall()
        ibans = {}
        if rows:
            placeholders = ",".join("?" * len(rows))
            for iban_row in conn.execute(
                PARTNER_IBANS_SQL.format(placeholders), [r["id"] for r in rows]
            ):
                ibans.setdefault(iban_row["kumppani"], []).append(iban_row["iban"])
    return [
        {
            "id": r["id"],
            "name": r["nimi"],
            "vat_id": r["alvtunnus"],
            "ibans": ibans.get(r["id"], []),
        }
        for r in rows
    ]


def list_vouchers(book, date_from, date_to, supplier=None, account=None, state=None) -> list[dict]:
    date_from = parse_iso_date(date_from, "date_from")
    date_to = parse_iso_date(date_to, "date_to")
    sql = [
        "SELECT t.id, t.pvm, t.tyyppi, t.tila, t.tunniste, t.otsikko, t.erapvm, k.nimi AS kumppani,",
        "       (SELECT max(coalesce(sum(debetsnt), 0), coalesce(sum(kreditsnt), 0)) "
        "        FROM Vienti WHERE tosite = t.id) AS summa",
        "FROM Tosite t LEFT JOIN Kumppani k ON k.id = t.kumppani",
        "WHERE t.pvm BETWEEN ? AND ?",
    ]
    params = [date_from, date_to]

    if state is None:
        sql.append("AND t.tila >= ?")
        params.append(TILA_KIRJANPIDOSSA)
    else:
        sql.append("AND t.tila = ?")
        params.append(state)

    if supplier is not None:
        sql.append("AND t.kumppani = ?")
        params.append(supplier)

    if account is not None:
        sql.append("AND EXISTS (SELECT 1 FROM Vienti v WHERE v.tosite = t.id AND v.tili = ?)")
        params.append(account)

    sql.append("ORDER BY t.pvm, t.id")

    with book.connect_read() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    return [
        {
            "id": r["id"],
            "date": r["pvm"],
            "type": r["tyyppi"],
            "state": r["tila"],
            "number": r["tunniste"],
            "title": r["otsikko"],
            "due_date": r["erapvm"],
            "supplier": r["kumppani"],
            "total": cents_to_euros(r["summa"] or 0),
        }
        for r in rows
    ]


def get_voucher(book, voucher_id: int):
    with book.connect_read() as conn:
        # The alias must not be "kumppani": t.* already brings a column of that
        # name (the partner id), and sqlite3.Row resolves a duplicated name to
        # the first of the two, so the partner's name would never be returned.
        header = conn.execute(
            "SELECT t.*, k.nimi AS kumppani_nimi FROM Tosite t "
            "LEFT JOIN Kumppani k ON k.id = t.kumppani WHERE t.id = ?",
            (voucher_id,),
        ).fetchone()
        if header is None:
            raise no_such_voucher_error(voucher_id)
        entries = conn.execute(
            "SELECT v.rivi, v.tyyppi, v.pvm, v.tili, v.selite, v.debetsnt, v.kreditsnt, "
            "       json_extract(ti.json, '$.nimi.fi') AS tilinimi "
            "FROM Vienti v LEFT JOIN Tili ti ON ti.numero = v.tili "
            "WHERE v.tosite = ? ORDER BY v.rivi",
            (voucher_id,),
        ).fetchall()
        attachments = conn.execute(
            "SELECT nimi, tyyppi, length(data) AS koko FROM Liite WHERE tosite = ? ORDER BY id",
            (voucher_id,),
        ).fetchall()

    return {
        "id": header["id"],
        "date": header["pvm"],
        "type": header["tyyppi"],
        "state": header["tila"],
        "number": header["tunniste"],
        "title": header["otsikko"],
        "supplier": header["kumppani_nimi"],
        "supplier_id": header["kumppani"],
        "invoice_date": header["laskupvm"],
        "due_date": header["erapvm"],
        "reference": header["viite"],
        "entries": [
            {
                "row": e["rivi"],
                "type": e["tyyppi"],
                "date": e["pvm"],
                "account": e["tili"],
                "account_name": e["tilinimi"],
                "description": e["selite"],
                "debit": cents_to_euros(e["debetsnt"] or 0),
                "credit": cents_to_euros(e["kreditsnt"] or 0),
            }
            for e in entries
        ],
        "attachments": [
            {"name": a["nimi"], "mime": a["tyyppi"], "bytes": a["koko"]} for a in attachments
        ],
    }
