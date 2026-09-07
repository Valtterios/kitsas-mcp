import asyncio
import json

import pytest

from kitsas_mcp.server import TOOLS, build_server, call_tool, resolve_book_path


def test_every_tool_has_a_description_and_a_handler():
    for name, spec in TOOLS.items():
        assert spec["description"].strip(), f"{name} has no description"
        assert callable(spec["handler"]), f"{name} has no handler"


def test_every_tool_declares_which_arguments_are_required():
    for name, spec in TOOLS.items():
        assert "required" in spec, f"{name} does not say which arguments are required"
        for argument in spec["required"]:
            assert argument in spec["schema"], f"{name} requires undeclared argument {argument}"


def test_the_required_arguments_are_the_ones_the_handlers_cannot_do_without():
    expected = {
        "list_accounts": [],
        "list_fiscal_years": [],
        "find_supplier": ["query"],
        "list_vouchers": ["date_from", "date_to"],
        "get_voucher": ["voucher_id"],
        "suggest_account": ["supplier"],
        "add_purchase_invoice": ["supplier_name", "lines", "booking_date"],
        "delete_draft": ["voucher_id"],
        "bank_balance": ["on_date"],
        "bank_movements": ["date_from", "date_to"],
    }
    assert {name: spec["required"] for name, spec in TOOLS.items()} == expected


def test_suggest_account_accepts_a_partner_id_as_a_string_or_a_number(book_path):
    # history.suggest_account takes the id path for "7" as well as 7, so the
    # schema must not tell the model that only one of the two is allowed.
    assert TOOLS["suggest_account"]["schema"]["supplier"]["type"] == ["string", "integer"]
    by_string = call_tool(book_path, "suggest_account", {"supplier": "7"})
    by_number = call_tool(book_path, "suggest_account", {"supplier": 7})
    assert by_string == by_number
    assert by_string[0]["account"] == 4590


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


def test_build_server_registers_all_ten_tools_with_a_schema(book_path):
    # Smoke test for the part call_tool's own tests never touch: the actual
    # mcp.server.Server wiring. The installed mcp package (2.x) registers
    # request handlers through Server's on_list_tools/on_call_tool
    # constructor keywords rather than through @server.list_tools()/
    # @server.call_tool() decorators, so this exercises the tools/list
    # handler through the same public Server.get_request_handler() lookup
    # the runtime itself uses, rather than assuming a decorator API that
    # this mcp version does not have.
    server = build_server(book_path)

    list_tools_entry = server.get_request_handler("tools/list")
    assert list_tools_entry is not None, "no tools/list handler was registered"
    result = asyncio.run(list_tools_entry.handler(None, None))

    names = {tool.name for tool in result.tools}
    assert names == set(TOOLS)
    for tool in result.tools:
        assert tool.description.strip(), f"{tool.name} has no description"
        assert tool.input_schema["type"] == "object"
        # A schema with no "required" list tells the model that every
        # argument is optional, including the ones the handler indexes.
        assert tool.input_schema["required"] == TOOLS[tool.name]["required"]

    advertised = {tool.name: tool.input_schema["required"] for tool in result.tools}
    assert advertised["add_purchase_invoice"] == ["supplier_name", "lines", "booking_date"]
    assert advertised["delete_draft"] == ["voucher_id"]
    assert advertised["list_accounts"] == []


def test_build_server_tools_call_handler_actually_runs_a_tool(book_path):
    # test_build_server_registers_all_ten_tools_with_a_schema only checks
    # that a tools/call handler is registered, not that invoking it behaves
    # correctly. A regression in on_call_tool's return shape (wrong
    # CallToolResult field, a changed content type) would still pass that
    # test and only show up when a real client called a tool, which is the
    # same class of gap that hid the missing-decorator-API defect. This
    # drives the real handler, the one mcp itself calls on "tools/call", for
    # both a successful and a failing invocation.
    from mcp.types import CallToolRequestParams

    server = build_server(book_path)
    call_tool_entry = server.get_request_handler("tools/call")
    assert call_tool_entry is not None, "no tools/call handler was registered"

    ok_params = CallToolRequestParams(name="list_accounts", arguments={})
    ok_result = asyncio.run(call_tool_entry.handler(None, ok_params))
    assert ok_result.is_error is False
    assert len(ok_result.content) == 1
    accounts = json.loads(ok_result.content[0].text)
    assert {"number": 1910, "name": "Pankkitili", "type": "ARP"} in accounts

    bad_params = CallToolRequestParams(name="drop_everything", arguments={})
    bad_result = asyncio.run(call_tool_entry.handler(None, bad_params))
    payload = json.loads(bad_result.content[0].text)
    assert "drop_everything" in payload["error"]
