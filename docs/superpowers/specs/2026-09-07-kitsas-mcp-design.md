# kitsas-mcp design

Date: 2026-09-07
Status: approved for implementation planning

## Purpose

An MCP server that lets Claude read a local Kitsas bookkeeping file and enter
purchase invoices into it from PDF invoices and receipts, without the treasurer
retyping supplier, dates, sums and account for every bill.

Kitsas is Finnish open source bookkeeping software. A locally stored book is a
single SQLite file with a `.kitsas` extension. No MCP server for Kitsas exists
today; this is the first.

The immediate user is Fuusio ry, a non VAT registered association. Kapital ry is
a second book used as a contrasting test case.

## Scope

In scope:

- Reading the chart of accounts, fiscal years, partners, vouchers and entries
- Suggesting an expense account from the supplier's own booking history
- Creating purchase invoice vouchers as drafts, with the PDF attached
- Deleting a draft the server created
- Reconciling the bank account against a real bank statement

Out of scope, deliberately:

- Reading or extracting data from PDFs. Claude does that and passes the fields
  to the server. Keeps the server free of OCR dependencies and keeps the risky
  code small.
- VAT handling. Both target books record `alvkoodi = 0` on every entry.
- Sales invoices, payroll, depreciation, fiscal year closing.
- Open item (`eraid`) tracking and payment matching. Deferred; see "Deferred".
- Any write to a voucher that is already in the ledger.

## Constraints discovered in the source and the data

These were verified against the Kitsas source at `artoh/kitupiikki` and against
two real books, not assumed.

**The book must be closed.** `kitsas/sqlite/sqlitemodel.cpp:174` issues
`PRAGMA LOCKING_MODE = EXCLUSIVE` together with `PRAGMA JOURNAL_MODE = WAL`.
While Kitsas has a book open it holds the file exclusively and no other process
can read it, let alone write. There is no way around this and no reason to try.
The server detects the lock and says so plainly.

**Voucher states.** `kitsas/model/tosite.h` defines
`POISTETTU = 0, MALLIPOHJA = 5, HYLATTY = 10, SAAPUNUT = 20, TARKASTETTU = 30,
HYVAKSYTTY = 40, LUONNOS = 50, KIRJANPIDOSSA = 100`. Only vouchers at
`tila >= 100` are in the ledger. The server writes at `tila = 20` and never
higher.

**Voucher numbering.** `TositeRoute::patch` in
`kitsas/sqlite/routes/tositeroute.cpp` allocates `tunniste` from
`MAX(tunniste)` within the fiscal year, and only on the transition to
`tila >= 100`. A draft therefore never consumes a voucher number. Kitsas
assigns it when the user approves the draft.

**Entry type is composed.** `Vienti.tyyppi` is the voucher type plus the row
role, using `VientiTyyppi` from `kitsas/model/tositevienti.h`
(`KIRJAUS = 1`, `VASTAKIRJAUS = 2`, `OSTO = 100`). Fuusio's expense vouchers
carry `101` on expense rows and `102` on the bank counter row, confirming
`OSTO + KIRJAUS` and `OSTO + VASTAKIRJAUS`.

**Account names are JSON.** `Tili` has columns `numero`, `tyyppi`, `iban`,
`json`, `muokattu`. The name lives at `json.nimi.fi` and `json.nimi.sv`. There
is no name column. `tyyppi` is a short code: `ARP` for a bank account, `BO` for
current payables, `DZ` for expense accounts.

**Attachments.** `Liite` stores the file as a blob in `data`, with `nimi` the
original filename, `tyyppi` the MIME type, `sha` a hex sha256, and
`roolinimi` NULL for ordinary attachments. `UNIQUE(tosite, roolinimi)` permits
many NULL-role attachments per voucher; Fuusio has up to 9 on one voucher.

**Balance is enforced by the schema.** `Vienti` carries
`CHECK (debetsnt = 0 OR kreditsnt = 0)`, so each row is debit or credit, never
both. Amounts are integer cents in `BIGINT` columns.

**Both target books are non VAT.** `alvkoodi` is 0 on every entry in both.

**Schema version.** `Asetus` holds `KpVersio = 24` in both books.

### How the two books differ

