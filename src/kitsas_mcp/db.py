"""Opening a Kitsas book, with the checks that keep a real book safe."""

import os
import shutil
import sqlite3
import unicodedata
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

# One backup per book per process, not one per write. The MCP server builds a
# fresh Book for every tool call, so a per-instance cache meant every bill
# copied the whole book: ten bills against the 110 MB book the trial used cost
# 1.1 GB of .bak files that nothing prunes. The snapshot a session keeps is
# the state of the book before that session's first write; the writes after it
# are protected by that same snapshot rather than by one of their own, which
# is the trade-off the README states. Keyed by the resolved path so that the
# same book reached by two different spellings is still one book.
_SESSION_BACKUPS: dict[str, "_SessionBackup"] = {}


class _SessionBackup:
    """The one backup of a book in this process, and whether a write needs it.

    `protects_a_write` turns True at the first COMMIT made against it and
    never turns back. Once it is True the backup is the only copy of what the
    book looked like before those writes, so nothing may delete it.
    """

    def __init__(self, path: Path):
        self.path = path
        self.protects_a_write = False


def forget_session_backups() -> None:
    """Forget which books this process has backed up. For tests only.

    Every test in the suite is its own notional session; without this they
    would share the first test's backup the way two writes in one server
    process do.
    """
    _SESSION_BACKUPS.clear()


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

# The name the accent-blind fold below is registered under. It exists only so
# that a name whose umlauts were dropped can be NOTICED beside the partner it
# resembles, never to resolve one name to the other: in Finnish a and ä are
# different letters, not variants, so 'Kärkkäinen' and 'Karkkainen' are two
# names that may well belong to two different people.
CASEFOLD_UNACCENTED = "casefold_unaccented"


def _casefold(value):
    """str.casefold, as a SQL function. NULL in, NULL out, like SQLite's own."""
    return None if value is None else str(value).casefold()


def _casefold_unaccented(value):
    """str.casefold with the combining marks dropped, as a SQL function.

    Case folding alone does not bring an accent-stripped spelling together
    with the real one: 'Karkkainen'.casefold() and 'Kärkkäinen'.casefold() are
    still different strings. Decomposing to NFD splits ä into an a plus a
    combining diaeresis, and dropping the combining characters leaves the two
    comparable. Dropped umlauts are ordinary in Finnish practice: an OCR of a
    scanned invoice, a supplier's ASCII-only billing system, a treasurer
    typing quickly.
    """
    if value is None:
        return None
    decomposed = unicodedata.normalize("NFD", str(value).casefold())
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def register_functions(conn) -> None:
    """Put the Python helpers every query in this package may use on a connection.

    Registered on both the read and the write connection, so a name is
    matched by the same rule whichever one the caller happens to hold.
    Deterministic, so SQLite may use it in an index or a partial index
    without caching a stale answer.
    """
    conn.create_function(CASEFOLD, 1, _casefold, deterministic=True)
    conn.create_function(CASEFOLD_UNACCENTED, 1, _casefold_unaccented, deterministic=True)


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
        # Taken while nothing of ours has touched the file yet, so that the
        # comparison after a rollback is against the state the backup holds.
        before = self._fingerprint()
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
        discardable = False
        try:
            try:
                yield conn
            except Exception:
                discardable = self._rolled_back_unchanged(conn, before)
                raise
            else:
                conn.execute("COMMIT")
                # From here the backup is the only copy of what the book
                # looked like before this write, so nothing may delete it.
                entry = _SESSION_BACKUPS.get(self._session_key())
                if entry is not None:
                    entry.protects_a_write = True
        finally:
            # Closed before the backup is judged spent, because closing the
            # last connection to a WAL book checkpoints it, which rewrites the
            # book file without changing anything in it.
            conn.close()
            if discardable:
                self._discard_backup()

    def _rolled_back_unchanged(self, conn, before) -> bool:
        """True only when this write left the book exactly as the backup has it.

        Three things have to hold, and the answer is False if any of them
        cannot be established: the ROLLBACK went through, the connection is
        out of its transaction afterwards, and the book file is byte-for-byte
        the size and modification time it was before the transaction opened.
        The rollback is the real guarantee; the file check is a second opinion
        that costs one stat. Only the book itself is compared, not the -wal
        sidecar: SQLite may have spilled pages of an abandoned transaction
        into the WAL, which changes that file without any of it having been
        committed to the book.

        A failed ROLLBACK is not re-raised. The caller's own error is the one
        worth reporting, closing the connection rolls the transaction back
        anyway, and the backup is kept.
        """
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            return False
        if conn.in_transaction:
            return False
        after = self._fingerprint()
        return before is not None and after == before

    def _fingerprint(self):
        """Cheap evidence of whether the book file has changed, or None.

        None when it cannot be read, which never compares equal to anything,
        so an unreadable book keeps its backup.
        """
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_size, stat.st_mtime_ns)

    # -- backup ---------------------------------------------------------

    def _session_key(self) -> str:
        """This book's identity for the process-wide backup registry.

        Resolved and case-normalised, so the same file reached as
        C:/Kirjanpito/kirja.kitsas and c:/kirjanpito/../Kirjanpito/kirja.kitsas
        is one book with one backup rather than two.
        """
        try:
            resolved = self.path.resolve()
        except OSError:
            resolved = self.path
        return os.path.normcase(str(resolved))

    def backup(self) -> Path:
        """Copy the book beside itself, once per book per process.

        The first write to a given book in the life of this process copies
        the whole book; every later write to the same book reuses that copy
        and copies nothing. The MCP server builds a fresh Book for each tool
        call, so a per-instance cache made that a full copy per bill: ten
        bills against a 110 MB book left 1.1 GB of .bak files behind.

        What the session keeps is therefore the book as it stood before the
        session's first write, not before the latest one. Anything written
        since is inside the book but not inside the backup, which is why the
        summary of a write says so in as many words. Drafts are reversible
        with delete_draft and Kitsas is the real undo for anything approved;
        the backup is the last resort.

        A backup whose file has since been removed by hand is taken again, so
        that no write ever proceeds without a backup existing for it.
        """
        key = self._session_key()
        entry = _SESSION_BACKUPS.get(key)
        if entry is not None:
            if entry.path.exists():
                return entry.path
            del _SESSION_BACKUPS[key]
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
        _SESSION_BACKUPS[key] = _SessionBackup(target)
        return target

    def _discard_backup(self) -> None:
        """Remove a backup that turned out to protect nothing, with its sidecars.

        Only reached when the write it was taken for rolled back and left the
        book as the backup has it. A backup that any earlier write in this
        session already committed against is the only copy of what the book
        looked like before those writes, so it is kept: protects_a_write is
        the flag that says so, and it is checked here rather than trusted to
        the caller. A backup that cannot be deleted is left where it is; a
        stray copy is not worth replacing the refusal the caller is about to
        read.
        """
        key = self._session_key()
        entry = _SESSION_BACKUPS.get(key)
        if entry is None or entry.protects_a_write:
            return
        for path in [Path(str(entry.path) + suffix) for suffix in SIDECAR_SUFFIXES] + [entry.path]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return  # Something still holds it; keep the whole set.
        del _SESSION_BACKUPS[key]

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
