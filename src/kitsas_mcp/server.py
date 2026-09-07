"""MCP tool definitions. Thin: every tool is a call into one of the modules."""

import argparse
import json
import os
import sys
from pathlib import Path

from . import accounts, history, read, reconcile, write
from .db import Book
from .errors import KitsasError

TOOLS = {
    "list_accounts": {
        "description": "List the book's chart of accounts. Optional search matches a substring of the account name, or the start of the account number.",
        "schema": {"search": {"type": "string", "description": "Substring of the name, or the first digits of the number"}},
        "required": [],
        "handler": lambda book, args: accounts.list_accounts(book, args.get("search")),
    },
    "list_fiscal_years": {
        "description": (
            "List fiscal years, showing which is current and which have been confirmed. "
            "Nothing can be written into a confirmed year. 'confirmed' is the confirmation "
            "date or null; 'confirmed_unknown' is true when the year's stored data could "
            "not be read, and a year like that is closed to writes too, because whether it "
            "was confirmed cannot be told."
        ),
        "schema": {},
        "required": [],
        "handler": lambda book, args: read.list_fiscal_years(book),
    },
    "find_supplier": {
        "description": "Find a partner by name, business id or IBAN.",
        "schema": {"query": {"type": "string"}},
        "required": ["query"],
        "handler": lambda book, args: read.find_supplier(book, args["query"]),
    },
    "list_vouchers": {
        "description": "List vouchers in a date range. Shows ledger vouchers unless a state is given.",
        "schema": {
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            "supplier": {"type": "integer", "description": "Partner id"},
            "account": {"type": "integer"},
            "state": {"type": "integer", "description": "20 for drafts this server created"},
        },
        "required": ["date_from", "date_to"],
        "handler": lambda book, args: read.list_vouchers(
            book,
            args["date_from"],
            args["date_to"],
            supplier=args.get("supplier"),
            account=args.get("account"),
            state=args.get("state"),
        ),
    },
    "get_voucher": {
        "description": "Get one voucher with all its entries and attachment names.",
        "schema": {"voucher_id": {"type": "integer"}},
        "required": ["voucher_id"],
        "handler": lambda book, args: read.get_voucher(book, args["voucher_id"]),
    },
    "suggest_account": {
        "description": (
            "Which expense accounts this supplier's earlier bills were booked to, most used "
            "first. Empty for a supplier with no history. Call this before add_purchase_invoice "
            "so a recurring supplier keeps landing on the same account instead of a guess."
        ),
        "schema": {
            "supplier": {
                "type": ["string", "integer"],
                "description": "Partner name, or the partner id as a number or a digit string",
            }
        },
        "required": ["supplier"],
        "handler": lambda book, args: history.suggest_account(book, args["supplier"]),
    },
    "add_purchase_invoice": {
        "description": (
            "Create a purchase invoice as a DRAFT. It does not enter the ledger and gets no "
            "voucher number until a human reviews and approves it in Kitsas; this tool can never "
            "book money on its own. Expense lines are debited; the total is credited to the bank "
            "account unless credit_account says otherwise. Call suggest_account first so the "
            "supplier keeps landing on the same account it always has. A supplier name that "
            "identifies a partner already in the book, the same match suggest_account makes, "
            "reuses that partner and says so in the summary; only a name that matches no "
            "partner creates one. A partner matched that way, by a substring of its name "
            "rather than by its own name, gets the voucher but keeps its own business id "
            "and IBAN: use partner_id when you mean a partner outright."
        ),
        "schema": {
            "supplier_name": {"type": "string"},
            "lines": {
                "type": "array",
                "description": "Expense lines: account (int), amount (euros as a string), description (optional)",
            },
            "booking_date": {"type": "string", "description": "YYYY-MM-DD, must be in an open fiscal year"},
            "business_id": {"type": "string"},
            "iban": {"type": "string"},
            "partner_id": {
                "type": "integer",
                "description": (
                    "Use the partner with this id and do not match by name at all. "
                    "For when the name rule picks the wrong partner: a supplier whose "
                    "name is contained in another partner's name always matches that "
                    "partner, however fully it is spelled. Get the id from "
                    "find_supplier. The partner must exist. supplier_name is still "
                    "required and is then only the text written on the voucher; "
                    "partner_id decides which partner the voucher belongs to."
                ),
            },
            "invoice_date": {"type": "string", "description": "YYYY-MM-DD"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD"},
            "reference": {"type": "string"},
            "description": {"type": "string"},
            "credit_account": {"type": "integer", "description": "Defaults to the bank account"},
            "pdf_path": {"type": "string", "description": "The original invoice, attached to the voucher"},
        },
        "required": ["supplier_name", "lines", "booking_date"],
        "handler": lambda book, args: write.add_purchase_invoice(book, **args),
    },
    "delete_draft": {
        "description": (
            "Delete a voucher that is not yet in the ledger. That includes drafts Kitsas "
            "itself created, such as a document waiting in its inbox or a draft someone is "
            "still working on, not only drafts this server wrote. Refuses any voucher that "
            "has already reached the ledger."
        ),
        "schema": {"voucher_id": {"type": "integer"}},
        "required": ["voucher_id"],
        "handler": lambda book, args: write.delete_draft(book, args["voucher_id"]),
    },
    "bank_balance": {
        "description": "The book's balance on the bank account as of a date, for checking against a bank statement.",
        "schema": {
            "on_date": {"type": "string", "description": "YYYY-MM-DD"},
            "account": {"type": "integer"},
        },
        "required": ["on_date"],
        "handler": lambda book, args: reconcile.bank_balance(
            book, args["on_date"], args.get("account")
        ),
    },
    "bank_movements": {
        "description": "Every ledger entry on the bank account in a date range, with a running balance, for reconciling against a statement.",
        "schema": {
            "date_from": {"type": "string", "description": "YYYY-MM-DD"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            "account": {"type": "integer"},
        },
        "required": ["date_from", "date_to"],
        "handler": lambda book, args: reconcile.bank_movements(
            book, args["date_from"], args["date_to"], args.get("account")
        ),
    },
}


def resolve_book_path(argument):
    path = argument or os.environ.get("KITSAS_BOOK")
    if not path:
        raise SystemExit(
            "No book given. Pass --book /path/to/book.kitsas or set KITSAS_BOOK."
        )
    return Path(path)


def _validate_arguments(name, spec, args):
    """Check `args` against what this tool declared, before the handler ever runs.

    A KeyError or TypeError that only happens because the caller left out a
    required argument, or sent one that does not exist, must be reported as
    the caller's mistake. Checking the declared required list and schema
    keys up front, before the handler runs, means any KeyError or TypeError
    that still escapes the handler afterwards cannot be one of these two
    caller mistakes: it is a bug inside the handler itself, and call_tool
    reports it as such instead of blaming the caller's arguments.

    Returns an error message, or None if `args` is fine.
    """
    for required in spec["required"]:
        if required not in args:
            return f"Missing required argument {required!r} for {name}. Pass it and try again."
    unexpected = sorted(set(args) - set(spec["schema"]))
    if unexpected:
        valid = ", ".join(sorted(spec["schema"])) or "(none)"
        return (
            f"{name} does not accept argument {unexpected[0]!r}. "
            f"Its arguments are: {valid}."
        )
    return None


def call_tool(book_path, name, args):
    """Run a tool and return plain data, turning known errors into messages."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"There is no tool called {name}."}
    validation_error = _validate_arguments(name, spec, args)
    if validation_error is not None:
        return {"error": validation_error}
    try:
        return spec["handler"](Book(book_path), args)
    except KitsasError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        # Everything that is not a KitsasError. Arguments were already checked
        # above against this tool's required list and its schema's argument
        # names, so a KeyError or TypeError reaching here did not come from a
        # missing or unexpected argument, and an OperationalError ("no such
        # table", deliberately re-raised by db.connect_read rather than
        # mistaken for a lock) or an InterfaceError (a dict where a string
        # belonged) never comes from one either. All of them are bugs inside
        # the handler rather than something the caller did wrong, so they are
        # labelled as internal errors rather than reported as bad-arguments
        # messages that would send the caller back to double-check arguments
        # that were actually correct.
        #
        # Catching them at all is the point: an exception leaving here reaches
        # the MCP transport, where the failure is reported without is_error
        # set, which is the exact defect this shape exists to close.
        return {
            "error": (
                f"Internal error in {name}: {exc!r}. This is a bug in the tool, "
                "not in your arguments; it should be reported."
            )
        }


def build_server(book_path):
    """Build the MCP server, wiring TOOLS onto the installed mcp package's request handlers.

    The installed mcp package (2.x) registers request handlers through the
    Server constructor's on_list_tools/on_call_tool keywords, taking
    (ctx, params); it no longer offers the @server.list_tools()/@server.call_tool()
    decorator methods some older mcp releases had. Using the decorator form
    against this version fails immediately with AttributeError, before a
    client ever connects, so it is exercised by a test rather than only
    discovered by hand.
    """
    from mcp.server import Server
    from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

    async def on_list_tools(ctx, params):
        return ListToolsResult(
            tools=[
                Tool(
                    name=name,
                    description=spec["description"],
                    inputSchema={
                        "type": "object",
                        "properties": spec["schema"],
                        # Without this the model is told what every argument
                        # means but never which ones it has to send.
                        "required": spec["required"],
                    },
                )
                for name, spec in TOOLS.items()
            ]
        )

    async def on_call_tool(ctx, params):
        result = call_tool(book_path, params.name, params.arguments or {})
        # call_tool catches every Exception and returns it as a dict with an
        # "error" key, so a failure always arrives here as that key rather
        # than as an exception on its way to the transport. Leaving is_error
        # at its default (False) would tell an MCP client that a refused
        # write - a confirmed fiscal year, a locked book, an unknown account
        # - completed successfully, because the failure is otherwise visible
        # only by inspecting the JSON payload for that key.
        is_error = isinstance(result, dict) and "error" in result
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))],
            is_error=is_error,
        )

    return Server("kitsas-mcp", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def main():
    parser = argparse.ArgumentParser(description="MCP server for a local Kitsas book")
    parser.add_argument("--book", help="Path to the .kitsas file. Kitsas must not have it open.")
    args = parser.parse_args()
    book_path = resolve_book_path(args.book)

    import asyncio

    from mcp.server.stdio import stdio_server

    async def run():
        server = build_server(book_path)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