| | Fuusio ry | Kapital ry |
|---|---|---|
| Vouchers | 307, fiscal years 2023-2026 | 429, fiscal year 2023-2024 |
| Expense voucher (`tyyppi = 100`) | debit expense, credit 1910 Pankkitili | debit expense, credit 2960 Ostovelat |
| `erapvm` | never set, 0 of 197 | set on 12 of 111 |
| Payment | none, paid at booking | separate `tyyppi = 300` voucher, 65 of them |
| `eraid` | 1 in the whole book | in use with 2960 |

Fuusio's expenses are mostly member reimbursements ("Reimbursement for event
supplies", "Reimbursement for annual ball representation") with
`pvm == laskupvm` and no due date. The money has already left the bank; there
was never a payable. Crediting 1910 is correct for that, not a shortcut.

The server therefore books paid-from-bank by default, and takes the credit
account as a parameter so Kapital's payable pattern is reachable by passing
2960 instead. Only open item tracking is genuinely extra work, and it is
deferred.

**Neither book has ever imported a bank statement.** `arkistotunnus` is NULL on
all 305 entries touching 1910 in Fuusio and all 288 in Kapital. Nothing has
ever reconciled the book's bank account against the actual bank. Since every
automated bill asserts a payment that left the account, an error in one makes
1910 drift silently. The reconciliation tool exists to make that findable.

## Architecture

Python 3.11+, the `mcp` SDK, stdio transport, so it works from both Claude Code
and Claude Desktop.

| Module | Responsibility |
|---|---|
| `db.py` | Open the book, run the safety checks, take the session backup, expose a connection |
| `money.py` | Euro string to integer cents and back, via `Decimal`. No floats anywhere near money |
| `accounts.py` | Chart of accounts: JSON name extraction, lookup by number, resolve the bank and payable accounts by `tyyppi` code |
| `read.py` | All queries. No mutation |
| `history.py` | The supplier to account history join behind `suggest_account` |
| `write.py` | The only module that mutates. Voucher creation and draft deletion |
| `reconcile.py` | Bank account movement listing and balance as of a date |
| `server.py` | Thin MCP tool definitions over the above |

Each module is independently testable and holds one concern. `write.py` is kept
deliberately small because it is the only place that can damage a book.

## Opening a book

Four checks in order, each failing with a message that names the fix rather
than a stack trace, because Claude relays it to the user:

1. **It is a Kitsas book.** The expected tables are present and `Asetus` has a
   `KpVersio` key. Otherwise: "This file is not a Kitsas book."
2. **It is not locked.** `busy_timeout` is set to 2000 ms so an open book fails
   fast. Otherwise: "Kitsas has this book open. Close Kitsas and try again."
3. **The schema version is known.** `KpVersio` is compared against a supported
   set, currently `{24}`. An unknown version allows reads and refuses writes,
   with a message saying so.
4. **A backup exists.** Before the first write of a session, copy the book plus
   any `-wal` and `-shm` sidecars to `<name>.YYYYMMDD-HHMMSS.bak` beside the
   original. The tool response states the backup path.

## Tools

### Read

- `list_accounts(search=None)`: account number, Finnish name, type code. Filters
  by substring of number or name.
- `list_fiscal_years()`: from `Tilikausi`, with the current one marked.
- `find_supplier(query)`: `Kumppani` by name, business id or IBAN.
- `list_vouchers(date_from, date_to, supplier=None, account=None, state=None)`:
  defaults to `tila >= 100` so deleted and draft vouchers do not pollute normal
  queries. Passing `state` opts in to drafts explicitly.
- `get_voucher(id)`: header, all entries with account names, attachment names.

### suggest_account

Joins `Kumppani` to that supplier's past vouchers of `tyyppi = 100` at
`tila >= 100`, and returns each expense account used, with a hit count and the
most recent date, ordered by count.

Returns an empty list for an unknown supplier. Claude then picks from the chart
of accounts and marks the choice as a guess in the voucher's `selite`, so the
treasurer's attention goes to the right place when approving.

### add_purchase_invoice

Arguments: `supplier_name`, `business_id=None`, `iban=None`, `booking_date`,
`invoice_date=None`, `due_date=None`, `reference=None`, `description`,
`lines` (a list of `{account, amount, description}`), `credit_account=None`
defaulting to the book's bank account, `pdf_path=None`.

Multiple expense lines are supported from the start because Fuusio's real
vouchers have them; voucher 289 has four expense rows against one bank row.

In one transaction:

