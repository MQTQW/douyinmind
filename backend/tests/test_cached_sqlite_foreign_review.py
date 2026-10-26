"""Preserved foreign-hot/empty-ledger counterexamples; no real data is used.

Each case owns a fresh mkdtemp directory and leaves it for forensic inspection.
No recursive cleanup is performed. Only these newly owned fixture files are
replaced or truncated; subprocesses are ours and exit deliberately with73.
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
from types import SimpleNamespace

import pytest

from app.assistant.queue import AssistantQueue
from app.assistant.conversation import ConversationStore
from app.assistant.registry import Registry
from app.mention_agent.store import AgentStore
from app.services.mention_ingestion_service import MentionIngestionService


CHILD = r'''
import json,os,sqlite3,sys
from pathlib import Path
db=sqlite3.connect(sys.argv[1])
db.execute('PRAGMA journal_mode=DELETE')
db.execute('PRAGMA synchronous=FULL')
db.execute('PRAGMA cache_size=4')
db.execute('PRAGMA cache_spill=ON')
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE filler SET payload=?',(b'Z'*8192,))
journal=Path(sys.argv[1]+'-journal')
print(json.dumps({'pid':os.getpid(),'journal_header':journal.read_bytes()[:8].hex()}),flush=True)
os._exit(73)
'''


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def fixture_root(label):
    root = Path(tempfile.mkdtemp(prefix="dm-cached-owner-" + label + "-")).resolve()
    assert root.is_absolute() and root.is_dir()
    return root


def foreign_hot_pair(root, component_table):
    path = root / "preserved-foreign-original.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE " + component_table + "(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("INSERT INTO " + component_table + " VALUES('component','Foreign.review.fixture.v1')")
        db.execute("CREATE TABLE filler(id INTEGER PRIMARY KEY,payload BLOB)")
        db.executemany("INSERT INTO filler VALUES(?,?)", ((i, b"A" * 8192) for i in range(256)))
        db.commit()
    result = subprocess.run([sys.executable, "-B", "-c", CHILD, str(path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 73 and not result.stderr
    child = json.loads(result.stdout)
    assert child["journal_header"] == "d9d505f920a163d7"
    journal = Path(str(path) + "-journal")
    assert journal.stat().st_size > 512
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("SELECT value FROM " + component_table + " WHERE key='component'").fetchone()
    return path, journal, child


@pytest.mark.parametrize("kind,component_table", [("queue", "assistant_meta"), ("conversation", "context_meta"), ("agent", "agent_meta")])
def test_cached_store_never_recovers_foreign_hot_pair_before_owner_check(kind, component_table):
    root = fixture_root(kind)
    target = root / "already-constructed-own.sqlite3"
    if kind == "queue":
        store = AssistantQueue(target); observe = store.counts
    elif kind == "conversation":
        store = ConversationStore(target); observe = lambda: store.task_scope("synthetic-no-task")
    else:
        store = AgentStore(target); observe = store.list_tasks
    shutil.copyfile(target, root / "owned-before-replacement.sqlite3")
    foreign, foreign_journal, child = foreign_hot_pair(root, component_table)
    journal = Path(str(target) + "-journal")
    assert target.parent == root and journal.parent == root
    shutil.copyfile(foreign, target)
    shutil.copyfile(foreign_journal, journal)
    before_db, before_journal = target.read_bytes(), journal.read_bytes()
    rejected = None
    try:
        observe()
    except ValueError as error:
        rejected = type(error).__name__
    after_db = target.read_bytes()
    after_journal = journal.read_bytes() if journal.exists() else None
    unchanged = before_db == after_db and before_journal == after_journal
    manifest = {"kind": kind, "root": str(root), "owned_child": child, "child_exit_code": result_code(child),
        "cached_store_rejected_as": rejected, "unchanged": unchanged,
        "before_database_sha256": hashlib.sha256(before_db).hexdigest(), "after_database_sha256": digest(target),
        "before_journal_sha256": hashlib.sha256(before_journal).hexdigest(), "after_journal_sha256": digest(journal),
        "preserved_original_foreign_database": str(foreign), "preserved_original_foreign_journal": str(foreign_journal)}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
    print("preserved-counterexample=" + str(root))
    assert rejected is not None, "cached store must reject the foreign component"
    assert unchanged, "foreign DB/journal changed before rejection; see preserved manifest: " + str(root)


def result_code(child):
    # subprocess.run above returned only after the owned child exited with73.
    assert isinstance(child["pid"], int)
    return 73


@pytest.mark.parametrize("create", [False, True])
def test_running_receipt_service_never_bootstraps_an_existing_empty_file(create):
    root = fixture_root("receipt-empty")
    target = root / "receipt.sqlite3"
    def forbidden(*args, **kwargs): raise AssertionError("no business/model/provider dependency may run")
    service = MentionIngestionService(sessions=forbidden, worker=SimpleNamespace(_running=False), pipeline=forbidden,
        index_reader=forbidden, binding_path=root / "not-opened-binding.json", receipt_path=target)
    with service._ledger(create=True) as db:
        db.execute("INSERT INTO import_receipts VALUES(?,?,?,?,?)", ("synthetic-prior-request", "7999999000000000099", "{}", "unknown", "synthetic-owner"))
    shutil.copyfile(target, root / "owned-receipt-before-truncation.sqlite3")
    assert target.parent == root
    target.write_bytes(b"")  # Deliberately truncate only this new, owned fixture.
    before = target.read_bytes()
    rejected = None
    try:
        with service._ledger(create=create) as db: db.execute("SELECT 1").fetchone()
    except ValueError as error:
        rejected = type(error).__name__
    unchanged = target.read_bytes() == before
    (root / "manifest.json").write_text(json.dumps({"kind": "receipt-empty", "root": str(root), "create": create,
        "rejected_as": rejected, "unchanged": unchanged, "before_bytes": 0, "after_bytes": target.stat().st_size,
        "after_sha256": digest(target)}, indent=2), encoding="utf8")
    print("preserved-counterexample=" + str(root))
    assert rejected is not None, "existing empty receipt must not become a new ledger; see " + str(root)
    assert unchanged, "existing empty receipt bytes were rewritten; see " + str(root)


def test_preexisting_empty_registry_without_marker_is_never_adopted():
    root = fixture_root("registry-empty")
    target = root / "registry.sqlite3"
    target.write_bytes(b"")
    before = target.read_bytes()
    rejected = None
    try:
        Registry(target, catalog_reader=lambda: [])  # Default allow_create=True.
    except ValueError as error:
        rejected = type(error).__name__
    unchanged = target.read_bytes() == before
    marker = root / "registry-required.json"
    (root / "manifest.json").write_text(json.dumps({"kind": "registry-empty", "root": str(root),
        "rejected_as": rejected, "unchanged": unchanged, "before_bytes": 0,
        "after_bytes": target.stat().st_size, "marker_exists": marker.exists(),
        "after_sha256": digest(target)}, indent=2), encoding="utf8")
    print("preserved-counterexample=" + str(root))
    assert rejected is not None, "preexisting empty Registry is not a new exclusive-created file; see " + str(root)
    assert unchanged and not marker.exists(), "unknown empty Registry/marker was modified; see " + str(root)


def test_first_explicit_receipt_create_works_only_for_a_truly_absent_file():
    root = fixture_root("receipt-new")
    target = root / "receipt.sqlite3"
    def forbidden(*args, **kwargs): raise AssertionError("no business dependency may run")
    service = MentionIngestionService(sessions=forbidden, worker=SimpleNamespace(_running=False), pipeline=forbidden,
        index_reader=forbidden, binding_path=root / "not-opened-binding.json", receipt_path=target)
    assert not target.exists()
    with service._ledger(create=True) as db:
        row = db.execute("SELECT component FROM receipt_metadata").fetchone()
        assert row is not None and row[0] == "DouyinMind.mention_ingestion.receipts.v1"
    assert target.is_file() and target.stat().st_size > 0
    with service._ledger(create=False) as db:
        assert db.execute("SELECT COUNT(*) FROM import_receipts").fetchone()[0] == 0
    (root / "manifest.json").write_text(json.dumps({"kind": "receipt-new", "root": str(root),
        "created_explicitly_from_absent_file": True, "database_sha256": digest(target)}, indent=2), encoding="utf8")
    print("preserved-positive-fixture=" + str(root))
