# Kitsas MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An MCP server that reads a local Kitsas bookkeeping file and enters purchase invoices into it as drafts, from PDF invoices that Claude has read.

**Architecture:** A Python package exposing MCP tools over stdio. Claude extracts the fields from the PDF and calls the tools; the server only does correct database access. Reads go through a read-only SQLite connection. Writes go through one small module that creates vouchers at draft state only, in a single transaction, after a backup. The book must be closed in Kitsas because Kitsas holds the file with `PRAGMA LOCKING_MODE = EXCLUSIVE`.

**Tech Stack:** Python 3.11+, the `mcp` SDK, stdlib `sqlite3`, `pytest`, `uv` for install. No ORM, no OCR, no network access at runtime.

**Spec:** `docs/superpowers/specs/2026-09-07-kitsas-mcp-design.md`

## Global Constraints

- Python 3.11 or newer.
- Money is integer cents everywhere. `Decimal` for parsing, never `float`.
- Never write a voucher at `tila >= 100`. Never update or delete a voucher at `tila >= 100`.
- Never allocate `Tosite.tunniste`. Kitsas does that when the user approves the draft.
- Supported `Asetus.KpVersio` values: `{24}`. Unknown version allows reads, refuses writes.
- Every error raised to the user is a `KitsasError` subclass whose message names the cause and the fix. No bare tracebacks reach the MCP client.
- Never commit a `.kitsas`, `.sqlite`, `.bak` or `.pdf` file. The `.gitignore` already blocks them.
- Commits use the repository owner's name. No Claude attribution, no `Co-Authored-By` trailers.
- All Finnish column and table names from the Kitsas schema are kept verbatim in SQL. Python identifiers are English.

## Kitsas constants (used across tasks)

Verified against `artoh/kitupiikki` and against two real books.

| Constant | Value | Meaning |
|---|---|---|
| `TILA_POISTETTU` | 0 | deleted |
| `TILA_SAAPUNUT` | 20 | arrived, the state this server writes |
| `TILA_LUONNOS` | 50 | draft |
| `TILA_KIRJANPIDOSSA` | 100 | in the ledger, read only for us |
| `TOSITE_MENO` | 100 | purchase or expense voucher |
| `VIENTI_OSTO_KIRJAUS` | 101 | expense row (`OSTO 100 + KIRJAUS 1`) |
| `VIENTI_OSTO_VASTAKIRJAUS` | 102 | counter row (`OSTO 100 + VASTAKIRJAUS 2`) |
| `TILITYYPPI_PANKKI` | `"ARP"` | bank account |
| `TILITYYPPI_OSTOVELAT` | `"BO"` | current payables |

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, deps, console script `kitsas-mcp` |
| `README.md` | What it is, install, client config, the closed-book rule |
| `src/kitsas_mcp/constants.py` | The table above, as module constants |
| `src/kitsas_mcp/errors.py` | `KitsasError` and subclasses |
| `src/kitsas_mcp/money.py` | Euro to cent conversion |
| `src/kitsas_mcp/db.py` | `Book`: open, safety checks, backup, connections |
| `src/kitsas_mcp/accounts.py` | Chart of accounts, JSON names, account resolution |
| `src/kitsas_mcp/read.py` | Queries over vouchers, partners, fiscal years |
| `src/kitsas_mcp/history.py` | `suggest_account` supplier history join |
| `src/kitsas_mcp/write.py` | Voucher creation and draft deletion. The only mutating module |
| `src/kitsas_mcp/reconcile.py` | Bank balance and movements |
| `src/kitsas_mcp/server.py` | MCP tool definitions, thin |
| `tests/fixtures/luo.sql` | Kitsas schema, vendored from upstream |
| `tests/conftest.py` | Builds a synthetic book from `luo.sql` |
| `tests/test_*.py` | One per module |

---

### Task 1: Project scaffold, constants, errors, money

**Files:**
- Create: `pyproject.toml`, `src/kitsas_mcp/__init__.py`, `src/kitsas_mcp/constants.py`, `src/kitsas_mcp/errors.py`, `src/kitsas_mcp/money.py`
- Test: `tests/test_money.py`

**Interfaces:**
- Consumes: nothing
- Produces: `euros_to_cents(value) -> int`, `cents_to_euros(cents: int) -> str`, `KitsasError` and subclasses, the constants above

- [ ] **Step 1: Create the package skeleton**

`pyproject.toml`:

```toml
[project]
name = "kitsas-mcp"
version = "0.1.0"
description = "MCP server for local Kitsas bookkeeping files"
requires-python = ">=3.11"
dependencies = ["mcp>=1.2.0"]

[project.scripts]
kitsas-mcp = "kitsas_mcp.server:main"

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/kitsas_mcp"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create `src/kitsas_mcp/__init__.py` as an empty file.

- [ ] **Step 2: Write constants and errors**

`src/kitsas_mcp/constants.py`:

```python
"""Constants verified against the Kitsas source and against real books."""

TILA_POISTETTU = 0
TILA_SAAPUNUT = 20
TILA_LUONNOS = 50
TILA_KIRJANPIDOSSA = 100

TOSITE_MENO = 100

VIENTI_OSTO_KIRJAUS = 101
VIENTI_OSTO_VASTAKIRJAUS = 102

TILITYYPPI_PANKKI = "ARP"
TILITYYPPI_OSTOVELAT = "BO"

SUPPORTED_KPVERSIO = {24}
```

`src/kitsas_mcp/errors.py`:

```python
"""Errors whose messages are shown to the user by the MCP client."""


class KitsasError(Exception):
    """Base class. The message must name the cause and the fix."""


class NotAKitsasBookError(KitsasError):
    pass


class BookLockedError(KitsasError):
    pass


class UnsupportedSchemaError(KitsasError):
    pass


class NoFiscalYearError(KitsasError):
    pass


class ClosedFiscalYearError(KitsasError):
    pass


class UnbalancedVoucherError(KitsasError):
    pass


class AccountNotFoundError(KitsasError):
    pass


class LedgerVoucherError(KitsasError):
    pass


class AmountError(KitsasError):
    pass
```

- [ ] **Step 3: Write the failing money tests**

`tests/test_money.py`:

```python
import pytest

from kitsas_mcp.errors import AmountError
from kitsas_mcp.money import cents_to_euros, euros_to_cents


@pytest.mark.parametrize(
    "value,expected",
    [
        ("12.34", 1234),
        ("0.01", 1),
        ("1000", 100000),
        ("428.38", 42838),
        ("0.1", 10),
        ("1 234,56", 123456),
        ("-5.00", -500),
        (12.34, 1234),
        (0, 0),
    ],
)
def test_euros_to_cents(value, expected):
    assert euros_to_cents(value) == expected


def test_float_that_breaks_naive_conversion():
    # int(1.15 * 100) is 114 because 1.15 is not representable in binary.
    assert euros_to_cents("1.15") == 115
    assert euros_to_cents(1.15) == 115


def test_more_than_two_decimals_is_rejected():
    with pytest.raises(AmountError):
        euros_to_cents("1.234")


def test_non_numeric_is_rejected():
    with pytest.raises(AmountError):
        euros_to_cents("about ten euros")


@pytest.mark.parametrize("cents,expected", [(1234, "12.34"), (1, "0.01"), (0, "0.00"), (-500, "-5.00")])
def test_cents_to_euros(cents, expected):
    assert cents_to_euros(cents) == expected
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_money.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.money'`

