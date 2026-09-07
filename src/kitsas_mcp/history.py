"""What account has this supplier's spending been booked to before?"""

from .constants import TILA_KIRJANPIDOSSA, TOSITE_MENO
from .errors import AmbiguousSupplierError

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
    if isinstance(supplier, int):
        return supplier
    row = conn.execute(
        "SELECT id FROM Kumppani WHERE lower(nimi) = lower(?) ORDER BY id LIMIT 1", (supplier,)
    ).fetchone()
    if row is not None:
        return row["id"]
    rows = conn.execute(
        "SELECT id, nimi FROM Kumppani WHERE lower(nimi) LIKE lower(?) ORDER BY id",
        (f"%{supplier}%",),
    ).fetchall()
    if len(rows) == 0:
        return None
    if len(rows) > 1:
        names = ", ".join(r["nimi"] for r in rows)
        raise AmbiguousSupplierError(
            f"{supplier!r} matches {len(rows)} partners in this book ({names}). "
            f"Pass the partner id instead; find_supplier lists them."
        )
    return rows[0]["id"]


def suggest_account(book, supplier) -> list[dict]:
    """Expense accounts this supplier's past bills were booked to, most used first.

    Only debit rows on ledger vouchers of the purchase type are counted, so the
    bank or payables counter account never appears as a suggestion.
    """
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
