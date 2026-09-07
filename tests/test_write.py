import sqlite3
from pathlib import Path

import pytest

from kitsas_mcp import write as write_module
from kitsas_mcp.errors import (
    AccountNotFoundError,
    AmbiguousSupplierError,
    AmountError,
    ClosedFiscalYearError,
    KitsasError,
    LedgerVoucherError,
    LineFormatError,
    NoFiscalYearError,
    UnbalancedVoucherError,
)
from kitsas_mcp.read import get_voucher
from kitsas_mcp.write import add_purchase_invoice, delete_draft

BILL = dict(
    supplier_name="Telia Finland Oyj",
    booking_date="2026-05-04",
    invoice_date="2026-04-28",
    description="Broadband April",
    lines=[{"account": 4000, "amount": "42.90"}],
)


def test_writes_a_draft_not_a_ledger_voucher(book):
    result = add_purchase_invoice(book, **BILL)
    voucher = get_voucher(book, result["voucher_id"])
    assert voucher["state"] == 20
    assert voucher["number"] is None, "a draft must not consume a voucher number"


def test_the_draft_is_invisible_to_a_default_ledger_listing(book):
    from kitsas_mcp.read import list_vouchers

    result = add_purchase_invoice(book, **BILL)
    listed = list_vouchers(book, "2026-01-01", "2026-12-31")
    assert result["voucher_id"] not in [v["id"] for v in listed]


def test_books_the_expense_as_debit_and_the_bank_as_credit(book):
    result = add_purchase_invoice(book, **BILL)
    entries = get_voucher(book, result["voucher_id"])["entries"]
    counter = [e for e in entries if e["row"] == 1][0]
    expense = [e for e in entries if e["row"] == 2][0]
    assert counter["account"] == 1910
    assert counter["credit"] == "42.90"
    assert counter["debit"] == "0.00"
    assert counter["type"] == 102
    assert expense["account"] == 4000
    assert expense["debit"] == "42.90"
    assert expense["credit"] == "0.00"
    assert expense["type"] == 101


def test_supports_several_expense_lines(book):
    result = add_purchase_invoice(
        book,
        **{**BILL, "lines": [
            {"account": 4000, "amount": "75.54"},
            {"account": 4590, "amount": "163.45"},
        ]},
    )
    voucher = get_voucher(book, result["voucher_id"])
    assert len(voucher["entries"]) == 3
    assert [e for e in voucher["entries"] if e["row"] == 1][0]["credit"] == "238.99"
    assert [e["row"] for e in voucher["entries"]] == [1, 2, 3]
    assert [e["type"] for e in voucher["entries"]] == [102, 101, 101]


def test_an_explicit_credit_account_is_used(book):
    result = add_purchase_invoice(book, **{**BILL, "credit_account": 2960})
    entries = get_voucher(book, result["voucher_id"])["entries"]
    assert [e for e in entries if e["row"] == 1][0]["account"] == 2960
    assert result["credit_account"] == 2960


def test_creates_the_supplier_when_it_is_new(book):
    add_purchase_invoice(book, **BILL)
    from kitsas_mcp.read import find_supplier

    assert find_supplier(book, "Telia")[0]["name"] == "Telia Finland Oyj"


def test_reuses_an_existing_supplier(book):
    add_purchase_invoice(book, **{**BILL, "supplier_name": "Hetzner"})
    with book.connect_read() as conn:
        count = conn.execute("SELECT count(*) FROM Kumppani WHERE nimi='Hetzner'").fetchone()[0]
    assert count == 1


def test_stores_the_iban_against_the_supplier(book):
    add_purchase_invoice(book, **{**BILL, "iban": "FI21 1234 5600 0007 85"})
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT i.iban FROM KumppaniIban i JOIN Kumppani k ON k.id = i.kumppani "
            "WHERE k.nimi = 'Telia Finland Oyj'"
        ).fetchall()
    assert [r["iban"] for r in rows] == ["FI2112345600000785"]


def test_attaches_the_pdf(book, tmp_path):
    pdf = tmp_path / "telia-invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake invoice")
    result = add_purchase_invoice(book, **{**BILL, "pdf_path": str(pdf)})
    voucher = get_voucher(book, result["voucher_id"])
    assert voucher["attachments"] == [
        {"name": "telia-invoice.pdf", "mime": "application/pdf", "bytes": 21}
    ]
    with book.connect_read() as conn:
        sha = conn.execute("SELECT sha FROM Liite").fetchone()["sha"]
    assert len(sha) == 64