- [ ] **Step 5: Implement money.py**

`src/kitsas_mcp/money.py`:

```python
"""Money is integer cents. Floats never touch a stored amount."""

from decimal import Decimal, InvalidOperation

from .errors import AmountError

CENTS = Decimal("0.01")


def euros_to_cents(value) -> int:
    """Convert a euro amount to integer cents.

    Accepts a string (with an optional space thousands separator and a comma
    or dot decimal separator), an int, a float or a Decimal. Floats are routed
    through str() so that 1.15 becomes 115 rather than 114.
    """
    if isinstance(value, Decimal):
        amount = value
    else:
        text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            amount = Decimal(text)
        except InvalidOperation:
            raise AmountError(f"{value!r} is not an amount of money.") from None

    if amount != amount.quantize(CENTS):
        raise AmountError(f"{value!r} has more precision than one cent.")

    return int(amount.quantize(CENTS) * 100)


def cents_to_euros(cents: int) -> str:
    """Format integer cents as a plain euro string, for display only."""
    return f"{Decimal(cents) / 100:.2f}"
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_money.py -v`
Expected: PASS, 15 passed

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/kitsas_mcp tests/test_money.py
git commit -m "Add package scaffold, Kitsas constants, errors and money conversion"
```

---

### Task 2: The synthetic test book, and opening a book safely

**Files:**
- Create: `tests/fixtures/luo.sql`, `tests/conftest.py`, `src/kitsas_mcp/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `constants.SUPPORTED_KPVERSIO`, the error classes
- Produces: `Book(path)` with `.connect_read()`, `.connect_write()`, `.backup()`, `.kpversio`, `.path`; pytest fixtures `book_path` (a synthetic book on disk) and `book` (a `Book` for it)

- [ ] **Step 1: Vendor the Kitsas schema**

```bash
mkdir -p tests/fixtures
curl -sL https://raw.githubusercontent.com/artoh/kitupiikki/master/kitsas/sqlite/luo.sql -o tests/fixtures/luo.sql
head -5 tests/fixtures/luo.sql
```

Expected: the file starts with `CREATE TABLE Asetus`. This is the real Kitsas schema, so the synthetic book has the same shape as a real one.

- [ ] **Step 2: Write conftest.py that builds a synthetic book**

`tests/conftest.py`:

```python
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Accounts mirroring the real books: bank, payables, two expense accounts.
ACCOUNTS = [
    (1910, "ARP", "Pankkitili"),
    (2960, "BO", "Ostovelat"),
    (4000, "DZ", "Tavaraostot, varsinainen toiminta"),
    (4590, "DZ", "Edustuskulut"),
    (3000, "CZ", "Jasenmaksut"),
]

# One confirmed year and one open year, as Fuusio has.
FISCAL_YEARS = [
    ("2025-01-01", "2025-12-31", {"vahvistettu": "2026-04-28"}),
    ("2026-01-01", "2026-12-31", {}),
]


def _build(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / "luo.sql").read_text(encoding="utf-8"))

    conn.execute("INSERT INTO Asetus (avain, arvo) VALUES ('KpVersio', '24')")
    conn.execute("INSERT INTO Asetus (avain, arvo) VALUES ('Nimi', 'Testi ry')")

    for numero, tyyppi, nimi in ACCOUNTS:
        conn.execute(
            "INSERT INTO Tili (numero, tyyppi, json) VALUES (?,?,?)",
            (numero, tyyppi, json.dumps({"nimi": {"fi": nimi, "sv": nimi}})),
        )

    for alkaa, loppuu, extra in FISCAL_YEARS:
        conn.execute(
            "INSERT INTO Tilikausi (alkaa, loppuu, json) VALUES (?,?,?)",
            (alkaa, loppuu, json.dumps(extra)),
        )

    # A supplier with two historical expense vouchers on 4590, so that
    # suggest_account has something to learn from.
    conn.execute("INSERT INTO Kumppani (id, nimi, json) VALUES (7, 'Hetzner', '{}')")
    for n, (pvm, cents) in enumerate([("2026-02-06", 4731), ("2026-03-06", 4731)], start=1):
        conn.execute(
            "INSERT INTO Tosite (id, pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm) "
            "VALUES (?,?,100,100,?, 'Hetzner', 7, ?)",
            (n, pvm, n, pvm),
        )
        conn.execute(
            "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
            "VALUES (1,?,102,?,1910,0,'Hetzner',0,?,7)",
            (n, pvm, cents),
        )
        conn.execute(
            "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
            "VALUES (2,?,101,?,4590,0,'Hetzner',?,0,7)",
            (n, pvm, cents),
        )

    conn.commit()
    conn.close()


@pytest.fixture
def book_path(tmp_path) -> Path:
    path = tmp_path / "testi.kitsas"
    _build(path)
    return path


@pytest.fixture
def book(book_path):
    from kitsas_mcp.db import Book

    return Book(book_path)


@pytest.fixture
def real_book_path(tmp_path):
    """A copy of a real book, from KITSAS_TEST_BOOK. Skips when unset."""
    import os

    source = os.environ.get("KITSAS_TEST_BOOK")
    if not source or not Path(source).exists():
        pytest.skip("KITSAS_TEST_BOOK is not set to an existing book")
    target = tmp_path / Path(source).name
    shutil.copy2(source, target)
    return target
```

- [ ] **Step 3: Write the failing db tests**

`tests/test_db.py`:

```python
import sqlite3

import pytest

from kitsas_mcp.db import Book
from kitsas_mcp.errors import BookLockedError, NotAKitsasBookError, UnsupportedSchemaError


def test_opens_a_kitsas_book(book):
    assert book.kpversio == 24


def test_rejects_a_file_that_is_not_a_kitsas_book(tmp_path):
    other = tmp_path / "other.sqlite"
    sqlite3.connect(other).execute("CREATE TABLE t (a)")
    with pytest.raises(NotAKitsasBookError) as excinfo:
        Book(other).kpversio
    assert "not a Kitsas book" in str(excinfo.value)


def test_rejects_a_missing_file(tmp_path):
    with pytest.raises(NotAKitsasBookError):
        Book(tmp_path / "nope.kitsas").kpversio


def test_unknown_schema_version_refuses_writes_but_allows_reads(book_path):
    conn = sqlite3.connect(book_path)
    conn.execute("UPDATE Asetus SET arvo='99' WHERE avain='KpVersio'")
    conn.commit()
    conn.close()

    b = Book(book_path)
    assert b.kpversio == 99
    with b.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tili").fetchone()[0] == 5
    with pytest.raises(UnsupportedSchemaError) as excinfo:
        with b.connect_write():
            pass
    assert "99" in str(excinfo.value)


def test_reports_a_locked_book(book_path, monkeypatch):
    holder = sqlite3.connect(book_path, isolation_level=None)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(BookLockedError) as excinfo:
            with Book(book_path).connect_write():
                pass
        assert "Close Kitsas" in str(excinfo.value)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_read_connection_cannot_write(book):
    with book.connect_read() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO Asetus (avain, arvo) VALUES ('x','y')")


def test_backup_copies_the_book_once_per_session(book, book_path):
    first = book.backup()
    assert first.exists()
    assert first.stat().st_size == book_path.stat().st_size
    assert book.backup() == first, "a second backup in the same session reuses the first"
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.db'`

- [ ] **Step 5: Implement db.py**

`src/kitsas_mcp/db.py`:

