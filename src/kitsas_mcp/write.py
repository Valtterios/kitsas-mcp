"""The only module that changes a book. Drafts only, one transaction, never without a backup.

Everything written here lands at TILA_SAAPUNUT with no tunniste, which is what
Kitsas calls an incoming, unapproved document. Kitsas itself moves the voucher
into the ledger and allocates its number when a human approves it. This server
never does either, and never touches a voucher that is already in the ledger.

Nothing outside the voucher being created is ever rewritten. A partner may be
created, and a blank business id filled in and an IBAN bound on a partner the
caller identified exactly, but an existing IBAN binding is never re-pointed and
neither a business id nor an IBAN on an established partner is ever
overwritten. A supplier name that already identifies a partner joins that
partner rather than forking a second one beside it; the name is resolved by
partners.py, the same rule suggest_account answers with, and a match on a name
that is not the partner's own is named in the summary. A name that matches no
partner, but that a partner already in the book resembles once accents are
ignored, is refused rather than quietly forking that partner in two;
confirm_new_partner is how the caller says the two spellings really are two
different suppliers. An IBAN that belongs elsewhere is refused; a business id
on an established partner is left alone and reported in the summary, because
the voucher does not depend on it and refusing would block the ordinary path.

Every one of those refusals is reached on a read connection first, before the
write transaction and so before the backup that opening one takes. The same
checks run again inside the transaction, where they are what the write
actually depends on.

When the partner was found only by a substring of its name, the voucher is
still written but the business id and the IBAN are not. The voucher is a
reversible draft; those two are edits to somebody else's row that delete_draft
does not undo. partner_id names a partner outright, with no name matching at
all, for the cases where the name rule cannot reach the right one.
"""

import hashlib
import json
import mimetypes
from pathlib import Path

from .accounts import account_by_number, default_bank_account_of, list_accounts
from .constants import (
    TILA_HYLATTY,
    TILA_HYVAKSYTTY,
    TILA_KIRJANPIDOSSA,
    TILA_LUONNOS,
    TILA_MALLIPOHJA,
    TILA_POISTETTU,
    TILA_SAAPUNUT,
    TILA_TARKASTETTU,
    TOSITE_MENO,
    VIENTI_OSTO_KIRJAUS,
    VIENTI_OSTO_VASTAKIRJAUS,
)
from .dates import parse_iso_date
from .errors import (
    AmountError,
    ClosedFiscalYearError,
    KitsasError,
    LedgerVoucherError,
    LineFormatError,
    PartnerNotFoundError,
    UnbalancedVoucherError,
)
from .money import cents_to_euros, euros_to_cents
from .partners import PartnerMatch, resolve_partner
from .read import CONFIRMED_UNKNOWN_KEY, fiscal_year_for, no_such_voucher_error

# The voucher states delete_draft is actually meant for: a document waiting
# in the inbox, one that has been checked or accepted but not yet booked, and
# a plain draft. Deliberately excludes TILA_MALLIPOHJA (a saved voucher
# template) and TILA_HYLATTY (a rejected document): both sit below the
# TILA_KIRJANPIDOSSA ledger threshold, so the old "anything below the
# threshold" guard let delete_draft mark either one deleted, which for a
# template is recoverable only from a .bak.
DELETABLE_STATES = frozenset({TILA_SAAPUNUT, TILA_TARKASTETTU, TILA_HYVAKSYTTY, TILA_LUONNOS})

# Plain-language names for the states below the ledger threshold that
# delete_draft still refuses, so the error can name what the voucher actually
# is rather than just its number.
_NON_DELETABLE_STATE_NAMES = {
    TILA_POISTETTU: "already deleted",
    TILA_MALLIPOHJA: "a voucher template Kitsas keeps for reuse",
    TILA_HYLATTY: "a rejected document",
}

