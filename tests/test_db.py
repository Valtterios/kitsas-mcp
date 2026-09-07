import shutil
import sqlite3
from pathlib import Path

import pytest

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