```python
"""Opening a Kitsas book, with the checks that keep a real book safe."""

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .constants import SUPPORTED_KPVERSIO
from .errors import BookLockedError, NotAKitsasBookError, UnsupportedSchemaError

REQUIRED_TABLES = {"Asetus", "Tili", "Tilikausi", "Tosite", "Vienti", "Liite", "Kumppani"}
BUSY_TIMEOUT_MS = 2000
SIDECAR_SUFFIXES = ("-wal", "-shm")


class Book:
    """A local Kitsas book. Kitsas must not have it open."""

    def __init__(self, path):
        self.path = Path(path)
        self._kpversio = None
        self._backup_path = None

    # -- checks ---------------------------------------------------------

    @property
    def kpversio(self) -> int:
        if self._kpversio is None:
            self._kpversio = self._read_kpversio()
        return self._kpversio

    def _read_kpversio(self) -> int:
        if not self.path.exists():
            raise NotAKitsasBookError(f"There is no file at {self.path}.")
        try:
            with self.connect_read() as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES.issubset(tables):
                    raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book.")
                row = conn.execute("SELECT arvo FROM Asetus WHERE avain='KpVersio'").fetchone()
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                raise self._locked() from None
            raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book.") from None
        if row is None:
            raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book.")
        return int(row[0])

    def _locked(self) -> BookLockedError:
        return BookLockedError(
            f"Kitsas has {self.path.name} open. Close Kitsas and try again. "
            "Kitsas locks a book exclusively while it is open."
        )

    # -- connections ----------------------------------------------------

    @contextmanager
    def connect_read(self):
        uri = f"file:{self.path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "unable to open" in str(exc):
                raise self._locked() from None
            raise
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def connect_write(self):
        if self.kpversio not in SUPPORTED_KPVERSIO:
            raise UnsupportedSchemaError(
                f"{self.path.name} uses Kitsas schema version {self.kpversio}, which this server "
                f"has not been tested against (it supports {sorted(SUPPORTED_KPVERSIO)}). "
                "Reading works; writing is refused."
            )
        self.backup()
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            conn.close()
            if "locked" in str(exc) or "busy" in str(exc):
                raise self._locked() from None
            raise
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        finally:
            conn.close()

    # -- backup ---------------------------------------------------------

    def backup(self) -> Path:
        """Copy the book beside itself, once per Book instance."""
        if self._backup_path is not None:
            return self._backup_path
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_suffix(self.path.suffix + f".{stamp}.bak")
        shutil.copy2(self.path, target)
        for suffix in SIDECAR_SUFFIXES:
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(target) + suffix))
        self._backup_path = target
        return target
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS, 7 passed

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures/luo.sql tests/conftest.py tests/test_db.py src/kitsas_mcp/db.py
git commit -m "Add synthetic test book and safe book opening with lock and schema checks"
```

---

### Task 3: Chart of accounts

**Files:**
- Create: `src/kitsas_mcp/accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Consumes: `Book.connect_read`, `constants.TILITYYPPI_PANKKI`, `constants.TILITYYPPI_OSTOVELAT`, `AccountNotFoundError`
- Produces: `list_accounts(book, search=None) -> list[dict]` with keys `number`, `name`, `type`; `get_account(book, number) -> dict`; `default_bank_account(book) -> int`; `default_payable_account(book) -> int`

- [ ] **Step 1: Write the failing tests**

`tests/test_accounts.py`:

```python
import pytest

from kitsas_mcp.accounts import default_bank_account, default_payable_account, get_account, list_accounts
from kitsas_mcp.errors import AccountNotFoundError


def test_lists_every_account_with_its_finnish_name(book):
    accounts = list_accounts(book)
    assert len(accounts) == 5
    assert {"number": 1910, "name": "Pankkitili", "type": "ARP"} in accounts


def test_accounts_are_ordered_by_number(book):
    numbers = [a["number"] for a in list_accounts(book)]
    assert numbers == sorted(numbers)


def test_search_matches_the_name_case_insensitively(book):
    assert [a["number"] for a in list_accounts(book, "edustus")] == [4590]


def test_search_matches_the_account_number(book):
    assert [a["number"] for a in list_accounts(book, "4590")] == [4590]


def test_get_account_returns_one(book):
    assert get_account(book, 4000)["name"] == "Tavaraostot, varsinainen toiminta"


def test_get_account_names_the_missing_number(book):
    with pytest.raises(AccountNotFoundError) as excinfo:
        get_account(book, 9999)
    assert "9999" in str(excinfo.value)


def test_default_bank_account_is_the_arp_account(book):
    assert default_bank_account(book) == 1910


def test_default_payable_account_is_the_bo_account(book):
    assert default_payable_account(book) == 2960
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_accounts.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.accounts'`

- [ ] **Step 3: Implement accounts.py**

`src/kitsas_mcp/accounts.py`:

```python
"""The chart of accounts. Account names live in Tili.json, not in a column."""

import json

from .constants import TILITYYPPI_OSTOVELAT, TILITYYPPI_PANKKI
from .errors import AccountNotFoundError

SELECT = "SELECT numero, tyyppi, json FROM Tili"


def _row_to_account(row) -> dict:
    data = json.loads(row["json"] or "{}")
    names = data.get("nimi") or {}
    return {
        "number": row["numero"],
        "name": names.get("fi") or names.get("sv") or "",
        "type": row["tyyppi"],
    }


def list_accounts(book, search=None) -> list[dict]:
    with book.connect_read() as conn:
        accounts = [_row_to_account(r) for r in conn.execute(f"{SELECT} ORDER BY numero")]
    if search:
        needle = str(search).lower()
        accounts = [a for a in accounts if needle in a["name"].lower() or needle in str(a["number"])]
    return accounts


def get_account(book, number: int) -> dict:
    with book.connect_read() as conn:
        row = conn.execute(f"{SELECT} WHERE numero = ?", (number,)).fetchone()
    if row is None:
        raise AccountNotFoundError(f"Account {number} does not exist in this book.")
    return _row_to_account(row)


def _first_of_type(book, tyyppi: str, description: str) -> int:
    with book.connect_read() as conn:
        row = conn.execute(f"{SELECT} WHERE tyyppi = ? ORDER BY numero", (tyyppi,)).fetchone()
    if row is None:
        raise AccountNotFoundError(
            f"This book has no {description} (an account of type {tyyppi}). "
            "Pass the account number explicitly."
        )
    return row["numero"]


def default_bank_account(book) -> int:
    return _first_of_type(book, TILITYYPPI_PANKKI, "bank account")


def default_payable_account(book) -> int:
    return _first_of_type(book, TILITYYPPI_OSTOVELAT, "payables account")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_accounts.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/kitsas_mcp/accounts.py tests/test_accounts.py
git commit -m "Add chart of accounts reading with JSON name extraction"
```

---

### Task 4: Reading fiscal years, partners and vouchers

**Files:**
- Create: `src/kitsas_mcp/read.py`
- Test: `tests/test_read.py`

**Interfaces:**
- Consumes: `Book.connect_read`, `accounts._row_to_account`, `constants.TILA_KIRJANPIDOSSA`, `money.cents_to_euros`
- Produces: `list_fiscal_years(book) -> list[dict]` with keys `starts`, `ends`, `confirmed`, `current`; `fiscal_year_for(book, date) -> dict` raising `NoFiscalYearError`; `find_supplier(book, query) -> list[dict]` with keys `id`, `name`, `vat_id`; `list_vouchers(book, date_from, date_to, supplier=None, account=None, state=None) -> list[dict]`; `get_voucher(book, voucher_id) -> dict`