# The ways out of an ambiguity here, in the order they are worth trying:
# name the partner outright with partner_id, narrow the name, or merge the
# partners in Kitsas if they are really one supplier.
AMBIGUITY_REMEDY = (
    "Pass partner_id to say which one you mean, use a name that matches exactly one "
    "of them, or merge them in Kitsas; find_supplier lists them with their ids."
)

# The ways out when the name matched nothing but a partner already in the book
# differs from it only in its accents. Both have to be here: booking onto the
# existing partner, and creating the new one anyway, because in Finnish the two
# spellings really can be two different suppliers.
NEAR_MATCH_REMEDY = (
    "If it is the same supplier, pass partner_id to book onto the partner already in "
    "the book; find_supplier lists the ids. If it really is a different supplier, pass "
    "confirm_new_partner true to create it as a partner of its own beside that one."
)

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
    if year[CONFIRMED_UNKNOWN_KEY]:
        # Whether this year was confirmed cannot be read at all (Tilikausi.json
        # is corrupt). Refusing is the safe direction: reporting it as
        # unconfirmed instead would let a write through a year that might
        # really be closed, and there would be no way to notice.
        raise ClosedFiscalYearError(
            f"The fiscal year {year['starts']} to {year['ends']} has unreadable data in "
            "this book, so whether it has been confirmed cannot be determined. Nothing "
            "may be added to it until that is known, in case it is really closed. Open "
            "the book in Kitsas, which will rewrite the year's data, then try again."
        )
    if year["confirmed"]:
        raise ClosedFiscalYearError(
            f"The fiscal year {year['starts']} to {year['ends']} was confirmed on "
            f"{year['confirmed']}. Nothing may be added to it. Use a date in an open "
            "fiscal year, or unconfirm the year in Kitsas first."
        )


def _prepare_lines(accounts, lines, description, supplier_name) -> list[tuple]:
    """Validate every line against a chart of accounts already loaded once."""
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

        account_by_number(accounts, account)  # raises AccountNotFoundError
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


def _matched_partner_note(typed: str, matched_name: str) -> str:
    """Say out loud which partner a name that was not the partner's own landed on.

    Reusing the partner is right: it is the one suggest_account just showed the
    history of, and creating a second one beside it would split the supplier in
    the book. But the voucher is being attached to a partner spelled
    differently from what was typed, and on someone's books that is worth
    reading rather than guessing at.

    The remedy named here has to be one that can actually work. It used to be
    "pass the full name", which is the very input that just failed: a supplier
    whose real name is contained in an existing partner's name, a sole trader
    "Nieminen" against the member "Kari Nieminen", matches that partner however
    fully it is spelled. partner_id is the way out, because it does not go
    through name matching at all.
    """
    return (
        f"Booked to {matched_name}, the partner already in this book that {typed!r} "
        "matched; no new partner was created. If that is the wrong partner, pass "
        "partner_id for the right one (find_supplier lists the ids), or add the "
        "supplier in Kitsas first."
    )


def _fuzzy_write_skipped_note(lead: str, typed: str) -> str:
    """Say that a write onto an existing partner's own data was skipped, and why.

    Same shape as the business-id skip note below: what was left alone, why,
    and what to do if it really was meant. The voucher is a draft and can be
    deleted; a business id or an IBAN written onto a partner cannot be undone
    by delete_draft, only by restoring a backup. So when the partner was found
    by a substring of its name rather than by the name itself, which is a
    guess, those two writes do not happen at all.
    """
    return (
        f"{lead}, because {typed!r} only matched inside that partner's name rather than "
        "being the name itself. Pass partner_id if that really is the supplier, or make "
        "the change in Kitsas."
    )


def _partner_id_number(value) -> int:
    """The partner id as a number. JSON may deliver it as the string '7'."""
    text = str(value).strip()
    if isinstance(value, bool) or not (text.isascii() and text.isdigit()):
        raise PartnerNotFoundError(
            f"partner_id is {value!r}, which is not a partner id. Pass the number "
            "find_supplier returns as 'id', or leave partner_id out to match the "
            "supplier by name."
        )
    return int(text)


