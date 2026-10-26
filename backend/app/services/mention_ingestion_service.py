"""Single-backend-owner bridge into the existing serial ingestion worker.

Receipts survive restarts; queued/running work from a different service owner
is reconciled, never automatically redispatched. This does not turn the legacy
worker/profile thread locks into cross-process locks: run one backend owner.
The agent process must use this API and must never start an ingestion worker.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import uuid

from sqlalchemy import select

from app.mention_agent.contracts import Binding
from app.models.entities import KnowledgeBase, KnowledgeBaseVideo, VideoCache
from app.storage_guard import (guarded_sqlite_file, StorageGuardError,
    verified_readonly_connection, SqliteOwnershipError, assert_no_orphan_sqlite_files)


COMPONENT = "DouyinMind.mention_ingestion.receipts.v1"
ACTIVE_STATES = ("reserved", "prepared", "queued", "processing", "unknown")
PROCESSING_STATUSES = frozenset({"downloading", "transcribing", "embedding"})
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "storage" / "mention_agent"


class MentionIngestionError(ValueError):
    def __init__(self, code: str, status_code: int = 409):
        super().__init__(code)
        self.code, self.status_code = code, status_code


def _validate(payload):
    keys = {"request_id", "binding_id", "instance_id", "video_id", "knowledge_base_id", "title"}
    if not isinstance(payload, dict) or set(payload) - keys or keys - {"title"} - set(payload):
        raise MentionIngestionError("invalid_request", 422)
    value = dict(payload)
    for key in ("request_id", "binding_id", "instance_id"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value[key]):
            raise MentionIngestionError("invalid_request", 422)
    if not isinstance(value["video_id"], str) or not re.fullmatch(r"\d{5,30}", value["video_id"]):
        raise MentionIngestionError("invalid_video_id", 422)
    if type(value["knowledge_base_id"]) is not int or value["knowledge_base_id"] <= 0:
        raise MentionIngestionError("invalid_library", 422)
    title = value.get("title")
    if title is None:
        title = "抖音视频" + value["video_id"]
    if not isinstance(title, str) or not title.strip() or len(title) > 512 or "\x00" in title:
        raise MentionIngestionError("invalid_title", 422)
    value["title"] = title.strip()
    return value


class MentionIngestionService:
    """Dependencies are injected; construction opens no DB and starts no thread."""

    def __init__(self, *, sessions, worker, pipeline, index_reader,
                 binding_path=DEFAULT_ROOT / "local-binding.json",
                 receipt_path=DEFAULT_ROOT / "backend-ingestion.sqlite3", protected_paths=(),
                 binding_resolver=None, run_reader=None):
        self.sessions, self.worker, self.pipeline, self.index_reader = sessions, worker, pipeline, index_reader
        self.binding_path = Path(binding_path)
        self.receipt_path = Path(os.path.abspath(os.fspath(receipt_path)))
        self.protected_paths = tuple(Path(path).resolve() for path in (*protected_paths, self.binding_path))
        self.owner = uuid.uuid4().hex
        self._lock = threading.RLock()
        if binding_resolver is not None and not callable(binding_resolver):
            raise ValueError("invalid_binding_resolver")
        self.binding_resolver = binding_resolver
        if run_reader is not None and not callable(run_reader):
            raise ValueError("invalid_run_reader")
        self.run_reader = run_reader

    def _video_processing(self, payload):
        """Read this request's one existing telemetry run; never borrow a latest run."""
        if self.run_reader is None:
            return None
        try:
            runs = self.run_reader(payload["video_id"])
            if not isinstance(runs, list):
                return None
            matches = [run for run in runs if isinstance(run, dict)
                       and run.get("video_id") == payload["video_id"]
                       and run.get("task_id") == payload["request_id"]]
            if len(matches) != 1:
                return None
            run = matches[0]
            if (not isinstance(run.get("run_id"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run["run_id"])
                    or not isinstance(run.get("stages"), dict)):
                return None
            stages = {}
            for name in ("download", "recognition", "indexing"):
                stage = run["stages"].get(name)
                value = stage.get("seconds") if isinstance(stage, dict) else None
                try:
                    valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
                except OverflowError:
                    valid = False
                if valid:
                    stages[name] = value
            if not stages:
                return None
            return {"run_id": run["run_id"], "request_id": payload["request_id"],
                    "video_id": payload["video_id"], "stages_seconds": stages}
        except Exception:
            # Missing/partial telemetry never changes the import's actual state.
            return None

    def _binding(self):
        try:
            value = Binding.from_dict(json.loads(self.binding_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, ValueError):
            raise MentionIngestionError("binding_unavailable", 403) from None
        if not value.active:
            raise MentionIngestionError("binding_inactive", 403)
        return value

    def _binding_for(self, payload):
        if self.binding_resolver is None:
            return self._binding()
        try:
            binding = self.binding_resolver(payload)
        except MentionIngestionError:
            raise
        except Exception:
            raise MentionIngestionError("binding_unavailable", 403) from None
        if not isinstance(binding, Binding) or not binding.active:
            raise MentionIngestionError("binding_inactive_or_unavailable", 403)
        return binding

    @staticmethod
    def _check_scope(payload, binding):
        if any(payload[key] != getattr(binding, key) for key in
               ("binding_id", "instance_id", "knowledge_base_id")):
            raise MentionIngestionError("binding_scope_mismatch", 403)

    def _running(self):
        return getattr(self.worker, "_running", False) is True

    def _guard_path(self):
        path = self.receipt_path
        if path.resolve() != path or path.parent.resolve() != path.parent:
            raise MentionIngestionError("redirected_receipt_path")
        for protected in self.protected_paths:
            if path == protected or path.is_relative_to(protected) or protected.is_relative_to(path):
                raise MentionIngestionError("protected_receipt_path")

    def _guard(self):
        self._guard_path()
        path = self.receipt_path
        for candidate in (path, *(Path(str(path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or candidate.resolve() != candidate:
                raise MentionIngestionError("unsafe_receipt_file")

    @contextmanager
    def _ledger(self, *, create=False):
        self._guard_path()
        if not self.receipt_path.parent.exists() and not create:
            raise MentionIngestionError("request_not_found", 404)
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with guarded_sqlite_file(self.receipt_path, timeout=10):
                with self._ledger_locked(create=create) as db:
                    yield db
        except StorageGuardError as error:
            raise MentionIngestionError(str(error)) from None

    @contextmanager
    def _ledger_locked(self, *, create=False):
        self._guard()
        exists = self.receipt_path.exists()
        created_identity = None
        if not exists and not create:
            raise MentionIngestionError("request_not_found", 404)
        if exists:
            if self.receipt_path.stat().st_size == 0:
                raise MentionIngestionError('foreign_receipt_database')
            try:
                def owned(check):
                    row = check.execute("SELECT component FROM receipt_metadata").fetchone()
                    return row is not None and row[0] == COMPONENT
                with verified_readonly_connection(self.receipt_path, owned, guard=self._guard):
                    pass
            except SqliteOwnershipError:
                raise MentionIngestionError("foreign_receipt_database") from None
        else:
            assert_no_orphan_sqlite_files(self.receipt_path)
            try:
                descriptor = os.open(self.receipt_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError:
                raise MentionIngestionError('receipt_creation_conflict') from None
            created = os.fstat(descriptor)
            os.close(descriptor)
            created_identity = (created.st_dev,created.st_ino)
            self._guard()
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        before = self.receipt_path.stat()
        identity = (before.st_dev,before.st_ino)
        db = sqlite3.connect(self.receipt_path.as_uri()+'?mode=rw', uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            self._guard()
            current = self.receipt_path.stat()
            if (current.st_dev,current.st_ino) != identity:
                raise MentionIngestionError('receipt_identity_changed')
            if created_identity is not None:
                if (current.st_dev,current.st_ino) != created_identity or current.st_size != 0:
                    raise MentionIngestionError('receipt_creation_conflict')
            elif not owned(db):
                raise MentionIngestionError('foreign_receipt_database')
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS receipt_metadata(component TEXT PRIMARY KEY)")
            db.execute("INSERT OR IGNORE INTO receipt_metadata VALUES(?)", (COMPONENT,))
            db.execute("""CREATE TABLE IF NOT EXISTS import_receipts(
                request_id TEXT PRIMARY KEY,video_id TEXT NOT NULL,payload TEXT NOT NULL,
                state TEXT NOT NULL,owner TEXT NOT NULL)""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_video ON import_receipts(video_id)
                WHERE state IN ('reserved','prepared','queued','processing','unknown')""")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _read(self, request_id):
        with self._ledger() as db:
            row = db.execute("SELECT * FROM import_receipts WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise MentionIngestionError("request_not_found", 404)
            return dict(row)

    def _state(self, request_id, state, *, expected=None):
        with self._ledger() as db:
            query = "UPDATE import_receipts SET state=? WHERE request_id=?"
            args = [state, request_id]
            if expected:
                query += " AND state IN (" + ",".join("?" for _ in expected) + ")"
                args.extend(expected)
            db.execute(query, args)

    def _facts(self, payload):
        with self.sessions() as db:
            cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == payload["video_id"]))
            membership = (db.get(KnowledgeBase, payload["knowledge_base_id"]) is not None
                and db.get(KnowledgeBaseVideo, (payload["knowledge_base_id"], payload["video_id"])) is not None)
            status = cache.status if cache else "missing"
        count, readable = 0, True
        if status == "done":
            try:
                documents = self.index_reader(payload["video_id"])
                count = sum(1 for item in documents if isinstance(item, dict)
                    and item.get("platform_item_id") == payload["video_id"]
                    and isinstance(item.get("text"), str) and item["text"].strip()
                    and item.get("chunk_index") not in (0, "0")
                    and not (isinstance(item.get("chunk_id"), str) and item["chunk_id"].endswith(":0"))
                    and not item["text"].lstrip().startswith(("视频标题：", "视频标题:")))
            except Exception:
                readable = False
        return membership, status, count, readable

    def _result(self, row):
        payload = json.loads(row["payload"])
        membership, cache_status, count, readable = self._facts(payload)
        if membership and cache_status == "done" and readable and count >= 1:
            state = "succeeded"
        elif row["state"] == "failed":
            state = "failed"
        elif row["state"] == "unknown" or not readable or row["owner"] != self.owner:
            state = "unknown"
        elif row["state"] == "processing":
            progress = self.worker.get_progress(payload["request_id"]) if self._running() else None
            state = "processing" if progress and progress.get("status") == "running" else "unknown"
        elif row["state"] in {"prepared", "queued", "reserved"}:
            progress = self.worker.get_progress(payload["request_id"]) if self._running() else None
            state = ("processing" if progress and progress.get("status") == "running" else
                     "accepted" if progress and progress.get("status") == "queued" else "unknown")
        else:
            state = "unknown"
        if state in {"succeeded", "unknown"} and row["state"] != state:
            self._state(payload["request_id"], state)
        return {key: payload[key] for key in
                ("request_id", "binding_id", "instance_id", "video_id", "knowledge_base_id")} | {
            "state": state, "membership_exists": membership, "cache_status": cache_status,
            "indexed_content_chunks": count}

    def submit(self, payload):
        payload = _validate(payload)
        # Nothing is created or changed when the original backend has no live worker.
        if not self._running():
            raise MentionIngestionError("backend_worker_not_running", 503)
        binding = self._binding_for(payload)
        self._check_scope(payload, binding)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock:
            with self._ledger(create=True) as ledger:
                old = ledger.execute("SELECT * FROM import_receipts WHERE request_id=?", (payload["request_id"],)).fetchone()
                if old:
                    if old["payload"] != encoded:
                        raise MentionIngestionError("request_id_conflict")
                    existing = dict(old)
                else:
                    if ledger.execute("SELECT 1 FROM import_receipts WHERE video_id=? AND state IN "
                            "('reserved','prepared','queued','processing','unknown')", (payload["video_id"],)).fetchone():
                        raise MentionIngestionError("video_import_inflight")
                    ledger.execute("INSERT INTO import_receipts VALUES(?,?,?,'reserved',?)",
                        (payload["request_id"], payload["video_id"], encoded, self.owner))
                    existing = None
            if existing:
                return self._result(existing)
            try:
                with self.sessions() as db:
                    if db.get(KnowledgeBase, payload["knowledge_base_id"]) is None:
                        raise MentionIngestionError("library_not_found", 404)
                    cache = db.scalar(select(VideoCache).where(VideoCache.platform_item_id == payload["video_id"]))
                    if cache is None:
                        cache = VideoCache(platform_item_id=payload["video_id"], title=payload["title"], status="pending")
                        db.add(cache)
                        db.flush()
                    status = cache.status
                    if db.get(KnowledgeBaseVideo, (payload["knowledge_base_id"], payload["video_id"])) is None:
                        db.add(KnowledgeBaseVideo(knowledge_base_id=payload["knowledge_base_id"],
                                                 platform_item_id=payload["video_id"]))
                    db.commit()
            except MentionIngestionError:
                self._state(payload["request_id"], "failed")
                raise
            except Exception:
                self._state(payload["request_id"], "unknown")
                return self._result(self._read(payload["request_id"]))
            if status == "done":
                self._state(payload["request_id"], "unknown")
            elif status in PROCESSING_STATUSES or not self._running():
                # A legacy job may own it, or a previous process may have died.
                # Neither case authorizes us to reset/re-dispatch it.
                self._state(payload["request_id"], "unknown")
            elif status in {"pending", "failed"}:
                self._state(payload["request_id"], "prepared")
                try:
                    self.worker.submit(payload["request_id"], self._run_receipt, payload["request_id"])
                    self._state(payload["request_id"], "queued", expected=("prepared",))
                except Exception:
                    self._state(payload["request_id"], "unknown", expected=ACTIVE_STATES)
            else:
                self._state(payload["request_id"], "unknown")
            return self._result(self._read(payload["request_id"]))

    def get(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
            raise MentionIngestionError("invalid_request_id", 422)
        with self._lock:
            row = self._read(request_id)
            payload = json.loads(row["payload"])
            self._check_scope(payload, self._binding_for(payload))
            result = self._result(row)
            processing = self._video_processing(payload)
            if processing is not None:
                result["video_processing"] = processing
            return result

    def _run_receipt(self, request_id, *, task_id=""):
        with self._lock:
            row = self._read(request_id)
            if (row["owner"] != self.owner or row["state"] not in {"prepared", "queued"}
                    or task_id != request_id or not self._running()):
                return
            payload = json.loads(row["payload"])
            try:
                self._check_scope(payload, self._binding_for(payload))
            except MentionIngestionError:
                self._state(request_id, "failed")
                return
            self._state(request_id, "processing", expected=("prepared", "queued"))
        failed = False
        try:
            self.pipeline([payload["video_id"]], task_id=request_id)
        except Exception:
            failed = True
        with self._lock:
            membership, status, count, readable = self._facts(payload)
            state = ("succeeded" if membership and status == "done" and readable and count >= 1 else
                     "failed" if failed and status == "failed" else "unknown")
            self._state(request_id, state)


def build_default_service():
    """Called lazily by the route, never on import; never starts the worker."""
    from app.core.config import settings
    from app.db.session import engine, session_factory
    from app.services.chroma_service import get_chroma_service
    from app.services.knowledge_service import knowledge_service
    from app.services.worker import worker
    from app.services.ingestion_metrics import list_ingestion_runs
    backend = Path(__file__).resolve().parents[2]
    registry_path = backend / "app/storage/assistant/registry.sqlite3"
    registry_required_path = registry_path.parent / "registry-required.json"
    registry = None
    def authorize(payload):
        nonlocal registry
        # Before explicit migration the established single-binding route keeps
        # its old contract. Once a registry exists, revoked legacy permission
        # must never fall back to the old file and silently become active again.
        if (registry is None and not registry_path.exists() and not registry_path.is_symlink()
                and not registry_required_path.exists() and not registry_required_path.is_symlink()):
            if registry_path.parent.is_dir():
                assert_no_orphan_sqlite_files(registry_path)
            return service._binding()
        if registry is None:
            from app.assistant.registry import Registry
            registry = Registry(registry_path, database_path=Path(engine.url.database),
                protected_paths=(DEFAULT_ROOT, backend / settings.chroma_persist_dir,
                                 backend / settings.playwright_user_data_dir),allow_create=False)
        return registry.authorize_binding(payload["binding_id"], payload["instance_id"],
                                          payload["knowledge_base_id"], action="import", require_local=True)
    service = MentionIngestionService(sessions=session_factory, worker=worker,
        pipeline=knowledge_service._run_sync,
        index_reader=lambda video_id: get_chroma_service().get_documents({video_id}),
        protected_paths=(Path(engine.url.database), DEFAULT_ROOT / "agent.sqlite3",
            backend / settings.chroma_persist_dir, backend / settings.playwright_user_data_dir),
        binding_resolver=authorize, run_reader=list_ingestion_runs)
    return service
