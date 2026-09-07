"""Opening a Kitsas book, with the checks that keep a real book safe."""

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .constants import SUPPORTED_KPVERSIO
from .errors import (
    BackupError,
    BookLockedError,
    CorruptBookError,
    NotAKitsasBookError,
    UnsupportedSchemaError,
)

REQUIRED_TABLES = {"Asetus", "Tili", "Tilikausi", "Tosite", "Vienti", "Liite", "Kumppani"}
BUSY_TIMEOUT_MS = 2000
SIDECAR_SUFFIXES = ("-wal", "-shm")
NOT_A_BOOK_FIX = "A Kitsas book is the .kitsas file that Kitsas itself saves; point this tool at that file."


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return "malformed" in text or "corrupt" in text


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
            raise NotAKitsasBookError(f"There is no file at {self.path}. Check the path and try again.")
        try:
            with self.connect_read() as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES.issubset(tables):
                    raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book. {NOT_A_BOOK_FIX}")
                row = conn.execute("SELECT arvo FROM Asetus WHERE avain='KpVersio'").fetchone()
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                raise self._locked() from None
            if _is_corruption(exc):
                raise CorruptBookError(
                    f"{self.path.name} is a database but it is damaged ({exc}). "
                    "Restore it from a backup or from Kitsas's own backup copy."
                ) from None
            raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book. {NOT_A_BOOK_FIX}") from None
        if row is None:
            raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book. {NOT_A_BOOK_FIX}")
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
        """Copy the book beside itself, once per Book instance.

        The backup is cached on this Book instance: a second call returns
        the same path without copying again. That is safe for how the
        server actually calls it, a fresh Book per MCP call, so every
        write gets its own just-taken snapshot. It is NOT safe to reuse a
        single Book across multiple writes spread over time: every write
        after the first is protected only by the first write's
        increasingly stale snapshot, not by a fresh one taken just before
        that write. Construct a new Book for each write if in doubt.
        """
        if self._backup_path is not None:
            return self._backup_path
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_suffix(self.path.suffix + f".{stamp}.bak")
        try:
            shutil.copy2(self.path, target)
            for suffix in SIDECAR_SUFFIXES:
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(str(target) + suffix))
        except OSError as exc:
            raise BackupError(
                f"Could not back up {self.path.name} before writing: {exc}. "
                "Check that the folder is writable and that Kitsas is closed, then try again."
            ) from exc
        self._backup_path = target
        return target
