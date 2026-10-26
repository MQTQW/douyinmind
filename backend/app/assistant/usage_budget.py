"""Local answer-model token accounting and conservative admission estimates.

This is an accounting gate, not an authorization grant or a provider hard cap.
The caller must reserve before sending a model request and use a durable unique
request key. An estimate can be exceeded by real provider usage. Unknown or
interrupted requests keep their reservation; there is deliberately no automatic
release operation. Embeddings, ASR and currency prices are outside this ledger.

Only ``prepare`` creates this component. Normal construction is strict/read-only
with respect to schema and never turns a missing ledger into a fresh allowance.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from app.storage_guard import (
    StorageGuardError,
    assert_no_orphan_sqlite_files,
    guarded_sqlite_file,
    verified_readonly_connection,
)


COMPONENT = "douyinmind.assistant.usage-budget"
VERSION = 1
DATABASE_NAME = "usage-budget.sqlite3"
MARKER_NAME = "usage-budget-required.json"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "storage" / "assistant" / DATABASE_NAME
MARKER_BYTES = (json.dumps({"component": COMPONENT, "version": VERSION},
                          separators=(",", ":")) + "\n").encode("ascii")
BEIJING = timezone(timedelta(hours=8))
MAX_TOKENS = 1_000_000_000_000
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:\-]{0,239}\Z")
_REASONS = frozenset({"request_interrupted", "request_timeout", "provider_error",
                      "usage_missing", "usage_invalid", "historical_usage_missing"})
_OUTCOMES = frozenset({"completed", "failed", "unknown"})

_SCHEMA = {
    "usage_budget_metadata": """CREATE TABLE usage_budget_metadata (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    "usage_budget_policy": """CREATE TABLE usage_budget_policy (
        id INTEGER PRIMARY KEY CHECK(id=1), per_task_tokens INTEGER,
        daily_tokens INTEGER, revision INTEGER NOT NULL,
        CHECK(per_task_tokens IS NULL OR per_task_tokens>=0),
        CHECK(daily_tokens IS NULL OR daily_tokens>=0), CHECK(revision>=1))""",
    "usage_budget_requests": """CREATE TABLE usage_budget_requests (
        request_key TEXT PRIMARY KEY, task_id TEXT NOT NULL,
        admitted_at_utc TEXT NOT NULL, day_key TEXT NOT NULL,
        origin TEXT NOT NULL CHECK(origin IN ('live','historical')),
        estimated_tokens INTEGER,
        state TEXT NOT NULL CHECK(state IN ('reserved','unknown','settled')),
        total_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER,
        outcome TEXT NOT NULL CHECK(outcome IN ('completed','failed','unknown')),
        reason_code TEXT, updated_at_utc TEXT NOT NULL,
        CHECK(estimated_tokens IS NULL OR estimated_tokens>0),
        CHECK(total_tokens IS NULL OR total_tokens>=0),
        CHECK(input_tokens IS NULL OR input_tokens>=0),
        CHECK(output_tokens IS NULL OR output_tokens>=0),
        CHECK((state='settled' AND total_tokens IS NOT NULL)
           OR (state!='settled' AND total_tokens IS NULL)),
        CHECK(state!='reserved' OR (origin='live' AND estimated_tokens IS NOT NULL)))""",
    "usage_budget_events": """CREATE TABLE usage_budget_events (
        id INTEGER PRIMARY KEY, request_key TEXT NOT NULL,
        event TEXT NOT NULL, occurred_at_utc TEXT NOT NULL, detail TEXT NOT NULL)""",
}


class UsageBudgetError(ValueError):
    """Fixed diagnostic code, never a prompt, provider error or secret."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class UsageBudgetExceeded(UsageBudgetError):
    """No new reservation/request has been admitted."""


def _integer(value, *, nullable=False, positive=False):
    if nullable and value is None:
        return None
    if type(value) is not int or value < (1 if positive else 0) or value > MAX_TOKENS:
        raise UsageBudgetError("invalid_token_amount")
    return value


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise UsageBudgetError("invalid_usage_identity")
    return value


def _instant(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise UsageBudgetError("invalid_usage_timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise UsageBudgetError("invalid_usage_timestamp")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _instant(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _provider_usage(usage):
    """Accept only explicit numeric provider totals; missing is not zero."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise UsageBudgetError("invalid_provider_usage")
    total = usage.get("total_tokens")
    if total is None:
        return None
    try:
        total = _integer(total)
        incoming = usage.get("input_tokens", usage.get("prompt_tokens"))
        outgoing = usage.get("output_tokens", usage.get("completion_tokens"))
        incoming = _integer(incoming, nullable=True)
        outgoing = _integer(outgoing, nullable=True)
    except UsageBudgetError:
        raise UsageBudgetError("invalid_provider_usage") from None
    return {"total_tokens": total, "input_tokens": incoming, "output_tokens": outgoing}


