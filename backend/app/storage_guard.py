"""Cooperating SQLite file lifecycle lock, using only the standard library.

Hold this across path guards, connect, transaction and close. No model, HTTP or
browser work belongs inside it. It does not replace each store's ownership and
protected-path checks. The fixed sibling lock file is retained after release.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time


MAGIC = b"DouyinMind SQLite lifecycle guard v1\n"
# Windows byte-range locks also block other descriptors' reads. Keep the
# immutable ownership header outside the lock range, including during startup.
LOCK_OFFSET = 4096
_registry_lock = threading.Lock()
_local_locks = {}
_thread_state = threading.local()


class StorageGuardError(ValueError):
    """Fixed diagnostic code; never includes a path or file contents."""


class SqliteOwnershipError(StorageGuardError):
    """Ownership could not be established without trusting a foreign DB."""


def assert_no_orphan_sqlite_files(database):
    """Before exclusive creation, refuse a missing main with any SQLite sidecar.

    The caller holds the lifecycle lock and validates its protected location.
    This creates nothing and does not infer history from the coordination file.
    An existing main is handled by ordinary component ownership verification.
    """
    path = Path(database)
    _target(path)
    try:
        path.lstat()
    except FileNotFoundError:
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                Path(str(path) + suffix).lstat()
            except FileNotFoundError:
                continue
            raise StorageGuardError("orphan_sqlite_sidecar")


def _target(database):
    path = Path(database)
    if (not path.is_absolute() or path.resolve() != path or not path.parent.is_dir()
            or path.parent.resolve() != path.parent):
        raise StorageGuardError("unsafe_storage_guard_directory")
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StorageGuardError("unsafe_storage_guard_database")
    return Path(str(path) + ".guard.lock")


def _checked_info(path):
    try:
        info = path.lstat()
    except OSError:
        raise StorageGuardError("storage_guard_file_unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path:
        raise StorageGuardError("unsafe_storage_guard_file")
    return info


def _identity(info):
    return info.st_dev, info.st_ino


def _check_descriptor(path, fd):
    try:
        opened = os.fstat(fd)
        current = _checked_info(path)
    except OSError:
        raise StorageGuardError("storage_guard_file_unavailable") from None
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or _identity(current) != _identity(opened)):
        raise StorageGuardError("unsafe_storage_guard_file")
    return opened


def _open_owned(path, deadline):
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    while True:
        created = False
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            before = _checked_info(path)
            try:
                fd = os.open(path, flags)
            except OSError:
                raise StorageGuardError("storage_guard_open_failed") from None
            if _identity(before) != _identity(os.fstat(fd)):
                os.close(fd)
                raise StorageGuardError("unsafe_storage_guard_file")
        except OSError:
            raise StorageGuardError("storage_guard_open_failed") from None
        try:
            _check_descriptor(path, fd)
            if created:
                # Only the exclusive creator writes. An existing empty/prefix
                # file is never adopted or overwritten by another process.
                remaining = MAGIC
                while remaining:
                    written = os.write(fd, remaining)
                    if not written:
                        raise StorageGuardError("storage_guard_initialization_failed")
                    remaining = remaining[written:]
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            header = os.read(fd, len(MAGIC) + 1)
            if header == MAGIC:
                return fd
            if len(header) < len(MAGIC) and MAGIC.startswith(header):
                # Another creator may not yet have published its small header.
                # Wait boundedly without writing or locking its unready file.
                os.close(fd)
                fd = None
                if time.monotonic() >= deadline:
                    raise StorageGuardError("storage_guard_initialization_timeout")
                time.sleep(min(.01, max(0, deadline - time.monotonic())))
                continue
            raise StorageGuardError("foreign_storage_guard_file")
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise


def _try_os_lock(fd):
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd):
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def guarded_sqlite_file(database, *, timeout=10.0):
    """Serialize a complete SQLite file lifecycle across threads/processes.

    The caller chooses a fixed absolute DB path. Its parent must already exist;
    this helper creates no directories and accepts no commands/URLs. Timeout is
    finite and covers local-lock contention, initialization and OS locking.
    Same-thread nesting for one path is supported without relocking another fd.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 <= timeout <= 60:
        raise StorageGuardError("invalid_storage_guard_timeout")
    path = _target(database)
    key = os.path.normcase(str(path))
    deadline = time.monotonic() + timeout
    with _registry_lock:
        local = _local_locks.setdefault(key, threading.RLock())
    if not local.acquire(timeout=max(0, deadline - time.monotonic())):
        raise StorageGuardError("storage_guard_timeout")
    depths = getattr(_thread_state, "depths", None)
    if depths is None:
        depths = _thread_state.depths = {}
    if key in depths:
        depths[key] += 1
        try:
            yield path
        finally:
            depths[key] -= 1
            local.release()

        return
    fd, acquired = None, False
    try:
        fd = _open_owned(path, deadline)
        while True:
            try:
                _try_os_lock(fd)
                acquired = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EBUSY, errno.EDEADLK}:
                    raise StorageGuardError("storage_guard_lock_failed") from None
                if time.monotonic() >= deadline:
                    raise StorageGuardError("storage_guard_timeout") from None
                # This is bounded wait for an actual cooperating OS mutex,
                # never a delay that guesses when a journal will disappear.
                time.sleep(min(.01, max(0, deadline - time.monotonic())))
        _check_descriptor(path, fd)
        os.lseek(fd, 0, os.SEEK_SET)
        if os.read(fd, len(MAGIC) + 1) != MAGIC:
            raise StorageGuardError("foreign_storage_guard_file")
        depths[key] = 1
        try:
            yield path
        finally:
            del depths[key]
    finally:
        try:
            if acquired:
                _os_unlock(fd)
        finally:
            if fd is not None:
                os.close(fd)
            local.release()