def test_refuses_an_attachment_that_is_not_there(book, tmp_path):
    with pytest.raises(KitsasError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "pdf_path": str(tmp_path / "missing.pdf")})
    assert "missing.pdf" in str(excinfo.value)
    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4


def test_takes_a_backup_before_the_first_write(book, book_path):
    result = add_purchase_invoice(book, **BILL)
    assert result["backup"].endswith(".bak")
    assert Path(result["backup"]).exists()


def test_the_backup_predates_the_write(book, book_path):
    result = add_purchase_invoice(book, **BILL)
    from kitsas_mcp.db import Book

    restored = Book(Path(result["backup"]))
    with restored.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4


def test_refuses_a_date_with_no_fiscal_year(book):
    with pytest.raises(NoFiscalYearError):
        add_purchase_invoice(book, **{**BILL, "booking_date": "2031-05-04"})


def test_refuses_a_confirmed_fiscal_year(book):
    with pytest.raises(ClosedFiscalYearError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "booking_date": "2025-05-04"})
    assert "confirmed" in str(excinfo.value)


def test_refuses_an_unknown_account(book):
    with pytest.raises(AccountNotFoundError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 9999, "amount": "1.00"}]})


def test_refuses_an_empty_bill(book):
    with pytest.raises(UnbalancedVoucherError):
        add_purchase_invoice(book, **{**BILL, "lines": []})


def test_refuses_a_line_that_is_not_positive(book):
    with pytest.raises(AmountError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 4000, "amount": "0.00"}]})
    with pytest.raises(AmountError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 4000, "amount": "-5.00"}]})


def test_refuses_a_malformed_line(book):
    with pytest.raises(LineFormatError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"amount": "1.00"}]})
    with pytest.raises(LineFormatError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 4000}]})
    with pytest.raises(LineFormatError):
        add_purchase_invoice(book, **{**BILL, "lines": ["4000: 1.00"]})


def test_refuses_an_empty_supplier_name(book):
    with pytest.raises(LineFormatError):
        add_purchase_invoice(book, **{**BILL, "supplier_name": "   "})


def test_refuses_an_amount_that_is_not_money(book):
    with pytest.raises(AmountError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 4000, "amount": "about ten"}]})


def test_a_failed_write_leaves_the_book_unchanged(book, book_path):
    before = book_path.read_bytes()
    with pytest.raises(AccountNotFoundError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 9999, "amount": "1.00"}]})
    assert book_path.read_bytes() == before


def test_a_failure_inside_the_transaction_rolls_everything_back(book, tmp_path, monkeypatch):
    pdf = tmp_path / "telia-invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake invoice")

    def boom(*args, **kwargs):
        raise UnbalancedVoucherError("forced failure after the voucher rows were written")

    monkeypatch.setattr("kitsas_mcp.write._attach", boom)

    with pytest.raises(UnbalancedVoucherError):
        add_purchase_invoice(book, **{**BILL, "pdf_path": str(pdf)})

    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM Vienti").fetchone()[0] == 8
        assert (
            conn.execute(
                "SELECT count(*) FROM Kumppani WHERE nimi = 'Telia Finland Oyj'"
            ).fetchone()[0]
            == 0
        ), "the supplier created in the same transaction must be rolled back too"


def test_delete_draft_removes_a_draft(book):
    result = add_purchase_invoice(book, **BILL)
    delete_draft(book, result["voucher_id"])
    assert get_voucher(book, result["voucher_id"])["state"] == 0


def test_delete_draft_refuses_a_ledger_voucher(book):
    with pytest.raises(LedgerVoucherError) as excinfo:
        delete_draft(book, 1)
    assert "already in the ledger" in str(excinfo.value)
    assert get_voucher(book, 1)["state"] == 100


def test_delete_draft_refuses_a_voucher_that_does_not_exist(book):
    with pytest.raises(LedgerVoucherError) as excinfo:
        delete_draft(book, 9999)
    assert "9999" in str(excinfo.value)


def test_delete_draft_accepts_the_other_draft_state(book):
    delete_draft(book, 4)
    assert get_voucher(book, 4)["state"] == 0


def test_writes_the_audit_log(book):
    result = add_purchase_invoice(book, **BILL)
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT tila FROM Tositeloki WHERE tosite = ?", (result["voucher_id"],)
        ).fetchall()
    assert [r["tila"] for r in rows] == [20]


