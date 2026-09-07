"""The only module that changes a book. Drafts only, one transaction, after a backup.

Everything written here lands at TILA_SAAPUNUT with no tunniste, which is what
Kitsas calls an incoming, unapproved document. Kitsas itself moves the voucher
into the ledger and allocates its number when a human approves it. This server
never does either, and never touches a voucher that is already in the ledger.
"""

import hashlib
import json
import mimetypes
from pathlib import Path

from .accounts import default_bank_account, get_account
from .constants import (
    TILA_KIRJANPIDOSSA,
    TILA_POISTETTU,
    TILA_SAAPUNUT,
    TOSITE_MENO,
    VIENTI_OSTO_KIRJAUS,
    VIENTI_OSTO_VASTAKIRJAUS,
)
from .errors import (
    ClosedFiscalYearError,
    KitsasError,
    LedgerVoucherError,
    UnbalancedVoucherError,
)
from .money import cents_to_euros, euros_to_cents
from .read import fiscal_year_for


def _check_fiscal_year(book, booking_date: str) -> None:
    year = fiscal_year_for(book, booking_date)
    if year["confirmed"]:
        raise ClosedFiscalYearError(
            f"The fiscal year {year['starts']} to {year['ends']} was confirmed on "
            f"{year['confirmed']}. Nothing may be added to it. Use a date in an open "
            "fiscal year, or unconfirm the year in Kitsas first."
        )


def _prepare_lines(book, lines, description, supplier_name) -> list[tuple]:
    """Validate every line before any connection is opened for writing."""
    if not lines:
        raise UnbalancedVoucherError(
            "A bill needs at least one expense line. Pass lines as, for example, "
            "[{'account': 4000, 'amount': '42.90'}]."
        )

    prepared = []
    for line in lines:
        try:
            account = line["account"]
        except (TypeError, KeyError):
            raise UnbalancedVoucherError(
                f"The expense line {line!r} has no 'account'. Every line needs an "
                "'account' number and an 'amount', for example "
                "{'account': 4000, 'amount': '42.90'}."
            ) from None
        if "amount" not in line:
            raise UnbalancedVoucherError(
                f"The expense line for account {account} has no 'amount'. Add it as a "
                "string like '42.90'."
            )

        get_account(book, account)  # raises AccountNotFoundError
        cents = euros_to_cents(line["amount"])  # raises AmountError
        if cents <= 0:
            raise UnbalancedVoucherError(
                f"The line for account {account} is {line['amount']}, which is not a "
                "positive amount. Split the bill so every line is a positive expense, "
                "or record a credit note in Kitsas instead."
            )
        prepared.append(
            (account, cents, line.get("description") or description or supplier_name)
        )
    return prepared


