from kitsas_mcp.reconcile import bank_balance, bank_movements


def test_balance_counts_only_ledger_vouchers(book):
    # The synthetic book has two Hetzner bills of 47.31 credited to 1910
    # that reached the ledger (tila=100). A draft (tila=20) and an
    # unbalanced draft (tila=50) also credit 1910 but must not count.
    assert bank_balance(book, "2026-12-31")["balance"] == "-94.62"


def test_balance_respects_the_date(book):
    assert bank_balance(book, "2026-02-28")["balance"] == "-47.31"


def test_balance_before_any_movement_is_zero(book):
    assert bank_balance(book, "2026-01-01")["balance"] == "0.00"


def test_balance_names_the_account_used(book):
    assert bank_balance(book, "2026-12-31")["account"] == 1910


def test_movements_are_listed_with_a_running_balance(book):
    movements = bank_movements(book, "2026-01-01", "2026-12-31")
    assert len(movements) == 2
    assert movements[0]["date"] == "2026-02-06"
    assert movements[0]["amount"] == "-47.31"
    assert movements[0]["running_balance"] == "-47.31"
    assert movements[1]["running_balance"] == "-94.62"
    assert movements[0]["counterparty"] == "Hetzner"


def test_movements_running_balance_starts_from_the_opening_balance(book):
    movements = bank_movements(book, "2026-03-01", "2026-12-31")
    assert len(movements) == 1
    assert movements[0]["running_balance"] == "-94.62"


def test_drafts_do_not_appear_in_movements(book):
    from kitsas_mcp.write import add_purchase_invoice

    # No credit_account is passed, so this drafts a credit to the default
    # bank account (1910) itself, at tila=TILA_SAAPUNUT (20). It must not
    # appear in bank_movements or shift bank_balance, because it never
    # reached the ledger.
    add_purchase_invoice(
        book,
        supplier_name="Telia",
        booking_date="2026-05-04",
        description="Broadband",
        lines=[{"account": 4000, "amount": "42.90"}],
    )
    assert len(bank_movements(book, "2026-01-01", "2026-12-31")) == 2
    assert bank_balance(book, "2026-12-31")["balance"] == "-94.62"
