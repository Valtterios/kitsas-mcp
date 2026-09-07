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
        "description": "List the book's chart of accounts. Optional search matches the account name or number.",
        "schema": {"search": {"type": "string", "description": "Substring of the name or number"}},
        "handler": lambda book, args: accounts.list_accounts(book, args.get("search")),
    },
    "list_fiscal_years": {
        "description": "List fiscal years, showing which is current and which have been confirmed. Nothing can be written into a confirmed year.",
        "schema": {},
        "handler": lambda book, args: read.list_fiscal_years(book),
    },
    "find_supplier": {
        "description": "Find a partner by name, business id or IBAN.",
        "schema": {"query": {"type": "string"}},
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
        "handler": lambda book, args: read.get_voucher(book, args["voucher_id"]),
    },
    "suggest_account": {
        "description": (
            "Which expense accounts this supplier's earlier bills were booked to, most used "
            "first. Empty for a supplier with no history. Call this before add_purchase_invoice "
            "so a recurring supplier keeps landing on the same account instead of a guess."
        ),
        "schema": {"supplier": {"type": "string", "description": "Partner name or id"}},
        "handler": lambda book, args: history.suggest_account(book, args["supplier"]),
    },
    "add_purchase_invoice": {
        "description": (
            "Create a purchase invoice as a DRAFT. It does not enter the ledger and gets no "
            "voucher number until a human reviews and approves it in Kitsas; this tool can never "
            "book money on its own. Expense lines are debited; the total is credited to the bank "
            "account unless credit_account says otherwise. Call suggest_account first so the "
            "supplier keeps landing on the same account it always has."
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
            "invoice_date": {"type": "string", "description": "YYYY-MM-DD"},
            "due_date": {"type": "string", "description": "YYYY-MM-DD"},
            "reference": {"type": "string"},
            "description": {"type": "string"},
            "credit_account": {"type": "integer", "description": "Defaults to the bank account"},
            "pdf_path": {"type": "string", "description": "The original invoice, attached to the voucher"},
        },
        "handler": lambda book, args: write.add_purchase_invoice(book, **args),
    },
    "delete_draft": {
        "description": "Delete a draft this server created. Refuses any voucher already in the ledger.",
        "schema": {"voucher_id": {"type": "integer"}},
        "handler": lambda book, args: write.delete_draft(book, args["voucher_id"]),
    },
    "bank_balance": {
        "description": "The book's balance on the bank account as of a date, for checking against a bank statement.",
        "schema": {
            "on_date": {"type": "string", "description": "YYYY-MM-DD"},
            "account": {"type": "integer"},
        },
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


def call_tool(book_path, name, args):
    """Run a tool and return plain data, turning known errors into messages."""
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"There is no tool called {name}."}
    try:
        return spec["handler"](Book(book_path), args)
    except KitsasError as exc:
        return {"error": str(exc)}
    except KeyError as exc:
        return {"error": f"Missing required argument {exc} for {name}."}
    except TypeError as exc:
        # A call whose arguments do not match the underlying function's
        # signature (an unexpected argument name, most often) surfaces here
        # as a raw TypeError rather than a KitsasError, because it never
        # reaches our own validation code. Without this, it would escape
        # call_tool as a traceback instead of the {"error": ...} shape every
        # other failure uses.
        return {"error": f"{name} was called with bad arguments {args!r}: {exc}"}


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
                    },
                )
                for name, spec in TOOLS.items()
            ]
        )

    async def on_call_tool(ctx, params):
        result = call_tool(book_path, params.name, params.arguments or {})
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
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
