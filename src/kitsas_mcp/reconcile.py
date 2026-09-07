"""Checking the book's bank account against a real bank statement.

Neither target book has ever imported a bank statement, so nothing has
reconciled the ledger's bank account against the bank. Every automated bill
asserts a payment; this is how a wrong one gets found.
"""

from .accounts import account_by_number, accounts_on, default_bank_account_of
from .constants import TILA_KIRJANPIDOSSA, TILITYYPPI_PANKKI
from .dates import parse_iso_date
from .errors import NotABankAccountError
from .money import cents_to_euros


def _resolve_account(conn, account):
    """An account passed explicitly is checked against Tili, and that it is a bank account.

    Takes the connection the caller already opened for the balance query
    itself, so resolving the account (explicit or default) needs no
    connection of its own: the chart is read once here and both questions,
    "is this account real and a bank account" and "which is the default
    bank account", are answered from that one list.

    Both bank_balance and bank_movements advertise themselves as reporting
    on the bank account, for checking against a real bank statement. Any
    other account number that happens to exist (an expense account, a
    payables account) would still produce a number, but not a bank balance,
    so it is refused here rather than silently answered.
    """
    chart = accounts_on(conn)
    if account is not None:
        found = account_by_number(chart, account)  # raises AccountNotFoundError for a bad number
        if found["type"] != TILITYYPPI_PANKKI:
            raise NotABankAccountError(
                f"Account {account} ({found['name'] or 'unnamed'}) is type {found['type']!r}, "
                f"not a bank account (type {TILITYYPPI_PANKKI!r}). Pass the number of an actual "
                "bank account, or omit account to use the book's default bank account."
            )
        return account
    return default_bank_account_of(chart)


def _balance_cents(conn, account: int, on_date: str) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(v.debetsnt), 0) - coalesce(sum(v.kreditsnt), 0) "
        "FROM Vienti v JOIN Tosite t ON t.id = v.tosite "
        "WHERE v.tili = ? AND v.pvm <= ? AND t.tila >= ?",
        (account, on_date, TILA_KIRJANPIDOSSA),
    ).fetchone()
    return row[0] or 0


def bank_balance(book, on_date: str, account=None) -> dict:
    on_date = parse_iso_date(on_date, "on_date")
    with book.connect_read() as conn:
        account = _resolve_account(conn, account)
        cents = _balance_cents(conn, account, on_date)
    return {"account": account, "date": on_date, "balance": cents_to_euros(cents)}


def bank_movements(book, date_from: str, date_to: str, account=None) -> list[dict]:
    date_from = parse_iso_date(date_from, "date_from")
    date_to = parse_iso_date(date_to, "date_to")
    with book.connect_read() as conn:
        account = _resolve_account(conn, account)
        opening_date = _day_before(date_from)
        opening = _balance_cents(conn, account, opening_date) if opening_date is not None else 0
        rows = conn.execute(
            "SELECT v.pvm, v.tosite, v.selite, v.debetsnt, v.kreditsnt, k.nimi AS kumppani "
            "FROM Vienti v JOIN Tosite t ON t.id = v.tosite "
            "LEFT JOIN Kumppani k ON k.id = v.kumppani "
            "WHERE v.tili = ? AND v.pvm BETWEEN ? AND ? AND t.tila >= ? "
            "ORDER BY v.pvm, v.tosite, v.rivi",
            (account, date_from, date_to, TILA_KIRJANPIDOSSA),
        ).fetchall()

    running = opening
    movements = []
    for row in rows:
        amount = (row["debetsnt"] or 0) - (row["kreditsnt"] or 0)
        running += amount
        movements.append(
            {
                "date": row["pvm"],
                "voucher_id": row["tosite"],
                "counterparty": row["kumppani"],
                "description": row["selite"],
                "amount": cents_to_euros(amount),
                "running_balance": cents_to_euros(running),
            }
        )
    return movements


def _day_before(when: str) -> str | None:
    """The day before `when`, or None if `when` is already the minimum date.

    `when` has already passed parse_iso_date by the time this is called, so
    date.fromisoformat here cannot raise ValueError. The one case that can
    still fail is date.min itself ("0001-01-01"): there is no day before it,
    and stepping back one day raises OverflowError. There being no prior
    date means trivially no prior movement, so callers treat None as an
    opening balance of zero rather than as an error.
    """
    from datetime import date, timedelta

    try:
        return (date.fromisoformat(when) - timedelta(days=1)).isoformat()
    except OverflowError:
        return None