def _partner_by_id(conn, partner_id) -> PartnerMatch:
    """The partner this id names, with no name matching of any kind.

    The escape hatch from the name rule. A partner named this way is exact by
    construction: the caller pointed at one row, so nothing here is a guess,
    and the business id and IBAN writes below go ahead as they do for a name
    that was the partner's own.
    """
    number = _partner_id_number(partner_id)
    row = conn.execute("SELECT id, nimi FROM Kumppani WHERE id = ?", (number,)).fetchone()
    if row is None:
        raise PartnerNotFoundError(
            f"There is no partner {number} in this book. Use find_supplier to get the "
            "id of the supplier you meant, or leave partner_id out to match by name."
        )
    return PartnerMatch(row["id"], row["nimi"], True)


def _named_partner_note(typed: str, matched: str) -> str | None:
    """Note the mismatch when partner_id and supplier_name disagree, or None.

    partner_id decides which partner the voucher belongs to; supplier_name is
    then only the text on the voucher. That is the point of the argument, so
    the mismatch is reported rather than refused: the name printed on an
    invoice often is not the name the partner is filed under, and that is
    exactly when a bookkeeper reaches for the id.
    """
    if typed.strip().casefold() == str(matched).strip().casefold():
        return None
    return (
        f"Booked to {matched}, the partner partner_id names. The supplier name "
        f"{typed!r} was not looked up at all; it is the voucher's text only."
    )


def _check_iban_free(conn, supplier_id, supplier_name: str, iban) -> bool:
    """True when this IBAN still needs binding to this partner.

    False when it is already bound to it, and a refusal when it belongs to
    somebody else. A KumppaniIban row is pre-existing book data outside the
    voucher being written. Silently reassigning one would, for a single
    mistyped digit, move the tax authority's bank account onto whatever
    supplier is being invoiced.

    Split out from the binding itself so that the same question can be asked
    on a read connection, before the book is copied for a write that this
    would then refuse. `supplier_id` is None when the partner does not exist
    yet, which no existing binding can belong to.
    """
    normalised = _normalise_iban(iban)
    owner = conn.execute(
        "SELECT i.kumppani, k.nimi FROM KumppaniIban i "
        "LEFT JOIN Kumppani k ON k.id = i.kumppani WHERE i.iban = ?",
        (normalised,),
    ).fetchone()

    if owner is None:
        return True
    if supplier_id is not None and owner["kumppani"] == supplier_id:
        return False  # Already bound to this partner. Nothing to do.
    raise KitsasError(
        f"IBAN {normalised} already belongs to {owner['nimi']!r} in this book, not "
        f"to {supplier_name!r}. Check the IBAN, or move it in Kitsas if it really "
        "has changed hands."
    )


def _bind_iban(conn, supplier_id: int, supplier_name: str, iban) -> None:
    """Bind an IBAN to this partner. Never re-point one that belongs elsewhere."""
    if _check_iban_free(conn, supplier_id, supplier_name, iban):
        conn.execute(
            "INSERT INTO KumppaniIban (iban, kumppani) VALUES (?,?)",
            (_normalise_iban(iban), supplier_id),
        )