- [ ] **Step 1: Write the failing tests**

`tests/test_read.py`:

```python
import pytest

from kitsas_mcp.errors import NoFiscalYearError
from kitsas_mcp.read import find_supplier, fiscal_year_for, get_voucher, list_fiscal_years, list_vouchers


def test_lists_fiscal_years_and_marks_the_confirmed_one(book):
    years = list_fiscal_years(book)
    assert [y["starts"] for y in years] == ["2025-01-01", "2026-01-01"]
    assert years[0]["confirmed"] == "2026-04-28"
    assert years[1]["confirmed"] is None


def test_fiscal_year_for_a_date_inside_a_year(book):
    assert fiscal_year_for(book, "2026-05-04")["starts"] == "2026-01-01"


def test_fiscal_year_for_a_date_outside_every_year(book):
    with pytest.raises(NoFiscalYearError) as excinfo:
        fiscal_year_for(book, "2031-01-03")
    assert "2031-01-03" in str(excinfo.value)


def test_find_supplier_by_name_substring(book):
    found = find_supplier(book, "hetz")
    assert len(found) == 1
    assert found[0]["id"] == 7
    assert found[0]["name"] == "Hetzner"


def test_find_supplier_returns_empty_for_an_unknown_name(book):
    assert find_supplier(book, "Telia") == []


def test_list_vouchers_shows_ledger_vouchers_by_default(book):
    vouchers = list_vouchers(book, "2026-01-01", "2026-12-31")
    assert len(vouchers) == 2
    assert vouchers[0]["total"] == "47.31"
    assert vouchers[0]["supplier"] == "Hetzner"


def test_list_vouchers_can_filter_by_account(book):
    assert len(list_vouchers(book, "2026-01-01", "2026-12-31", account=4590)) == 2
    assert list_vouchers(book, "2026-01-01", "2026-12-31", account=3000) == []


def test_list_vouchers_respects_the_date_range(book):
    assert list_vouchers(book, "2026-03-01", "2026-03-31")[0]["date"] == "2026-03-06"


def test_get_voucher_returns_header_and_entries(book):
    voucher = get_voucher(book, 1)
    assert voucher["date"] == "2026-02-06"
    assert voucher["state"] == 100
    assert len(voucher["entries"]) == 2
    credit = [e for e in voucher["entries"] if e["credit"] != "0.00"][0]
    assert credit["account"] == 1910
    assert credit["account_name"] == "Pankkitili"
    assert credit["credit"] == "47.31"


def test_get_voucher_returns_none_for_a_missing_id(book):
    assert get_voucher(book, 4242) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_read.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.read'`

- [ ] **Step 3: Implement read.py**

`src/kitsas_mcp/read.py`:

```python
"""Read-only queries over a Kitsas book."""

import json
from datetime import date

from .constants import TILA_KIRJANPIDOSSA
from .errors import NoFiscalYearError
from .money import cents_to_euros


def list_fiscal_years(book) -> list[dict]:
    today = date.today().isoformat()
    with book.connect_read() as conn:
        rows = conn.execute("SELECT alkaa, loppuu, json FROM Tilikausi ORDER BY alkaa").fetchall()
    years = []
    for row in rows:
        data = json.loads(row["json"] or "{}")
        years.append(
            {
                "starts": row["alkaa"],
                "ends": row["loppuu"],
                "confirmed": data.get("vahvistettu"),
                "current": row["alkaa"] <= today <= row["loppuu"],
            }
        )
    return years


def fiscal_year_for(book, when: str) -> dict:
    for year in list_fiscal_years(book):
        if year["starts"] <= when <= year["ends"]:
            return year
    raise NoFiscalYearError(
        f"No fiscal year in this book covers {when}. Create the fiscal year in Kitsas first."
    )


def find_supplier(book, query: str) -> list[dict]:
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT k.id, k.nimi, k.alvtunnus FROM Kumppani k "
            "LEFT JOIN KumppaniIban i ON i.kumppani = k.id "
            "WHERE lower(k.nimi) LIKE lower(?) OR lower(coalesce(k.alvtunnus,'')) = lower(?) "
            "OR replace(lower(coalesce(i.iban,'')),' ','') = replace(lower(?),' ','') "
            "GROUP BY k.id ORDER BY k.nimi",
            (f"%{query}%", query, query),
        ).fetchall()
    return [{"id": r["id"], "name": r["nimi"], "vat_id": r["alvtunnus"]} for r in rows]


def list_vouchers(book, date_from, date_to, supplier=None, account=None, state=None) -> list[dict]:
    sql = [
        "SELECT t.id, t.pvm, t.tyyppi, t.tila, t.tunniste, t.otsikko, t.erapvm, k.nimi AS kumppani,",
        "       (SELECT sum(debetsnt) FROM Vienti WHERE tosite = t.id) AS summa",
        "FROM Tosite t LEFT JOIN Kumppani k ON k.id = t.kumppani",
        "WHERE t.pvm BETWEEN ? AND ?",
    ]
    params = [date_from, date_to]

    if state is None:
        sql.append("AND t.tila >= ?")
        params.append(TILA_KIRJANPIDOSSA)
    else:
        sql.append("AND t.tila = ?")
        params.append(state)

    if supplier is not None:
        sql.append("AND t.kumppani = ?")
        params.append(supplier)

    if account is not None:
        sql.append("AND EXISTS (SELECT 1 FROM Vienti v WHERE v.tosite = t.id AND v.tili = ?)")
        params.append(account)

    sql.append("ORDER BY t.pvm, t.id")

    with book.connect_read() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    return [
        {
            "id": r["id"],
            "date": r["pvm"],
            "type": r["tyyppi"],
            "state": r["tila"],
            "number": r["tunniste"],
            "title": r["otsikko"],
            "due_date": r["erapvm"],
            "supplier": r["kumppani"],
            "total": cents_to_euros(r["summa"] or 0),
        }
        for r in rows
    ]


def get_voucher(book, voucher_id: int):
    with book.connect_read() as conn:
        header = conn.execute(
            "SELECT t.*, k.nimi AS kumppani FROM Tosite t "
            "LEFT JOIN Kumppani k ON k.id = t.kumppani WHERE t.id = ?",
            (voucher_id,),
        ).fetchone()
        if header is None:
            return None
        entries = conn.execute(
            "SELECT v.rivi, v.tyyppi, v.pvm, v.tili, v.selite, v.debetsnt, v.kreditsnt, "
            "       json_extract(ti.json, '$.nimi.fi') AS tilinimi "
            "FROM Vienti v LEFT JOIN Tili ti ON ti.numero = v.tili "
            "WHERE v.tosite = ? ORDER BY v.rivi",
            (voucher_id,),
        ).fetchall()
        attachments = conn.execute(
            "SELECT nimi, tyyppi, length(data) AS koko FROM Liite WHERE tosite = ? ORDER BY id",
            (voucher_id,),
        ).fetchall()

    return {
        "id": header["id"],
        "date": header["pvm"],
        "type": header["tyyppi"],
        "state": header["tila"],
        "number": header["tunniste"],
        "title": header["otsikko"],
        "supplier": header["kumppani"],
        "invoice_date": header["laskupvm"],
        "due_date": header["erapvm"],
        "reference": header["viite"],
        "entries": [
            {
                "row": e["rivi"],
                "type": e["tyyppi"],
                "date": e["pvm"],
                "account": e["tili"],
                "account_name": e["tilinimi"],
                "description": e["selite"],
                "debit": cents_to_euros(e["debetsnt"] or 0),
                "credit": cents_to_euros(e["kreditsnt"] or 0),
            }
            for e in entries
        ],
        "attachments": [
            {"name": a["nimi"], "mime": a["tyyppi"], "bytes": a["koko"]} for a in attachments
        ],
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_read.py -v`
Expected: PASS, 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/kitsas_mcp/read.py tests/test_read.py
git commit -m "Add reading of fiscal years, partners and vouchers"
```

---

### Task 5: suggest_account

**Files:**
- Create: `src/kitsas_mcp/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Consumes: `Book.connect_read`, `constants.TILA_KIRJANPIDOSSA`, `constants.TOSITE_MENO`
- Produces: `suggest_account(book, supplier) -> list[dict]` with keys `account`, `account_name`, `count`, `last_used`

