from pathlib import Path

import pytest

from kitsas_mcp.errors import (
    AccountNotFoundError,
    AmountError,
    ClosedFiscalYearError,
    KitsasError,
    LedgerVoucherError,
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
    with pytest.raises(UnbalancedVoucherError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 4000, "amount": "0.00"}]})
    with pytest.raises(UnbalancedVoucherError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 4000, "amount": "-5.00"}]})


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
