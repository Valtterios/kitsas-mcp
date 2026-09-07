"""The three call sites must resolve a supplier name to the same partner.

find_supplier, suggest_account and add_purchase_invoice each used to answer
this question their own way. add_purchase_invoice's own tool description tells
the model to call suggest_account first, so a disagreement between those two
means the account history the model was shown belongs to a partner the voucher
is not attached to, and the book grows a duplicate supplier every time a name
is abbreviated.
"""

import sqlite3

import pytest

from kitsas_mcp.errors import AmbiguousSupplierError, SimilarPartnerError
from kitsas_mcp.history import suggest_account
from kitsas_mcp.partners import contains_pattern, escape_like, resolve_partner
from kitsas_mcp.read import find_supplier
from kitsas_mcp.write import add_purchase_invoice

BILL = dict(
    booking_date="2026-05-04",
    description="Server rent April",
    lines=[{"account": 4590, "amount": "47.31"}],
)


def rename_partner_7(book_path, name):
    """Give the seeded partner with history the full legal name of a real supplier."""
    conn = sqlite3.connect(book_path)
    conn.execute("UPDATE Kumppani SET nimi = ? WHERE id = 7", (name,))
    conn.commit()
    conn.close()


def add_partner(book_path, name):
    conn = sqlite3.connect(book_path)
    cursor = conn.execute("INSERT INTO Kumppani (nimi, json) VALUES (?, '{}')", (name,))
    partner_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return partner_id


def partner_ids(book):
    with book.connect_read() as conn:
        return [r["id"] for r in conn.execute("SELECT id FROM Kumppani ORDER BY id")]


def voucher_partner(book, voucher_id):
    with book.connect_read() as conn:
        return conn.execute(
            "SELECT kumppani FROM Tosite WHERE id = ?", (voucher_id,)
        ).fetchone()["kumppani"]


# -- the shared rule ---------------------------------------------------------


def test_escape_like_makes_the_wildcards_literal():
    assert escape_like("50%") == "50\\%"
    assert escape_like("_e_zner") == "\\_e\\_zner"
    # The escape character itself goes first, or escaping the wildcards would
    # escape their own new backslashes a second time.
    assert escape_like("a\\_b") == "a\\\\\\_b"
    assert contains_pattern("50%") == "%50\\%%"


def test_resolve_partner_prefers_the_exact_name_over_the_longer_ones(book, book_path):
    add_partner(book_path, "Hetzner Cloud")
    with book.connect_read() as conn:
        match = resolve_partner(conn, "Hetzner", remedy="x")
    assert (match.id, match.name, match.exact) == (7, "Hetzner", True)


def test_resolve_partner_reports_a_substring_match_as_not_exact(book, book_path):
    rename_partner_7(book_path, "Hetzner Online GmbH")
    with book.connect_read() as conn:
        match = resolve_partner(conn, "Hetzner", remedy="x")
    assert (match.id, match.name, match.exact) == (7, "Hetzner Online GmbH", False)


def test_resolve_partner_returns_none_for_a_name_the_book_does_not_have(book):
    with book.connect_read() as conn:
        assert resolve_partner(conn, "Telia Finland Oyj", remedy="x") is None


def test_resolve_partner_treats_a_blank_name_as_no_partner(book):
    """Left to the LIKE, an empty name would match every partner in the book."""
    with book.connect_read() as conn:
        assert resolve_partner(conn, "   ", remedy="x") is None


def test_resolve_partner_carries_the_callers_remedy_into_the_error(book, book_path):
    add_partner(book_path, "Hetzner Cloud")
    with book.connect_read() as conn:
        with pytest.raises(AmbiguousSupplierError) as excinfo:
            resolve_partner(conn, "Hetz", remedy="Do the thing that helps here.")
    assert "Do the thing that helps here." in str(excinfo.value)


# -- the three call sites agree ---------------------------------------------


@pytest.mark.parametrize("typed", ["Hetzner", "hetzner online gmbh", "  Hetzner Online GmbH  "])
def test_all_three_call_sites_land_on_the_same_partner(book, book_path, typed):
    rename_partner_7(book_path, "Hetzner Online GmbH")

    # No .strip() here: find_supplier used to be the one call site that did
    # not trim the caller's text, so stripping it in the test hid the very
    # disagreement this test exists to catch.
    assert [p["id"] for p in find_supplier(book, typed)] == [7]
    assert suggest_account(book, typed)[0]["account"] == 4590

    result = add_purchase_invoice(book, **{**BILL, "supplier_name": typed})
    assert voucher_partner(book, result["voucher_id"]) == 7
    assert partner_ids(book) == [1, 7], "no second partner may appear beside the matched one"


def test_all_three_call_sites_refuse_the_same_ambiguous_name(book, book_path):
    rename_partner_7(book_path, "Hetzner Online GmbH")
    other = add_partner(book_path, "Hetzner Cloud")

    assert {p["id"] for p in find_supplier(book, "Hetzner")} == {7, other}
    with pytest.raises(AmbiguousSupplierError):
        suggest_account(book, "Hetzner")
    with pytest.raises(AmbiguousSupplierError):
        add_purchase_invoice(book, **{**BILL, "supplier_name": "Hetzner"})