`supplier` is either a `Kumppani.id` (int) or a name (str). A name is matched case insensitively against `Kumppani.nimi`.

- [ ] **Step 1: Write the failing tests**

`tests/test_history.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_history.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.history'`

- [ ] **Step 3: Implement history.py**

`src/kitsas_mcp/history.py`:

```python
"""What account has this supplier's spending been booked to before?"""

from .constants import TILA_KIRJANPIDOSSA, TOSITE_MENO

SQL = """
SELECT v.tili AS tili,
       json_extract(ti.json, '$.nimi.fi') AS tilinimi,
       count(*) AS lkm,
       max(t.pvm) AS viimeksi
FROM Vienti v
JOIN Tosite t ON t.id = v.tosite
LEFT JOIN Tili ti ON ti.numero = v.tili
WHERE t.kumppani = ?
  AND t.tyyppi = ?
  AND t.tila >= ?
  AND v.debetsnt > 0
GROUP BY v.tili
ORDER BY lkm DESC, viimeksi DESC
"""


def _resolve_supplier_id(conn, supplier):
    if isinstance(supplier, int):
        return supplier
    row = conn.execute(
        "SELECT id FROM Kumppani WHERE lower(nimi) = lower(?) ORDER BY id LIMIT 1", (supplier,)
    ).fetchone()
    if row is not None:
        return row["id"]
    row = conn.execute(
        "SELECT id FROM Kumppani WHERE lower(nimi) LIKE lower(?) ORDER BY id LIMIT 1",
        (f"%{supplier}%",),
    ).fetchone()
    return row["id"] if row else None


def suggest_account(book, supplier) -> list[dict]:
    """Expense accounts this supplier's past bills were booked to, most used first.

    Only debit rows on ledger vouchers of the purchase type are counted, so the
    bank or payables counter account never appears as a suggestion.
    """
    with book.connect_read() as conn:
        supplier_id = _resolve_supplier_id(conn, supplier)
        if supplier_id is None:
            return []
        rows = conn.execute(SQL, (supplier_id, TOSITE_MENO, TILA_KIRJANPIDOSSA)).fetchall()

    return [
        {
            "account": r["tili"],
            "account_name": r["tilinimi"],
            "count": r["lkm"],
            "last_used": r["viimeksi"],
        }
        for r in rows
    ]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_history.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/kitsas_mcp/history.py tests/test_history.py
git commit -m "Add supplier account history suggestions"
```

---

### Task 6: Writing a purchase invoice draft

**Files:**
- Create: `src/kitsas_mcp/write.py`
- Test: `tests/test_write.py`

**Interfaces:**
- Consumes: `Book.connect_write`, `accounts.get_account`, `accounts.default_bank_account`, `read.fiscal_year_for`, `money.euros_to_cents`, all constants, the error classes
- Produces: `add_purchase_invoice(book, *, supplier_name, lines, booking_date, business_id=None, iban=None, invoice_date=None, due_date=None, reference=None, description=None, credit_account=None, pdf_path=None) -> dict` returning keys `voucher_id`, `total`, `credit_account`, `lines`, `attachment`, `backup`, `summary`; and `delete_draft(book, voucher_id) -> dict`

`lines` is a list of dicts with keys `account` (int), `amount` (euro string or number) and optional `description`.

- [ ] **Step 1: Write the failing tests**

`tests/test_write.py`:

```python
import sqlite3

import pytest

from kitsas_mcp.errors import (
    AccountNotFoundError,
    ClosedFiscalYearError,
    LedgerVoucherError,
    NoFiscalYearError,
    UnbalancedVoucherError,
)
from kitsas_mcp.read import get_voucher
from kitsas_mcp.write import add_purchase_invoice, delete_draft

BILL = dict(
    supplier_name="Telia Finland Oyj",
    booking_date="2026-05-04",
    invoice_date="2026-04-28",
    description="Broadband April",
    lines=[{"account": 4000, "amount": "42.90"}],
)


def test_writes_a_draft_not_a_ledger_voucher(book):
    result = add_purchase_invoice(book, **BILL)
    voucher = get_voucher(book, result["voucher_id"])
    assert voucher["state"] == 20
    assert voucher["number"] is None, "a draft must not consume a voucher number"


def test_books_the_expense_as_debit_and_the_bank_as_credit(book):
    result = add_purchase_invoice(book, **BILL)
    entries = get_voucher(book, result["voucher_id"])["entries"]
    counter = [e for e in entries if e["row"] == 1][0]
    expense = [e for e in entries if e["row"] == 2][0]
    assert counter["account"] == 1910
    assert counter["credit"] == "42.90"
    assert counter["type"] == 102
    assert expense["account"] == 4000
    assert expense["debit"] == "42.90"
    assert expense["type"] == 101


def test_supports_several_expense_lines(book):
    result = add_purchase_invoice(
        book,
        **{**BILL, "lines": [
            {"account": 4000, "amount": "75.54"},
            {"account": 4590, "amount": "163.45"},
        ]},
    )
    voucher = get_voucher(book, result["voucher_id"])
    assert len(voucher["entries"]) == 3
    assert [e for e in voucher["entries"] if e["row"] == 1][0]["credit"] == "238.99"


def test_creates_the_supplier_when_it_is_new(book):
    add_purchase_invoice(book, **BILL)
    from kitsas_mcp.read import find_supplier

    assert find_supplier(book, "Telia")[0]["name"] == "Telia Finland Oyj"


def test_reuses_an_existing_supplier(book):
    add_purchase_invoice(book, **{**BILL, "supplier_name": "Hetzner"})
    with book.connect_read() as conn:
        count = conn.execute("SELECT count(*) FROM Kumppani WHERE nimi='Hetzner'").fetchone()[0]
    assert count == 1


def test_stores_the_iban_against_the_supplier(book):
    add_purchase_invoice(book, **{**BILL, "iban": "FI21 1234 5600 0007 85"})
    with book.connect_read() as conn:
        row = conn.execute("SELECT iban FROM KumppaniIban").fetchone()
    assert row["iban"] == "FI2112345600000785"


def test_attaches_the_pdf(book, tmp_path):
    pdf = tmp_path / "telia-invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake invoice")
    result = add_purchase_invoice(book, **{**BILL, "pdf_path": str(pdf)})
    voucher = get_voucher(book, result["voucher_id"])
    assert voucher["attachments"] == [
        {"name": "telia-invoice.pdf", "mime": "application/pdf", "bytes": 21}
    ]
    with book.connect_read() as conn:
        sha = conn.execute("SELECT sha FROM Liite").fetchone()["sha"]
    assert len(sha) == 64


def test_takes_a_backup_before_the_first_write(book, book_path):
    result = add_purchase_invoice(book, **BILL)
    assert result["backup"].endswith(".bak")
    from pathlib import Path

    assert Path(result["backup"]).exists()


def test_refuses_a_date_with_no_fiscal_year(book):
    with pytest.raises(NoFiscalYearError):
        add_purchase_invoice(book, **{**BILL, "booking_date": "2031-05-04"})


def test_refuses_a_confirmed_fiscal_year(book):
    with pytest.raises(ClosedFiscalYearError) as excinfo:
        add_purchase_invoice(book, **{**BILL, "booking_date": "2025-05-04"})
    assert "confirmed" in str(excinfo.value)


def test_refuses_an_unknown_account(book):
    with pytest.raises(AccountNotFoundError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 9999, "amount": "1.00"}]})


def test_refuses_an_empty_bill(book):
    with pytest.raises(UnbalancedVoucherError):
        add_purchase_invoice(book, **{**BILL, "lines": []})


def test_a_failed_write_leaves_the_book_unchanged(book, book_path):
    before = book_path.read_bytes()
    with pytest.raises(AccountNotFoundError):
        add_purchase_invoice(book, **{**BILL, "lines": [{"account": 9999, "amount": "1.00"}]})
    assert book_path.read_bytes() == before


def test_delete_draft_removes_a_draft(book):
    result = add_purchase_invoice(book, **BILL)
    delete_draft(book, result["voucher_id"])
    assert get_voucher(book, result["voucher_id"])["state"] == 0


def test_delete_draft_refuses_a_ledger_voucher(book):
    with pytest.raises(LedgerVoucherError) as excinfo:
        delete_draft(book, 1)
    assert "already in the ledger" in str(excinfo.value)


def test_writes_the_audit_log(book):
    result = add_purchase_invoice(book, **BILL)
    with book.connect_read() as conn:
        rows = conn.execute(
            "SELECT tila FROM Tositeloki WHERE tosite = ?", (result["voucher_id"],)
        ).fetchall()
    assert [r["tila"] for r in rows] == [20]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_write.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.write'`

