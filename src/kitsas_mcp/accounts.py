"""The chart of accounts. Account names live in Tili.json, not in a column."""

import json

from .constants import TILITYYPPI_OSTOVELAT, TILITYYPPI_PANKKI
from .errors import AccountNotFoundError

SELECT = "SELECT numero, tyyppi, json FROM Tili"


def _row_to_account(row) -> dict:
    try:
        data = json.loads(row["json"] or "{}")
    except json.JSONDecodeError:
        data = {}
    names = data.get("nimi") or {}
    return {
        "number": row["numero"],
        "name": names.get("fi") or names.get("sv") or "",
        "type": row["tyyppi"],
    }


def list_accounts(book, search=None) -> list[dict]:
    with book.connect_read() as conn:
        accounts = [_row_to_account(r) for r in conn.execute(f"{SELECT} ORDER BY numero")]
    if search:
        needle = str(search).lower()
        accounts = [a for a in accounts if needle in a["name"].lower() or str(a["number"]).startswith(needle)]
    return accounts


def get_account(book, number: int) -> dict:
    with book.connect_read() as conn:
        row = conn.execute(f"{SELECT} WHERE numero = ?", (number,)).fetchone()
    if row is None:
        raise AccountNotFoundError(f"Account {number} does not exist in this book.")
    return _row_to_account(row)


def _first_of_type(book, tyyppi: str, description: str) -> int:
    with book.connect_read() as conn:
        rows = conn.execute(f"{SELECT} WHERE tyyppi = ? ORDER BY numero", (tyyppi,)).fetchall()
    if len(rows) == 0:
        raise AccountNotFoundError(
            f"This book has no {description} (an account of type {tyyppi}). "
            "Pass the account number explicitly."
        )
    if len(rows) > 1:
        candidates = ", ".join(str(r["numero"]) for r in rows)
        raise AccountNotFoundError(
            f"This book has {len(rows)} {description}s ({candidates}). "
            "Pass the account number explicitly."
        )
    return rows[0]["numero"]


def default_bank_account(book) -> int:
    return _first_of_type(book, TILITYYPPI_PANKKI, "bank account")


def default_payable_account(book) -> int:
    return _first_of_type(book, TILITYYPPI_OSTOVELAT, "payables account")
