"""Private durable ingestion history; callables and automatic replay are absent.

This ledger coordinates one local worker owner. It records batches and their
observed progress, never decides that an interrupted external request was free
or safe to repeat. A restart only quarantines unfinished jobs for explicit review.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Literal

from app.storage_guard import (
    assert_no_orphan_sqlite_files, guarded_sqlite_file, verified_readonly_connection,
)


COMPONENT = "DouyinMind.ingestion_jobs.v1"
_STATUSES = {"queued", "running", "done", "failed", "interrupted", "needs_review"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_STAGE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SCHEMA_V1 = {
    "ingestion_job_meta": "CREATE TABLE ingestion_job_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "ingestion_jobs": """CREATE TABLE ingestion_jobs(
      task_id TEXT PRIMARY KEY,owner_token TEXT NOT NULL,status TEXT NOT NULL,
      stage TEXT NOT NULL,progress INTEGER NOT NULL,total INTEGER NOT NULL,
      message TEXT NOT NULL,metadata TEXT NOT NULL,reason_code TEXT,current_item_id TEXT,
      created_at REAL NOT NULL,updated_at REAL NOT NULL,started_at REAL,finished_at REAL,
      shutdown_requested_at REAL,shutdown_timed_out INTEGER NOT NULL DEFAULT 0,
      revision INTEGER NOT NULL DEFAULT 1)""",
    "ingestion_job_events": """CREATE TABLE ingestion_job_events(
      event_id INTEGER PRIMARY KEY,task_id TEXT NOT NULL,event TEXT NOT NULL,
      stage TEXT NOT NULL,status TEXT NOT NULL,progress INTEGER NOT NULL,total INTEGER NOT NULL,
      reason_code TEXT,created_at REAL NOT NULL)""",
}
_KIND_COLUMN = "kind TEXT NOT NULL DEFAULT 'generic' CHECK(kind IN ('generic','ingestion','notes'))"
_SCHEMA = {**_SCHEMA_V1, "ingestion_jobs": _SCHEMA_V1["ingestion_jobs"][:-1] + ", " + _KIND_COLUMN + ")"}
_KINDS = frozenset({"generic", "ingestion", "notes"})
TaskKind = Literal["generic", "ingestion", "notes"]
_OWNER_MAGIC = b"DouyinMind ingestion worker owner v1\n"
_OWNERS_LOCK = threading.Lock()
_OWNERS = set()


class IngestionJobError(ValueError):
    """A fixed local code; raw paths, SQL and provider exceptions stay private."""


def task_identity(value):
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise IngestionJobError("invalid_ingestion_task_id")
    return value


def job_metadata(value):
    if value is None:
        return {}
    if type(value) is not dict or set(value) - {
        "platform_item_ids", "knowledge_base_id", "analysis_mode", "recovery_of",
    }:
        raise IngestionJobError("invalid_ingestion_job_metadata")
    result = deepcopy(value)
    if "platform_item_ids" in result:
        ids = result["platform_item_ids"]
        if (type(ids) is not list or len(ids) > 10000
                or any(type(item) is not str or not item.strip() or len(item) > 128
                       or any(ord(char) < 32 for char in item) for item in ids)
                or len(set(ids)) != len(ids)):
            raise IngestionJobError("invalid_ingestion_job_metadata")
    if "knowledge_base_id" in result:
        identity = result["knowledge_base_id"]
        if identity is not None and (type(identity) is not int or identity <= 0):
            raise IngestionJobError("invalid_ingestion_job_metadata")
    if "analysis_mode" in result and result["analysis_mode"] not in {"audio", "audio_visual"}:
        raise IngestionJobError("invalid_ingestion_job_metadata")
    if "recovery_of" in result:
        task_identity(result["recovery_of"])
    return result


def checked_kind(value: TaskKind | None, metadata=None) -> TaskKind:
    """None preserves legacy submission semantics, never guesses a notes job."""
    if value is None:
        return "ingestion" if "platform_item_ids" in job_metadata(metadata) else "generic"
    if type(value) is not str or value not in _KINDS:
        raise IngestionJobError("invalid_job_kind")
    return value


def record_kind(record):
    if "kind" in record:
        if record["kind"] is None:
            raise IngestionJobError("invalid_job_kind")
        return checked_kind(record["kind"])
    return checked_kind(None, record.get("metadata"))


def _page_kind(value):
    return None if value is None else checked_kind(value)


def progress_values(progress, total, message="", stage=None):
    if (type(progress) is not int or type(total) is not int
            or not 0 <= progress <= total <= 1000000
            or type(message) is not str or len(message) > 1000 or "\0" in message
            or stage is not None and (type(stage) is not str or _STAGE.fullmatch(stage) is None)):
        raise IngestionJobError("invalid_ingestion_progress")
    return progress, total, message, stage


class IngestionJobLedger:
    def __init__(self, path, *, clock=None):
        # Construction and read access do not create a DB or adopt old files.
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.clock = clock or time.time

    def _now(self):
        value = self.clock()
        if type(value) not in {int, float} or not math.isfinite(value):
            raise IngestionJobError("invalid_ingestion_job_clock")
        return float(value)

    def _location(self):
        if self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent:
            raise IngestionJobError("unsafe_ingestion_job_path")
        for candidate in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise IngestionJobError("unsafe_ingestion_job_path")

    @staticmethod
    def _owned(db):
        try:
            meta = dict(db.execute("SELECT key,value FROM ingestion_job_meta"))
            if meta not in ({"component": COMPONENT, "version": "1"}, {"component": COMPONENT, "version": "2"}):
                return False
            schema = _SCHEMA_V1 if meta["version"] == "1" else _SCHEMA
            rows = list(db.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
            normalize = lambda value: " ".join(value.lower().split())
            return (all(row[1] == "table" for row in rows)
                    and {row[0] for row in rows} == set(schema)
                    and all(normalize(row[2]) == normalize(schema[row[0]]) for row in rows))
        except (sqlite3.Error, ValueError, TypeError):
            return False

    def prepare(self):
        self._location()
        if not self.path.exists() and Path(str(self.path) + ".owner.lock").exists():
            raise IngestionJobError("ingestion_job_ledger_missing")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._location()
        with guarded_sqlite_file(self.path):
            if self.path.exists():
                with verified_readonly_connection(self.path, self._owned, guard=self._location):
                    pass
                return self
            assert_no_orphan_sqlite_files(self.path)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            db = sqlite3.connect(self.path)
            try:
                db.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA.values():
                    db.execute(statement)
                db.executemany("INSERT INTO ingestion_job_meta VALUES(?,?)", [("component", COMPONENT), ("version", "2")])
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        return self

    @contextmanager
    def _db(self, *, write=False, deadline=None):
        self._location()
        if not self.path.is_file():
            raise IngestionJobError("ingestion_job_ledger_missing")
        def remaining(cap):
            if deadline is None:
                return cap
            value = deadline - time.monotonic()
            if value <= 0:
                raise IngestionJobError("ingestion_job_deadline_exhausted")
            return min(cap, value)
        def budget(db):
            if deadline is not None:
                db.execute("PRAGMA busy_timeout=" + str(int(remaining(5) * 1000)))
                db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        with guarded_sqlite_file(self.path, timeout=remaining(10)):
            if deadline is None:
                with verified_readonly_connection(self.path, self._owned, guard=self._location):
                    pass
            else:
                # Shutdown never spends an unbounded hot-journal recovery wait.
                # Failure leaves the record for normal, owned restart recovery.
                probe = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=remaining(5))
                try:
                    budget(probe)
                    if not self._owned(probe):
                        remaining(5)
                        raise IngestionJobError("ingestion_job_ledger_not_owned")
                finally:
                    probe.close()
                self._location()
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True, timeout=remaining(5))
            db.row_factory = sqlite3.Row
            try:
                budget(db)
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if not self._owned(db):
                    raise IngestionJobError("ingestion_job_ledger_not_owned")
                yield db
                if write:
                    budget(db)
                    db.commit()
            except BaseException:
                if write:
                    db.rollback()
                raise
            finally:
                db.close()

    def upgrade_owned_schema(self):
        """Called only after the worker owns its OS lock; never from a read API."""
        with self._db(write=True) as db:
            version = db.execute("SELECT value FROM ingestion_job_meta WHERE key='version'").fetchone()[0]
            if version == "2":
                return
            rows = [(row[0], checked_kind(None, job_metadata(json.loads(row[1]))))
                    for row in db.execute("SELECT task_id,metadata FROM ingestion_jobs")]
            db.execute("ALTER TABLE ingestion_jobs ADD COLUMN " + _KIND_COLUMN)
            db.executemany("UPDATE ingestion_jobs SET kind=? WHERE task_id=?", [(kind, identity) for identity, kind in rows])
            db.execute("UPDATE ingestion_job_meta SET value='2' WHERE key='version'")
            if not self._owned(db):
                raise IngestionJobError("ingestion_job_schema_upgrade_unconfirmed")

    @staticmethod
    def _view(row):
        if row is None:
            return None
        value = dict(row)
        value.pop("owner_token", None)
        value["metadata"] = job_metadata(json.loads(value["metadata"]))
        value["kind"] = record_kind(value)
        value["shutdown_timed_out"] = bool(value["shutdown_timed_out"])
        value["needs_review"] = (value["status"] in {"interrupted", "needs_review"}
                                 or value["status"] == "running" and value["shutdown_timed_out"])
        # No available status is an assertion about all external service fees.
        value["usage_state"] = "not_recorded"
        return value

    def get(self, task_id):
        task_identity(task_id)
        if not self.path.exists():
            if Path(str(self.path) + ".owner.lock").exists():
                raise IngestionJobError("ingestion_job_ledger_missing")
            return None
        with self._db() as db:
            return self._view(db.execute("SELECT * FROM ingestion_jobs WHERE task_id=?", (task_id,)).fetchone())

    @staticmethod
    def _kind_filter(db, kind):
        kind = _page_kind(kind)
        if kind is None:
            return "", ()
        version = db.execute("SELECT value FROM ingestion_job_meta WHERE key='version'").fetchone()[0]
        if version == "2":
            return " WHERE kind=?", (kind,)
        # Read-only v1 projection; filtering still precedes pagination/count.
        return (" WHERE (CASE WHEN json_type(metadata,'$.platform_item_ids')='array' "
                "THEN 'ingestion' ELSE 'generic' END)=?"), (kind,)

    def list(self, *, limit=50, offset=0, kind: TaskKind | None = None):
        _page_kind(kind)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 1000000:
            raise IngestionJobError("invalid_ingestion_job_page")
        if not self.path.exists():
            if Path(str(self.path) + ".owner.lock").exists():
                raise IngestionJobError("ingestion_job_ledger_missing")
            return []
        with self._db() as db:
            clause, values = self._kind_filter(db, kind)
            return [self._view(row) for row in db.execute(
                "SELECT * FROM ingestion_jobs" + clause + " ORDER BY created_at DESC,task_id DESC LIMIT ? OFFSET ?", (*values, limit, offset))]

    def count(self, *, kind: TaskKind | None = None):
        _page_kind(kind)
        if not self.path.exists():
            if Path(str(self.path) + ".owner.lock").exists():
                raise IngestionJobError("ingestion_job_ledger_missing")
            return 0
        with self._db() as db:
            clause, values = self._kind_filter(db, kind)
            return db.execute("SELECT COUNT(*) FROM ingestion_jobs" + clause, values).fetchone()[0]

    def _event(self, db, task_id, event, now):
        db.execute("""INSERT INTO ingestion_job_events(task_id,event,stage,status,progress,total,reason_code,created_at)
          SELECT task_id,?,stage,status,progress,total,reason_code,? FROM ingestion_jobs WHERE task_id=?""", (event, now, task_id))

    def register(self, task_id, owner_token, metadata=None, *, kind: TaskKind | None = None):
        task_identity(task_id)
        metadata = job_metadata(metadata)
        kind = checked_kind(kind, metadata)
        now = self._now()
        total = len(metadata.get("platform_item_ids", []))
        with self._db(write=True) as db:
            if db.execute("SELECT 1 FROM ingestion_jobs WHERE task_id=?", (task_id,)).fetchone():
                raise IngestionJobError("ingestion_task_exists")
            predecessor = metadata.get("recovery_of")
            if predecessor:
                old = self._view(db.execute("SELECT * FROM ingestion_jobs WHERE task_id=?", (predecessor,)).fetchone())
                if old is None or old['kind'] != kind or old['status'] not in {"failed", "interrupted", "needs_review"}:
                    raise IngestionJobError("ingestion_recovery_not_reviewable")
            db.execute("""INSERT INTO ingestion_jobs(task_id,owner_token,status,stage,progress,total,message,metadata,created_at,updated_at,kind)
              VALUES(?,?,'queued','queued',0,?,'等待处理...',?,?,?,?)""", (task_id, owner_token, total,
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), now, now, kind))
            self._event(db, task_id, "registered", now)
            return self._view(db.execute("SELECT * FROM ingestion_jobs WHERE task_id=?", (task_id,)).fetchone())

    def recover_previous(self, owner_token):
        now = self._now()
        with self._db(write=True) as db:
            rows = list(db.execute("SELECT task_id,status FROM ingestion_jobs WHERE status IN ('queued','running') AND owner_token<>?", (owner_token,)))
            for row in rows:
                running = row["status"] == "running"
                db.execute("""UPDATE ingestion_jobs SET status=?,message=?,reason_code=?,finished_at=?,updated_at=?,revision=revision+1
                  WHERE task_id=? AND status=?""", ("interrupted" if running else "needs_review",
                    "上次处理被中断，请先核对原资料与调用结果。" if running else "上次排队任务未自动继续，请核对后明确重新提交。",
                    "worker_restart_running" if running else "worker_restart_queued", now, now, row["task_id"], row["status"]))
                self._event(db, row["task_id"], "restart_requires_review", now)
        return len(rows)

    def publication_unconfirmed(self, task_id, owner_token):
        """Fence a possibly committed registration without touching an old owner."""
        now = self._now()
        with self._db(write=True) as db:
            updated = db.execute("""UPDATE ingestion_jobs SET status='needs_review',
              reason_code='queue_publication_unconfirmed',message='队列提交结果未确认，请核对后明确重新提交。',
              finished_at=?,updated_at=?,revision=revision+1 WHERE task_id=? AND owner_token=? AND status='queued'""",
                (now, now, task_id, owner_token))
            if updated.rowcount:
                self._event(db, task_id, "publication_unconfirmed", now)

    def start_job(self, task_id, owner_token):
        now = self._now()
        with self._db(write=True) as db:
            updated = db.execute("""UPDATE ingestion_jobs SET status='running',stage='starting',message='正在处理...',
              started_at=?,updated_at=?,revision=revision+1 WHERE task_id=? AND owner_token=? AND status='queued'""",
                (now, now, task_id, owner_token))
            if updated.rowcount != 1:
                raise IngestionJobError("ingestion_job_not_queued")
            self._event(db, task_id, "started", now)
            return self._view(db.execute("SELECT * FROM ingestion_jobs WHERE task_id=?", (task_id,)).fetchone())

    def progress(self, task_id, owner_token, progress, total, message="", *, stage=None, item_id=None):
        progress_values(progress, total, message, stage)
        now = self._now()
        with self._db(write=True) as db:
            row = db.execute("SELECT metadata FROM ingestion_jobs WHERE task_id=? AND owner_token=? AND status='running'",
                             (task_id, owner_token)).fetchone()
            if row is None:
                raise IngestionJobError("ingestion_job_not_running")
            metadata = job_metadata(json.loads(row[0]))
            items = metadata.get("platform_item_ids")
            if (items is not None and total != len(items)
                    or item_id is not None and (type(item_id) is not str or items is not None and item_id not in items)):
                raise IngestionJobError("ingestion_progress_scope_changed")
            updated = db.execute("""UPDATE ingestion_jobs SET progress=?,total=?,message=CASE WHEN ?='' THEN message ELSE ? END,
              stage=COALESCE(?,stage),current_item_id=COALESCE(?,current_item_id),updated_at=?,revision=revision+1
              WHERE task_id=? AND owner_token=? AND status='running'""",
                (progress, total, message, message, stage, item_id, now, task_id, owner_token))
            if updated.rowcount != 1:
                raise IngestionJobError("ingestion_job_not_running")
            self._event(db, task_id, "progress", now)
            return self._view(db.execute("SELECT * FROM ingestion_jobs WHERE task_id=?", (task_id,)).fetchone())

    def finish(self, task_id, owner_token, status, *, reason_code=None, message=None, deadline=None):
        if status not in {"done", "failed", "interrupted", "needs_review"}:
            raise IngestionJobError("invalid_ingestion_job_status")
        now = self._now()
        messages = {"done": "处理完成", "failed": "处理失败，请核对已保存资料与任务记录。",
                    "interrupted": "处理已中断，请核对原资料与调用结果。", "needs_review": "任务未继续执行，请核对后明确重新提交。"}
        with self._db(write=True, deadline=deadline) as db:
            updated = db.execute("""UPDATE ingestion_jobs SET status=?,stage=CASE WHEN ?='done' THEN 'finished' ELSE stage END,
              progress=CASE WHEN ?='done' THEN total ELSE progress END,message=?,reason_code=?,finished_at=?,updated_at=?,revision=revision+1
              WHERE task_id=? AND owner_token=? AND status IN ('queued','running')""",
                (status, status, status, message or messages[status], reason_code, now, now, task_id, owner_token))
            if updated.rowcount != 1:
                raise IngestionJobError("ingestion_job_already_terminal")
            self._event(db, task_id, "finished", now)
            return self._view(db.execute("SELECT * FROM ingestion_jobs WHERE task_id=?", (task_id,)).fetchone())

    def shutdown(self, task_id, owner_token, *, timed_out=False, deadline=None):
        now = self._now()
        with self._db(write=True, deadline=deadline) as db:
            updated = db.execute("""UPDATE ingestion_jobs SET shutdown_requested_at=COALESCE(shutdown_requested_at,?),
              shutdown_timed_out=MAX(shutdown_timed_out,?),updated_at=?,revision=revision+1
              WHERE task_id=? AND owner_token=? AND status='running'""", (now, int(timed_out), now, task_id, owner_token))
            if updated.rowcount:
                self._event(db, task_id, "shutdown_timeout" if timed_out else "shutdown_requested", now)


class IngestionOwnerLock:
    """A process-lifetime OS file lock, independent of short SQLite locks."""
    def __init__(self, database):
        self.path = Path(str(database) + ".owner.lock")
        self.fd = None
        self.key = os.path.normcase(str(self.path))

    def acquire(self):
        with _OWNERS_LOCK:
            if self.key in _OWNERS:
                raise IngestionJobError("ingestion_worker_owner_busy")
            _OWNERS.add(self.key)
        fd = None
        try:
            if self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent:
                raise IngestionJobError("unsafe_ingestion_worker_owner")
            flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            created = False
            try:
                fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                info = self.path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise IngestionJobError("unsafe_ingestion_worker_owner")
                fd = os.open(self.path, flags)
                opened = os.fstat(fd)
                if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
                    raise IngestionJobError("unsafe_ingestion_worker_owner")
            if created:
                remaining = _OWNER_MAGIC
                while remaining:
                    written = os.write(fd, remaining)
                    if not written:
                        raise IngestionJobError("ingestion_worker_owner_initialization_failed")
                    remaining = remaining[written:]
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            if os.read(fd, len(_OWNER_MAGIC) + 1) != _OWNER_MAGIC:
                raise IngestionJobError("unsafe_ingestion_worker_owner")
            # Lock beyond the immutable header: Windows byte locks must not
            # prevent another process from validating that header before refusal.
            os.lseek(fd, 4096, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = fd
        except BaseException as error:
            if fd is not None:
                os.close(fd)
            with _OWNERS_LOCK:
                _OWNERS.discard(self.key)
            if isinstance(error, OSError):
                raise IngestionJobError("ingestion_worker_owner_busy") from None
            raise

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.lseek(fd, 4096, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            with _OWNERS_LOCK:
                _OWNERS.discard(self.key)