def test_the_history_shown_belongs_to_the_partner_the_voucher_gets(book, book_path):
    """The end to end path the tool descriptions tell the model to take."""
    rename_partner_7(book_path, "Hetzner Online GmbH")

    suggested = suggest_account(book, "Hetzner")
    assert suggested[0]["account"] == 4590 and suggested[0]["count"] == 2

    result = add_purchase_invoice(
        book, **{**BILL, "supplier_name": "Hetzner", "lines": [{"account": 4590, "amount": "47.31"}]}
    )
    partner = voucher_partner(book, result["voucher_id"])
    assert suggest_account(book, partner)[0]["account"] == 4590, (
        "the voucher must be attached to the partner whose history was shown"
    )


# -- a Finnish name folds on both sides -------------------------------------
# SQLite's lower() and LIKE fold ASCII only: lower('KÄRKKÄINEN') comes back
# unchanged and 'KÄRKKÄINEN' LIKE '%kärkkäinen%' is 0. A large share of real
# Finnish supplier and member names carry ä, ö or å, so without a fold that
# handles them the duplicate partner this module exists to prevent appeared
# anyway, on exactly the names it matters most for.


def test_an_exact_finnish_name_matches_whatever_its_case(book, book_path):
    partner = add_partner(book_path, "Kärkkäinen Oy")
    with book.connect_read() as conn:
        match = resolve_partner(conn, "KÄRKKÄINEN OY", remedy="x")
    assert (match.id, match.name, match.exact) == (partner, "Kärkkäinen Oy", True)


def test_a_finnish_substring_matches_whatever_its_case(book, book_path):
    partner = add_partner(book_path, "Osuuskunta Ähtärin Sähkö")
    with book.connect_read() as conn:
        match = resolve_partner(conn, "ähtärin SÄHKÖ", remedy="x")
    assert match.id == partner
    assert match.exact is False


def test_find_supplier_finds_a_finnish_name_in_the_other_case(book, book_path):
    partner = add_partner(book_path, "Kärkkäinen Oy")
    assert [p["id"] for p in find_supplier(book, "KÄRKKÄINEN")] == [partner]


def test_a_finnish_supplier_name_does_not_fork_the_partner(book, book_path):
    """The whole point: 'KÄRKKÄINEN OY' shouted must be the partner already there."""
    partner = add_partner(book_path, "Kärkkäinen Oy")

    result = add_purchase_invoice(book, **{**BILL, "supplier_name": "KÄRKKÄINEN OY"})

    assert voucher_partner(book, result["voucher_id"]) == partner
    assert partner_ids(book) == [1, 7, partner], "no second partner may appear beside it"
    assert result["supplier"] == "Kärkkäinen Oy"


def test_two_finnish_partners_differing_only_by_case_are_refused(book, book_path):
    """Folding the names must make these two ambiguous, not silently pick one."""
    add_partner(book_path, "Kärkkäinen Oy")
    add_partner(book_path, "KÄRKKÄINEN OY")

    with pytest.raises(AmbiguousSupplierError):
        add_purchase_invoice(book, **{**BILL, "supplier_name": "kärkkäinen oy"})


def test_a_finnish_name_still_reads_its_like_metacharacters_literally(book, book_path):
    """The fold must not undo the LIKE escaping the pattern already carries."""
    add_partner(book_path, "Ähtäri 50% Oy")
    add_partner(book_path, "Ähtäri 50 prosenttia")

    with book.connect_read() as conn:
        match = resolve_partner(conn, "ähtäri 50%", remedy="x")
    assert match.name == "Ähtäri 50% Oy", "the % must be a literal, not a wildcard"


# -- a dropped umlaut is noticed, not resolved and not ignored ---------------
# The trial that found this billed "Karkkainen" against a book holding
# "Kärkkäinen Lahti". Case folding alone does not bring the two together, so
# nothing matched, and a second partner appeared beside the first with no
# warning of any kind. Accents are stripped only to NOTICE that: in Finnish ä
# and a are different letters, so "Karkkainen" and "Kärkkäinen" can be two
# different people and this server does not choose between them.

KARKKAINEN = "Kärkkäinen Lahti"


def test_the_accented_spelling_matches_exactly_and_books_onto_the_partner(book, book_path):
    """The case that already worked, pinned: nothing here may refuse it."""
    partner = add_partner(book_path, KARKKAINEN)

    with book.connect_read() as conn:
        match = resolve_partner(conn, KARKKAINEN, remedy="x")
    assert (match.id, match.name, match.exact) == (partner, KARKKAINEN, True)

    result = add_purchase_invoice(book, **{**BILL, "supplier_name": KARKKAINEN})
    assert voucher_partner(book, result["voucher_id"]) == partner
    assert partner_ids(book) == [1, 7, partner]


