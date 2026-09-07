from kitsas_mcp.history import suggest_account


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


def test_ignores_drafts_and_deleted_vouchers(book, book_path):
    import sqlite3

    conn = sqlite3.connect(book_path)
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, otsikko, kumppani) VALUES (99,'2026-04-01',100,20,'draft',7)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, debetsnt, kreditsnt) "
        "VALUES (2,99,101,'2026-04-01',4000,0,1000,0)"
    )
    conn.commit()
    conn.close()

    assert [s["account"] for s in suggest_account(book, "Hetzner")] == [4590]
