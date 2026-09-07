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