def test_a_name_with_the_umlauts_dropped_is_refused_and_names_the_partner(book, book_path):
    partner = add_partner(book_path, KARKKAINEN)

    with pytest.raises(SimilarPartnerError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "supplier_name": "Karkkainen"})

    message = str(excinfo.value)
    assert KARKKAINEN in message and f"id {partner}" in message
    assert "accents" in message
    assert "partner_id" in message, "the way to book onto the existing partner"
    assert "confirm_new_partner" in message, "the way to say they are different suppliers"

    assert partner_ids(book) == [1, 7, partner], "no second partner may be created"
    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tosite").fetchone()[0] == 4


def test_the_same_refusal_reaches_suggest_account(book, book_path):
    """The tool the model is told to call first must not answer 'no history'.

    Returning [] there is what sends a treasurer on to bill a partner the book
    already has, under a second spelling.
    """
    add_partner(book_path, KARKKAINEN)
    with pytest.raises(SimilarPartnerError):
        suggest_account(book, "Karkkainen")


def test_find_supplier_shows_the_partner_whose_umlauts_were_dropped(book, book_path):
    """The browsing tool decides nothing, so it may show both spellings."""
    partner = add_partner(book_path, KARKKAINEN)
    found = find_supplier(book, "Karkkainen")
    assert [(p["id"], p["name"]) for p in found] == [(partner, KARKKAINEN)]


def test_partner_id_books_the_bill_onto_the_partner_that_is_already_there(book, book_path):
    """The first way out of the refusal: it is the same supplier after all."""
    partner = add_partner(book_path, KARKKAINEN)

    result = add_purchase_invoice(
        book, **{**BILL, "supplier_name": "Karkkainen", "partner_id": partner}
    )

    assert voucher_partner(book, result["voucher_id"]) == partner
    assert partner_ids(book) == [1, 7, partner]


def test_confirm_new_partner_creates_the_separate_partner(book, book_path):
    """The second way out: two spellings, two real suppliers.

    'Karkkainen' without the dots is a name someone can actually be called,
    so there has to be a way to say so and get the bill entered.
    """
    existing = add_partner(book_path, KARKKAINEN)

    result = add_purchase_invoice(
        book, **{**BILL, "supplier_name": "Karkkainen", "confirm_new_partner": True}
    )

    created = [i for i in partner_ids(book) if i not in (1, 7, existing)]
    assert len(created) == 1, "exactly one new partner, beside the one already there"
    assert result["supplier"] == "Karkkainen"
    assert voucher_partner(book, result["voucher_id"]) == created[0]


def test_confirming_does_not_fork_a_partner_the_name_really_matches(book, book_path):
    """The flag answers a resemblance, not the matching rule itself."""
    partner = add_partner(book_path, KARKKAINEN)

    result = add_purchase_invoice(
        book, **{**BILL, "supplier_name": KARKKAINEN, "confirm_new_partner": True}
    )

    assert result["supplier_id"] == partner
    assert partner_ids(book) == [1, 7, partner]


def test_two_finnish_names_that_merely_look_similar_are_not_a_near_match(book, book_path):
    """Järvi and Jarvinen are two names, not one name spelt two ways.

    Nothing about the resemblance rule may make an ordinary new supplier
    harder to enter: only names that are the same once the accents come off
    are reported.
    """
    existing = add_partner(book_path, "Matti Järvi")

    result = add_purchase_invoice(book, **{**BILL, "supplier_name": "Matti Jarvinen"})

    created = [i for i in partner_ids(book) if i not in (1, 7, existing)]
    assert len(created) == 1
    assert result["supplier"] == "Matti Jarvinen"
    assert "accents" not in result["summary"]


def test_an_unrelated_new_supplier_is_still_created_without_a_word(book):
    result = add_purchase_invoice(book, **{**BILL, "supplier_name": "Telia Finland Oyj"})
    assert result["supplier"] == "Telia Finland Oyj"
    assert "accents" not in result["summary"]


def test_the_accented_spelling_wins_when_both_spellings_are_in_the_book(book, book_path):
    """An exact match is an answer; the resemblance is only looked for after one fails."""
    accented = add_partner(book_path, "Kärkkäinen")
    add_partner(book_path, "Karkkainen")

    with book.connect_read() as conn:
        match = resolve_partner(conn, "Kärkkäinen", remedy="x")
    assert (match.id, match.exact) == (accented, True)


def test_several_partners_can_share_the_resemblance_and_all_are_named(book, book_path):
    """Two partners already spelt differently from each other, both named."""
    first = add_partner(book_path, "Ähtärin Sähkö")
    second = add_partner(book_path, "Ähtärin Sahkö")

    with book.connect_read() as conn:
        with pytest.raises(SimilarPartnerError) as excinfo:
            resolve_partner(conn, "Ahtarin Sahko", remedy="x")

    message = str(excinfo.value)
    assert f"id {first}" in message and f"id {second}" in message


def test_a_resemblance_is_not_reported_when_the_name_matches_a_partner(book, book_path):
    """'Hetzner' is a partner, and 'Hetzner' with a stray accent is not consulted."""
    add_partner(book_path, "Hetzner Ähtäri")
    with book.connect_read() as conn:
        match = resolve_partner(conn, "Hetzner", remedy="x")
    assert match.id == 7 and match.exact is True
