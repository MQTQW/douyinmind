"""Owned temporary files and bounded Python children; no application services."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from app import storage_guard as guard_module
from app.storage_guard import (MAGIC, StorageGuardError, SqliteOwnershipError,
    guarded_sqlite_file, verified_readonly_connection, assert_no_orphan_sqlite_files)


def test_no_directory_creation_and_invalid_timeout(tmp_path):
    path = tmp_path / "absent" / "fixture.sqlite3"
    with pytest.raises(StorageGuardError, match="unsafe_storage_guard_directory"):
        with guarded_sqlite_file(path):
            pass
    assert not path.parent.exists()
    for invalid in (True, -1, float("inf"), float("nan"), 61, "1"):
        with pytest.raises(StorageGuardError, match="invalid_storage_guard_timeout"):
            with guarded_sqlite_file(tmp_path / "fixture.sqlite3", timeout=invalid):
                pass


def test_foreign_existing_lock_is_never_overwritten(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    lock = Path(str(path) + ".guard.lock")
    for value, reason in ((b"foreign precious fixture", "foreign_storage_guard_file"),
                          (b"", "storage_guard_initialization_timeout"),
                          (MAGIC[:6], "storage_guard_initialization_timeout")):
        lock.write_bytes(value)
        with pytest.raises(StorageGuardError, match=reason):
            with guarded_sqlite_file(path, timeout=.03):
                pass
        assert lock.read_bytes() == value


def test_hardlink_and_symlink_never_adopted(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    foreign = tmp_path / "precious"
    foreign.write_bytes(MAGIC)
    lock = Path(str(path) + ".guard.lock")
    os.link(foreign, lock)
    with pytest.raises(StorageGuardError, match="unsafe_storage_guard_file"):
        with guarded_sqlite_file(path):
            pass
    assert foreign.read_bytes() == MAGIC
    lock.unlink()
    try:
        lock.symlink_to(foreign)
    except OSError:
        # Windows without CreateSymbolicLink privilege still exercises hardlink.
        return
    with pytest.raises(StorageGuardError, match="unsafe_storage_guard_file"):
        with guarded_sqlite_file(path):
            pass
    assert foreign.read_bytes() == MAGIC


def test_same_thread_nesting_and_other_thread_timeout(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    with guarded_sqlite_file(path):
        with guarded_sqlite_file(path, timeout=0):
            with ThreadPoolExecutor(max_workers=1) as pool:
                def blocked():
                    started = time.monotonic()
                    with pytest.raises(StorageGuardError, match="storage_guard_timeout"):
                        with guarded_sqlite_file(path, timeout=.08):
                            pass
                    return time.monotonic() - started
                assert pool.submit(blocked).result(timeout=2) >= .06
    with guarded_sqlite_file(path, timeout=0):
        pass
    assert Path(str(path) + ".guard.lock").read_bytes() == MAGIC


CHILD = r'''
import json,os,sys,time
from pathlib import Path
from app.storage_guard import guarded_sqlite_file,StorageGuardError
path=Path(sys.argv[1]); timeout=float(sys.argv[2]); hold=float(sys.argv[3])
started=time.monotonic()
try:
    with guarded_sqlite_file(path,timeout=timeout):
        print(json.dumps({'event':'entered','at':time.monotonic(),'pid':os.getpid(),'wait':time.monotonic()-started}),flush=True)
        time.sleep(hold)
        print(json.dumps({'event':'leaving','at':time.monotonic(),'pid':os.getpid()}),flush=True)
except StorageGuardError as error:
    print(json.dumps({'event':'error','code':str(error),'elapsed':time.monotonic()-started,'pid':os.getpid()}),flush=True)
'''


def _spawn(path, timeout, hold):
    return subprocess.Popen([sys.executable, "-c", CHILD, str(path), str(timeout), str(hold)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _finish(process, *, timeout=6):
    stdout, stderr = process.communicate(timeout=timeout)
    assert process.returncode == 0, stderr
    assert not stderr
    return [json.loads(line) for line in stdout.splitlines()]


def test_real_process_lock_waits_without_blocking_header_read_and_timeout_is_bounded(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    holder = _spawn(path, 2, .6)
    first = json.loads(holder.stdout.readline())
    assert first["event"] == "entered"
    timeout_child = _spawn(path, .1, 0)
    waiter = _spawn(path, 3, .02)
    timed = _finish(timeout_child)
    waited = _finish(waiter)
    rest = _finish(holder)
    assert timed[0]["event"] == "error" and timed[0]["code"] == "storage_guard_timeout"
    assert .08 <= timed[0]["elapsed"] < 1
    assert waited[0]["event"] == "entered" and waited[0]["wait"] > .1
    assert waited[0]["at"] >= rest[0]["at"]
    # Locking beyond EOF must not mutate/extend the ownership file on Windows.
    assert Path(str(path) + ".guard.lock").read_bytes() == MAGIC
    from app.assistant.control import process_alive
    assert all(not process_alive(row["pid"]) for row in (first, timed[0], waited[0]))


QUEUE_CHILD = r'''
import json,os,sys
from pathlib import Path
from app.assistant.queue import AssistantQueue
queue=AssistantQueue(Path(sys.argv[1]))
for number in range(40):
    queue.update_control(lambda previous:dict(previous,count=previous.get('count',0)+1))
    queue.get_control()
print(json.dumps({'pid':os.getpid(),'done':True}),flush=True)
'''


def test_real_process_queue_initialization_and_transaction_close_serialize(tmp_path):
    path = tmp_path / "queue.sqlite3"
    processes = [subprocess.Popen([sys.executable, "-c", QUEUE_CHILD, str(path)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(2)]
    # Forty commits plus forty independently ownership-checked reads per child
    # are a workload budget, not the OS-lock timeout. The measured fixed build
    # takes about six seconds on Windows; allow bounded scheduler/AV variance.
    rows = [_finish(process, timeout=20) for process in processes]
    assert all(row[0]["done"] for row in rows)
    from app.assistant.queue import AssistantQueue
    from app.assistant.control import process_alive
    assert AssistantQueue(path).get_control()["count"] == 80
    assert all(not process_alive(row[0]["pid"]) for row in rows)


def test_verified_normal_owned_read_stays_readonly_and_never_opens_rw(tmp_path, monkeypatch):
    path = tmp_path / 'normal.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE component(value TEXT)')
        db.execute("INSERT INTO component VALUES('synthetic-owner')")
    original = guard_module.sqlite3.connect
    modes = []
    def observed(database, *args, **kwargs):
        modes.append(str(database))
        return original(database, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(guard_module.sqlite3, 'connect', observed)
        with verified_readonly_connection(path,
                lambda db: db.execute('SELECT value FROM component').fetchone() == ('synthetic-owner',),
                guard=lambda: None) as db:
            assert db.execute('SELECT count(*) FROM component').fetchone() == (1,)
    assert len(modes) == 1 and modes[0].endswith('?mode=ro')


HOT_CHILD = r'''
import json,os,sqlite3,sys
from pathlib import Path
from app.storage_guard import guarded_sqlite_file
path=Path(sys.argv[1])
with guarded_sqlite_file(path):
    db=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA cache_size=8')
    db.execute('BEGIN IMMEDIATE')
    db.execute('UPDATE filler SET payload=?',(b'Z'*8192,))
    print(json.dumps({'pid':os.getpid(),'journal_header':Path(str(path)+'-journal').read_bytes()[:8].hex()}),flush=True)
    os._exit(73)
'''


@pytest.mark.parametrize('owned', [True, False])
def test_hot_journal_recovers_only_an_immutable_proven_owned_database(owned, monkeypatch):
    # Deliberately retained mkdtemp, not pytest cleanup of a crash fixture.
    root = Path(tempfile.mkdtemp(prefix='dm-verified-owner-crash-')).resolve()
    path = root / 'fixture.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE component(value TEXT)')
        db.execute('INSERT INTO component VALUES(?)', ('synthetic-owner' if owned else 'foreign-fixture',))
        db.execute('CREATE TABLE filler(id INTEGER PRIMARY KEY,payload BLOB)')
        db.executemany('INSERT INTO filler VALUES(?,?)', ((index, b'A' * 8192) for index in range(256)))
    process = subprocess.Popen([sys.executable, '-c', HOT_CHILD, str(path)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stdout, stderr = process.communicate(timeout=15)
    result = json.loads(stdout)
    assert process.returncode == 73 and not stderr
    assert result['journal_header'] == 'd9d505f920a163d7'
    from app.assistant.control import process_alive
    assert not process_alive(process.pid) and not process_alive(result['pid'])
    journal = Path(str(path) + '-journal')
    before = (path.read_bytes(), journal.read_bytes())
    original = guard_module.sqlite3.connect
    modes = []
    def observed(database, *args, **kwargs):
        modes.append(str(database))
        return original(database, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(guard_module.sqlite3, 'connect', observed)
        if owned:
            with verified_readonly_connection(path,
                    lambda db: db.execute('SELECT value FROM component').fetchone() == ('synthetic-owner',),
                    guard=lambda: None) as db:
                assert db.execute('SELECT count(*) FROM filler WHERE payload=?', (b'A' * 8192,)).fetchone() == (256,)
            assert any(mode.endswith('?mode=rw') for mode in modes)
            assert not journal.exists()
        else:
            with pytest.raises(SqliteOwnershipError, match='sqlite_owner_unverified'):
                with verified_readonly_connection(path,
                        lambda db: db.execute('SELECT value FROM component').fetchone() == ('synthetic-owner',),
                        guard=lambda: None):
                    pytest.fail('foreign hot DB was adopted')
            assert not any(mode.endswith('?mode=rw') for mode in modes)
            assert (path.read_bytes(), journal.read_bytes()) == before


def test_successful_owner_check_latches_guard_policy_before_yield_and_closes_on_rejection(tmp_path, monkeypatch):
    path = tmp_path / 'metadata-policy.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE component(value TEXT)')
        db.execute("INSERT INTO component VALUES('synthetic-owner')")
    latched = [False]
    connections = []
    original = guard_module.sqlite3.connect
    def observed(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection
    def owner_check(db):
        assert db.execute('SELECT value FROM component').fetchone() == ('synthetic-owner',)
        latched[0] = True
        return True
    def guard():
        if latched[0]:
            raise StorageGuardError('synthetic_required_marker_missing')
    with monkeypatch.context() as patch:
        patch.setattr(guard_module.sqlite3, 'connect', observed)
        with pytest.raises(StorageGuardError, match='synthetic_required_marker_missing'):
            with verified_readonly_connection(path, owner_check, guard=guard):
                pytest.fail('unverified post-owner policy was exposed')
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connections[0].execute('SELECT 1')


def test_hot_recovery_checks_main_identity_after_rw_open_before_first_owner_select(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix='dm-owner-identity-crash-')).resolve()
    path = root / 'fixture.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE component(value TEXT)')
        db.execute("INSERT INTO component VALUES('synthetic-owner')")
        db.execute('CREATE TABLE filler(id INTEGER PRIMARY KEY,payload BLOB)')
        db.executemany('INSERT INTO filler VALUES(?,?)', ((index, b'A' * 8192) for index in range(256)))
    process = subprocess.Popen([sys.executable, '-c', HOT_CHILD, str(path)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    stdout, stderr = process.communicate(timeout=15)
    result = json.loads(stdout)
    assert process.returncode == 73 and not stderr and result['journal_header'] == 'd9d505f920a163d7'
    journal = Path(str(path) + '-journal')
    before = (path.read_bytes(), journal.read_bytes())
    original_connect, original_lstat = guard_module.sqlite3.connect, Path.lstat
    rw_open = [False]
    rw_owner_checks = []
    def observed(database, *args, **kwargs):
        connection = original_connect(database, *args, **kwargs)
        if str(database).endswith('?mode=rw'):
            rw_open[0] = True
        return connection
    def replacement_identity(candidate, *args, **kwargs):
        info = original_lstat(candidate, *args, **kwargs)
        if candidate == path and rw_open[0]:
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return info
    def owner_check(db):
        if rw_open[0]:
            rw_owner_checks.append(True)
        return db.execute('SELECT value FROM component').fetchone() == ('synthetic-owner',)
    with monkeypatch.context() as patch:
        patch.setattr(guard_module.sqlite3, 'connect', observed)
        patch.setattr(Path, 'lstat', replacement_identity)
        with pytest.raises(StorageGuardError, match='sqlite_recovery_identity_changed'):
            with verified_readonly_connection(path, owner_check, guard=lambda: None):
                pytest.fail('replacement identity reached owner query')
    assert rw_open[0] and not rw_owner_checks
    assert (path.read_bytes(), journal.read_bytes()) == before
    from app.assistant.control import process_alive
    assert not process_alive(process.pid) and not process_alive(result['pid'])


@pytest.mark.parametrize('suffix', ['-journal', '-wal', '-shm'])
@pytest.mark.parametrize('kind', ['empty', 'hardlink', 'dangling_symlink'])
def test_missing_main_with_any_orphan_sidecar_cannot_be_bootstrapped(suffix, kind):
    root = Path(tempfile.mkdtemp(prefix='dm-orphan-sidecar-review-')).resolve()
    path = root / 'not-created.sqlite3'
    sidecar = Path(str(path) + suffix)
    target = root / 'synthetic-preserve-file'
    if kind == 'empty':
        sidecar.write_bytes(b'')
    elif kind == 'hardlink':
        target.write_bytes(b'synthetic precious sidecar fixture')
        os.link(target, sidecar)
    else:
        try:
            sidecar.symlink_to(target)
        except OSError:
            pytest.skip('this isolated Windows environment cannot create symlinks')
    before = target.read_bytes() if target.exists() else None
    with guarded_sqlite_file(path):
        with pytest.raises(StorageGuardError, match='orphan_sqlite_sidecar'):
            assert_no_orphan_sqlite_files(path)
    assert not path.exists()
    assert sidecar.lstat() is not None
    assert (target.read_bytes() if target.exists() else None) == before


def test_orphan_gate_neither_claims_history_from_lock_nor_rejects_existing_main(tmp_path):
    path = tmp_path / 'fixture.sqlite3'
    with guarded_sqlite_file(path):
        assert_no_orphan_sqlite_files(path)
    assert not path.exists() and Path(str(path) + '.guard.lock').is_file()
    from app.assistant.queue import AssistantQueue
    queue = AssistantQueue(path)
    assert queue.list_tasks()['total'] == 0
    sidecar = Path(str(path) + '-journal')
    sidecar.write_bytes(b'synthetic residual journal for existing main')
    before = (path.read_bytes(), sidecar.read_bytes())
    with guarded_sqlite_file(path):
        assert_no_orphan_sqlite_files(path)
    assert (path.read_bytes(), sidecar.read_bytes()) == before
