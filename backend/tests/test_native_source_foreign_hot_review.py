"""Independent cached-source ownership counterexample; own fixtures only.

The child is a fixed standard-library SQLite fixture and exits with 73.
All fixture files are preserved; no real ledger, credentials or network is used.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

from app.assistant.native_source_store import NativeSourceStore


CHILD = r"""
import os,sqlite3,sys
from pathlib import Path
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=4')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE filler SET payload=?',(b'Z'*8192,))
journal=Path(sys.argv[1]+'-journal')
print(journal.read_bytes()[:8].hex(),flush=True)
os._exit(73)
"""


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def test_cached_native_store_never_recovers_foreign_hot_journal_before_owner_check():
    root = Path(tempfile.mkdtemp(prefix="dm-native-source-foreign-hot-")).resolve()
    target = root / "sources.sqlite3"
    store = NativeSourceStore(target, "9001", "https://www.douyin.com/user/synthetic_hot_B", create=True)
    shutil.copyfile(target, root / "preserved-owned-before.sqlite3")
    foreign = root / "preserved-foreign.sqlite3"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES('component','Ordinary.foreign.fixture')")
        db.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY,payload BLOB)")
        db.executemany("INSERT INTO filler VALUES(?,?)", ((i, b"A" * 8192) for i in range(256)))
        db.commit()
    child = subprocess.run([sys.executable, "-B", "-c", CHILD, str(foreign)],
        capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 73 and not child.stderr
    assert child.stdout.strip() == "d9d505f920a163d7"
    foreign_journal = Path(str(foreign) + "-journal")
    with closing(sqlite3.connect(foreign.as_uri() + "?mode=ro", uri=True)) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("SELECT value FROM meta WHERE key='component'").fetchone()
    journal = Path(str(target) + "-journal")
    assert target.parent == root and journal.parent == root
    shutil.copyfile(foreign, target)
    shutil.copyfile(foreign_journal, journal)
    before_db, before_journal = target.read_bytes(), journal.read_bytes()
    rejected = None
    try:
        store.safe_status()
    except Exception as error:
        rejected = type(error).__name__
    unchanged = before_db == target.read_bytes() and journal.exists() and before_journal == journal.read_bytes()
    report = {
        "kind": "cached_native_source_foreign_hot", "fixture_root": str(root),
        "owned_child_exit_code": child.returncode, "rejected_as": rejected,
        "foreign_database_and_journal_unchanged": unchanged,
        "before_database_sha256": hashlib.sha256(before_db).hexdigest(),
        "after_database_sha256": sha(target),
        "before_journal_sha256": hashlib.sha256(before_journal).hexdigest(),
        "after_journal_sha256": sha(journal),
        "not_performed": ["real ledger", "account", "credentials", "network", "browser", "model", "import", "publish"]
    }
    (root / "manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("preserved-native-hot-counterexample=" + str(root))
    assert rejected is not None, "a foreign component must be rejected"
    assert unchanged, "foreign DB/journal changed before rejection; preserved fixture: " + str(root)
