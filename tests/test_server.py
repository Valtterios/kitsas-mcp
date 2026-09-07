import asyncio
import json
import sqlite3

import pytest

from kitsas_mcp import read
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


def test_call_tool_returns_an_error_for_a_missing_required_argument(book_path):
    # date_to is required and missing. This must name date_to specifically,
    # not blame the tool call in general or crash with a raw KeyError.
    result = call_tool(book_path, "list_vouchers", {"date_from": "2026-01-01"})
    assert "error" in result
    assert "date_to" in result["error"]
    assert "list_vouchers" in result["error"]


def test_call_tool_returns_an_error_for_an_unexpected_argument_on_a_tool_with_only_optional_arguments(book_path):
    # list_accounts declares no required arguments, but "serach" (a typo of
    # "search") is still not one of its declared arguments, and must be
    # refused rather than silently ignored.
    result = call_tool(book_path, "list_accounts", {"serach": "Pankki"})
    assert "error" in result
    assert "serach" in result["error"]


def test_call_tool_labels_an_internal_error_distinctly_from_a_caller_mistake(book_path):
    # business_id is a genuinely declared, present argument, so it passes
    # argument validation; but a dict rather than a string is bound into an
    # INSERT deep inside the write transaction, where sqlite3 raises
    # InterfaceError. That is a bug surface, not a sign the caller passed a
    # wrong argument NAME, so its message must say so instead of claiming
    # "bad arguments", which would send the caller off to recheck arguments
    # that were, in fact, correctly named. InterfaceError is not a
    # KitsasError, a KeyError or a TypeError either, so before the catch-all
    # it escaped call_tool entirely and reached the transport.
    result = call_tool(
        book_path,
        "add_purchase_invoice",
        {
            "supplier_name": "Telia Finland Oyj",
            "lines": [{"account": 4590, "amount": "10.00"}],
            "booking_date": "2026-01-15",
            "business_id": {"y-tunnus": "1234567-8"},
        },
    )
    assert "error" in result
    assert "internal error" in result["error"].lower()
    assert "bad arguments" not in result["error"].lower()
    assert "Traceback" not in result["error"]


def test_call_tool_does_not_let_an_unexpected_exception_reach_the_transport(book_path, monkeypatch):
    # An sqlite3.OperationalError that is not a lock is deliberately
    # re-raised by db.connect_read rather than mistaken for one, so it
    # travelled all the way out of call_tool: it is neither a KitsasError
    # nor a KeyError nor a TypeError. On the wire that is a failure reported
    # without is_error, which is the exact defect the is_error work set out
    # to close.
    def boom(book):
        raise sqlite3.OperationalError("no such table: Tilikausi")

    monkeypatch.setattr(read, "list_fiscal_years", boom)

    result = call_tool(book_path, "list_fiscal_years", {})

    assert "error" in result, "the exception must not escape call_tool"
    assert "internal error" in result["error"].lower()
    assert "no such table: Tilikausi" in result["error"]
    assert "bug in the tool" in result["error"]
    assert "Traceback" not in result["error"]


def test_call_tool_still_blames_the_caller_for_a_caller_mistake(book_path):
    """The catch-all must not blur a bad argument into an internal error."""
    result = call_tool(book_path, "list_vouchers", {"date_from": "2026-01-01"})
    assert "internal error" not in result["error"].lower()
    assert "date_to" in result["error"]


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
    # A refused call must be flagged as an error on the CallToolResult
    # itself, not only inside the JSON payload: a client that branches on
    # is_error (for retry policy, for surfacing failure in the UI, for not
    # counting a refusal as a completed write) would otherwise see this as
    # a success that merely happens to carry an "error" key.
    assert bad_result.is_error is True
    payload = json.loads(bad_result.content[0].text)
    assert "drop_everything" in payload["error"]


def test_on_call_tool_reports_is_error_true_for_a_refused_write(book_path):
    # Deleting an already-in-the-ledger voucher is refused by write.py, not
    # by call_tool's own argument validation. This drives the real
    # tools/call handler end to end for that refusal, to confirm the
    # refusal comes back on the wire as is_error True (previously always
    # False), with the reason still readable in the text content.
    from mcp.types import CallToolRequestParams

    server = build_server(book_path)
    call_tool_entry = server.get_request_handler("tools/call")

    params = CallToolRequestParams(name="delete_draft", arguments={"voucher_id": 1})
    result = asyncio.run(call_tool_entry.handler(None, params))

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert payload["error"].startswith("Voucher 1 is already in the ledger")


def test_on_call_tool_reports_is_error_true_for_an_internal_error(book_path, monkeypatch):
    # The failure the is_error work was really about: not a refusal the code
    # wrote on purpose, but an exception nobody expected. It must come back
    # as a CallToolResult with is_error True and a readable message, not as
    # an exception raised out of the handler into the transport.
    from mcp.types import CallToolRequestParams

    def boom(book):
        raise sqlite3.InterfaceError("Error binding parameter 1: type 'dict' is not supported")

    monkeypatch.setattr(read, "list_fiscal_years", boom)

    server = build_server(book_path)
    call_tool_entry = server.get_request_handler("tools/call")
    params = CallToolRequestParams(name="list_fiscal_years", arguments={})

    result = asyncio.run(call_tool_entry.handler(None, params))

    assert result.is_error is True
    payload = json.loads(result.content[0].text)
    assert "internal error" in payload["error"].lower()


def test_add_purchase_invoice_advertises_partner_id(book_path):
    """The escape hatch is no use if the model is never told it exists."""
    schema = TOOLS["add_purchase_invoice"]["schema"]
    assert "partner_id" in schema
    description = schema["partner_id"]["description"]
    assert "find_supplier" in description
    assert "do not match by name" in description
    assert "partner_id" not in TOOLS["add_purchase_invoice"]["required"]


def test_add_purchase_invoice_advertises_confirm_new_partner(book_path):
    """The other escape hatch: a name whose only rival differs in its accents.

    The refusal names it, but a model that has never been told the argument
    exists has no way to act on the half of the message that says the two
    really are different suppliers.
    """
    schema = TOOLS["add_purchase_invoice"]["schema"]
    assert schema["confirm_new_partner"]["type"] == "boolean"
    assert "partner_id" in schema["confirm_new_partner"]["description"]
    assert "confirm_new_partner" not in TOOLS["add_purchase_invoice"]["required"]


def test_the_accent_refusal_reaches_the_client_as_an_error(book_path):
    """Refused through the tool layer, not just as a Python exception."""
    conn = sqlite3.connect(book_path)
    conn.execute("INSERT INTO Kumppani (nimi, json) VALUES ('Kärkkäinen Lahti', '{}')")
    conn.commit()
    conn.close()

    bill = {
        "supplier_name": "Karkkainen",
        "booking_date": "2026-05-04",
        "lines": [{"account": 4000, "amount": "42.90"}],
    }
    refused = call_tool(book_path, "add_purchase_invoice", bill)
    assert "accents" in refused["error"]
    assert list(book_path.parent.glob("*.bak")) == []

    written = call_tool(book_path, "add_purchase_invoice", {**bill, "confirm_new_partner": True})
    assert "error" not in written
    assert len(list(book_path.parent.glob("*.bak"))) == 1
