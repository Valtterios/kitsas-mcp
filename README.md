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

## A book on a network share

A book kept on a NAS or a Windows share works, addressed either by its UNC
path (`\\server\share\kirjanpito.kitsas`) or through a mapped drive letter.
The same rule applies as for a local book: Kitsas itself must have it closed,
and so must every other Kitsas on the network, because the exclusive lock is
on the file.

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
- `list_fiscal_years` - list fiscal years, showing which is current and which are confirmed and closed to writes. `confirmed` is the confirmation date or null; `confirmed_unknown` is true for a year whose stored data cannot be read, which is closed to writes too.
- `find_supplier` - find a partner by name, business id or IBAN; returns each partner's id, name, business id and IBANs. Names also match with their accents ignored, so searching `Karkkainen` shows the book's `Kärkkäinen`.
- `list_vouchers` - list vouchers in a date range, ledger vouchers by default.
- `get_voucher` - get one voucher with all its entries and attachment names.
- `suggest_account` - which expense accounts a supplier's earlier bills were booked to, most used first; call this before `add_purchase_invoice`.
- `add_purchase_invoice` - create a purchase invoice as an unapproved draft; never enters the ledger on its own. `partner_id` names the partner outright when the supplier name would find the wrong one, and `confirm_new_partner` says that a name resembling an existing partner really is a different supplier.
- `delete_draft` - delete a voucher that is not yet in the ledger, including a draft Kitsas itself created; refuses anything already in the ledger.
- `bank_balance` - the book's balance on the bank account as of a date, for checking against a statement.
- `bank_movements` - every ledger entry on the bank account in a date range, with a running balance.

## Safety

These are the invariants the code enforces, not just intentions:

