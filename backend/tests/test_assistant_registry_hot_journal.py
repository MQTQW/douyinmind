"""Real abrupt SQLite writer exit in temporary Registry/foreign fixtures."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.assistant.registry import Registry, RegistryError


CHILD = r'''
import os,sqlite3,sys
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=4')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
db.execute("UPDATE registry_metadata SET value='uncommitted' WHERE key='fixture-value'")
for number in range(128):
    db.execute('INSERT INTO registry_crash_spill(payload) VALUES(?)',('x'*8192,))
os._exit(73)
'''


def seed(path, *, foreign=False):
    if foreign:
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE registry_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            db.execute("INSERT INTO registry_metadata VALUES('component','Foreign.fixture.v1')")
            db.commit()
        registry = None
    else:
        registry = Registry(path, catalog_reader=lambda: [])
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE registry_crash_spill(payload TEXT)")
        db.execute("INSERT INTO registry_metadata VALUES('fixture-value','committed')")
        db.commit()
    return registry


def abrupt_exit(path):
    child = subprocess.run([sys.executable, "-B", "-c", CHILD, str(path)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 73, child.stderr
    journal = Path(str(path) + "-journal")
    assert journal.is_file() and journal.stat().st_size > 512
    assert journal.read_bytes()[:8] == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as check:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            check.execute("SELECT value FROM registry_metadata WHERE key='component'").fetchone()
    return journal


@pytest.mark.parametrize("entry", ["constructor", "cached_read", "cached_write"])
def test_owned_hot_journal_recovers_in_constructor_and_cached_short_connections(tmp_path, entry):
    path = tmp_path / "registry.sqlite3"
    registry = seed(path)
    journal = abrupt_exit(path)
    if entry == "constructor":
        registry = Registry(path, catalog_reader=lambda: [], allow_create=False)
    elif entry == "cached_read":
        assert registry.snapshot()["users"] == []
    else:
        created = registry.register_user("https://www.douyin.com/user/hot-fixture-A",
                                         "https://www.douyin.com/user/hot-fixture-B")
        assert created["verification_status"] == "unverified"
    with registry._db() as db:
        assert db.execute("SELECT value FROM registry_metadata WHERE key='fixture-value'").fetchone()[0] == "committed"
        assert db.execute("SELECT COUNT(*) FROM registry_crash_spill").fetchone()[0] == 0
    assert not journal.exists()
    assert registry.required_marker_path.is_file()


def test_missing_required_marker_blocks_before_any_hot_journal_rw_recovery(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = seed(path)
    journal = abrupt_exit(path)
    registry.required_marker_path.unlink()
    before = hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(journal.read_bytes()).hexdigest()
    with pytest.raises(RegistryError, match="registry_required_marker_missing"):
        Registry(path, catalog_reader=lambda: [], allow_create=False)
    assert not registry.required_marker_path.exists()
    assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(journal.read_bytes()).hexdigest())


def test_foreign_hot_database_and_journal_are_never_rw_recovered_or_adopted(tmp_path):
    path = tmp_path / "foreign.sqlite3"
    seed(path, foreign=True)
    journal = abrupt_exit(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(journal.read_bytes()).hexdigest()
    with pytest.raises(RegistryError, match="foreign_registry_database"):
        Registry(path, catalog_reader=lambda: [], allow_create=False)
    assert before == (hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(journal.read_bytes()).hexdigest())
    assert not (tmp_path / "registry-required.json").exists()