- [ ] **Step 3: Implement write.py**

`src/kitsas_mcp/write.py`:

```python
"""The only module that changes a book. Drafts only, one transaction, after a backup."""

import hashlib
import json
import mimetypes
from pathlib import Path

from .accounts import default_bank_account, get_account
from .constants import (
    TILA_KIRJANPIDOSSA,
    TILA_POISTETTU,
    TILA_SAAPUNUT,
    TOSITE_MENO,
    VIENTI_OSTO_KIRJAUS,
    VIENTI_OSTO_VASTAKIRJAUS,
)
from .errors import ClosedFiscalYearError, LedgerVoucherError, UnbalancedVoucherError
from .money import cents_to_euros, euros_to_cents
from .read import fiscal_year_for


def _check_fiscal_year(book, booking_date: str) -> None:
    year = fiscal_year_for(book, booking_date)
    if year["confirmed"]:
        raise ClosedFiscalYearError(
            f"The fiscal year {year['starts']} to {year['ends']} was confirmed on "
            f"{year['confirmed']}. Nothing may be added to it. Use a date in an open "
            "fiscal year, or unconfirm the year in Kitsas first."
        )


def _upsert_supplier(conn, name: str, business_id, iban) -> int:
    row = conn.execute(
        "SELECT id FROM Kumppani WHERE lower(nimi) = lower(?) ORDER BY id LIMIT 1", (name,)
    ).fetchone()
    if row is None:
        cursor = conn.execute(
            "INSERT INTO Kumppani (nimi, alvtunnus, json) VALUES (?,?,?)",
            (name, business_id, "{}"),
        )
        supplier_id = cursor.lastrowid
    else:
        supplier_id = row["id"]
        if business_id:
            conn.execute(
                "UPDATE Kumppani SET alvtunnus = ? WHERE id = ? AND coalesce(alvtunnus,'') = ''",
                (business_id, supplier_id),
            )

    if iban:
        normalised = iban.replace(" ", "").upper()
        conn.execute(
            "INSERT INTO KumppaniIban (iban, kumppani) VALUES (?,?) "
            "ON CONFLICT (iban) DO UPDATE SET kumppani = excluded.kumppani",
            (normalised, supplier_id),
        )
    return supplier_id


def _attach(conn, voucher_id: int, pdf_path: str) -> dict:
    path = Path(pdf_path)
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    conn.execute(
        "INSERT INTO Liite (tosite, nimi, roolinimi, tyyppi, sha, data) VALUES (?,?,NULL,?,?,?)",
        (voucher_id, path.name, mime, hashlib.sha256(data).hexdigest(), data),
    )
    return {"name": path.name, "mime": mime, "bytes": len(data)}


def add_purchase_invoice(
    book,
    *,
    supplier_name: str,
    lines: list,
    booking_date: str,
    business_id=None,
    iban=None,
    invoice_date=None,
    due_date=None,
    reference=None,
    description=None,
    credit_account=None,
    pdf_path=None,
) -> dict:
    """Create a purchase invoice as a draft. Kitsas approves it into the ledger."""
    _check_fiscal_year(book, booking_date)

    if not lines:
        raise UnbalancedVoucherError("A bill needs at least one expense line.")

    prepared = []
    for line in lines:
        get_account(book, line["account"])  # raises AccountNotFoundError
        cents = euros_to_cents(line["amount"])
        if cents <= 0:
            raise UnbalancedVoucherError(
                f"Line for account {line['account']} is {line['amount']}, which is not a positive amount."
            )
        prepared.append((line["account"], cents, line.get("description") or description or supplier_name))

    total = sum(cents for _, cents, _ in prepared)
    counter_account = credit_account if credit_account is not None else default_bank_account(book)
    get_account(book, counter_account)

    title = description or supplier_name

    with book.connect_write() as conn:
        supplier_id = _upsert_supplier(conn, supplier_name, business_id, iban)

        cursor = conn.execute(
            "INSERT INTO Tosite (pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm, erapvm, viite, json) "
            "VALUES (?,?,?,NULL,?,?,?,?,?,'{}')",
            (booking_date, TOSITE_MENO, TILA_SAAPUNUT, title, supplier_id, invoice_date, due_date, reference),
        )
        voucher_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, "
            "alvkoodi, kumppani, json) VALUES (1,?,?,?,?,0,?,0,?,0,?,'{}')",
            (voucher_id, VIENTI_OSTO_VASTAKIRJAUS, booking_date, counter_account, title, total, supplier_id),
        )

        for row_number, (account, cents, line_description) in enumerate(prepared, start=2):
            conn.execute(
                "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, "
                "alvkoodi, kumppani, json) VALUES (?,?,?,?,?,0,?,?,0,0,?,'{}')",
                (
                    row_number,
                    voucher_id,
                    VIENTI_OSTO_KIRJAUS,
                    booking_date,
                    account,
                    line_description,
                    cents,
                    supplier_id,
                ),
            )

        debit, credit = conn.execute(
            "SELECT sum(debetsnt), sum(kreditsnt) FROM Vienti WHERE tosite = ?", (voucher_id,)
        ).fetchone()
        if debit != credit:
            raise UnbalancedVoucherError(
                f"Debits {cents_to_euros(debit)} do not equal credits {cents_to_euros(credit)}. "
                "Nothing was written."
            )

        attachment = _attach(conn, voucher_id, pdf_path) if pdf_path else None

        conn.execute(
            "INSERT INTO Tositeloki (tosite, tila, data) VALUES (?,?,?)",
            (
                voucher_id,
                TILA_SAAPUNUT,
                json.dumps(
                    {
                        "source": "kitsas-mcp",
                        "supplier": supplier_name,
                        "booking_date": booking_date,
                        "lines": [
                            {"account": a, "cents": c, "description": d} for a, c, d in prepared
                        ],
                        "credit_account": counter_account,
                    }
                ),
            ),
        )

    return {
        "voucher_id": voucher_id,
        "total": cents_to_euros(total),
        "credit_account": counter_account,
        "lines": [{"account": a, "amount": cents_to_euros(c)} for a, c, _ in prepared],
        "attachment": attachment,
        "backup": str(book.backup()),
        "summary": (
            f"Draft voucher {voucher_id} for {supplier_name}, {cents_to_euros(total)} euros on "
            f"{booking_date}, credited to account {counter_account}. It is not in the ledger; "
            "open Kitsas to check and approve it."
        ),
    }


def delete_draft(book, voucher_id: int) -> dict:
    """Mark a draft deleted, as Kitsas does. Refuses anything already in the ledger."""
    with book.connect_read() as conn:
        row = conn.execute("SELECT tila FROM Tosite WHERE id = ?", (voucher_id,)).fetchone()
    if row is None:
        raise LedgerVoucherError(f"There is no voucher {voucher_id} in this book.")
    if row["tila"] >= TILA_KIRJANPIDOSSA:
        raise LedgerVoucherError(
            f"Voucher {voucher_id} is already in the ledger and cannot be deleted here. "
            "Do it in Kitsas if you really mean to."
        )

    with book.connect_write() as conn:
        conn.execute("UPDATE Tosite SET tila = ? WHERE id = ?", (TILA_POISTETTU, voucher_id))
        conn.execute(
            "INSERT INTO Tositeloki (tosite, tila) VALUES (?,?)", (voucher_id, TILA_POISTETTU)
        )

    return {"voucher_id": voucher_id, "summary": f"Draft voucher {voucher_id} deleted."}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_write.py -v`
