import pytest
import sqlite3

from kitsas_mcp.history import suggest_account
from kitsas_mcp.errors import AmbiguousSupplierError


def test_suggests_the_account_this_supplier_has_been_booked_to(book):
    suggestions = suggest_account(book, "Hetzner")
    assert len(suggestions) == 1
    assert suggestions[0]["account"] == 4590
    assert suggestions[0]["account_name"] == "Edustuskulut"
    assert suggestions[0]["count"] == 2
    assert suggestions[0]["last_used"] == "2026-03-06"


def test_accepts_a_supplier_id(book):
    assert suggest_account(book, 7)[0]["account"] == 4590


def test_unknown_supplier_gets_no_suggestions(book):
    assert suggest_account(book, "Telia") == []


def test_does_not_suggest_the_counter_account(book):
    assert 1910 not in [s["account"] for s in suggest_account(book, "Hetzner")]


def test_counts_vouchers_not_lines(book, book_path):
    """A single voucher with multiple debit lines on the same account counts as 1, not N."""
    conn = sqlite3.connect(book_path)
    # Insert a new supplier
    conn.execute("INSERT INTO Kumppani (id, nimi, json) VALUES (99, 'MultiLine Inc', '{}')")
    # Insert one purchase voucher with two debit lines on account 4590
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm) "
        "VALUES (100, '2026-04-01', 100, 100, '100', 'MultiLine', 99, '2026-04-01')"
    )
    # First debit line on 4590
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (1, 100, 101, '2026-04-01', 4590, 0, 'MultiLine', 1000, 0, 99)"
    )
    # Second debit line on 4590 (same account)
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (2, 100, 101, '2026-04-01', 4590, 0, 'MultiLine', 1000, 0, 99)"
    )
    # Credit line to balance
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (3, 100, 102, '2026-04-01', 1910, 0, 'MultiLine', 0, 2000, 99)"
    )
    conn.commit()
    conn.close()

    suggestions = suggest_account(book, "MultiLine Inc")
    assert len(suggestions) == 1
    assert suggestions[0]["account"] == 4590
    assert suggestions[0]["count"] == 1  # One voucher, not two lines


def test_ignores_drafts_and_deleted_vouchers(book, book_path):
    conn = sqlite3.connect(book_path)
    # Draft voucher (tila=20)
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, otsikko, kumppani) VALUES (99,'2026-04-01',100,20,'draft',7)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, debetsnt, kreditsnt) "
        "VALUES (2,99,101,'2026-04-01',4000,0,1000,0)"
    )
    # Deleted voucher (tila=0)
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, otsikko, kumppani) VALUES (98,'2026-04-02',100,0,'deleted',7)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, debetsnt, kreditsnt) "
        "VALUES (1,98,101,'2026-04-02',4000,0,1000,0)"
    )
    conn.commit()
    conn.close()

    assert [s["account"] for s in suggest_account(book, "Hetzner")] == [4590]


def test_exact_supplier_name_match_wins_over_prefix_matches(book, book_path):
    """Exact match should take precedence even when partial matches exist."""
    conn = sqlite3.connect(book_path)
    # Add another supplier with a name that contains "Hetzner"
    conn.execute("INSERT INTO Kumppani (id, nimi, json) VALUES (99, 'Hetzner Extra Services', '{}')")
    conn.commit()
    conn.close()

    # Query for exact name should find Hetzner (id 7), not the ambiguous prefix match
    suggestions = suggest_account(book, "Hetzner")
    assert len(suggestions) == 1
    assert suggestions[0]["account"] == 4590


def test_fuzzy_match_on_unique_substring_works(book, book_path):
    """A unique prefix should still match the supplier."""
    suggestions = suggest_account(book, "Hetz")
    assert len(suggestions) == 1
    assert suggestions[0]["account"] == 4590


def test_fuzzy_match_on_ambiguous_substring_raises(book, book_path):
    """Ambiguous substring should raise AmbiguousSupplierError with all names."""
    conn = sqlite3.connect(book_path)
    # Add another supplier with overlapping name
    conn.execute("INSERT INTO Kumppani (id, nimi, json) VALUES (99, 'Hetzner Extra Services', '{}')")
    conn.execute("INSERT INTO Kumppani (id, nimi, json) VALUES (100, 'Hetzner Cloud', '{}')")
    conn.commit()
    conn.close()

    # Querying with a prefix that matches multiple suppliers should raise
    with pytest.raises(AmbiguousSupplierError) as exc_info:
        suggest_account(book, "Hetz")
    error_msg = str(exc_info.value)
    assert "matches 3 partners" in error_msg
    assert "Hetzner" in error_msg
    assert "Hetzner Extra Services" in error_msg
    assert "Hetzner Cloud" in error_msg
    assert "find_supplier lists them" in error_msg


def test_exact_name_match_on_two_partners_differing_only_by_case_raises(book, book_path):
    """'Hetzner' and 'HETZNER' are two partners; neither one's history may be assumed."""
    conn = sqlite3.connect(book_path)
    conn.execute("INSERT INTO Kumppani (id, nimi, json) VALUES (99, 'HETZNER', '{}')")
    conn.commit()
    conn.close()

    with pytest.raises(AmbiguousSupplierError) as exc_info:
        suggest_account(book, "Hetzner")
    message = str(exc_info.value)
    assert "matches 2 partners" in message
    assert "Hetzner (id 7)" in message
    assert "HETZNER (id 99)" in message
    assert "Pass the partner id instead" in message


def test_a_partner_id_sent_as_a_digit_string_is_read_as_an_id(book):
    """The error message tells the caller to pass the id; JSON may send it as a string."""
    assert suggest_account(book, "7") == suggest_account(book, 7)
    assert suggest_account(book, "7")[0]["account"] == 4590


def test_a_partner_id_as_an_integer_still_works(book):
    assert suggest_account(book, 7)[0]["account"] == 4590


def test_a_name_is_still_treated_as_a_name(book):
    assert suggest_account(book, "Hetzner")[0]["account"] == 4590
    assert suggest_account(book, "Telia") == []