def _fill_blank_business_id(conn, supplier_id: int, name: str, business_id) -> str | None:
    """Fill in a missing business id, but never on an established partner.

    A partner that already carries ledger vouchers is pre-existing book data:
    its business id belongs on the invoices and reports those vouchers are part
    of, and a business id read off a scanned invoice is only as good as the
    scan. Writing one here would be undoable except by restoring a backup, so
    this leaves the value alone and says so. A partner created by this same
    call has no history to contradict, and is filled in freely.

    Skipping rather than refusing is deliberate. A Finnish invoice nearly
    always prints the supplier's Y-tunnus, so refusing would block the ordinary
    path, every bill from a long-standing supplier, over a field the voucher
    does not depend on. Returns a sentence for the caller's summary when the
    write was skipped, so the skip is visible rather than silent.

    A partner that already has a non-blank alvtunnus is never touched at all,
    whatever its history: the WHERE clause carries that condition itself.
    """
    booked = conn.execute(
        "SELECT count(*) FROM Tosite WHERE kumppani = ? AND tila >= ?",
        (supplier_id, TILA_KIRJANPIDOSSA),
    ).fetchone()[0]
    if booked:
        return (
            f"Left the business id unchanged on {name}, which already has bookkeeping "
            "history; set it in Kitsas if it needs updating."
        )
    conn.execute(
        "UPDATE Kumppani SET alvtunnus = ? WHERE id = ? AND coalesce(alvtunnus,'') = ''",
        (business_id, supplier_id),
    )
    return None


# What the strings that mean "no" look like coming through JSON. Read as a
# Python truth value, "false" is true, and taking it as consent would create
# the very duplicate partner the resemblance refusal exists to prevent.
_DENIALS = {"", "false", "0", "no"}
_CONSENTS = {"true", "1", "yes"}


def _confirmed(value) -> bool:
    """confirm_new_partner as the boolean it is meant to be, or a refusal."""
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _CONSENTS:
            return True
        if text in _DENIALS:
            return False
        raise LineFormatError(
            f"confirm_new_partner is {value!r}, which is neither true nor false. Pass "
            "true only to create a supplier as a new partner beside one whose name "
            "differs from it only in accents, and leave it out otherwise."
        )
    return bool(value)


def _plan_supplier(
    conn, name: str, iban, partner_id=None, confirm_new_partner=False
) -> tuple[PartnerMatch | None, list[str]]:
    """Decide which partner this bill belongs to, deciding nothing else.

    Every question here is answerable from a read connection, and every one of
    them can refuse the call: an explicit partner_id that names no partner, a
    name matching several partners, a name matching none but resembling one,
    an IBAN that belongs to a different partner. add_purchase_invoice asks
    them all before it opens the write transaction, so that a refusal costs no
    backup, and _upsert_supplier asks them again inside the transaction, so
    that no answer can go stale between the two.

    Returns the partner the bill goes to, None when it is a supplier the book
    does not have, and the sentences the caller's summary should carry.
    """
    notes = []
    if partner_id is not None:
        match = _partner_by_id(conn, partner_id)
        named = _named_partner_note(name, match.name)
        if named:
            notes.append(named)
    else:
        match = resolve_partner(
            conn,
            name,
            remedy=AMBIGUITY_REMEDY,
            # None is how the caller says it has already been told that an
            # existing partner resembles this name and that they are really
            # two different suppliers.
            near_remedy=None if _confirmed(confirm_new_partner) else NEAR_MATCH_REMEDY,
        )

    # Only checked where the binding would actually be attempted: a partner
    # found by a substring of its name keeps its own IBAN either way, so an
    # IBAN conflict is not that call's problem.
    if iban and (match is None or match.exact):
        _check_iban_free(
            conn,
            None if match is None else match.id,
            name if match is None else match.name,
            iban,
        )
    return match, notes


