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


def accounts_on(conn) -> list[dict]:
    """The whole chart of accounts, on a connection the caller already has open.

    One read, and every question below is then answered against the list it
    returns. There used to be three spellings of "which account is this"
    (by number, on a connection, over a loaded chart) and three of "which
    account is the default of this type", differing only in where the rows
    came from. Splitting the read from the two questions leaves one helper
    per question, so an error message can only be written once.
    """
    return [_row_to_account(r) for r in conn.execute(f"{SELECT} ORDER BY numero")]


def list_accounts(book, search=None) -> list[dict]:
    with book.connect_read() as conn:
        accounts = accounts_on(conn)
    if search:
        # casefold, not lower: the account names in a Finnish book are
        # Finnish, and this is the same fold the partner name matching uses.
        needle = str(search).casefold()
        accounts = [
            a for a in accounts
            if needle in a["name"].casefold() or str(a["number"]).startswith(needle)
        ]
    return accounts


def account_by_number(accounts: list[dict], number: int) -> dict:
    """Which account is `number`, in a chart already read by accounts_on or list_accounts."""
    for account in accounts:
        if account["number"] == number:
            return account
    raise AccountNotFoundError(f"Account {number} does not exist in this book.")


def only_account_of_type(accounts: list[dict], tyyppi: str, description: str) -> int:
    """The book's one account of this type, or an error naming what to do instead."""
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
    return only_account_of_type(accounts, TILITYYPPI_PANKKI, "bank account")


def default_payable_account_of(accounts: list[dict]) -> int:
    return only_account_of_type(accounts, TILITYYPPI_OSTOVELAT, "payables account")