def _owned_connection(path, owner_check, suffix, *, before_check=None):
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + suffix, uri=True, timeout=10)
        if before_check is not None:
            before_check()
        if owner_check(db) is True:
            return db
    except sqlite3.Error:
        pass
    except BaseException:
        if db is not None:
            db.close()
        raise
    if db is not None:
        db.close()
    return None


@contextmanager
def verified_readonly_connection(database, owner_check, *, guard):
    """Read a known component, recovering its own hot journal only if needed.

    Callers first validate their fixed/protected location and hold their normal
    lifecycle lock. ``guard`` rechecks all their path/sidecar restrictions here;
    ``owner_check`` is a trusted, read-only check of stable component metadata,
    never a model/user callback or a mutable authorization test.

    Ordinary RO success never opens RW. If RO fails and a safe journal exists,
    immutable RO may prove ownership without attempting rollback. Only then can
    an ordinary RW SQLite read perform SQLite's own recovery. No journal bytes
    are manually modified/deleted, no schema is created and no SQL is repaired.
    """
    path = Path(database)
    if not callable(owner_check) or not callable(guard):
        raise StorageGuardError("invalid_sqlite_owner_check")
    with guarded_sqlite_file(path):
        guard()
        db = _owned_connection(path, owner_check, "?mode=ro")
        if db is None:
            journal = Path(str(path) + "-journal")
            try:
                journal_info = journal.lstat()
            except FileNotFoundError:
                raise SqliteOwnershipError("sqlite_owner_unverified") from None
            if (not stat.S_ISREG(journal_info.st_mode) or journal_info.st_nlink != 1
                    or journal.resolve() != journal):
                raise StorageGuardError("unsafe_sqlite_recovery_journal")
            identity = _identity(path.lstat())
            immutable = _owned_connection(path, owner_check, "?mode=ro&immutable=1")
            if immutable is None:
                raise SqliteOwnershipError("sqlite_owner_unverified")
            immutable.close()
            # The component guard can additionally enforce metadata-dependent
            # rules (for example the Registry's permanent required marker).
            def verify_identity():
                guard()
                try:
                    same_file = _identity(path.lstat()) == identity
                except OSError:
                    same_file = False
                if not same_file:
                    raise StorageGuardError("sqlite_recovery_identity_changed")
            verify_identity()
            recovered = _owned_connection(path, owner_check, "?mode=rw", before_check=verify_identity)
            if recovered is None:
                raise SqliteOwnershipError("sqlite_owner_unverified")
            recovered.close()
            verify_identity()
            db = _owned_connection(path, owner_check, "?mode=ro", before_check=verify_identity)
            if db is None:
                raise SqliteOwnershipError("sqlite_owner_unverified")
        try:
            # Owner metadata may latch a stricter caller policy, including on
            # the ordinary RO fast path. Enforce it before exposing the handle.
            guard()
            yield db
        finally:
            db.close()
