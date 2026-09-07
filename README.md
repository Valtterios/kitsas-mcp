# kitsas-mcp

An MCP server for local [Kitsas](https://kitsas.fi/) bookkeeping files. Kitsas
is a Finnish double-entry bookkeeping application that stores each book as a
single SQLite `.kitsas` file on disk. As far as is known, this is the first
MCP server built for Kitsas: it lets an MCP client such as Claude read a
book's accounts, fiscal years, partners and vouchers, and create purchase
invoice drafts directly from a chat, for example from a PDF invoice dropped
into the conversation.

## Kitsas must have the book closed

Kitsas holds an open book's file with `PRAGMA LOCKING_MODE = EXCLUSIVE`.
While Kitsas has the book open, no other process, including this server, can
read or write it: every attempt fails with a locked-database error. Close the
book in Kitsas before pointing this server at it. This is not a limitation of
this server; it is how Kitsas itself protects the file.

## Drafts only, never the ledger

This server never books anything into the ledger. `add_purchase_invoice`
writes a new voucher in an unnumbered draft state, exactly like a document
Kitsas received but nobody has looked at yet. It gets no voucher number and
is invisible to a normal ledger listing. A human still has to open Kitsas,
review the draft, and approve it before it becomes part of the book's
bookkeeping. `delete_draft` is the only other tool that writes, and it can
only discard a voucher that has not reached the ledger. Every other tool
only reads.

## Install

From a clone:

```bash
git clone <this repository>
cd kitsas-mcp
uv tool install .
```

Or, once published:

```bash
uv tool install kitsas-mcp
```

Either way you end up with a `kitsas-mcp` command on your PATH, which an MCP
client launches with `--book /path/to/book.kitsas` or the `KITSAS_BOOK`
environment variable pointing at the closed `.kitsas` file.

### Claude Desktop

Add this to your Claude Desktop MCP server configuration:

```json
{
  "mcpServers": {
    "kitsas": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/kitsas-mcp", "kitsas-mcp"],
      "env": { "KITSAS_BOOK": "/path/to/kirjanpito.kitsas" }
    }
  }
}
```

### Claude Code

```bash
claude mcp add kitsas -e KITSAS_BOOK=/path/to/kirjanpito.kitsas -- uv run --directory /path/to/kitsas-mcp kitsas-mcp
```

## Tools

- `list_accounts` - list the book's chart of accounts, optionally filtered by a substring of the name or the first digits of the number.
- `list_fiscal_years` - list fiscal years, showing which is current and which are confirmed and closed to writes.
- `find_supplier` - find a partner by name, business id or IBAN.
- `list_vouchers` - list vouchers in a date range, ledger vouchers by default.
- `get_voucher` - get one voucher with all its entries and attachment names.
- `suggest_account` - which expense accounts a supplier's earlier bills were booked to, most used first; call this before `add_purchase_invoice`.
- `add_purchase_invoice` - create a purchase invoice as an unapproved draft; never enters the ledger on its own.
- `delete_draft` - delete a voucher that is not yet in the ledger, including a draft Kitsas itself created; refuses anything already in the ledger.
- `bank_balance` - the book's balance on the bank account as of a date, for checking against a statement.
- `bank_movements` - every ledger entry on the bank account in a date range, with a running balance.

## Safety

These are the invariants the code enforces, not just intentions:

- No write this server makes ever lands at or above the ledger threshold. `add_purchase_invoice` writes in the draft state (`TILA_SAAPUNUT`) that Kitsas itself uses for an unapproved incoming document, and `delete_draft` writes the deleted state (`TILA_POISTETTU`) Kitsas uses for a discarded one. Nothing this server writes can be mistaken for a booked entry.
- A draft never gets a voucher number (`tunniste`); Kitsas allocates that only when a human approves it. `add_purchase_invoice` verifies this by reading the row back before committing.
- Nothing can be written into a confirmed fiscal year. `add_purchase_invoice` looks up the fiscal year for the booking date and refuses if it has been confirmed, naming the confirmation date.
- A backup of the book is taken before every write transaction, alongside its `-wal`/`-shm` sidecars if present. A write is refused, and the book left untouched, if the backup cannot be made.
- Anything already in the ledger is read-only through this server. `delete_draft` refuses a voucher whose state has reached the ledger threshold, both before and again inside the write transaction, and no update statement it issues can touch such a voucher even on its own.
- An IBAN already bound to one partner is never silently re-pointed to another. `add_purchase_invoice` refuses instead of overwriting the binding, naming who the IBAN currently belongs to.
- An attachment over 20 MB is refused before it is even read from disk, so an oversized file is never copied into the book or into every future backup of it.
- A business id read off an invoice is written onto an existing partner only when that partner has no business id yet and no vouchers in the ledger. One already on file is left alone, and so is a blank one on a partner that already has bookkeeping history: the draft is still written, and the summary names the partner that was left as it was, so the correction can be made in Kitsas. A wrong value read from a scan would otherwise be undoable except by restoring a backup.

### Backups accumulate

Every write takes its own timestamped `.bak` copy of the whole book, plus its
`-wal`/`-shm` sidecars, in the same folder as the book. Nothing prunes them.
Each copy contains every PDF embedded in the book, so a book with many
attachments grows a folder of large duplicates over a year of use. Tidy the
old `.kitsas.*.bak` files yourself now and then, keeping the recent ones.