def _read_attachment(pdf_path: str) -> tuple[str, str, bytes]:
    """Read the attachment before the transaction opens, so a bad path costs nothing."""
    path = Path(pdf_path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise KitsasError(
            f"Could not read the attachment {path.name} at {path}: {exc}. "
            "Check the path and that the file is readable, then try again."
        ) from None
    if not data:
        raise KitsasError(
            f"The attachment {path.name} is empty. Attach the real invoice file, "
            "or leave pdf_path out."
        )
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, mime, data


def _upsert_supplier(conn, name: str, business_id, iban) -> int:
    row = conn.execute(
        "SELECT id FROM Kumppani WHERE lower(nimi) = lower(?) ORDER BY id LIMIT 1", (name,)
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO Kumppani (nimi, alvtunnus, json) VALUES (?,?,?)",
            (name, business_id, "{}"),
        )
        supplier_id = cursor.lastrowid
    else:
        supplier_id = row["id"]
        if business_id:
            conn.execute(
                "UPDATE Kumppani SET alvtunnus = ? WHERE id = ? AND coalesce(alvtunnus,'') = ''",
                (business_id, supplier_id),
            )

    if iban:
        normalised = str(iban).replace(" ", "").replace("\xa0", "").upper()
        conn.execute(
            "INSERT INTO KumppaniIban (iban, kumppani) VALUES (?,?) "
            "ON CONFLICT (iban) DO UPDATE SET kumppani = excluded.kumppani",
            (normalised, supplier_id),
        )
    return supplier_id


def _attach(conn, voucher_id: int, attachment: tuple) -> dict:
    name, mime, data = attachment
    conn.execute(
        "INSERT INTO Liite (tosite, nimi, roolinimi, tyyppi, sha, data) VALUES (?,?,NULL,?,?,?)",
        (voucher_id, name, mime, hashlib.sha256(data).hexdigest(), data),
    )
    return {"name": name, "mime": mime, "bytes": len(data)}


def _verify_draft(conn, voucher_id: int) -> None:
    """Read the voucher back out of the database before the commit.

    The code's own arithmetic is not evidence. Tosite.tila defaults to 100 in
    the Kitsas schema, so a single missing column in an INSERT would put the
    voucher straight into the ledger; this is the check that catches that.
    """
    header = conn.execute(
        "SELECT tila, tunniste FROM Tosite WHERE id = ?", (voucher_id,)
    ).fetchone()
    if header is None or header["tila"] != TILA_SAAPUNUT or header["tunniste"] is not None:
        raise LedgerVoucherError(
            f"Voucher {voucher_id} did not come back as an unnumbered draft "
            f"(state {None if header is None else header['tila']}, number "
            f"{None if header is None else header['tunniste']}). Nothing was written. "
            "This is a bug in kitsas-mcp; report it rather than working around it."
        )

    debit, credit = conn.execute(
        "SELECT coalesce(sum(debetsnt), 0), coalesce(sum(kreditsnt), 0) "
        "FROM Vienti WHERE tosite = ?",
        (voucher_id,),
    ).fetchone()
    if debit != credit:
        raise UnbalancedVoucherError(
            f"Debits {cents_to_euros(debit)} do not equal credits {cents_to_euros(credit)}. "
            "Nothing was written. Check that the expense lines add up to the invoice total."
        )


def add_purchase_invoice(
    book,
    *,
    supplier_name: str,
    lines: list,
    booking_date: str,
    business_id=None,
    iban=None,
    invoice_date=None,
    due_date=None,
    reference=None,
    description=None,
    credit_account=None,
    pdf_path=None,
) -> dict:
    """Create a purchase invoice as a draft. Kitsas approves it into the ledger."""
    _check_fiscal_year(book, booking_date)

    prepared = _prepare_lines(book, lines, description, supplier_name)
    total = sum(cents for _, cents, _ in prepared)

    counter_account = credit_account if credit_account is not None else default_bank_account(book)
    get_account(book, counter_account)

    attachment_source = _read_attachment(pdf_path) if pdf_path else None
    title = description or supplier_name

    with book.connect_write() as conn:
        supplier_id = _upsert_supplier(conn, supplier_name, business_id, iban)

        cursor = conn.execute(
            "INSERT INTO Tosite (pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm, "
            "erapvm, viite, json) VALUES (?,?,?,NULL,?,?,?,?,?,'{}')",
            (
                booking_date,
                TOSITE_MENO,
                TILA_SAAPUNUT,
                title,
                supplier_id,
                invoice_date,
                due_date,
                reference,
            ),
        )
        voucher_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, "
            "kreditsnt, alvkoodi, kumppani, json) VALUES (1,?,?,?,?,0,?,0,?,0,?,'{}')",
            (voucher_id, VIENTI_OSTO_VASTAKIRJAUS, booking_date, counter_account, title, total, supplier_id),
        )

        for row_number, (account, cents, line_description) in enumerate(prepared, start=2):
            conn.execute(
                "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, "
                "debetsnt, kreditsnt, alvkoodi, kumppani, json) VALUES (?,?,?,?,?,0,?,?,0,0,?,'{}')",
                (
                    row_number,
                    voucher_id,
                    VIENTI_OSTO_KIRJAUS,
                    booking_date,
                    account,
                    line_description,
                    cents,
                    supplier_id,
                ),
            )

        _verify_draft(conn, voucher_id)

        attachment = _attach(conn, voucher_id, attachment_source) if attachment_source else None

        conn.execute(
            "INSERT INTO Tositeloki (tosite, tila, data) VALUES (?,?,?)",
            (
                voucher_id,
                TILA_SAAPUNUT,
                json.dumps(
                    {
                        "source": "kitsas-mcp",
                        "supplier": supplier_name,
                        "booking_date": booking_date,
                        "lines": [
                            {"account": a, "cents": c, "description": d} for a, c, d in prepared
                        ],
                        "credit_account": counter_account,
                    }
                ),
            ),
        )

    return {
        "voucher_id": voucher_id,
        "total": cents_to_euros(total),
        "credit_account": counter_account,
        "lines": [{"account": a, "amount": cents_to_euros(c)} for a, c, _ in prepared],
        "attachment": attachment,
        "backup": str(book.backup()),
        "summary": (
            f"Draft voucher {voucher_id} for {supplier_name}, {cents_to_euros(total)} euros on "
            f"{booking_date}, credited to account {counter_account}. It is not in the ledger; "
            "open Kitsas to check and approve it."
        ),
    }


def _check_deletable(conn, voucher_id: int) -> None:
    row = conn.execute("SELECT tila FROM Tosite WHERE id = ?", (voucher_id,)).fetchone()
    if row is None:
        raise LedgerVoucherError(
            f"There is no voucher {voucher_id} in this book. "
            "Use list_vouchers to find the id of the voucher you meant."
        )
    if row["tila"] >= TILA_KIRJANPIDOSSA:
        raise LedgerVoucherError(
            f"Voucher {voucher_id} is already in the ledger and cannot be deleted here. "
            "Booked history is read-only through this server; do it in Kitsas if you "
            "really mean to."
        )


def delete_draft(book, voucher_id: int) -> dict:
    """Mark a draft deleted, as Kitsas does. Refuses anything already in the ledger."""
    # Checked read-only first so that refusing costs no backup, then again
    # inside the write transaction so the decision cannot go stale.
    with book.connect_read() as conn:
        _check_deletable(conn, voucher_id)

    with book.connect_write() as conn:
        _check_deletable(conn, voucher_id)
        conn.execute("UPDATE Tosite SET tila = ? WHERE id = ?", (TILA_POISTETTU, voucher_id))
        conn.execute(
            "INSERT INTO Tositeloki (tosite, tila) VALUES (?,?)", (voucher_id, TILA_POISTETTU)
        )

    return {
        "voucher_id": voucher_id,
        "summary": (
            f"Draft voucher {voucher_id} is marked deleted. Its rows stay in the file, "
            "as they do when Kitsas deletes a draft."
        ),
    }
