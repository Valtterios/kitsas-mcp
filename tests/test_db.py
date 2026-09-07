import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from kitsas_mcp import db
from kitsas_mcp.db import Book
from kitsas_mcp.errors import (
    BackupError,
    BookLockedError,
    CorruptBookError,
    NotAKitsasBookError,
    UnsupportedSchemaError,
)


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


def test_backup_failure_blocks_the_write_and_leaves_the_book_untouched(book, book_path, monkeypatch):
    original = book_path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copy2", boom)

    with pytest.raises(BackupError):
        with book.connect_write():
            pass

    assert book_path.read_bytes() == original


def test_reports_a_damaged_book(book_path):
    size = book_path.stat().st_size
    data = book_path.read_bytes()
    book_path.write_bytes(data[: size // 2])

    with pytest.raises(CorruptBookError) as excinfo:
        Book(book_path).kpversio
    assert "damaged" in str(excinfo.value)


def test_backup_copies_wal_sidecar_files(book_path):
    wal = Path(str(book_path) + "-wal")
    shm = Path(str(book_path) + "-shm")

    # Open a second connection and write without checkpointing, so the
    # -wal sidecar has real, uncheckpointed content at backup time, the
    # way a real Kitsas book does while it is in use.
    conn = sqlite3.connect(book_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("INSERT INTO Asetus (avain, arvo) VALUES ('extra', '1')")
        assert wal.exists() and wal.stat().st_size > 0

        backup_target = Book(book_path).backup()

        backup_wal = Path(str(backup_target) + "-wal")
        assert backup_wal.exists()
        assert backup_wal.stat().st_size == wal.stat().st_size
        if shm.exists():
            assert Path(str(backup_target) + "-shm").exists()
    finally:
        conn.close()


# -- paths that sqlite's URI parser treats as syntax ----------------------


def _book_in_folder(book_path: Path, folder_name: str) -> Path:
    """The fixture book copied into a folder whose name needs escaping."""
    folder = book_path.parent / folder_name
    folder.mkdir()
    target = folder / book_path.name
    shutil.copy2(book_path, target)
    return target


@pytest.mark.parametrize(
    "folder_name",
    [
        pytest.param("kirjanpito #2", id="hash"),
        pytest.param("kirja%41nposto", id="percent"),
        pytest.param("tili kausi 2026", id="space"),
    ],
)
def test_reads_a_book_whose_path_needs_uri_escaping(book_path, folder_name):
    path = _book_in_folder(book_path, folder_name)

    book = Book(path)
    assert book.kpversio == 24
    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tili").fetchone()[0] == 5


@pytest.mark.parametrize(
    "folder_name",
    [
        pytest.param("kirjanpito #2", id="hash"),
        pytest.param("kirja%41nposto", id="percent"),
        pytest.param("tili kausi 2026", id="space"),
    ],
)
def test_read_connection_cannot_write_through_a_path_that_needs_escaping(book_path, folder_name):
    path = _book_in_folder(book_path, folder_name)
    before = sorted(p.name for p in path.parent.parent.iterdir())

    with Book(path).connect_read() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE proof (x)")

    with sqlite3.connect(path) as check:
        names = {r[0] for r in check.execute("SELECT name FROM sqlite_master")}
    assert "proof" not in names
    assert sorted(p.name for p in path.parent.parent.iterdir()) == before, (
        "an escaped path must not open, or create, some other file"
    )


# -- a lock that is only hit inside the caller's body ---------------------


def test_a_lock_hit_inside_the_read_body_is_reported_as_a_locked_book(book_path):
    # Kitsas holds a book with PRAGMA locking_mode = EXCLUSIVE, so opening
    # the connection succeeds and the lock is only hit by the first read.
    holder = sqlite3.connect(book_path, isolation_level=None)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(BookLockedError) as excinfo:
            with Book(book_path).connect_read() as conn:
                conn.execute("SELECT numero FROM Tili").fetchall()
        assert "Close Kitsas" in str(excinfo.value)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_a_lock_hit_by_the_kpversio_read_is_reported_as_a_locked_book(book_path):
    holder = sqlite3.connect(book_path, isolation_level=None)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(BookLockedError):
            Book(book_path).kpversio
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_an_unrelated_read_error_is_not_reported_as_a_locked_book(book):
    with pytest.raises(sqlite3.OperationalError) as excinfo:
        with book.connect_read() as conn:
            conn.execute("SELECT * FROM EiOleTaulua")
    assert "no such table" in str(excinfo.value)


# -- a missing book is not a locked book ---------------------------------


def test_a_missing_book_names_the_path_instead_of_blaming_kitsas(tmp_path):
    missing = tmp_path / "nope.kitsas"
    with pytest.raises(NotAKitsasBookError) as excinfo:
        with Book(missing).connect_read():
            pass
    assert str(missing) in str(excinfo.value)
    assert "Close Kitsas" not in str(excinfo.value)


def test_a_locked_book_is_still_reported_as_locked(book_path):
    holder = sqlite3.connect(book_path, isolation_level=None)
    holder.execute("PRAGMA locking_mode = EXCLUSIVE")
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(BookLockedError) as excinfo:
            with Book(book_path).connect_read() as conn:
                conn.execute("SELECT count(*) FROM Tili").fetchone()
        assert "Close Kitsas" in str(excinfo.value)
    finally:
        holder.execute("ROLLBACK")
        holder.close()


# -- backups never overwrite each other -----------------------------------


def test_two_backups_in_immediate_succession_do_not_collide(book_path):
    first = Book(book_path).backup()

    conn = sqlite3.connect(book_path)
    conn.execute("INSERT INTO Asetus (avain, arvo) VALUES ('merkki', 'toinen')")
    conn.commit()
    conn.close()

    second = Book(book_path).backup()

    assert first != second
    assert first.exists() and second.exists()

    def marker(path):
        with sqlite3.connect(path) as c:
            return c.execute("SELECT arvo FROM Asetus WHERE avain='merkki'").fetchone()

    assert marker(first) is None, "the first backup must still hold the pre-write state"
    assert marker(second)[0] == "toinen"


def test_backup_picks_the_next_free_name_when_the_stamp_repeats(book_path, monkeypatch):
    frozen = datetime(2026, 9, 7, 12, 0, 0, 500000)

    class FrozenClock:
        @staticmethod
        def now():
            return frozen

    monkeypatch.setattr(db, "datetime", FrozenClock)

    first = Book(book_path).backup()
    second = Book(book_path).backup()

    assert first != second
    assert first.exists() and second.exists()


# -- a KpVersio that is not a number --------------------------------------


def test_a_non_numeric_kpversio_is_a_kitsas_error(book_path):
    conn = sqlite3.connect(book_path)
    conn.execute("UPDATE Asetus SET arvo='24b' WHERE avain='KpVersio'")
    conn.commit()
    conn.close()

    with pytest.raises(NotAKitsasBookError) as excinfo:
        Book(book_path).kpversio
    assert "24b" in str(excinfo.value)


# -- a book on a network share -------------------------------------------
# A UNC path, \\server\share\book.kitsas, came out of Path.as_posix() as
# //server/share/book.kitsas, and "file:" + that reads "server" as the URI
# authority, which sqlite refuses outright: OperationalError("invalid uri
# authority: server"). It matched neither _is_lock nor the missing-file
# check, so it propagated bare out of every read tool, while connect_write,
# which passes the path itself and no URI, opened the same book happily.


def test_a_unc_path_becomes_a_uri_with_an_empty_authority():
    """Two extra slashes: the authority is empty and the path stays a UNC path."""
    assert (
        Book("//nas/kirjanpito/yhdistys.kitsas")._read_uri()
        == "file:////nas/kirjanpito/yhdistys.kitsas?mode=ro"
    )


def test_an_ordinary_path_keeps_the_form_it_had(book_path):
    """Only a UNC path gets the empty-authority form; nothing else changed."""
    uri = Book(book_path)._read_uri()
    assert not uri.startswith("file://")
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")
    assert book_path.as_posix() in uri


def _local_share_path(path: Path):
    """The same file reached through this machine's own admin share, or None.

    A real network share is not something a test can conjure, but the
    loopback admin share is a genuine UNC path and takes the same code
    path. Where it is not reachable (not Windows, admin shares disabled)
    the test skips rather than pretending.
    """
    if os.name != "nt":
        return None
    drive = path.drive
    if len(drive) != 2 or not drive.endswith(":"):
        return None
    candidate = Path(f"//127.0.0.1/{drive[0]}$/{str(path)[3:]}")
    try:
        return candidate if candidate.exists() else None
    except OSError:
        return None


def test_reads_a_book_through_a_unc_path(book_path):
    unc = _local_share_path(book_path)
    if unc is None:
        pytest.skip("no UNC path to this book is reachable on this machine")

    book = Book(unc)
    assert book._read_uri().startswith("file:////")
    assert book.kpversio == 24
    with book.connect_read() as conn:
        assert conn.execute("SELECT count(*) FROM Tili").fetchone()[0] == 5


def test_a_unc_book_reads_and_writes_the_same_file(book_path):
    """Reads and writes must not disagree about a book on a share."""
    unc = _local_share_path(book_path)
    if unc is None:
        pytest.skip("no UNC path to this book is reachable on this machine")

    with Book(unc).connect_write() as conn:
        conn.execute("INSERT INTO Asetus (avain, arvo) VALUES ('merkki', 'unc')")

    with Book(unc).connect_read() as conn:
        assert conn.execute("SELECT arvo FROM Asetus WHERE avain='merkki'").fetchone()[0] == "unc"


# -- the backup name search gives up rather than overwriting ---------------


def test_backup_gives_up_after_max_attempts_rather_than_overwriting(book_path, monkeypatch):
    """Every candidate name taken must be an error, never a reused name.

    Overwriting a .bak would destroy the only copy of an earlier pre-write
    state, which is the one thing the backup exists to hold.
    """
    frozen = datetime(2026, 9, 7, 12, 0, 0, 500000)

    class FrozenClock:
        @staticmethod
        def now():
            return frozen

    monkeypatch.setattr(db, "datetime", FrozenClock)
    monkeypatch.setattr(db, "MAX_BACKUP_ATTEMPTS", 2)

    taken = [Book(book_path).backup(), Book(book_path).backup()]
    assert len(set(taken)) == 2

    with pytest.raises(BackupError) as excinfo:
        Book(book_path).backup()
    message = str(excinfo.value)
    assert "free backup name" in message
    assert "Move the existing .bak files away" in message
    for path in taken:
        assert path.exists(), "no earlier backup may be overwritten or removed"


def test_a_write_is_refused_when_no_backup_name_is_free(book_path, monkeypatch):
    """The book stays byte-identical: no backup, no write."""
    from kitsas_mcp.write import add_purchase_invoice

    frozen = datetime(2026, 9, 7, 12, 0, 0, 500000)

    class FrozenClock:
        @staticmethod
        def now():
            return frozen

    monkeypatch.setattr(db, "datetime", FrozenClock)
    monkeypatch.setattr(db, "MAX_BACKUP_ATTEMPTS", 1)
    Book(book_path).backup()

    before = book_path.read_bytes()
    with pytest.raises(BackupError):
        add_purchase_invoice(
            Book(book_path),
            supplier_name="Telia Finland Oyj",
            booking_date="2026-05-04",
            lines=[{"account": 4000, "amount": "42.90"}],
        )
    assert book_path.read_bytes() == before