1. Upsert `Kumppani` by name and business id; upsert `KumppaniIban` on the IBAN.
2. Insert `Tosite`: `tyyppi = 100`, `tila = 20`, `tunniste` NULL, `pvm`,
   `laskupvm`, `erapvm`, `viite`, `otsikko`, `kumppani`.
3. Insert the counter row as `rivi = 1`, `tyyppi = 102`, on `credit_account`,
   with `kreditsnt` equal to the total.
4. Insert each expense line as `rivi = 2..n`, `tyyppi = 101`, with `debetsnt`.
5. If a PDF was given, insert `Liite` with the blob, its sha256, MIME type and
   original filename, `roolinimi` NULL.
6. Insert `Tositeloki` with `tila = 20` and the request as JSON.

Returns the new voucher id and a plain sentence describing exactly what was
written, including the backup path if one was taken this session.

On any exception the transaction rolls back and the file is unchanged.

### delete_draft

Sets `tila = 0` and writes a `Tositeloki` row, mirroring `TositeRoute::doDelete`.
Refuses on any voucher at `tila >= 100`.

### Bank reconciliation

`bank_balance(date, account=None)` returns the book's balance on the bank
account as of a date. `bank_movements(date_from, date_to, account=None)` lists
every entry touching it with date, counterparty, description, amount and
running balance, so the treasurer can tick the books against a statement and
find where they diverge. Read only.

## Invariants, enforced in code rather than by convention

- Never write `tila >= 100`.
- Never update or delete any voucher at `tila >= 100`. Booked history is
  permanently read only through this server.
- Never allocate `tunniste`.
- Debits must equal credits, asserted before commit.
- The booking date must fall inside an existing `Tilikausi`, or the write is
  refused. The server never creates a fiscal year.
- All money is integer cents, converted by one audited function.
- Writes are refused entirely on an unrecognised `KpVersio`.

## Error handling

Every failure returns a sentence naming the cause and the fix: "Kitsas has this
book open. Close Kitsas and try again", "No fiscal year in this book covers
2027-01-03", "Account 4340 does not exist in this book", "This voucher is
already in the ledger and cannot be changed here". Never a bare traceback.

## Testing

Two tiers, because a passing suite proves only that the server wrote what it
intended, not that Kitsas accepts it.

**Synthetic book.** Kitsas's own `luo.sql` is vendored as a test fixture and a
book is built from it in a temp directory, with a small chart of accounts, two
fiscal years and a few historical vouchers. The whole suite runs against this,
so anyone who clones the repo can run the tests with no private data.

**Real books.** Used through a temp copy, never the original, from a path given
by an environment variable. Tests needing them skip cleanly when absent so the
public repo stays runnable.

Written test first, at minimum:

- Euro to cent conversion, including values that break floats
- The debit equals credit assertion, rejecting an unbalanced voucher
- The fiscal year check, rejecting an out of range date
- The `tila >= 100` refusal on both update and delete
- The lock detection path
- Round trip: write a draft, read it back through `read.py`, assert every field
- `suggest_account` returns the historically used account for a known supplier
  and an empty list for an unknown one

**The verification that decides whether this works** is manual and cannot be
done from the development machine: write a draft, open the book in Kitsas,
confirm the bill appears with its PDF attached and approves into the ledger
cleanly. Until that passes on the machine running Kitsas, the tool is unproven
regardless of test results.

## Milestones

1. Read only core: `db.py` with all four checks, `accounts.py`, `read.py`, and
   the read tools. Useful on its own for asking questions about a book.
2. `suggest_account`.
3. `add_purchase_invoice`, attachments, `delete_draft`.
4. `bank_balance` and `bank_movements`.
5. Verification in real Kitsas against the Fuusio book.

## Deferred

- **Open items and payment matching** (`eraid`, `tyyppi = 300` payment
  vouchers). Needed only for the payable flow. Fuusio has one `eraid` in the
  entire book, so this is not needed now. Revisit if Fuusio starts recording
  unpaid invoices.
- **VAT.** Add when a book that uses it appears.
- **Cloud books.** The tool surface deliberately mirrors the route names of the
  documented cloud API at `api.kitsas.fi`, so a future transport swap could
  reach cloud books with the same tools.

## Repository

`github.com/Valtterios/kitsas-mcp`, public. `.gitignore` blocks `*.kitsas`,
`*.sqlite`, `*.bak`, `*.pdf` and `tests/fixtures/books/` from the first commit,
before there is anything to protect. No real book, invoice or association data
is ever committed.