def test_deleting_appends_to_the_audit_log(book):
    result = add_purchase_invoice(book, **BILL)
    delete_draft(book, result["voucher_id"])
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT tila FROM Tositeloki WHERE tosite = ? ORDER BY id", (result["voucher_id"],)
        ).fetchall()
    assert [r["tila"] for r in rows] == [20, 0]


def test_the_summary_says_the_voucher_is_not_in_the_ledger(book):
    result = add_purchase_invoice(book, **BILL)
    assert result["total"] == "42.90"
    assert result["lines"] == [{"account": 4000, "amount": "42.90"}]
    assert result["attachment"] is None
    assert "not in the ledger" in result["summary"]


# -- Finding 1: an existing IBAN binding is never re-pointed -----------------


def test_a_new_iban_is_bound_to_the_new_supplier(book):
    add_purchase_invoice(book, **{**BILL, "iban": "FI21 1234 5600 0007 85"})
    with book.connect_read() as conn:
        row = conn.execute(
            "SELECT k.nimi FROM KumppaniIban i JOIN Kumppani k ON k.id = i.kumppani "
            "WHERE i.iban = 'FI2112345600000785'"
        ).fetchone()
    assert row["nimi"] == "Telia Finland Oyj"


def test_rebinding_the_same_iban_to_the_same_supplier_is_a_no_op(book):
    add_purchase_invoice(book, **{**BILL, "iban": "FI21 1234 5600 0007 85"})
    add_purchase_invoice(book, **{**BILL, "iban": "FI2112345600000785"})
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT i.iban FROM KumppaniIban i JOIN Kumppani k ON k.id = i.kumppani "
            "WHERE k.nimi = 'Telia Finland Oyj'"
        ).fetchall()
    assert [r["iban"] for r in rows] == ["FI2112345600000785"]


def test_refuses_to_move_an_iban_that_belongs_to_another_partner(book):
    # FI5689199710000724 is seeded against Verohallinto, the tax authority.
    with pytest.raises(KitsasError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "iban": "FI56 8919 9710 000724"})
    message = str(excinfo.value)
    assert "Verohallinto" in message and "Telia Finland Oyj" in message

    with book.connect_read() as conn:
        owner = conn.execute(
            "SELECT k.nimi FROM KumppaniIban i JOIN Kumppani k ON k.id = i.kumppani "
            "WHERE i.iban = 'FI5689199710000724'"
        ).fetchone()
        created = conn.execute(
            "SELECT count(*) FROM Kumppani WHERE nimi = 'Telia Finland Oyj'"
        ).fetchone()[0]
    assert owner["nimi"] == "Verohallinto", "the tax authority's IBAN must not move"
    assert created == 0, "the refusal must roll the whole transaction back"


# -- Finding 2: partner matching does not choose silently --------------------


def test_a_supplier_name_with_stray_whitespace_reuses_the_existing_partner(book):
    add_purchase_invoice(book, **{**BILL, "supplier_name": "  Hetzner  "})
    with book.connect_read() as conn:
        rows = conn.execute("SELECT id FROM Kumppani WHERE trim(nimi) = 'Hetzner'").fetchall()
    assert len(rows) == 1, "trailing whitespace must not create a second partner"


def test_two_partners_differing_only_by_case_are_refused(book, book_path):
    conn = sqlite3.connect(book_path)
    conn.execute("INSERT INTO Kumppani (nimi, json) VALUES ('HETZNER', '{}')")
    conn.commit()
    conn.close()

    with pytest.raises(AmbiguousSupplierError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "supplier_name": "Hetzner"})
    message = str(excinfo.value)
    assert "Hetzner" in message and "HETZNER" in message


# -- Finding 3: the attachment size is capped --------------------------------


def test_refuses_an_attachment_over_the_size_limit(book, tmp_path):
    huge = tmp_path / "scan.pdf"
    with open(huge, "wb") as handle:
        handle.truncate(write_module.MAX_ATTACHMENT_BYTES + 1)

    with pytest.raises(KitsasError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "pdf_path": str(huge)})
    message = str(excinfo.value)
    assert str(write_module.MAX_ATTACHMENT_BYTES + 1) in message
    assert str(write_module.MAX_ATTACHMENT_BYTES) in message

    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4


def test_an_attachment_at_the_limit_is_accepted(book, tmp_path):
    ok = tmp_path / "scan.pdf"
    with open(ok, "wb") as handle:
        handle.truncate(write_module.MAX_ATTACHMENT_BYTES)
    result = add_purchase_invoice(book, **{**BILL, "pdf_path": str(ok)})
    assert result["attachment"]["bytes"] == write_module.MAX_ATTACHMENT_BYTES


