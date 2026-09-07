"""Read-only queries over a Kitsas book."""

import json
from datetime import date

from .constants import TILA_KIRJANPIDOSSA
from .dates import parse_iso_date
from .errors import LedgerVoucherError, NoFiscalYearError
from .money import cents_to_euros
from .partners import ESCAPE_CLAUSE, contains_pattern

# Sentinel for list_fiscal_years' "confirmed" field when Tilikausi.json could
# not be read as an object: whether the year is confirmed is then genuinely
# unknown, not "no". A missing or unreadable confirmation date must never
# read as unconfirmed, because that is exactly the field that stops a write
# into a closed year (see _check_fiscal_year in write.py); reporting None
# here would silently reopen a fiscal year that might really be closed. The
# sentinel is truthy, so a caller that only tests `if year["confirmed"]`
# still refuses the write, and it is distinguishable from a real date so
# _check_fiscal_year can raise a message that names the actual cause.
FISCAL_YEAR_CONFIRMED_UNKNOWN = "unknown (unreadable fiscal year data)"


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
        if isinstance(data, dict):
            confirmed = data.get("vahvistettu")
        else:
            confirmed = FISCAL_YEAR_CONFIRMED_UNKNOWN
        years.append(
            {
                "starts": row["alkaa"],
                "ends": row["loppuu"],
                "confirmed": confirmed,
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


def find_supplier(book, query: str) -> list[dict]:
    """Every partner whose name contains the query, or whose business id or IBAN is it.

    A browsing tool: it returns a list, and finding several partners is a
    result, not an error. The name matching is the same rule partners.py
    resolves a single supplier with, so what this lists and what
    suggest_account and add_purchase_invoice pick are never at odds.
    """
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT k.id, k.nimi, k.alvtunnus FROM Kumppani k "
            "LEFT JOIN KumppaniIban i ON i.kumppani = k.id "
            f"WHERE lower(trim(k.nimi)) LIKE lower(?) {ESCAPE_CLAUSE} "
            "OR lower(coalesce(k.alvtunnus,'')) = lower(?) "
            "OR replace(lower(coalesce(i.iban,'')),' ','') = replace(lower(?),' ','') "
            "GROUP BY k.id ORDER BY k.nimi",
            (contains_pattern(query), query, query),
        ).fetchall()
    return [{"id": r["id"], "name": r["nimi"], "vat_id": r["alvtunnus"]} for r in rows]


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
