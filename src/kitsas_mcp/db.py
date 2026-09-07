"""Opening a Kitsas book, with the checks that keep a real book safe."""

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

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
# A backup name is the timestamp plus, if that name is somehow taken, a counter.
MAX_BACKUP_ATTEMPTS = 1000

# SQLite parses a URI filename: an unescaped "#" starts a fragment and drops
# everything after it, including "?mode=ro", and a "%" starts an escape. Both
# occur in real book paths, so the path is percent-encoded. "/" and ":" are
# left alone: SQLite wants forward slashes, and a Windows drive letter has to
# survive as "C:/...".
URI_SAFE_CHARACTERS = "/:"

# The name the Python casefold below is registered under on every connection.
# SQLite's own lower() and LIKE fold ASCII only: lower('KÄRKKÄINEN') comes
# back unchanged and 'KÄRKKÄINEN' LIKE '%kärkkäinen%' is 0. This is a Finnish
# application, so å, ä and ö decide whether a supplier is found at all.
CASEFOLD = "casefold"


def _casefold(value):
    """str.casefold, as a SQL function. NULL in, NULL out, like SQLite's own."""
    return None if value is None else str(value).casefold()


def register_functions(conn) -> None:
    """Put the Python helpers every query in this package may use on a connection.

    Registered on both the read and the write connection, so a name is
    matched by the same rule whichever one the caller happens to hold.
    Deterministic, so SQLite may use it in an index or a partial index
    without caching a stale answer.
    """
    conn.create_function(CASEFOLD, 1, _casefold, deterministic=True)


def _is_corruption(exc: sqlite3.DatabaseError) -> bool:
    text = str(exc).lower()
    return "malformed" in text or "corrupt" in text


def _is_lock(exc: sqlite3.OperationalError) -> bool:
    """True only for the lock sqlite reports when Kitsas holds the book."""
    text = str(exc).lower()
    return "locked" in text or "busy" in text


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
            raise self._missing()
        try:
            with self.connect_read() as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES.issubset(tables):
                    raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book. {NOT_A_BOOK_FIX}")
                row = conn.execute("SELECT arvo FROM Asetus WHERE avain='KpVersio'").fetchone()
        except sqlite3.DatabaseError as exc:
            if isinstance(exc, sqlite3.OperationalError) and _is_lock(exc):
                raise self._locked() from None
            if _is_corruption(exc):
                raise CorruptBookError(
                    f"{self.path.name} is a database but it is damaged ({exc}). "
                    "Restore it from a backup or from Kitsas's own backup copy."
                ) from None
            raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book. {NOT_A_BOOK_FIX}") from None
        if row is None:
            raise NotAKitsasBookError(f"{self.path.name} is not a Kitsas book. {NOT_A_BOOK_FIX}")
        try:
            return int(row[0])
        except (TypeError, ValueError):
            # Not UnsupportedSchemaError: that one promises reads still work
            # and only writes are refused, which cannot be judged without a
            # version number to compare against SUPPORTED_KPVERSIO.
            raise NotAKitsasBookError(
                f"{self.path.name} stores KpVersio as {row[0]!r}, which is not a version "
                f"number, so this file is not a Kitsas book. {NOT_A_BOOK_FIX}"
            ) from None

    def _missing(self) -> NotAKitsasBookError:
        return NotAKitsasBookError(
            f"There is no file at {self.path}. Check the path (the --book argument or "
            "KITSAS_BOOK) and try again."
        )

    def _locked(self) -> BookLockedError:
        return BookLockedError(
            f"Kitsas has {self.path.name} open. Close Kitsas and try again. "
            "Kitsas locks a book exclusively while it is open."
        )

    # -- connections ----------------------------------------------------

    def _read_uri(self) -> str:
        """The read-only URI for this book, including the UNC case.

        A Windows UNC path, `\\\\server\\share\\book.kitsas`, comes out of
        as_posix() as `//server/share/book.kitsas`. Pasted after "file:"
        that reads as the authority "server", which SQLite refuses outright
        ("invalid uri authority: server"), so every read tool failed on a
        book kept on a NAS while connect_write, which does not use a URI,
        opened the same file happily. Two more slashes leave the authority
        empty, which is the form SQLite documents for a UNC path, and the
        path stays the UNC path Windows expects.
        """
        encoded = quote(self.path.as_posix(), safe=URI_SAFE_CHARACTERS)
        prefix = "file://" if encoded.startswith("//") else "file:"
        return prefix + encoded + "?mode=ro"

    @contextmanager
    def connect_read(self):
        # A path that does not exist is a typo, not a locked book. Telling the
        # user to close Kitsas over a book Kitsas never had open sends them in
        # a circle, so the two are told apart here.
        if not self.path.exists():
            raise self._missing()
        try:
            conn = sqlite3.connect(self._read_uri(), uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        except sqlite3.OperationalError as exc:
            if _is_lock(exc):
                raise self._locked() from None
            if "unable to open" in str(exc).lower():
                raise NotAKitsasBookError(
                    f"{self.path} could not be opened as a database file. Check that the path "
                    "names a .kitsas file and that the folder is readable, then try again."
                ) from None
            raise
        conn.row_factory = sqlite3.Row
        register_functions(conn)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            yield conn
        except sqlite3.OperationalError as exc:
            # With PRAGMA locking_mode = EXCLUSIVE, which is how Kitsas holds a
            # book, the lock is not hit by connect() but by the caller's first
            # statement, inside this yield. Unrelated OperationalErrors (a
            # missing table, a syntax error) still propagate as themselves.
            if _is_lock(exc):
                raise self._locked() from None
            raise
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
        register_functions(conn)
        try:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            conn.close()
            if _is_lock(exc):
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
        target = self._free_backup_path()
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

    def _free_backup_path(self) -> Path:
        """A backup name no file holds yet, so no backup can overwrite another.

        The stamp carries microseconds because two writes can land in the same
        second, and the counter covers the rest: overwriting a backup would
        destroy the only copy of an earlier pre-write state.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        for attempt in range(MAX_BACKUP_ATTEMPTS):
            name = stamp if attempt == 0 else f"{stamp}-{attempt}"
            target = self.path.with_suffix(self.path.suffix + f".{name}.bak")
            taken = target.exists() or any(
                Path(str(target) + suffix).exists() for suffix in SIDECAR_SUFFIXES
            )
            if not taken:
                return target
        raise BackupError(
            f"Could not find a free backup name beside {self.path.name}. "
            "Move the existing .bak files away and try again."
        )