Expected: PASS, 16 passed

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -v`
Expected: PASS, all tests

- [ ] **Step 6: Commit**

```bash
git add src/kitsas_mcp/write.py tests/test_write.py
git commit -m "Add purchase invoice draft creation and draft deletion"
```

---

### Task 7: Bank reconciliation

**Files:**
- Create: `src/kitsas_mcp/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `Book.connect_read`, `accounts.default_bank_account`, `constants.TILA_KIRJANPIDOSSA`, `money.cents_to_euros`
- Produces: `bank_balance(book, on_date, account=None) -> dict` with keys `account`, `date`, `balance`; `bank_movements(book, date_from, date_to, account=None) -> list[dict]` with keys `date`, `voucher_id`, `counterparty`, `description`, `amount`, `running_balance`

Amounts are signed: money into the account is positive, money out is negative.

- [ ] **Step 1: Write the failing tests**

`tests/test_reconcile.py`:

```python
from kitsas_mcp.reconcile import bank_balance, bank_movements


def test_balance_counts_only_ledger_vouchers(book):
    # The synthetic book has two Hetzner bills of 47.31 credited to 1910.
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

    add_purchase_invoice(
        book,
        supplier_name="Telia",
        booking_date="2026-05-04",
        description="Broadband",
        lines=[{"account": 4000, "amount": "42.90"}],
    )
    assert len(bank_movements(book, "2026-01-01", "2026-12-31")) == 2
    assert bank_balance(book, "2026-12-31")["balance"] == "-94.62"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.reconcile'`

- [ ] **Step 3: Implement reconcile.py**

`src/kitsas_mcp/reconcile.py`:

```python
"""Checking the book's bank account against a real bank statement.

Neither target book has ever imported a bank statement, so nothing has
reconciled the ledger's bank account against the bank. Every automated bill
asserts a payment; this is how a wrong one gets found.
"""

from .accounts import default_bank_account
from .constants import TILA_KIRJANPIDOSSA
from .money import cents_to_euros


def _balance_cents(conn, account: int, on_date: str) -> int:
    row = conn.execute(
        "SELECT coalesce(sum(v.debetsnt), 0) - coalesce(sum(v.kreditsnt), 0) "
        "FROM Vienti v JOIN Tosite t ON t.id = v.tosite "
        "WHERE v.tili = ? AND v.pvm <= ? AND t.tila >= ?",
        (account, on_date, TILA_KIRJANPIDOSSA),
    ).fetchone()
    return row[0] or 0


def bank_balance(book, on_date: str, account=None) -> dict:
    account = account if account is not None else default_bank_account(book)
    with book.connect_read() as conn:
        cents = _balance_cents(conn, account, on_date)
    return {"account": account, "date": on_date, "balance": cents_to_euros(cents)}


def bank_movements(book, date_from: str, date_to: str, account=None) -> list[dict]:
    account = account if account is not None else default_bank_account(book)
    with book.connect_read() as conn:
        opening = _balance_cents(conn, account, _day_before(date_from))
        rows = conn.execute(
            "SELECT v.pvm, v.tosite, v.selite, v.debetsnt, v.kreditsnt, k.nimi AS kumppani "
            "FROM Vienti v JOIN Tosite t ON t.id = v.tosite "
            "LEFT JOIN Kumppani k ON k.id = v.kumppani "
            "WHERE v.tili = ? AND v.pvm BETWEEN ? AND ? AND t.tila >= ? "
            "ORDER BY v.pvm, v.tosite, v.rivi",
            (account, date_from, date_to, TILA_KIRJANPIDOSSA),
        ).fetchall()

    running = opening
    movements = []
    for row in rows:
        amount = (row["debetsnt"] or 0) - (row["kreditsnt"] or 0)
        running += amount
        movements.append(
            {
                "date": row["pvm"],
                "voucher_id": row["tosite"],
                "counterparty": row["kumppani"],
                "description": row["selite"],
                "amount": cents_to_euros(amount),
                "running_balance": cents_to_euros(running),
            }
        )
    return movements


def _day_before(when: str) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(when) - timedelta(days=1)).isoformat()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/kitsas_mcp/reconcile.py tests/test_reconcile.py
git commit -m "Add bank balance and movement listing for reconciliation"
```

---

### Task 8: The MCP server and the README

**Files:**
- Create: `src/kitsas_mcp/server.py`, `README.md`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: every module above
- Produces: `main()` console entry point; `build_server(book_path)` returning the configured MCP server, so tests can exercise tool registration without stdio

The book path comes from the `--book` argument or the `KITSAS_BOOK` environment variable. Tools that take no book argument use it.

- [ ] **Step 1: Write the failing tests**

`tests/test_server.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'kitsas_mcp.server'`

- [ ] **Step 3: Implement server.py**

`src/kitsas_mcp/server.py`:

