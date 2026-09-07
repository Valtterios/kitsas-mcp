"""The only module that changes a book. Drafts only, one transaction, after a backup.

Everything written here lands at TILA_SAAPUNUT with no tunniste, which is what
Kitsas calls an incoming, unapproved document. Kitsas itself moves the voucher
into the ledger and allocates its number when a human approves it. This server
never does either, and never touches a voucher that is already in the ledger.

Nothing outside the voucher being created is ever rewritten. A partner may be
created, and a blank business id filled in, but an existing IBAN binding is
never re-pointed: this module refuses instead.
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
    AmbiguousSupplierError,
    AmountError,
    ClosedFiscalYearError,
    KitsasError,
    LedgerVoucherError,
    LineFormatError,
    UnbalancedVoucherError,
)
from .money import cents_to_euros, euros_to_cents
from .read import fiscal_year_for

# An invoice scan is a few hundred kilobytes. Anything past this is a wrong
# path, and it would be copied into every future backup of the book forever.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

# Module level so a test can substitute a deliberately broken variant and prove
# that _verify_draft catches it. See test_write.py.
TOSITE_INSERT = (
    "INSERT INTO Tosite (pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm, "
    "erapvm, viite, json) VALUES (?,?,?,NULL,?,?,?,?,?,'{}')"
)


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
            raise LineFormatError(
                f"The expense line {line!r} has no 'account'. Every line needs an "
                "'account' number and an 'amount', for example "
                "{'account': 4000, 'amount': '42.90'}."
            ) from None
        if "amount" not in line:
            raise LineFormatError(
                f"The expense line for account {account} has no 'amount'. Add it as a "
                "string like '42.90'."
            )

        get_account(book, account)  # raises AccountNotFoundError
        cents = euros_to_cents(line["amount"])  # raises AmountError
        if cents <= 0:
            raise AmountError(
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
        size = path.stat().st_size
    except OSError as exc:
        raise KitsasError(
            f"Could not read the attachment {path.name} at {path}: {exc}. "
            "Check the path and that the file is readable, then try again."
        ) from None
    if size == 0:
        raise KitsasError(
            f"The attachment {path.name} is empty. Attach the real invoice file, "
            "or leave pdf_path out."
        )
    # Checked from the directory entry, so an enormous file is never read at all.
    if size > MAX_ATTACHMENT_BYTES:
        raise KitsasError(
            f"The attachment {path.name} is {size} bytes, over the "
            f"{MAX_ATTACHMENT_BYTES} byte limit. It would be stored inside the book "
            "and copied into every future backup. Attach a smaller scan of the "
            "invoice, or leave pdf_path out."
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise KitsasError(
            f"Could not read the attachment {path.name} at {path}: {exc}. "
            "Check the path and that the file is readable, then try again."
        ) from None
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.name, mime, data


def _normalise_iban(iban) -> str:
    return str(iban).replace(" ", "").replace("\xa0", "").upper()


def _find_supplier_id(conn, name: str) -> int | None:
    """Resolve a partner by name, refusing to choose between real alternatives."""
    rows = conn.execute(
        "SELECT id, nimi FROM Kumppani WHERE lower(trim(nimi)) = lower(?) ORDER BY id",
        (name,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        names = ", ".join(f"{r['nimi']!r} (id {r['id']})" for r in rows)
        raise AmbiguousSupplierError(
            f"{name!r} matches {len(rows)} partners in this book that differ only by "
            f"case or spacing: {names}. Merge or rename them in Kitsas, or pass a name "
            "that matches exactly one of them."
        )
    return rows[0]["id"]


def _bind_iban(conn, supplier_id: int, supplier_name: str, iban) -> None:
    """Bind an IBAN to this partner. Never re-point one that belongs elsewhere.

    A KumppaniIban row is pre-existing book data outside the voucher being
    written. Silently reassigning one would, for a single mistyped digit, move
    the tax authority's bank account onto whatever supplier is being invoiced.
    """
    normalised = _normalise_iban(iban)
    owner = conn.execute(
        "SELECT i.kumppani, k.nimi FROM KumppaniIban i "
        "LEFT JOIN Kumppani k ON k.id = i.kumppani WHERE i.iban = ?",
        (normalised,),
    ).fetchone()

    if owner is not None:
        if owner["kumppani"] == supplier_id:
            return  # Already bound to this partner. Nothing to do.
        raise KitsasError(
            f"IBAN {normalised} already belongs to {owner['nimi']!r} in this book, not "
            f"to {supplier_name!r}. Check the IBAN, or move it in Kitsas if it really "
            "has changed hands."
        )

    conn.execute(
        "INSERT INTO KumppaniIban (iban, kumppani) VALUES (?,?)", (normalised, supplier_id)
    )


def _upsert_supplier(conn, name: str, business_id, iban) -> int:
    supplier_id = _find_supplier_id(conn, name)
    if supplier_id is None:
        cursor = conn.execute(
            "INSERT INTO Kumppani (nimi, alvtunnus, json) VALUES (?,?,?)",
            (name, business_id, "{}"),
        )
        supplier_id = cursor.lastrowid
    elif business_id:
        conn.execute(
            "UPDATE Kumppani SET alvtunnus = ? WHERE id = ? AND coalesce(alvtunnus,'') = ''",
            (business_id, supplier_id),
        )

    if iban:
        _bind_iban(conn, supplier_id, name, iban)
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
    # debit > 0 as well as debit == credit, so that an empty voucher, which
    # satisfies 0 == 0, cannot pass this check on its own.
    if debit <= 0 or debit != credit:
        raise UnbalancedVoucherError(
            f"Debits {cents_to_euros(debit)} do not equal credits {cents_to_euros(credit)}, "
            "or the voucher has no expense at all. Nothing was written. Check that the "
            "expense lines add up to the invoice total."
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

    supplier_name = str(supplier_name).strip()
    if not supplier_name:
        raise LineFormatError(
            "The supplier name is empty. Pass the supplier's name as it should appear "
            "on the voucher, for example 'Telia Finland Oyj'."
        )

    prepared = _prepare_lines(book, lines, description, supplier_name)
    total = sum(cents for _, cents, _ in prepared)

    counter_account = credit_account if credit_account is not None else default_bank_account(book)
    get_account(book, counter_account)

    attachment_source = _read_attachment(pdf_path) if pdf_path else None
    title = description or supplier_name

    with book.connect_write() as conn:
        supplier_id = _upsert_supplier(conn, supplier_name, business_id, iban)

        cursor = conn.execute(
            TOSITE_INSERT,
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
        # The tila predicate repeats the check the statement depends on, so the
        # UPDATE cannot touch a ledger voucher even read on its own.
        conn.execute(
            "UPDATE Tosite SET tila = ? WHERE id = ? AND tila < ?",
            (TILA_POISTETTU, voucher_id, TILA_KIRJANPIDOSSA),
        )
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