class UsageBudget:
    """One local ledger, with admission-day daily limits in UTC+08:00.

    Limits default to None. Existing model round/call limits still apply in the
    caller. Limits are administrator configuration, never comment/model inputs.
    A reservation is an estimate, not proof that a provider cannot exceed it.
    """
    def __init__(self, path=DEFAULT_PATH, *, clock=None):
        self.path = Path(path)
        self.marker_path = self.path.parent / MARKER_NAME
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._location()
        if not self.path.parent.is_dir():
            raise UsageBudgetError("usage_budget_missing")
        with self._db():
            pass

    @classmethod
    def prepare(cls, path=DEFAULT_PATH, *, clock=None):
        """Explicit initialization; existing empty/foreign/lost ledgers fail closed."""
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance.marker_path = instance.path.parent / MARKER_NAME
        instance._clock = clock or (lambda: datetime.now(timezone.utc))
        instance._location()
        instance.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with guarded_sqlite_file(instance.path):
                instance._guard(require_marker=False)
                try:
                    info = instance.path.lstat()
                except FileNotFoundError:
                    info = None
                if info is not None:
                    # A pre-existing zero-byte file is not evidence of ownership.
                    if info.st_size == 0:
                        raise UsageBudgetError("foreign_usage_budget")
                    with instance._db():
                        pass
                    return instance
                if instance.marker_path.exists() or instance.marker_path.is_symlink():
                    raise UsageBudgetError("usage_budget_missing")
                assert_no_orphan_sqlite_files(instance.path)
                descriptor = os.open(instance.path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                try:
                    created = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
                instance._guard(require_marker=False)
                if instance._identity(instance.path.lstat()) != instance._identity(created):
                    raise UsageBudgetError("usage_budget_identity_changed")
                db = sqlite3.connect(instance.path.as_uri() + "?mode=rw", uri=True)
                try:
                    instance._guard(require_marker=False)
                    if instance._identity(instance.path.lstat()) != instance._identity(created):
                        raise UsageBudgetError("usage_budget_identity_changed")
                    if db.execute("SELECT name FROM sqlite_master").fetchone() is not None:
                        raise UsageBudgetError("foreign_usage_budget")
                    db.execute("BEGIN IMMEDIATE")
                    for statement in _SCHEMA.values():
                        db.execute(statement)
                    db.executemany("INSERT INTO usage_budget_metadata VALUES (?,?)",
                        [("component", COMPONENT), ("version", str(VERSION)), ("requires_marker", "1")])
                    db.execute("INSERT INTO usage_budget_policy VALUES (1,NULL,NULL,1)")
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
                instance._write_marker()
                with instance._db():
                    pass
                return instance
        except StorageGuardError:
            raise UsageBudgetError("unsafe_usage_budget_storage") from None

    def _location(self):
        if (not self.path.is_absolute() or self.path.name != DATABASE_NAME
                or self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent):
            raise UsageBudgetError("unsafe_usage_budget_path")

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def _marker(self, *, required):
        try:
            info = self.marker_path.lstat()
        except FileNotFoundError:
            if required:
                raise UsageBudgetError("usage_budget_marker_missing") from None
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or self.marker_path.resolve() != self.marker_path or info.st_size != len(MARKER_BYTES)):
            raise UsageBudgetError("unsafe_usage_budget_marker")
        try:
            with self.marker_path.open("rb") as source:
                opened = os.fstat(source.fileno())
                if self._identity(opened) != self._identity(info) or opened.st_nlink != 1:
                    raise UsageBudgetError("unsafe_usage_budget_marker")
                if source.read(len(MARKER_BYTES) + 1) != MARKER_BYTES:
                    raise UsageBudgetError("invalid_usage_budget_marker")
        except OSError:
            raise UsageBudgetError("unsafe_usage_budget_marker") from None
        return True

    def _write_marker(self):
        if self._marker(required=False):
            return
        try:
            descriptor = os.open(self.marker_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0), 0o600)
            try:
                opened = os.fstat(descriptor)
                current = self.marker_path.lstat()
                if (opened.st_nlink != 1 or self._identity(opened) != self._identity(current)
                        or not stat.S_ISREG(current.st_mode)):
                    raise UsageBudgetError("unsafe_usage_budget_marker")
                remaining = MARKER_BYTES
                while remaining:
                    count = os.write(descriptor, remaining)
                    if count <= 0:
                        raise UsageBudgetError("usage_budget_marker_write_failed")
                    remaining = remaining[count:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise UsageBudgetError("usage_budget_marker_write_failed") from None
        self._marker(required=True)

    def _guard(self, *, require_marker=True):
        self._location()
        for suffix in ("", "-journal", "-wal", "-shm"):
            item = Path(str(self.path) + suffix)
            try:
                info = item.lstat()
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or item.resolve() != item):
                raise UsageBudgetError("unsafe_usage_budget_file")
        self._marker(required=require_marker)

    @staticmethod
    def _owned(db):
        expected = {"component": COMPONENT, "version": str(VERSION), "requires_marker": "1"}
        try:
            if dict(db.execute("SELECT key,value FROM usage_budget_metadata")) != expected:
                return False
            actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
            if set(actual) != set(_SCHEMA):
                return False
            if any(" ".join(actual[name].split()) != " ".join(sql.split())
                   for name, sql in _SCHEMA.items()):
                return False
            if db.execute("SELECT count(*) FROM usage_budget_policy").fetchone()[0] != 1:
                return False
            return True
        except sqlite3.Error:
            return False

    @contextmanager
    def _db(self, *, write=False):
        try:
            with guarded_sqlite_file(self.path):
                self._guard()
                if not self.path.exists():
                    assert_no_orphan_sqlite_files(self.path)
                    raise UsageBudgetError("usage_budget_missing")
                if self.path.stat().st_size == 0:
                    raise UsageBudgetError("foreign_usage_budget")
                identity = self._identity(self.path.lstat())
                with verified_readonly_connection(self.path, self._owned, guard=self._guard) as checked:
                    checked.row_factory = sqlite3.Row
                    checked.execute("PRAGMA query_only=ON")
                    if not write:
                        yield checked
                        return
                self._guard()
                if self._identity(self.path.lstat()) != identity:
                    raise UsageBudgetError("usage_budget_identity_changed")
                db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)
                try:
                    self._guard()
                    if self._identity(self.path.lstat()) != identity or not self._owned(db):
                        raise UsageBudgetError("foreign_usage_budget")
                    db.row_factory = sqlite3.Row
                    db.execute("BEGIN IMMEDIATE")
                    yield db
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
        except StorageGuardError:
            raise UsageBudgetError("unsafe_usage_budget_storage") from None
        except sqlite3.Error:
            raise UsageBudgetError("usage_budget_unavailable") from None

    def _now(self):
        return _instant(self._clock())

    @staticmethod
    def _policy(db):
        return dict(db.execute("SELECT per_task_tokens,daily_tokens,revision FROM usage_budget_policy WHERE id=1").fetchone())

    @staticmethod
    def _totals(db, where, parameters):
        row = db.execute("""SELECT count(*) AS request_count,
            coalesce(sum(CASE WHEN state='settled' THEN total_tokens ELSE 0 END),0) AS actual_tokens,
            coalesce(sum(CASE WHEN state!='settled' THEN estimated_tokens ELSE 0 END),0) AS held_estimated_tokens,
            coalesce(sum(CASE WHEN state!='settled' AND estimated_tokens IS NULL THEN 1 ELSE 0 END),0) AS unknown_unbounded_requests,
            coalesce(sum(CASE WHEN state!='settled' THEN 1 ELSE 0 END),0) AS unsettled_requests,
            coalesce(sum(CASE WHEN state='settled' AND estimated_tokens IS NOT NULL
                AND total_tokens>estimated_tokens THEN 1 ELSE 0 END),0) AS overrun_requests
            FROM usage_budget_requests WHERE """ + where, parameters).fetchone()
        value = dict(row)
        value["committed_tokens"] = value["actual_tokens"] + value["held_estimated_tokens"]
        value["needs_reconciliation"] = value["unsettled_requests"] > 0
        return value

    @staticmethod
    def _row(row, *, created=False):
        value = dict(row)
        value["created"] = created
        value["may_request"] = bool(created and value["state"] == "reserved" and value["origin"] == "live")
        value["overrun"] = (value["state"] == "settled" and value["estimated_tokens"] is not None
                            and value["total_tokens"] > value["estimated_tokens"])
        value["needs_reconciliation"] = value["state"] != "settled"
        return value

    @staticmethod
    def _event(db, request_key, event, now, detail):
        db.execute("INSERT INTO usage_budget_events(request_key,event,occurred_at_utc,detail) VALUES (?,?,?,?)",
                   (request_key, event, now, json.dumps(detail, separators=(",", ":"), sort_keys=True)))

    def set_limits(self, *, per_task_tokens=None, daily_tokens=None, expected_revision=None):
        """Trusted administrator only; optionally reject stale full-form updates.

        The product adapter requires the revision it originally displayed. None
        remains available only for trusted internal/backward-compatible callers.
        """
        task = _integer(per_task_tokens, nullable=True)
        day = _integer(daily_tokens, nullable=True)
        if (expected_revision is not None and (type(expected_revision) is not int
                or expected_revision < 1 or expected_revision > MAX_TOKENS)):
            raise UsageBudgetError("invalid_usage_revision")
        with self._db(write=True) as db:
            old = self._policy(db)
            if expected_revision is not None and old["revision"] != expected_revision:
                raise UsageBudgetError("usage_policy_changed")
            if old["per_task_tokens"] != task or old["daily_tokens"] != day:
                db.execute("UPDATE usage_budget_policy SET per_task_tokens=?,daily_tokens=?,revision=revision+1 WHERE id=1", (task, day))
                self._event(db, "policy", "limits_changed", _stamp(self._now()),
                            {"per_task_tokens": task, "daily_tokens": day, "revision": old["revision"] + 1})
            return self._policy(db)

    def reserve(self, task_id, request_key, estimated_tokens):
        """Atomically admit at most one request for this durable key.

        A duplicate key never allows another provider request. Every live request
        needs a positive admission estimate, even when token limits are unset.
        The estimate is charged until real usage is explicitly reconciled.
        """
        task_id, request_key = _key(task_id), _key(request_key)
        estimate = _integer(estimated_tokens, positive=True)
        now = self._now()
        stamp = _stamp(now)
        day = now.astimezone(BEIJING).date().isoformat()
        with self._db(write=True) as db:
            old = db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone()
            if old is not None:
                if old["task_id"] != task_id or old["origin"] != "live" or old["estimated_tokens"] != estimate:
                    raise UsageBudgetError("usage_request_conflict")
                return self._row(old)
            policy = self._policy(db)
            for scope, clause, value, cap in (
                ("task", "task_id=?", task_id, policy["per_task_tokens"]),
                ("daily", "day_key=?", day, policy["daily_tokens"]),
            ):
                if cap is None:
                    continue
                totals = self._totals(db, clause, (value,))
                if totals["unknown_unbounded_requests"]:
                    raise UsageBudgetExceeded(scope + "_usage_unknown")
                if totals["committed_tokens"] + estimate > cap:
                    raise UsageBudgetExceeded(scope + "_token_limit")
            db.execute("""INSERT INTO usage_budget_requests
                (request_key,task_id,admitted_at_utc,day_key,origin,estimated_tokens,state,
                 total_tokens,input_tokens,output_tokens,outcome,reason_code,updated_at_utc)
                VALUES (?,?,?,?,'live',?,'reserved',NULL,NULL,NULL,'unknown',NULL,?)""",
                (request_key, task_id, stamp, day, estimate, stamp))
            self._event(db, request_key, "reserved", stamp,
                        {"estimated_tokens": estimate, "policy_revision": policy["revision"]})
            return self._row(db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone(), created=True)

    def _settle(self, db, row, usage, outcome, now):
        if usage is None:
            if row["state"] != "settled":
                reason = "historical_usage_missing" if row["origin"] == "historical" else "usage_missing"
                if row["state"] != "unknown" or row["reason_code"] != reason:
                    db.execute("UPDATE usage_budget_requests SET state='unknown',reason_code=?,outcome=?,updated_at_utc=? WHERE request_key=?",
                               (reason, outcome, now, row["request_key"]))
                    self._event(db, row["request_key"], "usage_unknown", now, {"reason_code": reason})
        elif row["state"] == "settled":
            if any(row[key] != usage[key] for key in ("total_tokens", "input_tokens", "output_tokens")):
                raise UsageBudgetError("provider_usage_conflict")
        else:
            db.execute("""UPDATE usage_budget_requests SET state='settled',total_tokens=?,
                input_tokens=?,output_tokens=?,outcome=?,reason_code=NULL,updated_at_utc=? WHERE request_key=?""",
                (usage["total_tokens"], usage["input_tokens"], usage["output_tokens"], outcome, now, row["request_key"]))
            self._event(db, row["request_key"], "settled", now,
                        {**usage, "outcome": outcome,
                         "overrun": row["estimated_tokens"] is not None and usage["total_tokens"] > row["estimated_tokens"]})
        return self._row(db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (row["request_key"],)).fetchone())

    def settle(self, request_key, provider_usage, *, outcome="completed"):
        request_key = _key(request_key)
        if outcome not in _OUTCOMES:
            raise UsageBudgetError("invalid_usage_outcome")
        usage = _provider_usage(provider_usage)
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone()
            if row is None:
                raise UsageBudgetError("usage_request_missing")
            return self._settle(db, row, usage, outcome, _stamp(self._now()))

    def mark_unknown(self, request_key, *, reason="request_interrupted"):
        request_key = _key(request_key)
        if reason not in _REASONS:
            raise UsageBudgetError("invalid_usage_reason")
        now = _stamp(self._now())
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone()
            if row is None:
                raise UsageBudgetError("usage_request_missing")
            if row["state"] != "settled" and (row["state"] != "unknown" or row["reason_code"] != reason):
                db.execute("UPDATE usage_budget_requests SET state='unknown',reason_code=?,updated_at_utc=? WHERE request_key=?", (reason, now, request_key))
                self._event(db, request_key, "usage_unknown", now, {"reason_code": reason})
            return self._row(db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone())

    def import_historical_usage(self, task_id, request_key, provider_usage, occurred_at, *, outcome="unknown"):
        """Idempotently import one old response, never an already budgeted response.

        The integrator must retain new request IDs in response metadata and must
        not import those a second time under a historical key. Missing old usage
        has no invented estimate: it blocks configured relevant scopes until a
        genuine provider total is reconciled using the same key.
        """
        task_id, request_key = _key(task_id), _key(request_key)
        if outcome not in _OUTCOMES:
            raise UsageBudgetError("invalid_usage_outcome")
        usage = _provider_usage(provider_usage)
        happened = _instant(occurred_at)
        admitted = _stamp(happened)
        day = happened.astimezone(BEIJING).date().isoformat()
        now = _stamp(self._now())
        with self._db(write=True) as db:
            old = db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone()
            if old is not None:
                if old["task_id"] != task_id or old["origin"] != "historical" or old["admitted_at_utc"] != admitted:
                    raise UsageBudgetError("usage_request_conflict")
                return self._settle(db, old, usage, outcome, now)
            db.execute("""INSERT INTO usage_budget_requests
                (request_key,task_id,admitted_at_utc,day_key,origin,estimated_tokens,state,
                 total_tokens,input_tokens,output_tokens,outcome,reason_code,updated_at_utc)
                VALUES (?,?,?,?,'historical',NULL,'unknown',NULL,NULL,NULL,?,'historical_usage_missing',?)""",
                (request_key, task_id, admitted, day, outcome, now))
            self._event(db, request_key, "historical_import", now, {"usage_present": usage is not None})
            row = db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone()
            return self._settle(db, row, usage, outcome, now)

    def get_request(self, request_key):
        request_key = _key(request_key)
        with self._db() as db:
            row = db.execute("SELECT * FROM usage_budget_requests WHERE request_key=?", (request_key,)).fetchone()
            return None if row is None else self._row(row)

    def summary(self, task_id=None):
        if task_id is not None:
            task_id = _key(task_id)
        day = self._now().astimezone(BEIJING).date().isoformat()
        with self._db() as db:
            value = {"policy": self._policy(db), "day_key": day,
                     "day_timezone": "UTC+08:00", "daily_basis": "request_admission_day",
                     "daily": self._totals(db, "day_key=?", (day,)),
                     "all_time": self._totals(db, "1=1", ()),
                     "reservation_is_estimate": True, "currency_cost": None,
                     "scope": "answer_model_provider_tokens",
                     "excluded_services": ["embedding", "asr"],
                     "automatic_release_supported": False}
            if task_id is not None:
                value["task_id"] = task_id
                value["task"] = self._totals(db, "task_id=?", (task_id,))
            return value


__all__ = ["UsageBudget", "UsageBudgetError", "UsageBudgetExceeded", "DEFAULT_PATH"]