# -- Finding 4: the two guards that nothing else catches ---------------------


def test_verify_draft_catches_a_tosite_insert_that_omits_tila(book, monkeypatch):
    """Drop the tila column from the INSERT, as a careless refactor would.

    Tosite.tila has DEFAULT 100 in the Kitsas schema, so omitting the column
    files the voucher straight into the ledger. The sarja column takes the
    third parameter so the statement's arity is unchanged and only tila is
    lost. This test fails if _verify_draft is removed.
    """
    monkeypatch.setattr(
        write_module,
        "TOSITE_INSERT",
        "INSERT INTO Tosite (pvm, tyyppi, sarja, tunniste, otsikko, kumppani, laskupvm, "
        "erapvm, viite, json) VALUES (?,?,?,NULL,?,?,?,?,?,'{}')",
    )

    with pytest.raises(LedgerVoucherError) as excinfo:
        add_purchase_invoice(book, **BILL)
    assert "unnumbered draft" in str(excinfo.value)

    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM Tosite WHERE tila >= 100").fetchone()[0] == 2


def test_verify_draft_catches_a_tosite_insert_that_allocates_a_number(book, monkeypatch):
    """Write a tunniste, which Kitsas alone may do. Fails if _verify_draft is removed."""
    monkeypatch.setattr(
        write_module,
        "TOSITE_INSERT",
        "INSERT INTO Tosite (pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm, "
        "erapvm, viite, json) VALUES (?,?,?,99,?,?,?,?,?,'{}')",
    )

    with pytest.raises(LedgerVoucherError):
        add_purchase_invoice(book, **BILL)

    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4


def test_verify_draft_rejects_an_empty_or_unbalanced_voucher(book, book_path):
    """The balance readback tested directly, against rows it did not write.

    sqlite3.Connection is an immutable type, so its execute cannot be patched
    to corrupt an amount mid-transaction the way TOSITE_INSERT can be swapped.
    The guard's own logic is exercised here instead. An empty voucher is the
    case that matters: it satisfies 0 == 0 and would pass a bare equality
    check, so this fails if the debit > 0 half of the guard is removed.
    """
    conn = sqlite3.connect(book_path)
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, tunniste) VALUES (5,'2026-06-01',100,20,NULL)"
    )
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, tunniste) VALUES (6,'2026-06-01',100,20,NULL)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, debetsnt, kreditsnt) "
        "VALUES (1,6,102,'2026-06-01',1910,0,0,400)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, debetsnt, kreditsnt) "
        "VALUES (2,6,101,'2026-06-01',4000,0,1000,0)"
    )
    conn.commit()
    conn.close()

    with book.connect_read() as conn:
        write_module._verify_draft(conn, 3)  # the balanced fixture draft passes

        with pytest.raises(UnbalancedVoucherError) as empty:
            write_module._verify_draft(conn, 5)  # a draft with no entries at all

        with pytest.raises(UnbalancedVoucherError):
            write_module._verify_draft(conn, 6)  # debit 1000 against credit 400

    assert "no expense at all" in str(empty.value)


def test_delete_draft_rechecks_inside_the_transaction(book, monkeypatch):
    """The read-only pre-check passes, the in-transaction one must still fire.

    The first call stands in for a state that was deletable when it was read.
    If the second, in-transaction _check_deletable call is removed, nothing
    raises and this test fails.
    """
    real_check = write_module._check_deletable
    calls = []

    def once_permissive(conn, voucher_id):
        calls.append(voucher_id)
        if len(calls) == 1:
            return  # the pre-check saw a deletable voucher
        real_check(conn, voucher_id)

    monkeypatch.setattr(write_module, "_check_deletable", once_permissive)

    with pytest.raises(LedgerVoucherError) as excinfo:
        delete_draft(book, 1)
    assert "already in the ledger" in str(excinfo.value)
    assert len(calls) == 2, "the write transaction must re-check, not trust the pre-check"
    assert get_voucher(book, 1)["state"] == 100


def test_the_update_statement_alone_cannot_touch_a_ledger_voucher(book, monkeypatch):
    """With both guards bypassed, the UPDATE's own tila predicate still holds."""
    monkeypatch.setattr(write_module, "_check_deletable", lambda conn, voucher_id: None)

    delete_draft(book, 1)
    assert get_voucher(book, 1)["state"] == 100, "the UPDATE must carry its own tila predicate"
