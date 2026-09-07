import pytest

from kitsas_mcp.server import TOOLS, call_tool, resolve_book_path


def test_every_tool_has_a_description_and_a_handler():
    for name, spec in TOOLS.items():
        assert spec["description"].strip(), f"{name} has no description"
        assert callable(spec["handler"]), f"{name} has no handler"


def test_the_expected_tools_are_registered():
    assert set(TOOLS) == {
        "list_accounts",
        "list_fiscal_years",
        "find_supplier",
        "list_vouchers",
        "get_voucher",
        "suggest_account",
        "add_purchase_invoice",
        "delete_draft",
        "bank_balance",
        "bank_movements",
    }


def test_resolve_book_path_prefers_the_argument(tmp_path, monkeypatch):
    monkeypatch.setenv("KITSAS_BOOK", str(tmp_path / "env.kitsas"))
    assert resolve_book_path(str(tmp_path / "arg.kitsas")).name == "arg.kitsas"


def test_resolve_book_path_falls_back_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("KITSAS_BOOK", str(tmp_path / "env.kitsas"))
    assert resolve_book_path(None).name == "env.kitsas"


def test_resolve_book_path_explains_when_unset(monkeypatch):
    monkeypatch.delenv("KITSAS_BOOK", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        resolve_book_path(None)
    assert "KITSAS_BOOK" in str(excinfo.value)


def test_call_tool_returns_data_for_a_read_tool(book_path):
    result = call_tool(book_path, "list_accounts", {})
    assert {"number": 1910, "name": "Pankkitili", "type": "ARP"} in result


def test_call_tool_turns_a_kitsas_error_into_a_message(book_path):
    result = call_tool(book_path, "get_voucher", {"voucher_id": 1})
    assert result["id"] == 1

    result = call_tool(book_path, "delete_draft", {"voucher_id": 1})
    assert result["error"].startswith("Voucher 1 is already in the ledger")


def test_call_tool_rejects_an_unknown_tool(book_path):
    result = call_tool(book_path, "drop_everything", {})
    assert "drop_everything" in result["error"]


def test_call_tool_returns_an_error_for_an_unexpected_argument_name(book_path):
    # "sender" is not a real add_purchase_invoice argument (it is
    # "supplier_name"). This must come back as an {"error": ...} message, not
    # escape call_tool as a raw TypeError traceback.
    result = call_tool(
        book_path,
        "add_purchase_invoice",
        {
            "sender": "Telia Finland Oyj",
            "lines": [{"account": 4590, "amount": "10.00"}],
            "booking_date": "2026-01-15",
        },
    )
    assert "error" in result
    assert "add_purchase_invoice" in result["error"]