- No write this server makes ever lands at or above the ledger threshold. `add_purchase_invoice` writes in the draft state (`TILA_SAAPUNUT`) that Kitsas itself uses for an unapproved incoming document, and `delete_draft` writes the deleted state (`TILA_POISTETTU`) Kitsas uses for a discarded one. Nothing this server writes can be mistaken for a booked entry.
- A draft never gets a voucher number (`tunniste`); Kitsas allocates that only when a human approves it. `add_purchase_invoice` verifies this by reading the row back before committing.
- Nothing can be written into a confirmed fiscal year. `add_purchase_invoice` looks up the fiscal year for the booking date and refuses if it has been confirmed, naming the confirmation date.
- No write ever proceeds without a backup of the book in place, alongside its `-wal`/`-shm` sidecars if present. A write is refused, and the book left untouched, if the backup cannot be made. One backup is taken per book per server session, not per write: the first write copies the book and every later write in that session is protected by that same copy. See "Backups" below for what that costs and what it does not.
- A refused write costs no backup. Everything that can be judged without writing is judged on a read connection first: the fiscal year, the accounts, the amounts, the attachment, an explicit `partner_id`, an ambiguous or resembling supplier name, an IBAN that belongs to another partner. The same checks run again inside the write transaction, so nothing rests on the earlier answer still being true. A write that does open a transaction and then rolls back removes the backup it took, but only when that backup protects no write that has already succeeded.
- Anything already in the ledger is read-only through this server. `delete_draft` refuses a voucher whose state has reached the ledger threshold, both before and again inside the write transaction, and no update statement it issues can touch such a voucher even on its own.
- A supplier name is resolved to a partner the same way everywhere: `find_supplier`, `suggest_account` and `add_purchase_invoice` share one rule, so the account history you are shown belongs to the partner the voucher is then attached to. A name that identifies a partner already in the book joins that partner instead of forking a duplicate beside it, and a match on a name that is not the partner's own is named in the summary. A name matching several partners is refused, listing them: which supplier a bill belongs to is the bookkeeper's call. Only a name that matches no partner creates one, and not even then when an existing partner differs from it only in accents (the next point). Names are compared case-folded with Python's `str.casefold`, not with SQLite's `lower()` and `LIKE`, which fold ASCII only: in a Finnish book `Kärkkäinen Oy` and `KÄRKKÄINEN OY` have to be one partner, and under `lower()` they were two.
- A supplier name that matches no partner, but that a partner already in the book matches once accents are ignored, is refused rather than quietly creating a second partner. `Karkkainen` billed against a book holding `Kärkkäinen Lahti` matched nothing, because case folding leaves `ä` alone, and the book grew a duplicate supplier with no warning; dropped umlauts are ordinary in Finnish practice, from an OCR pass, a supplier's ASCII-only billing system or a treasurer typing quickly. The refusal names the existing partner and its id. It is a refusal and not a remark because in Finnish `ä` and `a` are different letters, so the two spellings genuinely can be two different suppliers, and choosing between real alternatives in someone's books is not this server's call. Both ways out are in the message: `partner_id` books onto the partner that is already there, and `confirm_new_partner` creates the new one beside it. `find_supplier` matches accent-blind too, so the resemblance can be seen before a bill is entered rather than after it is refused.
- Only the voucher is written when the supplier name matched a partner by a substring of its name rather than by the name itself. That partner keeps its own business id and its own IBAN, and the summary says both were skipped. The voucher is a draft `delete_draft` can reverse; an edit to a partner that already existed is not, and a substring match is a guess: a sole trader "Nieminen" matches the member "Kari Nieminen", and it would be that member's record taking the supplier's bank account.
- `partner_id` is the way out when the name rule cannot reach the right partner, which is exactly the case above: a supplier whose real name is contained in another partner's name matches that partner however fully it is spelled. Given `partner_id`, `add_purchase_invoice` uses that partner with no name matching at all and refuses if no such partner exists; `supplier_name` is then only the text written on the voucher, and the summary says which partner the voucher went to when the two differ. `find_supplier` gives the ids.
- An IBAN already bound to one partner is never silently re-pointed to another. `add_purchase_invoice` refuses instead of overwriting the binding, naming who the IBAN currently belongs to.
- An attachment over 20 MB is refused before it is even read from disk, so an oversized file is never copied into the book or into every future backup of it.
- A business id read off an invoice is written onto an existing partner only when that partner has no business id yet, no vouchers in the ledger, and was identified exactly, by its own name or by `partner_id`. One already on file is left alone, and so is a blank one on a partner that already has bookkeeping history: the draft is still written, and the summary names the partner that was left as it was, so the correction can be made in Kitsas. A wrong value read from a scan would otherwise be undoable except by restoring a backup.
- Every failure an MCP client sees is a message naming the cause and the fix, flagged as an error on the response rather than only inside the JSON payload. That includes failures the code did not anticipate: an unexpected exception inside a tool is reported as an internal error, not raised into the transport where it would be delivered without the error flag set.

### Backups

The first write to a book in a server session copies the whole book, plus its
`-wal`/`-shm` sidecars, to a timestamped `.kitsas.<stamp>.bak` beside it. Every
later write in that session reuses that copy and copies nothing, and a call
that is refused before it writes copies nothing either. Restarting the client,
which restarts this server, starts a new session and so takes a new copy at
its first write.

That is much cheaper than a copy per bill: ten bills against a 110 MB book
cost 110 MB rather than 1.1 GB, and refused calls cost nothing at all. The
trade-off is real and worth knowing: the copy is the book as it stood before
the session's first write, so if a bad booking is noticed after several later
ones, restoring that copy undoes them all. It is the last resort rather than
the first. A draft this server wrote is reversible with `delete_draft`, and
Kitsas itself is the undo for anything already approved; the `.bak` is for the
case where neither of those will do.

Nothing prunes the `.bak` files. Each one contains every PDF embedded in the
book, so they are large. Tidy the old `.kitsas.*.bak` files yourself now and
then, keeping the recent ones.
