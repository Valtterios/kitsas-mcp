"""What account has this supplier's spending been booked to before?"""

from .constants import TILA_KIRJANPIDOSSA, TOSITE_MENO
from .partners import resolve_partner

# suggest_account's caller can pass an id, so that is the way out of an
# ambiguity here.
AMBIGUITY_REMEDY = "Pass the partner id instead; find_supplier lists them."

SQL = """
SELECT v.tili AS tili,
       json_extract(ti.json, '$.nimi.fi') AS tilinimi,
       count(DISTINCT v.tosite) AS lkm,
       max(t.pvm) AS viimeksi
FROM Vienti v
JOIN Tosite t ON t.id = v.tosite
LEFT JOIN Tili ti ON ti.numero = v.tili
WHERE t.kumppani = ?
  AND t.tyyppi = ?
  AND t.tila >= ?
  AND v.debetsnt > 0
GROUP BY v.tili
ORDER BY lkm DESC, viimeksi DESC
"""


def _resolve_supplier_id(conn, supplier):
    """The partner id, by id or by name. The name rule is shared with write."""
    if isinstance(supplier, int):
        return supplier
    match = resolve_partner(conn, supplier, remedy=AMBIGUITY_REMEDY)
    return None if match is None else match.id


def suggest_account(book, supplier) -> list[dict]:
    """Expense accounts this supplier's past bills were booked to, most used first.

    Only debit rows on ledger vouchers of the purchase type are counted, so the
    bank or payables counter account never appears as a suggestion.

    `supplier` is a partner id or a partner name. An MCP client that has been
    told to "pass the partner id" can only send it as JSON, where it may arrive
    as the string "7", so a digit-only string is read as an id rather than as
    the name of a partner nobody has.
    """
    if isinstance(supplier, str):
        text = supplier.strip()
        if text.isascii() and text.isdigit():
            supplier = int(text)

    with book.connect_read() as conn:
        supplier_id = _resolve_supplier_id(conn, supplier)
        if supplier_id is None:
            return []
        rows = conn.execute(SQL, (supplier_id, TOSITE_MENO, TILA_KIRJANPIDOSSA)).fetchall()

    return [
        {
            "account": r["tili"],
            "account_name": r["tilinimi"],
            "count": r["lkm"],
            "last_used": r["viimeksi"],
        }
        for r in rows
    ]