```python
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
        "description": "Which expense accounts this supplier's earlier bills were booked to, most used first. Empty for a supplier with no history.",
        "schema": {"supplier": {"type": "string", "description": "Partner name or id"}},
        "handler": lambda book, args: history.suggest_account(book, args["supplier"]),
    },
    "add_purchase_invoice": {
        "description": (
            "Create a purchase invoice as a DRAFT. It does not enter the ledger and gets no "
            "voucher number until the user approves it in Kitsas. Expense lines are debited; "
            "the total is credited to the bank account unless credit_account says otherwise. "
            "Call suggest_account first so the supplier keeps landing on the same account."
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
            "invoice_date": {"type": "string"},
            "due_date": {"type": "string"},
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
        "schema": {"on_date": {"type": "string"}, "account": {"type": "integer"}},
        "handler": lambda book, args: reconcile.bank_balance(
            book, args["on_date"], args.get("account")
        ),
    },
    "bank_movements": {
        "description": "Every ledger entry on the bank account in a date range, with a running balance, for reconciling against a statement.",
        "schema": {
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
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


def build_server(book_path):
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server = Server("kitsas-mcp")

    @server.list_tools()
    async def list_tools():
        return [
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

    @server.call_tool()
    async def handle(name: str, arguments: dict):
        result = call_tool(book_path, name, arguments or {})
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    return server


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: PASS, 8 passed

- [ ] **Step 5: Write the README**

`README.md` must cover, in this order: what it is and that it is the first MCP server for Kitsas; the hard rule that Kitsas must have the book closed, and why (`PRAGMA LOCKING_MODE = EXCLUSIVE`); that it writes drafts only and never enters the ledger; install with `uv tool install kitsas-mcp` or from a clone; the Claude Desktop config block below; the Claude Code command; the tool list with one line each; and a "Safety" section listing the invariants from the plan's Global Constraints.

Claude Desktop config to include verbatim:

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

Claude Code command to include verbatim:

```bash
claude mcp add kitsas -e KITSAS_BOOK=/path/to/kirjanpito.kitsas -- uv run --directory /path/to/kitsas-mcp kitsas-mcp
```

- [ ] **Step 6: Run the whole suite and commit**

```bash
uv run pytest -v
git add src/kitsas_mcp/server.py tests/test_server.py README.md
git commit -m "Add MCP server tool definitions and README"
```

---

### Task 9: Verification against the real books

This task produces evidence, not code. It cannot be completed on a machine without the books.

**Files:**
- Create: `tests/test_real_books.py`
- Modify: `README.md` (add a "Verified against" line once step 5 passes)

**Interfaces:**
- Consumes: the `real_book_path` fixture from `tests/conftest.py`, every module
- Produces: nothing further tasks depend on

- [ ] **Step 1: Write read-only tests against a real book**

`tests/test_real_books.py`:

```python
"""Runs only when KITSAS_TEST_BOOK points at a real book. Reads a copy, never the original."""

from kitsas_mcp.accounts import default_bank_account, list_accounts
from kitsas_mcp.db import Book
from kitsas_mcp.read import list_fiscal_years, list_vouchers
from kitsas_mcp.reconcile import bank_balance


def test_opens_a_real_book(real_book_path):
    assert Book(real_book_path).kpversio == 24


def test_reads_a_real_chart_of_accounts(real_book_path):
    accounts = list_accounts(Book(real_book_path))
    assert len(accounts) > 100
    assert all(a["name"] for a in accounts), "every account must have a Finnish name"


def test_finds_the_bank_account(real_book_path):
    assert default_bank_account(Book(real_book_path)) == 1910


def test_reads_real_fiscal_years(real_book_path):
    years = list_fiscal_years(Book(real_book_path))
    assert years
    assert any(y["confirmed"] for y in years), "a real book has confirmed years"


def test_reads_real_vouchers(real_book_path):
    book = Book(real_book_path)
    vouchers = list_vouchers(book, "2024-01-01", "2026-12-31")
    assert vouchers
    assert all(v["state"] >= 100 for v in vouchers)


def test_bank_balance_is_computable(real_book_path):
    assert bank_balance(Book(real_book_path), "2026-12-31")["balance"]
```

- [ ] **Step 2: Run them against the Fuusio book**

```bash
KITSAS_TEST_BOOK="/c/Users/k430431/Local Projects/Claude/_kitsas_books/Fuusiory-260428-backup2025complete.kitsas" uv run pytest tests/test_real_books.py -v
```

Expected: PASS. If `test_finds_the_bank_account` fails, the book has more than one `ARP` account and `default_bank_account` needs to become explicit rather than "first of type"; fix it and add a test.

- [ ] **Step 3: Run them against the Kapital book**

```bash
KITSAS_TEST_BOOK="/c/Users/k430431/Local Projects/Claude/_kitsas_books/Kapitalry-valmis1.kitsas" uv run pytest tests/test_real_books.py -v
```

Expected: PASS. Kapital has only one fiscal year, so confirm `test_reads_real_fiscal_years` still holds; if that year is unconfirmed the assertion needs relaxing to "years is non-empty".

- [ ] **Step 4: Write a draft into a copy of the Fuusio book**

```bash
cp "/c/Users/k430431/Local Projects/Claude/_kitsas_books/Fuusiory-260428-backup2025complete.kitsas" /tmp/verify.kitsas
uv run python -c "
from kitsas_mcp.db import Book
from kitsas_mcp.history import suggest_account
from kitsas_mcp.write import add_purchase_invoice
book = Book('/tmp/verify.kitsas')
print(suggest_account(book, 'Hetzner'))
print(add_purchase_invoice(
    book,
    supplier_name='Hetzner Online GmbH',
    booking_date='2026-09-07',
    invoice_date='2026-09-01',
    description='Server hosting September',
    lines=[{'account': 4991, 'amount': '47.31'}],
)['summary'])
"
```

Expected: a suggestion listing the account Hetzner's bills actually used, then a summary naming a new draft voucher id. The booking date is in 2026, the only open fiscal year.

- [ ] **Step 5: The verification that decides whether this works**

Open `/tmp/verify.kitsas` in Kitsas on the machine that has it. Confirm all of:

1. The draft appears in the incoming or draft list, not in the ledger.
2. Supplier, date, amount and account are what was passed.
3. It has no voucher number yet.
4. Approving it moves it into the ledger and Kitsas assigns a number.
5. The ledger's totals move by exactly the bill's amount, and nothing else changed.

Record the result in the README. If any of the five fails, that is a bug in `write.py`, not in Kitsas, and the fix belongs in a new task before this plan is done.

- [ ] **Step 6: Commit**

```bash
git add tests/test_real_books.py README.md
git commit -m "Add verification tests against real books"
```

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the four opening checks and the backup to Task 2; the chart of accounts to Task 3; the read tools to Task 4; `suggest_account` to Task 5; `add_purchase_invoice`, `delete_draft` and all write invariants to Task 6; bank reconciliation to Task 7; the tool surface and both client configs to Task 8; the two test tiers and the manual verification to Tasks 2 and 9. The confirmed-fiscal-year invariant added to the spec after the data check is covered by `test_refuses_a_confirmed_fiscal_year` in Task 6.

**Deferred, as the spec says:** open items (`eraid`), payment vouchers, VAT, cloud books. No task touches them.

**Type consistency.** `Book` exposes `path`, `kpversio`, `connect_read`, `connect_write`, `backup` and nothing else; every module uses only those. Account dicts are `{number, name, type}` throughout. Money crosses module boundaries as a euro string from `cents_to_euros` and is parsed by `euros_to_cents` on the way in. `suggest_account` returns `account`, not `number`, deliberately: it is a suggestion, not an account record.
