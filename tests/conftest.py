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
    # Real Kitsas books run in WAL mode, so the synthetic book should too.
    conn.execute("PRAGMA journal_mode = WAL")
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

    # A draft voucher (tila=20, below the TILA_KIRJANPIDOSSA=100 ledger
    # threshold). Balanced, same shape as the ledger vouchers above. Must
    # never appear in a default (no state=) list_vouchers() call.
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm) "
        "VALUES (3, '2026-04-15', 100, 20, NULL, 'Hetzner', 7, '2026-04-15')"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (1, 3, 102, '2026-04-15', 1910, 0, 'Hetzner', 0, 2000, 7)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (2, 3, 101, '2026-04-15', 4590, 0, 'Hetzner', 2000, 0, 7)"
    )

    # A deliberately unbalanced draft (tila=50, a different draft state so
    # it can be selected on its own). Debit total 1000 exceeds credit total
    # 400, exercising the "report the greater side" behaviour of totals.
    conn.execute(
        "INSERT INTO Tosite (id, pvm, tyyppi, tila, tunniste, otsikko, kumppani, laskupvm) "
        "VALUES (4, '2026-05-20', 100, 50, NULL, 'Hetzner', 7, '2026-05-20')"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (1, 4, 101, '2026-05-20', 4590, 0, 'Hetzner', 1000, 0, 7)"
    )
    conn.execute(
        "INSERT INTO Vienti (rivi, tosite, tyyppi, pvm, tili, kohdennus, selite, debetsnt, kreditsnt, kumppani) "
        "VALUES (2, 4, 102, '2026-05-20', 1910, 0, 'Hetzner', 0, 400, 7)"
    )

    conn.commit()
    conn.close()


@pytest.fixture(autouse=True)
def fresh_session():
    """Every test is its own server session, so no test inherits another's backup.

    A backup is taken once per book per process, so without this the second
    test to write to a book would reuse the first test's backup the way a
    second bill in one server session does, and the tests that count .bak
    files would count the wrong session's.
    """
    from kitsas_mcp.db import forget_session_backups

    forget_session_backups()
    yield
    forget_session_backups()


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
