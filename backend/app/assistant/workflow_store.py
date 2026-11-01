"""Owned durable workflows; planning is meaning, never execution authority.

Construction does no I/O. prepare/open are explicit, and no production entrypoint
imports this module. Token limits cover recorded model tokens, not unmeasured
ASR/embedding or currency charges. Current authorization/UI proof is injected
by the program through WorkflowEngine; JSON permission flags are not authority.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from app.storage_guard import (StorageGuardError, assert_no_orphan_sqlite_files,
    guarded_sqlite_file, verified_readonly_connection)
from app.mention_agent.contracts import public_profile

COMPONENT = "douyinmind.durable-workflow.v1"
TOOLS = frozenset({"import_current_video", "await_import", "query_current_video", "propose_reply"})
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z", re.ASCII)
HEX = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
SECRET = frozenset({"api_key", "cookie", "cookies", "password", "access_token",
    "refresh_token", "client_secret", "credentials", "storage_state",
    "reasoning_content", "raw_messages", "claim_token"})
SCOPE = frozenset({"user_id", "device_id", "instance_id", "sender_profile_url",
    "receiver_profile_url", "video_id", "root_reference_id", "reference_id",
    "knowledge_base_id", "query_scope"})
SOURCE = frozenset({"source_key", "origin_task_id", "created_at", "ui_digest", "effects_state"})
SHAPES = {
    "save": ("import_current_video", "await_import", "propose_reply"),
    "query": ("query_current_video", "propose_reply"),
    "save_then_query": ("import_current_video", "await_import", "query_current_video", "propose_reply"),
}
SCHEMA = {
    "workflow_meta": "CREATE TABLE workflow_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    "workflows": """CREATE TABLE workflows(
      workflow_id TEXT PRIMARY KEY,source_key TEXT UNIQUE NOT NULL,
      origin_task_id TEXT UNIQUE NOT NULL,immutable_hash TEXT NOT NULL,
      source TEXT NOT NULL,scope TEXT NOT NULL,plan TEXT NOT NULL,
      revision INTEGER NOT NULL,state TEXT NOT NULL,reason_code TEXT,
      budget_limit INTEGER NOT NULL,spent_tokens INTEGER NOT NULL,
      held_tokens INTEGER NOT NULL,publication TEXT,created_at REAL NOT NULL,
      updated_at REAL NOT NULL)""",
    "workflow_steps": """CREATE TABLE workflow_steps(
      workflow_id TEXT NOT NULL,ordinal INTEGER NOT NULL,spec TEXT NOT NULL,
      request_key TEXT UNIQUE NOT NULL,state TEXT NOT NULL,claim_token TEXT,
      lease_until REAL,started_at REAL,attempts INTEGER NOT NULL,
      estimated_tokens INTEGER NOT NULL,actual_tokens INTEGER,result TEXT,
      reason_code TEXT,next_poll_at REAL,updated_at REAL NOT NULL,
      PRIMARY KEY(workflow_id,ordinal))""",
    "workflow_events": """CREATE TABLE workflow_events(
      id INTEGER PRIMARY KEY,workflow_id TEXT NOT NULL,kind TEXT NOT NULL,
      facts TEXT NOT NULL,created_at REAL NOT NULL)""",
    "workflow_amendments": """CREATE TABLE workflow_amendments(
      source_key TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,immutable_hash TEXT NOT NULL,
      evidence TEXT NOT NULL,created_at REAL NOT NULL)""",
}


class WorkflowError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _key(value):
    if type(value) is not str or not KEY.fullmatch(value):
        raise WorkflowError("workflow_key_invalid")
    return value


def _integer(value, maximum=10**9):
    if type(value) is not int or not 0 <= value <= maximum:
        raise WorkflowError("workflow_integer_invalid")
    return value


def _timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise WorkflowError("workflow_timestamp_invalid")
    return float(value)


def _text(value, maximum=2000):
    if type(value) is not str or not value.strip() or "\x00" in value or len(value) > maximum:
        raise WorkflowError("workflow_text_invalid")
    return value


def _json(value):
    def check(item, depth=0):
        if depth > 12:
            raise WorkflowError("workflow_metadata_too_deep")
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str or key.casefold() in SECRET:
                    raise WorkflowError("workflow_metadata_forbidden")
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise WorkflowError("workflow_metadata_invalid")
    check(value)
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise WorkflowError("workflow_metadata_invalid") from None
    if len(result.encode("utf8")) > 131072:
        raise WorkflowError("workflow_metadata_too_large")
    return result


def digest(value):
    return hashlib.sha256(_json(value).encode("utf8")).hexdigest()


def validate_scope(value):
    if type(value) is not dict or set(value) != SCOPE:
        raise WorkflowError("workflow_scope_invalid")
    for key in ("user_id", "device_id", "instance_id", "root_reference_id", "reference_id"):
        _key(value[key])
    for key in ("sender_profile_url", "receiver_profile_url"):
        try:
            if public_profile(value[key]) != value[key]:
                raise ValueError()
        except (TypeError, ValueError):
            raise WorkflowError("workflow_scope_invalid") from None
    if value["sender_profile_url"] == value["receiver_profile_url"]:
        raise WorkflowError("workflow_scope_invalid")
    if type(value["video_id"]) is not str or not re.fullmatch(r"[0-9]{5,30}", value["video_id"]):
        raise WorkflowError("workflow_scope_invalid")
    library = value["knowledge_base_id"]
    if value["query_scope"] == "library":
        if type(library) is not int or library < 1:
            raise WorkflowError("workflow_scope_invalid")
    elif value["query_scope"] != "all_local" or library is not None:
        raise WorkflowError("workflow_scope_invalid")
    return deepcopy(value)


def validate_source(value):
    if type(value) is not dict or set(value) != SOURCE:
        raise WorkflowError("workflow_source_invalid")
    _key(value["source_key"])
    if type(value["origin_task_id"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", value["origin_task_id"]):
        raise WorkflowError("workflow_source_invalid")
    _timestamp(value["created_at"])
    if type(value["ui_digest"]) is not str or not HEX.fullmatch(value["ui_digest"]):
        raise WorkflowError("workflow_source_invalid")
    if value["effects_state"] != "none":
        raise WorkflowError("workflow_source_has_effects")
    return deepcopy(value)


def validate_amendment(value):
    if type(value) is not dict or set(value) != {"source_key", "created_at", "ui_digest"}:
        raise WorkflowError("workflow_amendment_invalid")
    _key(value["source_key"])
    _timestamp(value["created_at"])
    if type(value["ui_digest"]) is not str or not HEX.fullmatch(value["ui_digest"]):
        raise WorkflowError("workflow_amendment_invalid")
    return deepcopy(value)


def validate_plan(value, scope):
    """A model selects a supported goal, never owner/target/permission fields."""
    validate_scope(scope)
    if type(value) is not dict or set(value) != {"goal", "steps"}:
        raise WorkflowError("workflow_plan_invalid")
    goal, steps = value["goal"], value["steps"]
    if type(goal) is not str or goal not in SHAPES or type(steps) is not list or not 1 <= len(steps) <= 8:
        raise WorkflowError("workflow_plan_invalid")
    if tuple(item.get("tool") if type(item) is dict else None for item in steps) != SHAPES[goal]:
        raise WorkflowError("workflow_plan_order_invalid")
    if goal != "query" and scope["query_scope"] != "library":
        raise WorkflowError("workflow_save_library_required")
    for item in steps:
        if type(item) is not dict or set(item) != {"tool", "args"} or type(item["args"]) is not dict:
            raise WorkflowError("workflow_plan_invalid")
        tool, args = item["tool"], item["args"]
        if tool not in TOOLS:
            raise WorkflowError("workflow_tool_not_allowed")
        if tool in {"import_current_video", "await_import"}:
            if args:
                raise WorkflowError("workflow_plan_argument_invalid")
        elif tool == "query_current_video":
            if set(args) != {"question"}:
                raise WorkflowError("workflow_plan_argument_invalid")
            _text(args["question"])
        else:
            if set(args) != {"purpose"} or args["purpose"] != ("import_status" if goal == "save" else "answer"):
                raise WorkflowError("workflow_plan_argument_invalid")
    _json(value)
    return deepcopy(value)


@dataclass(frozen=True)
class WorkflowClaim:
    workflow_id: str
    ordinal: int
    token: str
    revision: int
    request_key: str
    seal: str


class WorkflowStore:
    def __init__(self, path, *, clock=None, protected_paths=()):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.marker = self.path.with_suffix(self.path.suffix + ".owner.json")
        self.clock = clock or time.time
        self.protected_paths = tuple(Path(os.path.abspath(os.fspath(p))) for p in protected_paths)
        self._owner = None
        self._seal_key = secrets.token_bytes(32)

    def _location(self):
        if self.path.resolve() != self.path or self.marker.resolve() != self.marker:
            raise WorkflowError("workflow_store_path_invalid")
        for protected in self.protected_paths:
            protected = protected.resolve()
            if any(p == protected or protected in p.parents or p in protected.parents
                   for p in (self.path, self.marker)):
                raise WorkflowError("workflow_store_path_invalid")

    @staticmethod
    def _regular(path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or path.resolve() != path or getattr(info, "st_file_attributes", 0) & 0x400):
            raise WorkflowError("workflow_store_path_invalid")

    def _read_marker(self):
        self._regular(self.marker)
        if self.marker.stat().st_size > 2048:
            raise WorkflowError("workflow_store_owner_unavailable")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        try:
            result = json.loads(self.marker.read_text(encoding="utf8"), object_pairs_hook=unique)
            if (type(result) is not dict or set(result) != {"component", "version", "owner"}
                    or result["component"] != COMPONENT or type(result["version"]) is not int
                    or result["version"] != 1 or type(result["owner"]) is not str
                    or not re.fullmatch(r"[a-f0-9]{32}", result["owner"])):
                raise ValueError()
            return result["owner"]
        except (ValueError, TypeError):
            raise WorkflowError("workflow_store_owner_unavailable") from None

    @classmethod
    def prepare(cls, path, *, activated_at=None, clock=None, protected_paths=()):
        value = cls(path, clock=clock, protected_paths=protected_paths)
        value._location()
        value.path.parent.mkdir(parents=True, exist_ok=True)
        activation = value._now() if activated_at is None else _timestamp(activated_at)
        try:
            with guarded_sqlite_file(value.path):
                if value.path.exists() or value.path.is_symlink():
                    value._owner = value._read_marker()
                    with value._connection():
                        pass
                    return value
                if value.marker.exists() or value.marker.is_symlink():
                    raise WorkflowError("workflow_store_database_missing")
                assert_no_orphan_sqlite_files(value.path)
                value._owner = secrets.token_hex(16)
                fd = os.open(value.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                made = os.fstat(fd)
                os.close(fd)
                value._regular(value.path)
                if (made.st_dev, made.st_ino) != (value.path.stat().st_dev, value.path.stat().st_ino):
                    raise WorkflowError("workflow_store_changed")
                db = sqlite3.connect(value.path.as_uri() + "?mode=rw", uri=True)
                try:
                    db.execute("PRAGMA synchronous=FULL")
                    for sql in SCHEMA.values():
                        db.execute(sql)
                    db.executemany("INSERT INTO workflow_meta VALUES(?,?)",
                        [("component", COMPONENT), ("version", "1"), ("owner", value._owner),
                         ("activated_at", repr(activation))])
                    db.commit()
                finally:
                    db.close()
                fd = os.open(value.marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "w", encoding="utf8") as stream:
                    stream.write(_json({"component": COMPONENT, "version": 1, "owner": value._owner}))
                    stream.flush()
                    os.fsync(stream.fileno())
        except (StorageGuardError, sqlite3.Error, OSError):
            raise WorkflowError("workflow_store_unavailable") from None
        with value._connection():
            pass
        return value

    @classmethod
    def open(cls, path, *, clock=None, protected_paths=()):
        value = cls(path, clock=clock, protected_paths=protected_paths)
        try:
            value._location()
            value._owner = value._read_marker()
            with value._connection():
                pass
        except (OSError, StorageGuardError, sqlite3.Error):
            raise WorkflowError("workflow_store_unavailable") from None
        return value

    def _guard(self):
        self._location()
        if self._owner is None:
            raise WorkflowError("workflow_store_not_open")
        self._regular(self.path)
        if self._read_marker() != self._owner:
            raise WorkflowError("workflow_store_owner_unavailable")
        for suffix in ("-journal", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.exists() or path.is_symlink():
                self._regular(path)

    def _owned(self, db):
        try:
            meta = dict(db.execute("SELECT key,value FROM workflow_meta"))
            if (set(meta) != {"component", "version", "owner", "activated_at"}
                    or meta["component"] != COMPONENT or meta["version"] != "1"
                    or meta["owner"] != self._owner):
                return False
            _timestamp(float(meta["activated_at"]))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type!='table' AND name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
                return False
            actual = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            normal = lambda sql: " ".join(sql.lower().split())
            return set(actual) == set(SCHEMA) and all(normal(actual[name]) == normal(sql) for name, sql in SCHEMA.items())
        except (sqlite3.Error, ValueError, TypeError):
            return False

    @contextmanager
    def _connection(self, *, write=False):
        if self._owner is None:
            raise WorkflowError("workflow_store_not_open")
        try:
            with guarded_sqlite_file(self.path):
                self._guard()
                before = self.path.stat()
                def same():
                    self._guard()
                    current = self.path.stat()
                    if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                        raise WorkflowError("workflow_store_changed")
                with verified_readonly_connection(self.path, self._owned, guard=self._guard) as ro:
                    same()
                    if not write:
                        ro.row_factory = sqlite3.Row
                        ro.execute("PRAGMA query_only=ON")
                        yield ro
                        same()
                        return
                same()
                db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
                db.row_factory = sqlite3.Row
                try:
                    same()
                    if not self._owned(db):
                        raise WorkflowError("workflow_store_owner_unavailable")
                    db.execute("PRAGMA synchronous=FULL")
                    db.execute("BEGIN IMMEDIATE")
                    yield db
                    same()
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
        except (StorageGuardError, sqlite3.Error, OSError):
            raise WorkflowError("workflow_store_unavailable") from None

    def _now(self):
        return _timestamp(self.clock())

    @staticmethod
    def _request_key(workflow_id, ordinal, spec):
        return "workflow-step-" + digest({"workflow": workflow_id, "ordinal": ordinal, "spec": spec})

    def _event(self, db, workflow_id, kind, facts):
        db.execute("INSERT INTO workflow_events(workflow_id,kind,facts,created_at) VALUES(?,?,?,?)",
            (workflow_id, kind, _json(facts), self._now()))

    def _snapshot(self, db, workflow_id):
        row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("source", "scope", "plan", "publication"):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        steps = []
        for step in db.execute("SELECT * FROM workflow_steps WHERE workflow_id=? ORDER BY ordinal", (workflow_id,)):
            item = {key: step[key] for key in step.keys() if key != "claim_token"}
            item["spec"] = json.loads(item["spec"])
            item["result"] = json.loads(item["result"]) if item["result"] is not None else None
            steps.append(item)
        value["steps"] = steps
        return value

    def get(self, workflow_id):
        with self._connection() as db:
            return self._snapshot(db, workflow_id)

    def events(self, workflow_id):
        with self._connection() as db:
            return [{**dict(row), "facts": json.loads(row["facts"])} for row in db.execute(
                "SELECT kind,facts,created_at FROM workflow_events WHERE workflow_id=? ORDER BY id", (_key(workflow_id),))]

    def create(self, *, source, scope, plan, budget_limit, initial_tokens=0):
        source, scope = validate_source(source), validate_scope(scope)
        plan = validate_plan(plan, scope)
        limit, spent = _integer(budget_limit), _integer(initial_tokens)
        if spent > limit:
            raise WorkflowError("workflow_budget_exceeded")
        immutable = digest({"source": source, "scope": scope, "initial_plan": plan,
            "budget_limit": limit, "initial_tokens": spent})
        workflow_id = "workflow-" + digest({"source_key": source["source_key"]})
        with self._connection(write=True) as db:
            activation = float(db.execute("SELECT value FROM workflow_meta WHERE key='activated_at'").fetchone()[0])
            if source["created_at"] < activation:
                raise WorkflowError("workflow_legacy_source_rejected")
            if source["created_at"] > self._now() + 5:
                raise WorkflowError("workflow_future_source_rejected")
            row = db.execute("SELECT workflow_id,immutable_hash FROM workflows WHERE source_key=? OR origin_task_id=?",
                (source["source_key"], source["origin_task_id"])).fetchone()
            if row is not None:
                if row["workflow_id"] != workflow_id or row["immutable_hash"] != immutable:
                    raise WorkflowError("workflow_source_conflict")
                return self._snapshot(db, workflow_id)
            now = self._now()
            db.execute("INSERT INTO workflows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (workflow_id, source["source_key"], source["origin_task_id"], immutable,
                 _json(source), _json(scope), _json(plan), 1, "pending", None, limit, spent, 0, None, now, now))
            for ordinal, spec in enumerate(plan["steps"]):
                db.execute("INSERT INTO workflow_steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (workflow_id, ordinal, _json(spec), self._request_key(workflow_id, ordinal, spec),
                     "pending", None, None, None, 0, 0, None, None, None, None, now))
            self._event(db, workflow_id, "created", {"plan": plan, "initial_tokens": spent})
            return self._snapshot(db, workflow_id)

    @staticmethod
    def _cas(row, expected):
        if row is None:
            raise WorkflowError("workflow_missing")
        if type(expected) is not int or row["revision"] != expected:
            raise WorkflowError("workflow_revision_changed")

    def _claim_seal(self, workflow_id, ordinal, token, revision, request_key):
        return hmac.new(self._seal_key, _json([workflow_id, ordinal, token, revision, request_key]).encode("utf8"),
            hashlib.sha256).hexdigest()

    def _check_claim(self, claim):
        if type(claim) is not WorkflowClaim or not hmac.compare_digest(claim.seal,
                self._claim_seal(claim.workflow_id, claim.ordinal, claim.token, claim.revision, claim.request_key)):
            raise WorkflowError("workflow_claim_invalid")

    def assert_claim_current(self, claim):
        self._check_claim(claim)
        with self._connection() as db:
            row = db.execute("SELECT revision,state FROM workflows WHERE workflow_id=?", (claim.workflow_id,)).fetchone()
            self._cas(row, claim.revision)
            step = db.execute("SELECT state,claim_token,lease_until,request_key FROM workflow_steps WHERE workflow_id=? AND ordinal=?",
                (claim.workflow_id, claim.ordinal)).fetchone()
            if (row["state"] != "running" or step is None or step["state"] != "reserved"
                    or step["claim_token"] != claim.token or step["request_key"] != claim.request_key):
                raise WorkflowError("workflow_claim_changed")
            if self._now() >= step["lease_until"]:
                raise WorkflowError("workflow_claim_expired")
        return True

    def reserve_next(self, workflow_id, *, expected_revision, estimated_tokens, lease_seconds=120):
        estimate = _integer(estimated_tokens)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 1200:
            raise WorkflowError("workflow_lease_invalid")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
            self._cas(row, expected_revision)
            if row["state"] not in {"pending", "running", "waiting_ingestion"}:
                return None
            plan = json.loads(row["plan"])
            step = db.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND ordinal<? AND state!='succeeded' ORDER BY ordinal LIMIT 1",
                (workflow_id, len(plan["steps"]))).fetchone()
            if step is None or step["state"] not in {"pending", "waiting"}:
                return None
            now = self._now()
            if step["state"] == "waiting" and (step["next_poll_at"] or 0) > now:
                return None
            tool = json.loads(step["spec"])["tool"]
            if step["state"] == "waiting" and tool != "await_import":
                raise WorkflowError("workflow_repeat_effect_rejected")
            if tool == "await_import" and estimate != 0:
                raise WorkflowError("workflow_poll_must_be_readonly")
            if row["spent_tokens"] + row["held_tokens"] + estimate > row["budget_limit"]:
                raise WorkflowError("workflow_budget_exceeded")
            token, revision = secrets.token_hex(24), row["revision"] + 1
            db.execute("UPDATE workflow_steps SET state='reserved',claim_token=?,lease_until=?,started_at=COALESCE(started_at,?),attempts=attempts+1,estimated_tokens=?,actual_tokens=NULL,updated_at=? WHERE workflow_id=? AND ordinal=?",
                (token, now + lease_seconds, now, estimate, now, workflow_id, step["ordinal"]))
            db.execute("UPDATE workflows SET state='running',held_tokens=held_tokens+?,revision=?,updated_at=? WHERE workflow_id=?",
                (estimate, revision, now, workflow_id))
            self._event(db, workflow_id, "step_reserved", {"ordinal": step["ordinal"], "request_key": step["request_key"], "estimated_tokens": estimate})
            seal = self._claim_seal(workflow_id, step["ordinal"], token, revision, step["request_key"])
            return WorkflowClaim(workflow_id, step["ordinal"], token, revision, step["request_key"], seal)

    def finish(self, claim, *, state, result=None, actual_tokens=None, reason_code=None, poll_seconds=5):
        self._check_claim(claim)
        if state not in {"succeeded", "waiting", "failed", "unknown"}:
            raise WorkflowError("workflow_step_state_invalid")
        if actual_tokens is not None:
            _integer(actual_tokens)
        if reason_code is not None:
            _key(reason_code)
        if result is not None:
            _json(result)
        if type(poll_seconds) is not int or not 1 <= poll_seconds <= 300:
            raise WorkflowError("workflow_poll_interval_invalid")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (claim.workflow_id,)).fetchone()
            self._cas(row, claim.revision)
            step = db.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND ordinal=?", (claim.workflow_id, claim.ordinal)).fetchone()
            if step is None or step["state"] != "reserved" or step["claim_token"] != claim.token or step["request_key"] != claim.request_key:
                raise WorkflowError("workflow_claim_changed")
            if self._now() >= step["lease_until"]:
                state, reason_code = "unknown", "workflow_claim_expired"
            if state == "waiting" and (json.loads(step["spec"])["tool"] != "await_import" or actual_tokens != 0):
                raise WorkflowError("workflow_wait_invalid")
            if actual_tokens is None:
                state, reason_code = "unknown", reason_code or "workflow_usage_unknown"
                held, spent = row["held_tokens"], row["spent_tokens"]
            else:
                held = row["held_tokens"] - step["estimated_tokens"]
                spent = row["spent_tokens"] + actual_tokens
            if state in {"unknown", "failed"} or spent + held > row["budget_limit"]:
                workflow_state = "needs_review"
                reason_code = reason_code or ("workflow_budget_overrun" if spent + held > row["budget_limit"] else "workflow_step_failed")
            elif state == "waiting":
                workflow_state = "waiting_ingestion"
            elif claim.ordinal == len(json.loads(row["plan"])["steps"]) - 1:
                workflow_state = "proposal_ready"
            else:
                workflow_state = "running"
            now = self._now()
            db.execute("UPDATE workflow_steps SET state=?,claim_token=NULL,lease_until=NULL,actual_tokens=?,result=?,reason_code=?,next_poll_at=?,updated_at=? WHERE workflow_id=? AND ordinal=?",
                (state, actual_tokens, _json(result) if result is not None else None, reason_code,
                 now + poll_seconds if state == "waiting" else None, now, claim.workflow_id, claim.ordinal))
            db.execute("UPDATE workflows SET state=?,reason_code=?,spent_tokens=?,held_tokens=?,revision=revision+1,updated_at=? WHERE workflow_id=?",
                (workflow_state, reason_code, spent, held, now, claim.workflow_id))
            self._event(db, claim.workflow_id, "step_" + state, {"ordinal": claim.ordinal,
                "request_key": claim.request_key, "actual_tokens": actual_tokens, "reason_code": reason_code})
            return self._snapshot(db, claim.workflow_id)

    def expire_reserved(self, workflow_id, *, expected_revision):
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
            self._cas(row, expected_revision)
            step = db.execute("SELECT * FROM workflow_steps WHERE workflow_id=? AND state='reserved'", (workflow_id,)).fetchone()
            if step is None or self._now() < step["lease_until"]:
                return self._snapshot(db, workflow_id)
            db.execute("UPDATE workflow_steps SET state='unknown',reason_code='workflow_interrupted_effect_unknown',claim_token=NULL,lease_until=NULL,updated_at=? WHERE workflow_id=? AND ordinal=?",
                (self._now(), workflow_id, step["ordinal"]))
            db.execute("UPDATE workflows SET state='needs_review',reason_code='workflow_interrupted_effect_unknown',revision=revision+1,updated_at=? WHERE workflow_id=?",
                (self._now(), workflow_id))
            self._event(db, workflow_id, "interrupted", {"ordinal": step["ordinal"], "held_tokens_preserved": True})
            return self._snapshot(db, workflow_id)

    def hold(self, workflow_id, *, expected_revision, reason_code):
        _key(reason_code)
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
            self._cas(row, expected_revision)
            if row["state"] not in {"pending", "running", "waiting_ingestion", "proposal_ready"} or db.execute(
                    "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND state='reserved'", (workflow_id,)).fetchone():
                raise WorkflowError("workflow_hold_not_allowed")
            db.execute("UPDATE workflows SET state='needs_review',reason_code=?,revision=revision+1,updated_at=? WHERE workflow_id=?",
                (reason_code, self._now(), workflow_id))
            self._event(db, workflow_id, "held", {"reason_code": reason_code})
            return self._snapshot(db, workflow_id)

    def revise(self, workflow_id, *, expected_revision, plan, amendment):
        amendment = validate_amendment(amendment)
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
            self._cas(row, expected_revision)
            if row["state"] not in {"pending", "running", "waiting_ingestion"}:
                raise WorkflowError("workflow_revision_not_allowed")
            scope, old = json.loads(row["scope"]), json.loads(row["plan"])
            plan = validate_plan(plan, scope)
            # This first contract only reduces already authorized goals. A new
            # action needs a separate verified workflow/amendment protocol.
            if (old["goal"] == "query" and plan["goal"] != "query"
                    or old["goal"] == "save" and plan["goal"] != "save"):
                raise WorkflowError("workflow_goal_expansion_rejected")
            origin = json.loads(row["source"])
            if (amendment["source_key"] == origin["source_key"]
                    or amendment["created_at"] < origin["created_at"]
                    or amendment["created_at"] > self._now() + 5):
                raise WorkflowError("workflow_amendment_invalid")
            immutable = digest({"workflow_id": workflow_id, "plan": plan, "amendment": amendment})
            used = db.execute("SELECT immutable_hash FROM workflow_amendments WHERE source_key=?",
                (amendment["source_key"],)).fetchone()
            if used is not None:
                if used["immutable_hash"] != immutable:
                    raise WorkflowError("workflow_amendment_conflict")
                return self._snapshot(db, workflow_id)
            started = db.execute("SELECT ordinal,state FROM workflow_steps WHERE workflow_id=? AND started_at IS NOT NULL ORDER BY ordinal",
                (workflow_id,)).fetchall()
            if any(step["state"] in {"reserved", "unknown", "failed"} for step in started):
                raise WorkflowError("workflow_revision_not_allowed")
            frozen = max((step["ordinal"] for step in started), default=-1) + 1
            if len(plan["steps"]) < frozen or plan["steps"][:frozen] != old["steps"][:frozen]:
                raise WorkflowError("workflow_executed_steps_immutable")
            if plan == old:
                return self._snapshot(db, workflow_id)
            now = self._now()
            db.execute("INSERT INTO workflow_amendments VALUES(?,?,?,?,?)",
                (amendment["source_key"], workflow_id, immutable, _json(amendment), now))
            for ordinal in range(frozen, max(len(old["steps"]), len(plan["steps"]))):
                if ordinal >= len(plan["steps"]):
                    db.execute("UPDATE workflow_steps SET state='cancelled',reason_code='workflow_plan_revised',updated_at=? WHERE workflow_id=? AND ordinal=? AND started_at IS NULL",
                        (now, workflow_id, ordinal))
                    continue
                spec = plan["steps"][ordinal]
                prior = db.execute("SELECT started_at FROM workflow_steps WHERE workflow_id=? AND ordinal=?", (workflow_id, ordinal)).fetchone()
                if prior is not None:
                    if prior["started_at"] is not None:
                        raise WorkflowError("workflow_executed_steps_immutable")
                    db.execute("UPDATE workflow_steps SET spec=?,request_key=?,state='pending',reason_code=NULL,result=NULL,updated_at=? WHERE workflow_id=? AND ordinal=?",
                        (_json(spec), self._request_key(workflow_id, ordinal, spec), now, workflow_id, ordinal))
                else:
                    db.execute("INSERT INTO workflow_steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (workflow_id, ordinal, _json(spec), self._request_key(workflow_id, ordinal, spec),
                         "pending", None, None, None, 0, 0, None, None, None, None, now))
            db.execute("UPDATE workflows SET plan=?,revision=revision+1,updated_at=? WHERE workflow_id=?", (_json(plan), now, workflow_id))
            self._event(db, workflow_id, "plan_revised", {"before": old, "after": plan,
                "amendment": amendment, "frozen_prefix": frozen})
            return self._snapshot(db, workflow_id)

    def cancel(self, workflow_id, *, expected_revision):
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
            self._cas(row, expected_revision)
            if row["state"] not in {"pending", "running", "waiting_ingestion"} or db.execute(
                    "SELECT 1 FROM workflow_steps WHERE workflow_id=? AND state IN ('reserved','unknown')", (workflow_id,)).fetchone():
                raise WorkflowError("workflow_cancel_not_allowed")
            now = self._now()
            db.execute("UPDATE workflow_steps SET state='cancelled',reason_code='workflow_cancelled',updated_at=? WHERE workflow_id=? AND state IN ('pending','waiting')",
                (now, workflow_id))
            db.execute("UPDATE workflows SET state='cancelled',reason_code='workflow_cancelled',revision=revision+1,updated_at=? WHERE workflow_id=?", (now, workflow_id))
            self._event(db, workflow_id, "cancelled", {"completed_effects_preserved": True})
            return self._snapshot(db, workflow_id)

    def record_publication(self, workflow_id, *, expected_revision, receipt):
        required = {"workflow_id", "origin_task_id", "state", "reply_id", "text_sha256"}
        if type(receipt) is not dict or set(receipt) != required or receipt["workflow_id"] != workflow_id:
            raise WorkflowError("workflow_publication_invalid")
        _key(receipt["reply_id"])
        if type(receipt["text_sha256"]) is not str or not HEX.fullmatch(receipt["text_sha256"]):
            raise WorkflowError("workflow_publication_invalid")
        if receipt["state"] not in {"b_view_visible", "delivery_unknown", "needs_review"}:
            raise WorkflowError("workflow_publication_invalid")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM workflows WHERE workflow_id=?", (_key(workflow_id),)).fetchone()
            if row is not None and row["publication"] == _json(receipt):
                return self._snapshot(db, workflow_id)
            self._cas(row, expected_revision)
            if row["state"] != "proposal_ready" or receipt["origin_task_id"] != row["origin_task_id"]:
                raise WorkflowError("workflow_publication_invalid")
            last = db.execute("SELECT result FROM workflow_steps WHERE workflow_id=? AND state='succeeded' ORDER BY ordinal DESC LIMIT 1", (workflow_id,)).fetchone()
            proposal = json.loads(last["result"])["proposal"]
            if hashlib.sha256(proposal["text"].encode("utf8")).hexdigest() != receipt["text_sha256"]:
                raise WorkflowError("workflow_publication_invalid")
            state = "completed" if receipt["state"] == "b_view_visible" else "needs_review"
            reason = None if state == "completed" else "workflow_publication_unconfirmed"
            db.execute("UPDATE workflows SET publication=?,state=?,reason_code=?,revision=revision+1,updated_at=? WHERE workflow_id=?",
                (_json(receipt), state, reason, self._now(), workflow_id))
            self._event(db, workflow_id, "publication_observed", receipt)
            return self._snapshot(db, workflow_id)
