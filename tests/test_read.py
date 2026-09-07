import pytest

from kitsas_mcp.errors import DateFormatError, NoFiscalYearError
from kitsas_mcp.read import find_supplier, fiscal_year_for, get_voucher, list_fiscal_years, list_vouchers


def test_lists_fiscal_years_and_marks_the_confirmed_one(book):
    years = list_fiscal_years(book)
    assert [y["starts"] for y in years] == ["2025-01-01", "2026-01-01"]
    assert years[0]["confirmed"] == "2026-04-28"
    assert years[1]["confirmed"] is None


def test_fiscal_year_for_a_date_inside_a_year(book):
    assert fiscal_year_for(book, "2026-05-04")["starts"] == "2026-01-01"


def test_fiscal_year_for_a_date_outside_every_year(book):
    with pytest.raises(NoFiscalYearError) as excinfo:
        fiscal_year_for(book, "2031-01-03")
    assert "2031-01-03" in str(excinfo.value)


def test_fiscal_year_for_rejects_an_unpadded_date(book):
    # "2026-5-4" sorts wrong against zero-padded fiscal year bounds under
    # plain string comparison, so it must be rejected rather than silently
    # matching (or failing to match) the wrong year.
    with pytest.raises(DateFormatError) as excinfo:
        fiscal_year_for(book, "2026-5-4")
    assert "2026-5-4" in str(excinfo.value)


def test_fiscal_year_for_accepts_a_valid_boundary_date(book):
    # The fiscal year's own start date is an inclusive boundary; validation
    # must not be so strict that it breaks this.
    assert fiscal_year_for(book, "2026-01-01")["starts"] == "2026-01-01"


def test_list_vouchers_rejects_an_unpadded_date_from(book):
    with pytest.raises(DateFormatError):
        list_vouchers(book, "2026-1-01", "2026-12-31")


def test_list_vouchers_rejects_an_unpadded_date_to(book):
    with pytest.raises(DateFormatError):
        list_vouchers(book, "2026-01-01", "2026-12-1")


def test_find_supplier_by_name_substring(book):
    found = find_supplier(book, "hetz")
    assert len(found) == 1
    assert found[0]["id"] == 7
    assert found[0]["name"] == "Hetzner"


def test_find_supplier_returns_empty_for_an_unknown_name(book):
    assert find_supplier(book, "Telia") == []


def test_list_vouchers_shows_ledger_vouchers_by_default(book):
    vouchers = list_vouchers(book, "2026-01-01", "2026-12-31")
    assert len(vouchers) == 2
    assert vouchers[0]["total"] == "47.31"
    assert vouchers[0]["supplier"] == "Hetzner"


def test_list_vouchers_can_filter_by_account(book):
    assert len(list_vouchers(book, "2026-01-01", "2026-12-31", account=4590)) == 2
    assert list_vouchers(book, "2026-01-01", "2026-12-31", account=3000) == []


def test_list_vouchers_respects_the_date_range(book):
    assert list_vouchers(book, "2026-03-01", "2026-03-31")[0]["date"] == "2026-03-06"


def test_list_vouchers_excludes_a_draft_by_default(book):
    vouchers = list_vouchers(book, "2026-01-01", "2026-12-31")
    assert {v["id"] for v in vouchers} == {1, 2}


def test_list_vouchers_can_select_a_draft_state_explicitly(book):
    drafts = list_vouchers(book, "2026-01-01", "2026-12-31", state=20)
    assert len(drafts) == 1
    assert drafts[0]["id"] == 3


def test_list_vouchers_totals_an_unbalanced_voucher_with_the_larger_side(book):
    vouchers = list_vouchers(book, "2026-01-01", "2026-12-31", state=50)
    assert len(vouchers) == 1
    assert vouchers[0]["total"] == "10.00"


def test_get_voucher_returns_header_and_entries(book):
    voucher = get_voucher(book, 1)
    assert voucher["date"] == "2026-02-06"
    assert voucher["state"] == 100
    assert len(voucher["entries"]) == 2
    credit = [e for e in voucher["entries"] if e["credit"] != "0.00"][0]
    assert credit["account"] == 1910
    assert credit["account_name"] == "Pankkitili"
    assert credit["credit"] == "47.31"


def test_get_voucher_returns_none_for_a_missing_id(book):
    assert get_voucher(book, 4242) is None


def test_get_voucher_names_the_supplier_rather_than_repeating_its_id(book):
    """Tosite.kumppani is the id; the joined Kumppani.nimi must not be shadowed by it."""
    voucher = get_voucher(book, 1)
    assert voucher["supplier"] == "Hetzner"
    assert voucher["supplier_id"] == 7


# -- LIKE metacharacters in the caller's query are literal text ---------------


def _add_partner(book_path, name):
    import sqlite3

    conn = sqlite3.connect(book_path)
    conn.execute("INSERT INTO Kumppani (nimi, json) VALUES (?, '{}')", (name,))
    conn.commit()
    conn.close()


def test_find_supplier_treats_a_percent_as_a_literal_percent(book, book_path):
    """Unescaped, '%' was a wildcard and listed every partner in the book."""
    _add_partner(book_path, "Alennus 50% Oy")

    assert [p["name"] for p in find_supplier(book, "%")] == ["Alennus 50% Oy"]
    assert [p["name"] for p in find_supplier(book, "50%")] == ["Alennus 50% Oy"]


def test_find_supplier_treats_an_underscore_as_a_literal_underscore(book, book_path):
    """Unescaped, '_' matched any single character, so '_e_zner' found 'Hetzner'."""
    _add_partner(book_path, "Nordic_IT Oy")

    assert find_supplier(book, "_e_zner") == []
    assert [p["name"] for p in find_supplier(book, "_")] == ["Nordic_IT Oy"]


def test_find_supplier_still_matches_a_name_containing_a_backslash(book, book_path):
    """The escape character is itself escaped, so it stays ordinary text."""
    _add_partner(book_path, "A\\B Oy")

    assert [p["name"] for p in find_supplier(book, "A\\B")] == ["A\\B Oy"]