def _upsert_supplier(
    conn, name: str, business_id, iban, partner_id=None, confirm_new_partner=False
) -> tuple[int, str, list[str]]:
    """Return the partner id, the name it is filed under, and notes for the summary.

    With partner_id the partner is that row and nothing is resolved. Without
    it the name is resolved by the rule in partners.py, the same one
    suggest_account answered with a moment earlier, so a bill for a supplier
    that is already in the book joins that partner instead of forking a second
    one that carries none of its history. A name that matches nothing really is
    a new supplier, and that partner is created here.

    Only the voucher is written when the partner was found by a substring of
    its name. The business id and the IBAN are edits to a partner that already
    existed, they survive delete_draft, and a substring match is a guess: a
    sole trader "Nieminen" matches the member "Kari Nieminen", and it is that
    member's record that would otherwise take the supplier's IBAN and business
    id. Both are skipped and named in the summary instead.

    Which partner it is, is decided by _plan_supplier, which
    add_purchase_invoice has already run once on a read connection. Running it
    again here is the guard against anything having changed in between.
    """
    match, notes = _plan_supplier(conn, name, iban, partner_id, confirm_new_partner)

    if match is None:
        cursor = conn.execute(
            "INSERT INTO Kumppani (nimi, alvtunnus, json) VALUES (?,?,?)",
            (name, business_id, "{}"),
        )
        # A partner created here is filed under exactly the name that was
        # typed, and has no data of its own for this call to overwrite.
        supplier_id, filed_as, identified = cursor.lastrowid, name, True
    else:
        supplier_id, filed_as, identified = match.id, match.name, match.exact
        if not identified:
            notes.append(_matched_partner_note(name, filed_as))
        if business_id:
            if not identified:
                notes.append(
                    _fuzzy_write_skipped_note(
                        f"Left the business id unchanged on {filed_as}", name
                    )
                )
            else:
                existing = conn.execute(
                    "SELECT coalesce(alvtunnus,'') AS alvtunnus FROM Kumppani WHERE id = ?",
                    (supplier_id,),
                ).fetchone()
                if existing["alvtunnus"] == "":
                    skipped = _fill_blank_business_id(conn, supplier_id, filed_as, business_id)
                    if skipped:
                        notes.append(skipped)

    if iban:
        if identified:
            _bind_iban(conn, supplier_id, filed_as, iban)
        else:
            notes.append(
                _fuzzy_write_skipped_note(
                    f"Did not bind the IBAN {_normalise_iban(iban)} to {filed_as}", name
                )
            )
    return supplier_id, filed_as, notes


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
    partner_id=None,
    confirm_new_partner=False,
    invoice_date=None,
    due_date=None,
    reference=None,
    description=None,
    credit_account=None,
    pdf_path=None,
) -> dict:
    """Create a purchase invoice as a draft. Kitsas approves it into the ledger.

    partner_id, when given, names the partner outright: no name matching of
    any kind happens, the partner must exist, and supplier_name is then only
    the text that goes on the voucher.

    confirm_new_partner answers the one refusal that has no other way out: a
    supplier name that matches no partner but that an existing partner
    resembles once accents are ignored. It creates the new partner anyway, for
    when the two spellings really are two different suppliers. It does not
    create a duplicate of a partner the name does match, exactly or by a
    substring; those still join the partner that is already there.
    """
    # booking_date is validated here rather than only inside
    # _check_fiscal_year, whose own parse was a side effect: the raw value
    # went on to be bound into Tosite.pvm and both Vienti.pvm columns, where
    # an unpadded "2026-2-28" compares wrong as text against every other
    # date in the book. Assigned like invoice_date and due_date below.
    booking_date = parse_iso_date(booking_date, "booking_date")
    _check_fiscal_year(book, booking_date)

    supplier_name = str(supplier_name).strip()
    if not supplier_name:
        raise LineFormatError(
            "The supplier name is empty. Pass the supplier's name as it should appear "
            "on the voucher, for example 'Telia Finland Oyj'."
        )

    # Optional, but not free-form: booking_date was validated and reassigned
    # above. invoice_date and due_date are stored as-is into laskupvm/erapvm
    # with no fiscal-year check of their own, so without this they would be
    # the only two date fields on this voucher a malformed value could reach
    # unchecked.
    if invoice_date is not None:
        invoice_date = parse_iso_date(invoice_date, "invoice_date")
    if due_date is not None:
        due_date = parse_iso_date(due_date, "due_date")

    # Loaded once and validated against in memory: a five-line bill used to
    # open a fresh connection per account lookup, plus one more for the
    # default bank account, all before the write transaction even opened.
    accounts = list_accounts(book)

    prepared = _prepare_lines(accounts, lines, description, supplier_name)
    total = sum(cents for _, cents, _ in prepared)

    if credit_account is None:
        # Already validated by construction: default_bank_account_of only
        # ever returns a number that is in `accounts`.
        counter_account = default_bank_account_of(accounts)
    else:
        account_by_number(accounts, credit_account)  # raises AccountNotFoundError
        counter_account = credit_account

    attachment_source = _read_attachment(pdf_path) if pdf_path else None
    title = description or supplier_name

    # Everything about the partner that can be judged without writing is
    # judged here, on a read connection. Each of these refusals used to happen
    # inside the write transaction, which connect_write opens only after
    # copying the whole book: the trial that found this refused 19 calls
    # against a 114 MB book and paid 2.1 GB of backups for writes that never
    # happened. _upsert_supplier asks the same questions again inside the
    # transaction, where the answers are the ones the write depends on.
    with book.connect_read() as conn:
        _plan_supplier(conn, supplier_name, iban, partner_id, confirm_new_partner)

    with book.connect_write() as conn:
        supplier_id, filed_as, supplier_notes = _upsert_supplier(
            conn, supplier_name, business_id, iban, partner_id, confirm_new_partner
        )

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
                        # Both names, because they differ when the typed one
                        # matched a partner already in the book.
                        "partner": filed_as,
                        "partner_id": supplier_id,
                        "booking_date": booking_date,
                        "lines": [
                            {"account": a, "cents": c, "description": d} for a, c, d in prepared
                        ],
                        "credit_account": counter_account,
                    }
                ),
            ),
        )

    # The same copy every write in this session is protected by, not one taken
    # for this bill: see Book.backup. Named in the summary in those words,
    # because "backup taken" reads as a snapshot from a moment ago.
    backup_path = book.backup()

    return {
        "voucher_id": voucher_id,
        # The partner the voucher is actually attached to, which is not always
        # spelled the way the supplier name was typed.
        "supplier": filed_as,
        "supplier_id": supplier_id,
        "total": cents_to_euros(total),
        "credit_account": counter_account,
        "lines": [{"account": a, "amount": cents_to_euros(c)} for a, c, _ in prepared],
        "attachment": attachment,
        "backup": str(backup_path),
        "summary": (
            f"Draft voucher {voucher_id} for {supplier_name}, {cents_to_euros(total)} euros on "
            f"{booking_date}, credited to account {counter_account}. It is not in the ledger; "
            "open Kitsas to check and approve it."
            + "".join(f" {note}" for note in supplier_notes)
            + f" The backup {backup_path.name} holds the book as it stood before the first "
            "write of this session, not before this bill: anything entered since is in the "
            "book but not in that copy. Use delete_draft to undo this draft."
        ),
    }


def _describe_state(tila: int) -> str:
    """A plain-language name for a state delete_draft refuses to touch."""
    return _NON_DELETABLE_STATE_NAMES.get(tila, f"in state {tila}, which delete_draft does not handle")


def _check_deletable(conn, voucher_id: int) -> None:
    row = conn.execute("SELECT tila FROM Tosite WHERE id = ?", (voucher_id,)).fetchone()
    if row is None:
        raise no_such_voucher_error(voucher_id)
    tila = row["tila"]
    if tila >= TILA_KIRJANPIDOSSA:
        raise LedgerVoucherError(
            f"Voucher {voucher_id} is already in the ledger and cannot be deleted here. "
            "Booked history is read-only through this server; do it in Kitsas if you "
            "really mean to."
        )
    if tila not in DELETABLE_STATES:
        raise LedgerVoucherError(
            f"Voucher {voucher_id} is {_describe_state(tila)}, not a document waiting in "
            "its inbox or a draft someone is still working on. Only those "
            f"(states {sorted(DELETABLE_STATES)}) can be deleted here; manage this "
            "voucher in Kitsas directly instead."
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
