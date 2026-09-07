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
    # Valid JSON is not necessarily an object: `null` parses to None, and a
    # list or a bare number parses too. Either would make the next .get()
    # raise AttributeError, so anything that is not a dict is treated the
    # same as unreadable json. The same applies one level down: "nimi" is
    # supposed to be an object of language codes, but a scanned-in value
    # could just as well be a plain string.
    if not isinstance(data, dict):
        data = {}
    names = data.get("nimi")
    if not isinstance(names, dict):
        names = {}
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


def account_by_number(accounts: list[dict], number: int) -> dict:
    """Find `number` in a chart of accounts already loaded, e.g. by list_accounts.

    Same result, and the same error, as get_account, but without a query of
    its own: for validating several account numbers against one chart read
    once, such as add_purchase_invoice's expense lines.
    """
    for account in accounts:
        if account["number"] == number:
            return account
    raise AccountNotFoundError(f"Account {number} does not exist in this book.")


def get_account(book, number: int) -> dict:
    with book.connect_read() as conn:
        row = conn.execute(f"{SELECT} WHERE numero = ?", (number,)).fetchone()
    if row is None:
        raise AccountNotFoundError(f"Account {number} does not exist in this book.")
    return _row_to_account(row)


def account_on(conn, number: int) -> dict:
    """Same result as get_account, on a connection the caller already has open.

    For a caller, such as reconcile.py, that already opened a read
    connection for the query the account number feeds into: validating the
    number needs no connection of its own.
    """
    row = conn.execute(f"{SELECT} WHERE numero = ?", (number,)).fetchone()
    if row is None:
        raise AccountNotFoundError(f"Account {number} does not exist in this book.")
    return _row_to_account(row)


def _select_of_type(accounts: list[dict], tyyppi: str, description: str) -> int:
    matches = [a for a in accounts if a["type"] == tyyppi]
    if len(matches) == 0:
        raise AccountNotFoundError(
            f"This book has no {description} (an account of type {tyyppi}). "
            "Pass the account number explicitly."
        )
    if len(matches) > 1:
        candidates = ", ".join(str(a["number"]) for a in matches)
        raise AccountNotFoundError(
            f"This book has {len(matches)} {description}s ({candidates}). "
            "Pass the account number explicitly."
        )
    return matches[0]["number"]


def default_bank_account_of(accounts: list[dict]) -> int:
    """Same rule as default_bank_account, over a chart already loaded."""
    return _select_of_type(accounts, TILITYYPPI_PANKKI, "bank account")


def _first_of_type(book, tyyppi: str, description: str) -> int:
    return _select_of_type(list_accounts(book), tyyppi, description)


def default_bank_account(book) -> int:
    return _first_of_type(book, TILITYYPPI_PANKKI, "bank account")


def default_bank_account_on(conn) -> int:
    """Same result as default_bank_account, on a connection the caller already has open."""
    rows = conn.execute(f"{SELECT} WHERE tyyppi = ? ORDER BY numero", (TILITYYPPI_PANKKI,)).fetchall()
    return _select_of_type([_row_to_account(r) for r in rows], TILITYYPPI_PANKKI, "bank account")


def default_payable_account(book) -> int:
    return _first_of_type(book, TILITYYPPI_OSTOVELAT, "payables account")
