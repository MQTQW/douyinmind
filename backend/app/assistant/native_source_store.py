"""Owned durable sidecar for native intake, history fences and UI admission.

Native records never constitute a task authorization.  UI plans are frozen
before queue admission; a crashed or expired planned lease is never re-issued.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse
import uuid

from .native_events import canonical_profile, validate_notice_identifier_diagnostic
from .native_transport import decimal_id
from app.storage_guard import (StorageGuardError, assert_no_orphan_sqlite_files,
                               guarded_sqlite_file, verified_readonly_connection)


class NativeStoreError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _code(value):
    if type(value) is not str or not re.fullmatch(r"[a-z_]{1,80}", value):
        raise NativeStoreError("invalid_hold_code")
    return value


_PRIVATE_KEYS = {"cookie", "cookieheader", "authorizationheader", "headers", "rawbody", "responsebody",
                 "storageState".casefold(), "password", "secret", "apikey", "useragent", "ua", "session"}
_NOTICE_SKIP_REASONS = frozenset({'native_comment_unavailable','native_visibility_unknown'})


def _safe_json(value, *, depth=0):
    """Structural guard, not general content DLP; Registry authorization is public."""
    if depth > 25:
        raise NativeStoreError("unsafe_ui_plan")
    if value is None or type(value) in (str, bool, int):
        if type(value) is str and len(value) > 20000:
            raise NativeStoreError("unsafe_ui_plan")
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list and len(value) <= 200:
        for item in value:
            _safe_json(item, depth=depth + 1)
        return
    if type(value) is dict and len(value) <= 100:
        for key, item in value.items():
            if type(key) is not str:
                raise NativeStoreError("unsafe_ui_plan")
            normalized = key.casefold().replace("_", "").replace("-", "")
            if normalized in _PRIVATE_KEYS or normalized.endswith(("token", "nonce")):
                raise NativeStoreError("unsafe_ui_plan")
            _safe_json(item, depth=depth + 1)
        return
    raise NativeStoreError("unsafe_ui_plan")


_LEGACY_REVIEW_BRAND = object()


class _LegacyPrequeuePermit:
    """Internal owner-lane evidence carrier, never a JSON/request permission."""
    __slots__ = ('store', 'snapshot', 'review')

    def __init__(self, store, snapshot, review, *, _brand=None):
        if _brand is not _LEGACY_REVIEW_BRAND:
            raise NativeStoreError('native_legacy_review_required')
        self.store, self.snapshot, self.review = store, snapshot, review


def _legacy_prequeue_permit(store, snapshot, review):
    # Only NativeLane's trusted owner review calls this; it is not an exposed
    # authorization API or cryptographic proof of caller-supplied JSON flags.
    return _LegacyPrequeuePermit(store, snapshot, review, _brand=_LEGACY_REVIEW_BRAND)


class NativeSourceStore:
    def __init__(self, path, receiver_uid, receiver_profile_url, *, create=False, clock=None):
        self.path = Path(path).absolute()
        self.receiver_uid = decimal_id(receiver_uid)
        prefix = "https://www.douyin.com/user/"
        if (type(receiver_profile_url) is not str or not receiver_profile_url.startswith(prefix)
                or canonical_profile(receiver_profile_url[len(prefix):]) != receiver_profile_url):
            raise NativeStoreError("invalid_receiver_profile")
        self.receiver_profile_url = receiver_profile_url
        self._clock = clock or time.time
        self._marker = self.path.with_suffix(self.path.suffix + ".owner.json")
        if self.path.resolve() != self.path or self._marker.resolve() != self._marker:
            raise NativeStoreError("source_store_path_not_owned")
        if create and not self.path.exists() and not self._marker.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with guarded_sqlite_file(self.path):
                    assert_no_orphan_sqlite_files(self.path)
            except StorageGuardError:
                raise NativeStoreError("source_store_path_not_owned") from None
            self._owner = uuid.uuid4().hex
            marker = {"version": 1, "owner": self._owner, "receiver_uid": self.receiver_uid,
                      "receiver_profile_url": self.receiver_profile_url}
            fd = os.open(self._marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(_dump(marker))
                stream.flush()
                os.fsync(stream.fileno())
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            self._initialize()
        else:
            self._owner = self._read_marker()
        self._guard()
        with self._connection() as db:
            meta = dict(db.execute("SELECT key,value FROM meta"))
            if meta.get("owner") != self._owner or meta.get("receiver_uid") != self.receiver_uid or meta.get("schema") != "1":
                raise NativeStoreError("source_store_owner_mismatch")

    def _read_marker(self):
        if not self.path.exists() or not self._marker.exists():
            raise NativeStoreError("source_store_missing")
        self._regular(self._marker)
        if self._marker.stat().st_size > 4096:
            raise NativeStoreError("source_store_owner_mismatch")
        try:
            value = json.loads(self._marker.read_text(encoding="utf-8"))
            if (type(value) is not dict or set(value) != {"version", "owner", "receiver_uid", "receiver_profile_url"}
                    or value["version"] != 1 or value["receiver_uid"] != self.receiver_uid
                    or value["receiver_profile_url"] != self.receiver_profile_url
                    or type(value["owner"]) is not str or not re.fullmatch(r"[a-f0-9]{32}", value["owner"])):
                raise ValueError()
            return value["owner"]
        except (ValueError, TypeError, OSError):
            raise NativeStoreError("source_store_owner_mismatch") from None

    def _regular(self, path):
        if path.resolve() != path or not path.is_file() or path.stat().st_nlink != 1:
            raise NativeStoreError("source_store_path_not_owned")

    def _guard(self):
        if not self.path.exists() or not self._marker.exists():
            raise NativeStoreError("source_store_missing")
        self._regular(self.path)
        self._regular(self._marker)
        if self._read_marker() != self._owner:
            raise NativeStoreError("source_store_owner_mismatch")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                self._regular(sidecar)

    def _owner_check(self, db):
        try:
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('owner','receiver_uid','schema')"))
        except sqlite3.Error:
            return False
        return (meta.get("owner") == self._owner and meta.get("receiver_uid") == self.receiver_uid
                and meta.get("schema") == "1")

    @contextmanager
    def _connection(self, *, write=False):
        self._guard()
        try:
            with guarded_sqlite_file(self.path):
                self._guard()
                before = self.path.stat()
                # RO ownership precedes ANY ordinary RW open.  The shared
                # helper may recover only a proven-own hot journal.  A cached
                # object presented with a foreign hot pair cannot roll it back.
                with verified_readonly_connection(self.path, self._owner_check, guard=self._guard) as readonly:
                    if not write:
                        readonly.row_factory = sqlite3.Row
                        readonly.execute("PRAGMA query_only=ON")
                        yield readonly
                        self._check_inode(before)
                        return
                self._check_inode(before)
                uri = self.path.as_uri() + "?mode=rw"
                db = sqlite3.connect(uri, uri=True, timeout=5)
                db.row_factory = sqlite3.Row
                try:
                    self._check_inode(before)
                    if not self._owner_check(db):
                        raise NativeStoreError("source_store_owner_mismatch")
                    db.execute("PRAGMA foreign_keys=ON")
                    db.execute("BEGIN IMMEDIATE")
                    yield db
                    self._check_inode(before)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                finally:
                    db.close()
        except StorageGuardError:
            raise NativeStoreError("source_store_owner_unverified") from None

    def _check_inode(self, before):
        self._guard()
        after = self.path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise NativeStoreError("source_store_changed")

    def _initialize(self):
        db = sqlite3.connect(self.path)
        try:
            db.executescript("""
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE channels(channel INTEGER PRIMARY KEY,group_id TEXT NOT NULL,baseline_done INTEGER NOT NULL DEFAULT 0,
                    scan INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 0,min_cursor TEXT NOT NULL DEFAULT '0',max_cursor TEXT NOT NULL DEFAULT '0');
                CREATE TABLE scan_pages(channel INTEGER NOT NULL,scan INTEGER NOT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(channel,scan,fingerprint));
                CREATE TABLE identities(uid TEXT NOT NULL,sec TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,PRIMARY KEY(uid,sec));
                CREATE TABLE sources(source_id TEXT PRIMARY KEY,video_id TEXT NOT NULL,cid TEXT NOT NULL,immutable_hash TEXT NOT NULL,
                    candidate TEXT NOT NULL,state TEXT NOT NULL,historical INTEGER NOT NULL,has_at INTEGER NOT NULL,has_reply INTEGER NOT NULL,
                    identity_revision TEXT NOT NULL,hold_code TEXT,first_seen REAL NOT NULL,last_seen REAL NOT NULL,
                    claim_owner TEXT,claim_token TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,plan TEXT,task_id TEXT,reference_id TEXT);
                CREATE UNIQUE INDEX native_comment_scope ON sources(video_id,cid);
                CREATE TABLE notice_refs(notice_id TEXT PRIMARY KEY,source_id TEXT,notice_type INTEGER NOT NULL,FOREIGN KEY(source_id) REFERENCES sources(source_id));
                CREATE TABLE notice_skips(notice_id TEXT NOT NULL,channel INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(notice_id,channel));
                CREATE TABLE observations(source_id TEXT NOT NULL,notice_id TEXT NOT NULL,channel INTEGER NOT NULL,fact TEXT NOT NULL,
                    PRIMARY KEY(source_id,notice_id,channel),FOREIGN KEY(source_id) REFERENCES sources(source_id));
            """)
            db.executemany("INSERT INTO meta VALUES(?,?)", [("owner", self._owner), ("receiver_uid", self.receiver_uid), ("schema", "1"), ("armed_at", ""), ("verified_at", "0"), ("pause_code", "")])
            db.commit()
        finally:
            db.close()

    def set_verified(self):
        with self._connection(write=True) as db:
            db.execute("UPDATE meta SET value=? WHERE key='verified_at'", (str(self._clock()),))
            db.execute("UPDATE meta SET value='' WHERE key='pause_code'")

    def pause(self, code):
        with self._connection(write=True) as db:
            db.execute("UPDATE meta SET value=? WHERE key='pause_code'", (_code(code),))
            db.execute("UPDATE meta SET value='0' WHERE key='verified_at'")

    def record_notice_identifier_diagnostic(self, value):
        """Diagnostic only: three enums for the currently paused format error."""
        clean = validate_notice_identifier_diagnostic(value)
        if clean is None:
            raise NativeStoreError('invalid_notice_identifier_diagnostic')
        with self._connection(write=True) as db:
            current = db.execute("SELECT value FROM meta WHERE key='pause_code'").fetchone()
            if current is None or current[0] != 'invalid_notice_identifier':
                raise NativeStoreError('notice_identifier_diagnostic_not_current')
            db.execute("INSERT INTO meta(key,value) VALUES('notice_identifier_diagnostic_v1',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_dump(clean),))
        return True

    def prepare_channel(self, channel, group):
        if type(channel) is not int or channel not in (7, 9):
            raise NativeStoreError("invalid_native_channel")
        group = decimal_id(group)
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM channels WHERE channel=?", (channel,)).fetchone()
            if row is None:
                db.execute("INSERT INTO channels(channel,group_id) VALUES(?,?)", (channel, group))
                row = db.execute("SELECT * FROM channels WHERE channel=?", (channel,)).fetchone()
            if row["group_id"] != group:
                raise NativeStoreError("native_group_changed")
            if not row["active"]:
                db.execute("UPDATE channels SET active=1,scan=scan+1,min_cursor='0',max_cursor='0' WHERE channel=?", (channel,))
                row = db.execute("SELECT * FROM channels WHERE channel=?", (channel,)).fetchone()
            return {"channel": channel, "group": group, "cursor": (row["min_cursor"], row["max_cursor"]),
                    "scan": row["scan"], "historical_scan": not bool(row["baseline_done"])}

    def arm_if_ready(self, allow_live):
        if allow_live is not True:
            return False
        with self._connection(write=True) as db:
            rows = list(db.execute("SELECT channel,baseline_done FROM channels"))
            if {r[0] for r in rows if r[1]} != {7, 9}:
                return False
            current = db.execute("SELECT value FROM meta WHERE key='armed_at'").fetchone()[0]
            if not current:
                db.execute("UPDATE meta SET value=? WHERE key='armed_at'", (str(self._clock()),))
            return True

    def _pin_identity(self, db, candidate):
        uid, sec = candidate["sender_uid"], candidate["sender_sec_uid"]
        rows = list(db.execute("SELECT * FROM identities WHERE uid=? OR sec=?", (uid, sec)))
        if any(r["uid"] != uid or r["sec"] != sec or r["state"] != "pinned" for r in rows):
            db.execute("UPDATE identities SET state='conflict' WHERE uid=? OR sec=?", (uid, sec))
            for row in db.execute("SELECT source_id,candidate FROM sources"):
                existing = json.loads(row["candidate"])
                if existing["sender_uid"] == uid or existing["sender_sec_uid"] == sec:
                    db.execute("UPDATE sources SET state='held_conflict',hold_code='native_identity_conflict' WHERE source_id=?", (row["source_id"],))
            return "0", False
        if rows:
            return str(rows[0]["revision"]), True
        revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM identities").fetchone()[0]
        db.execute("INSERT INTO identities VALUES(?,?,?,'pinned')", (uid, sec, revision))
        return str(revision), True

    def record_page(self, context, projection, *, next_cursor, has_more, allow_live=False):
        """One atomic event+cursor commit; a failed page cannot skip input."""
        skipped_ids=set()
        for notice in projection['notices']:
            if 'skip_reason' in notice:
                reason=notice['skip_reason']
                if type(reason) is not str or reason not in _NOTICE_SKIP_REASONS:
                    raise NativeStoreError('native_skip_reason_unknown')
                skipped_ids.add(notice['native_notice_id'])
        if any(candidate['native_notice_id'] in skipped_ids for candidate in projection['candidates']):
            raise NativeStoreError('native_skip_candidate_conflict')
        now = self._clock()
        channel = context["channel"]
        new_ids = []
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM channels WHERE channel=?", (channel,)).fetchone()
            if (row is None or row["group_id"] != context["group"] or row["scan"] != context["scan"]
                    or not row["active"] or (row["min_cursor"], row["max_cursor"]) != context["cursor"]):
                raise NativeStoreError("native_cursor_conflict")
            fingerprint = projection["fingerprint"]
            if db.execute("SELECT 1 FROM scan_pages WHERE channel=? AND scan=? AND fingerprint=?", (channel, row["scan"], fingerprint)).fetchone():
                raise NativeStoreError("native_page_repeated")
            notices = projection["notices"]
            all_known = bool(notices) and all(db.execute("SELECT 1 FROM notice_refs WHERE notice_id=?", (item["native_notice_id"],)).fetchone() for item in notices)
            armed = db.execute("SELECT value FROM meta WHERE key='armed_at'").fetchone()[0]
            for candidate in projection["candidates"]:
                if candidate["recipient_uid"] != self.receiver_uid or candidate["receiver_profile_url"] != self.receiver_profile_url:
                    raise NativeStoreError("native_receiver_scope_mismatch")
                source_id = "native:" + _digest(["douyin_web_v1", self.receiver_uid, candidate["video_id"], candidate["native_comment_id"]])
                immutable = {key: candidate[key] for key in ("namespace", "recipient_uid", "sender_uid", "sender_sec_uid", "sender_profile_url", "receiver_profile_url", "video_id", "native_comment_id", "content")}
                payload_hash = _digest(immutable)
                revision, identity_ok = self._pin_identity(db, candidate)
                historical = (not allow_live or not armed or not row["baseline_done"]
                              or candidate["created_time"] is None or int(candidate["created_time"]) <= float(armed))
                state = "historical" if historical else "fresh"
                reason = "native_timestamp_unknown" if candidate["created_time"] is None else None
                if not identity_ok:
                    state, reason = "held_conflict", "native_identity_conflict"
                candidate = {**candidate, "native_source_key": source_id, "source_id": source_id,
                             "identity_link_revision": revision}
                previous = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
                is_at = candidate["source_kind"] == "native_notice_candidate"
                if previous is None:
                    db.execute("INSERT INTO sources(source_id,video_id,cid,immutable_hash,candidate,state,historical,has_at,has_reply,identity_revision,hold_code,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (source_id, candidate["video_id"], candidate["native_comment_id"], payload_hash, _dump(candidate), state, int(historical), int(is_at), int(not is_at), revision, reason, now, now))
                    if state == "fresh":
                        new_ids.append(source_id)
                else:
                    if previous["immutable_hash"] != payload_hash:
                        db.execute("UPDATE sources SET state='held_conflict',hold_code='native_payload_conflict',last_seen=? WHERE source_id=?", (now, source_id))
                    else:
                        db.execute("UPDATE sources SET has_at=MAX(has_at,?),has_reply=MAX(has_reply,?),last_seen=? WHERE source_id=?", (int(is_at), int(not is_at), now, source_id))
                nid = candidate["native_notice_id"]
                old_notice = db.execute("SELECT source_id FROM notice_refs WHERE notice_id=?", (nid,)).fetchone()
                if old_notice is not None and old_notice[0] not in (None, source_id):
                    raise NativeStoreError("native_notice_conflict")
                db.execute("INSERT OR IGNORE INTO notice_refs VALUES(?,?,?)", (nid, source_id, candidate["notice_type"]))
                db.execute("UPDATE notice_refs SET source_id=? WHERE notice_id=? AND source_id IS NULL", (source_id, nid))
                fact = {key: candidate[key] for key in ("source_kind", "native_notice_id", "notice_type", "created_time", "parent_id_hint")}
                db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?)", (source_id, nid, channel, _dump(fact)))
            for item in notices:
                db.execute("INSERT OR IGNORE INTO notice_refs VALUES(?,NULL,?)", (item["native_notice_id"], item["notice_type"]))
                if item.get("skip_reason") in _NOTICE_SKIP_REASONS:
                    db.execute("INSERT OR IGNORE INTO notice_skips VALUES(?,?,?)", (item["native_notice_id"], channel, item['skip_reason']))
            db.execute("INSERT INTO scan_pages VALUES(?,?,?)", (channel, row["scan"], fingerprint))
            done = not has_more or (bool(row["baseline_done"]) and all_known)
            db.execute("UPDATE channels SET active=?,baseline_done=?,min_cursor=?,max_cursor=? WHERE channel=?",
                       (int(not done), int(bool(row["baseline_done"]) or done), next_cursor[0], next_cursor[1], channel))
        return [self.get_source(key)["candidate"] for key in new_ids]

    def _view(self, db, row):
        if row is None:
            return None
        candidate = json.loads(row["candidate"])
        candidate["source_kind"] = "native_notice_candidate" if row["has_at"] else "native_reply_notice_candidate"
        if row["has_at"]:
            at_fact = db.execute("SELECT fact FROM observations WHERE source_id=? AND channel=9 ORDER BY notice_id LIMIT 1", (row["source_id"],)).fetchone()
            if at_fact is not None:
                fact = json.loads(at_fact[0])
                candidate["native_notice_id"] = fact["native_notice_id"]
            candidate["notice_type"], candidate["at_sub_type"] = 45, 2
            candidate.pop("comment_type", None)
        if row["has_reply"] and candidate.get("parent_id_hint") is None:
            hints = [json.loads(item[0])["parent_id_hint"] for item in db.execute("SELECT fact FROM observations WHERE source_id=? AND channel=7", (row["source_id"],))]
            hints = {hint for hint in hints if hint not in (None, "0")}
            if len(hints) == 1:
                candidate["parent_id_hint"] = next(iter(hints))
        return {"source_id": row["source_id"], "candidate": candidate, "state": row["state"],
                "historical": bool(row["historical"]), "hold_code": row["hold_code"],
                "task_id": row["task_id"], "reference_id": row["reference_id"],
                "claim_token": row["claim_token"], "plan": json.loads(row["plan"]) if row["plan"] else None}

    def get_source(self, source_id):
        with self._connection() as db:
            return self._view(db, db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone())

    def find_source_by_native_comment(self, receiver_uid, video_id, native_comment_id):
        if receiver_uid != self.receiver_uid:
            return None
        with self._connection() as db:
            return self._view(db, db.execute("SELECT * FROM sources WHERE video_id=? AND cid=?", (video_id, native_comment_id)).fetchone())

    def lookup_source_for_task(self, task_id):
        with self._connection() as db:
            rows = list(db.execute("SELECT * FROM sources WHERE task_id=?", (task_id,)))
            return self._view(db, rows[0]) if len(rows) == 1 else None

    def find_sources_by_ui_reference(self, reference_id):
        """Include a pre-enqueue plan even when no queue task was linked yet."""
        with self._connection() as db:
            result = []
            for row in db.execute("SELECT * FROM sources WHERE reference_id=? OR plan IS NOT NULL", (reference_id,)):
                plan = json.loads(row["plan"]) if row["plan"] else None
                if row["reference_id"] == reference_id or (plan is not None and plan.get("reference_id") == reference_id):
                    result.append(self._view(db, row))
            return result

    def admission_current(self, source_id):
        """Intake freshness view; retained for callers that need a live receipt."""
        with self._connection() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if not self._admitted_scope_current(db, row):
                return False
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at')"))
            verified = float(meta["verified_at"])
            return (not meta["pause_code"] and 0 < verified <= self._clock() + 5
                    and self._clock() - verified <= 120)

    def _admitted_scope_current(self, db, row):
        """Persistent accepted intent, independent of receipt transport health.

        This supplies only the native source portion of authorization. The
        caller must still match the exact task/reference/full frozen plan and
        recheck the current Registry scope; publication also verifies actual B.
        """
        if (row is None or row["state"] != "admitted" or row["historical"]
                or not row["plan"] or not row["task_id"]):
            return False
        candidate = json.loads(row["candidate"])
        identity = db.execute("SELECT state,revision FROM identities WHERE uid=? AND sec=?",
                              (candidate["sender_uid"], candidate["sender_sec_uid"])).fetchone()
        return (identity is not None and identity[0] == "pinned"
                and str(identity[1]) == row["identity_revision"]
                and not self._semantic_conflicts(db, candidate))

    def admitted_source_current(self, source_id):
        """Accepted source remains valid until its persistent scope changes.

        A 120-second intake identity lease or a later polling/network pause is
        not a withdrawal of an already admitted request. Historical/unlinked,
        identity/payload/semantic conflicts and unavailable ownership still fail
        closed. This never refreshes a timestamp, source state or authorization.
        """
        with self._connection() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            return self._admitted_scope_current(db, row)

    def _semantic_conflicts(self, db, candidate):
        for row in db.execute("SELECT candidate FROM sources WHERE video_id=?", (candidate["video_id"],)):
            other = json.loads(row[0])
            if (other["sender_profile_url"] == candidate["sender_profile_url"]
                    and re.sub(r"\s+", "", other["content"]) == re.sub(r"\s+", "", candidate["content"])
                    and other["native_comment_id"] != candidate["native_comment_id"]):
                return True
        return False

    def semantic_conflicts(self, candidate):
        with self._connection() as db:
            return self._semantic_conflicts(db, candidate)

    def _valid(self, db, row, token):
        if row is None or row["claim_token"] != token or row["state"] not in ("claimed", "planned") or row["historical"]:
            return False, "native_scope_changed", False
        candidate = json.loads(row["candidate"])
        identity = db.execute("SELECT state,revision FROM identities WHERE uid=? AND sec=?", (candidate["sender_uid"], candidate["sender_sec_uid"])).fetchone()
        verified = float(db.execute("SELECT value FROM meta WHERE key='verified_at'").fetchone()[0])
        paused = db.execute("SELECT value FROM meta WHERE key='pause_code'").fetchone()[0]
        if paused or not verified or self._clock() - verified > 120 or verified > self._clock() + 5:
            return False, "native_identity_unverified", False
        if identity is None or identity[0] != "pinned" or str(identity[1]) != row["identity_revision"]:
            return False, "native_identity_unverified", False
        if row["lease_until"] is None or self._clock() > row["lease_until"]:
            return False, "native_scope_changed", False
        conflict = self._semantic_conflicts(db, candidate)
        return not conflict, "native_semantic_conflict" if conflict else "ok", conflict

    def peek_ui_candidate(self):
        """Read the next scope without spending its once-only UI attempt."""
        with self._connection() as db:
            row=db.execute("SELECT * FROM sources WHERE state='fresh' AND historical=0 ORDER BY first_seen,source_id LIMIT 1").fetchone()
            view=self._view(db,row)
            return view['candidate'] if view is not None else None

    def claim_ui_candidate(self, owner, lease_seconds=120):
        if type(owner) is not str or not 1 <= len(owner) <= 100 or type(lease_seconds) not in (int, float) or not 1 <= lease_seconds <= 300:
            raise NativeStoreError("invalid_native_claim")
        with self._connection(write=True) as db:
            # Unknown outcome is held even if the process died before admitting.
            db.execute("UPDATE sources SET state='held',hold_code='native_unknown_lease' WHERE state='claimed' AND lease_until<?", (self._clock(),))
            row = db.execute("SELECT * FROM sources WHERE state='fresh' AND historical=0 ORDER BY first_seen,source_id LIMIT 1").fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute("UPDATE sources SET state='claimed',claim_owner=?,claim_token=?,lease_until=?,attempts=attempts+1 WHERE source_id=? AND state='fresh'", (owner, token, self._clock() + lease_seconds, row["source_id"]))
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (row["source_id"],)).fetchone()
            view = self._view(db, row)
            return {"source_id": view["source_id"], "claim_token": token, "candidate": view["candidate"]}

    def check_candidate(self, source_id, claim_token):
        with self._connection() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            valid, reason, conflict = self._valid(db, row, claim_token)
            return {"valid": valid, "reason": reason, "semantic_conflict": conflict,
                    "candidate": self._view(db, row)["candidate"] if row else None}

    def candidate_identity_refresh_allowed(self, source_id, claim_token):
        """Only a live, unplanned claim with *solely* stale receipt identity.

        Read-only: no timestamps, claim tokens, leases, cursors or source state
        change here. The caller must still perform one real B identity GET and
        repeat its ordinary complete candidate/UI/Registry checks afterwards.
        """
        if (type(source_id) is not str or not re.fullmatch(r"native:[a-f0-9]{64}", source_id)
                or type(claim_token) is not str or not re.fullmatch(r"[a-f0-9]{32}", claim_token)):
            return False
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            return False
        with self._connection() as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if (row is None or row["state"] != "claimed" or row["historical"]
                    or row["claim_token"] != claim_token or row["plan"] is not None
                    or row["task_id"] is not None or row["reference_id"] is not None):
                return False
            lease = row["lease_until"]
            if type(lease) not in (int, float) or not math.isfinite(lease) or lease <= now:
                return False
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at')"))
            if set(meta) != {"pause_code", "verified_at"}:
                return False
            try:
                verified = float(meta["verified_at"])
            except (ValueError, TypeError, KeyError):
                return False
            if (meta.get("pause_code") or not math.isfinite(verified)
                    or not 0 < verified < now or now - verified <= 120):
                return False
            try:
                candidate = json.loads(row["candidate"])
                if (type(candidate) is not dict or candidate["recipient_uid"] != self.receiver_uid
                        or candidate["receiver_profile_url"] != self.receiver_profile_url
                        or canonical_profile(candidate["sender_sec_uid"]) != candidate["sender_profile_url"]):
                    return False
                immutable = {key: candidate[key] for key in ("namespace", "recipient_uid", "sender_uid", "sender_sec_uid", "sender_profile_url", "receiver_profile_url", "video_id", "native_comment_id", "content")}
                if _digest(immutable) != row["immutable_hash"]:
                    return False
                identity = db.execute("SELECT state,revision FROM identities WHERE uid=? AND sec=?",
                    (candidate["sender_uid"], candidate["sender_sec_uid"])).fetchone()
                return (identity is not None and identity[0] == "pinned"
                        and str(identity[1]) == row["identity_revision"]
                        and not self._semantic_conflicts(db, candidate))
            except (ValueError, TypeError, KeyError):
                return False

    def record_ui_plan(self, source_id, claim_token, plan):
        allowed = {"task_id", "reference_id", "envelope", "thread", "captured_at", "queue_item", "native_correlation"}
        if type(plan) is not dict or set(plan) - allowed or not {"reference_id", "envelope", "thread", "queue_item", "native_correlation"} <= set(plan):
            raise NativeStoreError("invalid_ui_plan")
        _safe_json(plan)
        if len(_dump(plan).encode()) > 131072:
            raise NativeStoreError("unsafe_ui_plan")
        if type(plan["reference_id"]) is not str or not plan["reference_id"]:
            raise NativeStoreError("invalid_ui_plan")
        if type(plan["queue_item"]) is not dict or plan["queue_item"].get("envelope") != plan["envelope"]:
            raise NativeStoreError("invalid_ui_plan")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            valid, reason, _ = self._valid(db, row, claim_token)
            if not valid:
                raise NativeStoreError(reason)
            if row["plan"] and row["plan"] != _dump(plan):
                raise NativeStoreError("native_plan_conflict")
            db.execute("UPDATE sources SET state='planned',plan=? WHERE source_id=?", (_dump(plan), source_id))

    def _visibility_hint_checkpoint(self, db, hint):
        """Existing quarantined notice plus live pin/fence; no task permission."""
        from .native_visibility_hint_store import validate_review_hint, hint_source_id
        hint = validate_review_hint(hint)
        now = self._clock()
        if (type(now) not in (int, float) or not math.isfinite(now) or now <= 0
                or hint['recipient_uid'] != self.receiver_uid
                or hint['receiver_profile_url'] != self.receiver_profile_url):
            raise NativeStoreError('native_visibility_hint_scope_changed')
        meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at','armed_at')"))
        try:
            verified, armed = float(meta['verified_at']), float(meta['armed_at'])
        except (ValueError, TypeError, KeyError):
            raise NativeStoreError('native_identity_unverified') from None
        if (meta.get('pause_code') or not math.isfinite(verified) or not 0 < verified <= now + 5
                or now - verified > 120 or not math.isfinite(armed) or not 0 < armed <= now
                or not armed < int(hint['created_time']) <= now + 5):
            raise NativeStoreError('native_identity_unverified')
        notice = db.execute('SELECT source_id,notice_type FROM notice_refs WHERE notice_id=?',
            (hint['native_notice_id'],)).fetchone()
        skip = db.execute('SELECT reason FROM notice_skips WHERE notice_id=? AND channel=9',
            (hint['native_notice_id'],)).fetchone()
        if (notice is None or notice['source_id'] is not None or notice['notice_type'] != 45
                or skip is None or skip['reason'] != 'native_visibility_unknown'):
            raise NativeStoreError('native_visibility_hint_skip_required')
        sid = hint_source_id(hint)
        if db.execute('SELECT 1 FROM sources WHERE source_id=? OR (video_id=? AND cid=?)',
                (sid, hint['video_id'], hint['native_comment_id'])).fetchone():
            raise NativeStoreError('native_visibility_hint_source_exists')
        pins = list(db.execute('SELECT uid,sec,state,revision FROM identities WHERE uid=? OR sec=?',
            (hint['sender_uid'], hint['sender_sec_uid'])))
        if (len(pins) != 1 or pins[0]['uid'] != hint['sender_uid'] or pins[0]['sec'] != hint['sender_sec_uid']
                or pins[0]['state'] != 'pinned' or canonical_profile(pins[0]['sec']) != hint['sender_profile_url']):
            raise NativeStoreError('native_identity_unverified')
        if self._semantic_conflicts(db, hint):
            raise NativeStoreError('native_semantic_conflict')
        return {'source_id': sid, 'identity_revision': str(pins[0]['revision']), 'armed_at': armed}

    def visibility_hint_checkpoint(self, hint):
        with self._connection() as db:
            return self._visibility_hint_checkpoint(db, hint)

    def native_meaning_checkpoint(self, message):
        """Only authorize read-only interpretation of this pinned author's text.

        No visibility, original-thread or execution permission is inferred.
        This uses an existing UID/sec pin and a recent actual B identity GET;
        it does not change source/notice state or spend a UI claim.
        """
        try:
            if (type(message) is not dict or message.get('namespace') != 'douyin_web_v1'
                    or message.get('source_kind') not in {'native_notice_candidate','native_visibility_review_hint'}
                    or type(message.get('notice_type')) is not int or message['notice_type'] != 45
                    or type(message.get('at_sub_type')) is not int or message['at_sub_type'] != 2
                    or message.get('dispatchable') is not False
                    or message.get('recipient_uid') != self.receiver_uid
                    or message.get('receiver_profile_url') != self.receiver_profile_url):
                return False
            for key in ('sender_uid','video_id','native_comment_id','native_notice_id'):
                if type(message.get(key)) is not str or decimal_id(message[key]) != message[key]:return False
            if (canonical_profile(message['sender_sec_uid']) != message.get('sender_profile_url')
                    or message['sender_uid'] == self.receiver_uid
                    or type(message.get('content')) is not str or not message['content'].strip()
                    or not 1 <= len(message['content']) <= 4000):return False
            now=self._clock()
            if type(message.get('created_time')) is not str:return False
            created=int(message['created_time'])
            with self._connection() as db:
                meta=dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at','armed_at')"))
                verified,armed=float(meta['verified_at']),float(meta['armed_at'])
                if (meta.get('pause_code') or not all(math.isfinite(v) for v in (now,verified,armed))
                        or not 0 < verified <= now+5 or now-verified > 120
                        or not 0 < armed < created <= now+5):return False
                pins=list(db.execute('SELECT uid,sec,state FROM identities WHERE uid=? OR sec=?',
                    (message['sender_uid'],message['sender_sec_uid'])))
                if (len(pins)!=1 or pins[0]['state']!='pinned' or pins[0]['uid']!=message['sender_uid']
                        or pins[0]['sec']!=message['sender_sec_uid']):return False
                rows=list(db.execute('SELECT state,candidate,plan,task_id,reference_id FROM sources WHERE video_id=? AND cid=?',
                    (message['video_id'],message['native_comment_id'])))
                for row in rows:
                    old=json.loads(row['candidate'])
                    keys=('namespace','recipient_uid','sender_uid','sender_sec_uid','sender_profile_url',
                        'receiver_profile_url','video_id','native_comment_id','content')
                    if (row['state']!='fresh' or row['plan'] is not None or row['task_id'] is not None
                            or row['reference_id'] is not None or any(old.get(k)!=message.get(k) for k in keys)):
                        return False
            return True
        except (KeyError,ValueError,TypeError,NativeStoreError):
            return False

    def accept_verified_hint_ui_plan(self, permit):
        """Create one ordinary planned source only after owner-page proof.

        The hint ledger reserves its once-only claim first. A crash or failure
        between the two DB commits stays spent/unknown; it is never retried by
        a poll, and neither the original skip nor its NULL notice ref is reset.
        """
        from .native_visibility_hint_store import _VerifiedHintUIPlanPermit
        if type(permit) is not _VerifiedHintUIPlanPermit or permit.source_store is not self:
            raise NativeStoreError('native_visibility_hint_ui_proof_required')
        plan = permit.plan
        allowed = {'task_id', 'reference_id', 'envelope', 'thread', 'captured_at', 'queue_item', 'native_correlation'}
        if (type(plan) is not dict or set(plan) - allowed
                or not {'reference_id', 'envelope', 'thread', 'queue_item', 'native_correlation'} <= set(plan)
                or plan.get('task_id') is not None):
            raise NativeStoreError('invalid_ui_plan')
        _safe_json(plan)
        if (len(_dump(plan).encode()) > 131072 or type(plan['reference_id']) is not str or not plan['reference_id']
                or type(plan['queue_item']) is not dict or plan['queue_item'].get('envelope') != plan['envelope']):
            raise NativeStoreError('invalid_ui_plan')
        snapshot = permit.ledger._begin_source_admission(permit, self)
        hint, source_id = snapshot['hint'], snapshot['source_id']
        envelope, correlation = plan['envelope'], plan['native_correlation']
        observed = envelope.get('native_comment_id') if type(envelope) is dict else None
        normal = lambda text: re.sub(r'\s+', '', text) if type(text) is str else None
        if (type(envelope) is not dict or envelope.get('source_mode') != 'live_ui'
                or envelope.get('reference_id') != plan['reference_id']
                or envelope.get('sender_profile_url') != hint['sender_profile_url']
                or envelope.get('receiver_profile_url') != hint['receiver_profile_url']
                or envelope.get('video_id') != hint['video_id']
                or normal(envelope.get('comment_text')) != normal(hint['content'])
                or observed not in (None, hint['native_comment_id'])
                or correlation != {'source_native_comment_id': hint['native_comment_id'],
                    'observed_native_comment_id': observed,
                    'quality': 'native_equal' if observed == hint['native_comment_id'] else 'semantic_only'}):
            raise NativeStoreError('native_visibility_hint_ui_scope_changed')
        with self._connection(write=True) as db:
            scope = self._visibility_hint_checkpoint(db, hint)
            if scope != snapshot['scope'] or source_id != scope['source_id']:
                raise NativeStoreError('native_visibility_hint_scope_changed')
            candidate = permit.ledger._candidate(hint, scope)
            immutable = {key: candidate[key] for key in ('namespace', 'recipient_uid', 'sender_uid',
                'sender_sec_uid', 'sender_profile_url', 'receiver_profile_url', 'video_id', 'native_comment_id', 'content')}
            now = self._clock()
            if snapshot['lease_until'] < now:
                raise NativeStoreError('native_visibility_hint_claim_expired')
            audit = {'version': 1, 'operation': 'accept_verified_hint_ui_plan', 'hint_id': permit.hint_id,
                'source_id': source_id, 'native_visibility': 'unknown', 'hint_immutable_sha256': snapshot['immutable_hash'],
                'page_fingerprint': snapshot['page_fingerprint'], 'plan_sha256': permit.plan_hash,
                'native_correlation': correlation, 'reviewed_at': now,
                'cross_database_atomic': False, 'original_skip_and_notice_ref_preserved': True}
            db.execute('INSERT INTO sources(source_id,video_id,cid,immutable_hash,candidate,state,historical,has_at,has_reply,identity_revision,hold_code,first_seen,last_seen,claim_owner,claim_token,lease_until,attempts,plan) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (source_id, hint['video_id'], hint['native_comment_id'], _digest(immutable), _dump(candidate),
                'planned', 0, 1, 0, scope['identity_revision'], None, now, now, snapshot['claim_owner'],
                snapshot['claim_token'], snapshot['lease_until'], 1, _dump(plan)))
            row = db.execute('SELECT * FROM sources WHERE source_id=?', (source_id,)).fetchone()
            valid, reason, _ = self._valid(db, row, snapshot['claim_token'])
            if not valid:
                raise NativeStoreError(reason)
            fact = {key: candidate[key] for key in ('source_kind', 'native_notice_id', 'notice_type', 'created_time', 'parent_id_hint')}
            fact['hint_provenance'] = candidate['hint_provenance']
            db.execute('INSERT INTO observations VALUES(?,?,?,?)', (source_id, hint['native_notice_id'], 9, _dump(fact)))
            db.execute('INSERT INTO meta VALUES(?,?)', ('visibility_hint_admission_v1:' + source_id, _dump(audit)))
        permit.ledger._finish_source_admission(permit, source_id)
        return {'source_id': source_id, 'claim_token': snapshot['claim_token'], 'candidate': candidate}

    def finish_ui_admission(self, source_id, claim_token, task_id, reference_id):
        if type(task_id) is not str or not task_id or type(reference_id) is not str or not reference_id:
            raise NativeStoreError("invalid_ui_admission")
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None or row["claim_token"] != claim_token or not row["plan"]:
                raise NativeStoreError("native_plan_conflict")
            plan = json.loads(row["plan"])
            if plan["reference_id"] != reference_id or (row["task_id"] is not None and row["task_id"] != task_id):
                raise NativeStoreError("native_plan_conflict")
            # Keep a known queue outcome, but never clear a concurrent conflict.
            state = row["state"] if row["state"] == "held_conflict" else "admitted"
            db.execute("UPDATE sources SET state=?,task_id=?,reference_id=? WHERE source_id=?", (state, task_id, reference_id, source_id))

    def hold(self, source_id, claim_token, code):
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None or row["claim_token"] != claim_token:
                raise NativeStoreError("native_claim_conflict")
            if row["state"] not in ("admitted", "historical", "held_conflict"):
                db.execute("UPDATE sources SET state='held',hold_code=? WHERE source_id=?", (_code(code), source_id))

    def defer(self, source_id, claim_token, code):
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None or row["claim_token"] != claim_token or row["state"] != "claimed" or row["plan"]:
                raise NativeStoreError("native_claim_conflict")
            state = "fresh" if row["attempts"] < 3 else "held"
            db.execute("UPDATE sources SET state=?,hold_code=?,claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=?", (state, _code(code), source_id))

    def request_ui_recheck(self, source_id, *, expected_old_code, operator_reason):
        """One explicit local request to RE-READ one unadmitted plain reply.

        No task/authorization is created here. Intake still requires fresh
        native identity and actual UI/Registry proof. The old observation rows,
        first-seen fence, attempts and immutable source are never rewritten.
        This method has no HTTP route and is never called by a poll timer.
        """
        if (type(source_id) is not str or not re.fullmatch(r"native:[a-f0-9]{64}", source_id)
                or type(expected_old_code) is not str
                or expected_old_code not in {"native_reply_not_matched", "native_reply_parent_unresolved"}
                or operator_reason != "explicit_operator_choice_ui_recheck" or type(operator_reason) is not str):
            raise NativeStoreError("native_recheck_request_invalid")
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise NativeStoreError("native_recheck_request_invalid")
        audit_key = "ui_recheck_v1:" + source_id
        with self._connection(write=True) as db:
            if db.execute("SELECT 1 FROM meta WHERE key=?", (audit_key,)).fetchone():
                raise NativeStoreError("native_recheck_already_requested")
            row = db.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if (row is None or row["state"] != "held" or row["hold_code"] != expected_old_code
                    or row["historical"] or row["has_reply"] != 1 or row["has_at"] != 0
                    or row["attempts"] != 1 or row["plan"] is not None
                    or row["task_id"] is not None or row["reference_id"] is not None):
                raise NativeStoreError("native_recheck_not_allowed")
            candidate = json.loads(row["candidate"])
            identity = db.execute("SELECT state,revision FROM identities WHERE uid=? AND sec=?",
                (candidate["sender_uid"], candidate["sender_sec_uid"])).fetchone()
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at')"))
            try:
                verified = float(meta["verified_at"])
            except (ValueError, TypeError, KeyError):
                raise NativeStoreError("native_identity_unverified") from None
            if (meta.get("pause_code") or not math.isfinite(verified) or not 0 < verified <= now + 5
                    or now - verified > 120 or identity is None or identity[0] != "pinned"
                    or str(identity[1]) != row["identity_revision"]):
                raise NativeStoreError("native_identity_unverified")
            if self._semantic_conflicts(db, candidate):
                raise NativeStoreError("native_semantic_conflict")
            audit = {"version": 1, "operation": "request_ui_recheck", "source_id": source_id,
                "operator_reason": operator_reason, "old_state": row["state"],
                "old_hold_code": row["hold_code"], "old_attempts": row["attempts"],
                "source_first_seen": row["first_seen"], "source_last_seen": row["last_seen"],
                "old_hold_at": None, "hold_timestamp_was_not_recorded": True,
                "requested_at": float(now), "maximum_requests": 1}
            db.execute("INSERT INTO meta(key,value) VALUES(?,?)", (audit_key, _dump(audit)))
            db.execute("UPDATE sources SET state='fresh',hold_code=NULL,claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=?",
                (source_id,))
            return {"source_id": source_id, "status": "ui_recheck_requested", "previous_hold_code": expected_old_code,
                "attempts_preserved": row["attempts"], "maximum_requests": 1,
                "audit_recorded": True, "dispatchable": False}

    def request_followup_review(self,source_id,*,operator_reason):
        """One explicit READ for an unspent plain reply, never a task grant.

        Trusted caller MUST first hold the paused/current-run/idle/manual-safe
        owner gate and revalidate Registry plus absence of Queue/Agent effects.
        This Store knows only native identity, immutable source and its fences;
        it cannot certify those external permissions. No poll invokes this.
        """
        from .native_transport import NativeTransportError
        if (type(source_id) is not str or not re.fullmatch(r'native:[a-f0-9]{64}',source_id)
                or type(operator_reason) is not str or operator_reason!='explicit_operator_thread_followup_review'):
            raise NativeStoreError('native_followup_review_request_invalid')
        now=self._clock()
        if type(now) not in (int,float) or not math.isfinite(now) or now<=0:
            raise NativeStoreError('native_followup_review_request_invalid')
        audit_key='thread_followup_review_v1:'+source_id
        with self._connection(write=True) as db:
            if db.execute('SELECT 1 FROM meta WHERE key IN (?,?,?)',
                    (audit_key,'ui_recheck_v1:'+source_id,'ui_read_fix_recheck_v1:'+source_id)).fetchone():
                raise NativeStoreError('native_followup_review_already_requested')
            row=db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
            if (row is None or row['state']!='held' or row['hold_code']!='native_reply_parent_unresolved'
                    or row['historical'] or row['has_reply']!=1 or row['has_at']!=0 or row['attempts']!=1
                    or row['plan'] is not None or row['task_id'] is not None or row['reference_id'] is not None
                    or type(row['claim_token']) is not str or not re.fullmatch(r'[a-f0-9]{32}',row['claim_token'])):
                raise NativeStoreError('native_followup_review_not_allowed')
            candidate=self._prequeue_identity(db,row,fresh=True)
            try:
                if (candidate.get('namespace')!='douyin_web_v1'
                        or candidate.get('source_kind')!='native_reply_notice_candidate'
                        or type(candidate.get('notice_type')) is not int or candidate['notice_type']!=31
                        or type(candidate.get('comment_type')) is not int or candidate['comment_type']!=2
                        or candidate.get('dispatchable') is not False or candidate.get('relation_status')!='unknown'
                        or candidate.get('root_comment_id') is not None or candidate.get('parent_comment_id') is not None
                        or candidate['sender_uid']==self.receiver_uid
                        or row['video_id']!=candidate['video_id'] or row['cid']!=candidate['native_comment_id']
                        or source_id!='native:'+_digest(['douyin_web_v1',self.receiver_uid,candidate['video_id'],candidate['native_comment_id']])):
                    raise ValueError()
                for key in ('sender_uid','recipient_uid','video_id','native_comment_id','native_notice_id'):
                    if type(candidate.get(key)) is not str or decimal_id(candidate[key])!=candidate[key]:raise ValueError()
                created=candidate.get('created_time')
                armed=float(db.execute("SELECT value FROM meta WHERE key='armed_at'").fetchone()[0])
                if (type(created) is not str or decimal_id(created,zero=True)!=created
                        or not math.isfinite(armed) or not 0<armed<int(created)<=now+5):raise ValueError()
                ref=db.execute('SELECT source_id,notice_type FROM notice_refs WHERE notice_id=?',
                    (candidate['native_notice_id'],)).fetchone()
                facts=db.execute('SELECT fact FROM observations WHERE source_id=? AND notice_id=? AND channel=7',
                    (source_id,candidate['native_notice_id'])).fetchone()
                fact=json.loads(facts[0]) if facts else None
                if (ref is None or ref['source_id']!=source_id or type(ref['notice_type']) is not int or ref['notice_type']!=31
                        or type(fact) is not dict or fact.get('source_kind')!='native_reply_notice_candidate'
                        or type(fact.get('notice_type')) is not int or fact['notice_type']!=31
                        or fact.get('native_notice_id')!=candidate['native_notice_id']
                        or fact.get('created_time')!=created or fact.get('parent_id_hint')!=candidate.get('parent_id_hint')):
                    raise ValueError()
            except (ValueError,TypeError,KeyError,IndexError,NativeTransportError):
                raise NativeStoreError('native_followup_review_not_allowed') from None
            audit={'version':1,'operation':'request_followup_review','source_id':source_id,
                'operator_reason':operator_reason,'request_one_count':1,'maximum_requests':1,
                'old_state':row['state'],'old_hold_code':row['hold_code'],'old_attempts':row['attempts'],
                'old_claim_sha256':hashlib.sha256(row['claim_token'].encode()).hexdigest(),
                'old_lease_until':row['lease_until'],'immutable_hash':row['immutable_hash'],
                'identity_revision':row['identity_revision'],'source_first_seen':row['first_seen'],
                'source_last_seen':row['last_seen'],'requested_at':float(now),'old_hold_at':None,
                'hold_timestamp_was_not_recorded':True,'permission_scope':'fresh_ui_read_only'}
            db.execute('INSERT INTO meta(key,value) VALUES(?,?)',(audit_key,_dump(audit)))
            db.execute("UPDATE sources SET state='fresh',hold_code=NULL,claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=? AND state='held' AND attempts=1",(source_id,))
        return {'source_id':source_id,'status':'followup_ui_review_requested','attempts_preserved':1,
            'maximum_requests':1,'audit_recorded':True,'dispatchable':False}

    def request_ui_recheck_after_read_fix(self, source_id, *, expected_old_code, operator_reason):
        """One further explicit READ after an evidenced pre-admission fix.

        Requires the original one-shot audit and exactly two read attempts,
        with no native plan, Queue link or effect. This is not a timer retry,
        cannot reopen spent/unknown outcomes, and never resets either audit.
        A selected Choice must additionally be freshly revalidated by the lane.
        """
        if (type(source_id) is not str or not re.fullmatch(r"native:[a-f0-9]{64}", source_id)
                or type(expected_old_code) is not str or expected_old_code != 'native_reply_not_matched'
                or type(operator_reason) is not str or operator_reason != 'explicit_operator_verified_read_fix'):
            raise NativeStoreError('native_recheck_request_invalid')
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
            raise NativeStoreError('native_recheck_request_invalid')
        audit_key = 'ui_read_fix_recheck_v1:' + source_id
        with self._connection(write=True) as db:
            if db.execute('SELECT 1 FROM meta WHERE key=?', (audit_key,)).fetchone():
                raise NativeStoreError('native_recheck_already_requested')
            prior = db.execute('SELECT value FROM meta WHERE key=?', ('ui_recheck_v1:' + source_id,)).fetchone()
            try:
                prior = json.loads(prior[0]) if prior is not None else None
            except (TypeError, ValueError):
                prior = None
            row = db.execute('SELECT * FROM sources WHERE source_id=?', (source_id,)).fetchone()
            if (type(prior) is not dict or prior.get('version') != 1
                    or prior.get('operation') != 'request_ui_recheck' or prior.get('source_id') != source_id
                    or prior.get('old_attempts') != 1 or row is None or row['state'] != 'held'
                    or row['hold_code'] != expected_old_code or row['attempts'] != 2
                    or row['historical'] or row['has_at'] != 0 or row['has_reply'] != 1
                    or row['plan'] is not None or row['task_id'] is not None or row['reference_id'] is not None
                    or prior.get('source_first_seen') != row['first_seen']):
                raise NativeStoreError('native_recheck_not_allowed')
            candidate = json.loads(row['candidate'])
            immutable = {key: candidate[key] for key in ('namespace', 'recipient_uid', 'sender_uid',
                'sender_sec_uid', 'sender_profile_url', 'receiver_profile_url', 'video_id',
                'native_comment_id', 'content')}
            if (_digest(immutable) != row['immutable_hash'] or candidate['recipient_uid'] != self.receiver_uid
                    or candidate['receiver_profile_url'] != self.receiver_profile_url):
                raise NativeStoreError('native_recheck_not_allowed')
            identity = db.execute('SELECT state,revision FROM identities WHERE uid=? AND sec=?',
                (candidate['sender_uid'], candidate['sender_sec_uid'])).fetchone()
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at')"))
            try:
                verified = float(meta['verified_at'])
            except (ValueError, TypeError, KeyError):
                raise NativeStoreError('native_identity_unverified') from None
            if (meta.get('pause_code') or not math.isfinite(verified) or not 0 < verified <= now + 5
                    or now - verified > 120 or identity is None or identity[0] != 'pinned'
                    or str(identity[1]) != row['identity_revision']):
                raise NativeStoreError('native_identity_unverified')
            if self._semantic_conflicts(db, candidate):
                raise NativeStoreError('native_semantic_conflict')
            audit = {'version': 1, 'operation': 'request_ui_recheck_after_read_fix', 'source_id': source_id,
                'operator_reason': operator_reason, 'old_state': row['state'], 'old_hold_code': row['hold_code'],
                'old_attempts': row['attempts'], 'source_first_seen': row['first_seen'], 'requested_at': float(now),
                'prior_audit_key': 'ui_recheck_v1:' + source_id, 'maximum_this_operation_requests': 1,
                'maximum_explicit_rechecks_including_prior': 2, 'old_hold_at': None,
                'hold_timestamp_was_not_recorded': True}
            db.execute('INSERT INTO meta(key,value) VALUES(?,?)', (audit_key, _dump(audit)))
            db.execute("UPDATE sources SET state='fresh',hold_code=NULL,claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=?", (source_id,))
            return {'source_id': source_id, 'status': 'fixed_ui_read_recheck_requested',
                'previous_hold_code': expected_old_code, 'attempts_preserved': 2,
                'maximum_this_operation_requests': 1, 'audit_recorded': True, 'dispatchable': False}

    def _prequeue_identity(self, db, row, *, fresh=False):
        """Validate immutable/pinned scope without adopting unknown work."""
        if (row is None or row['historical'] or row['plan'] is not None
                or row['task_id'] is not None or row['reference_id'] is not None):
            raise NativeStoreError('native_prequeue_not_unspent')
        try:
            candidate = json.loads(row['candidate'])
            immutable = {key: candidate[key] for key in ('namespace','recipient_uid','sender_uid',
                'sender_sec_uid','sender_profile_url','receiver_profile_url','video_id','native_comment_id','content')}
            if (candidate['recipient_uid'] != self.receiver_uid
                    or candidate['receiver_profile_url'] != self.receiver_profile_url
                    or canonical_profile(candidate['sender_sec_uid']) != candidate['sender_profile_url']
                    or _digest(immutable) != row['immutable_hash']):
                raise ValueError()
            pin = db.execute('SELECT state,revision FROM identities WHERE uid=? AND sec=?',
                (candidate['sender_uid'],candidate['sender_sec_uid'])).fetchone()
            if pin is None or pin[0] != 'pinned' or str(pin[1]) != row['identity_revision']:
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise NativeStoreError('native_identity_unverified') from None
        if self._semantic_conflicts(db,candidate):
            raise NativeStoreError('native_semantic_conflict')
        if fresh:
            now = self._clock()
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','verified_at')"))
            try: verified = float(meta['verified_at'])
            except (ValueError,TypeError,KeyError): verified = 0
            if (type(now) not in (int,float) or not math.isfinite(now) or not math.isfinite(verified)
                    or set(meta) != {'pause_code','verified_at'} or meta.get('pause_code')
                    or not 0 < verified <= now+5 or now-verified > 120):
                raise NativeStoreError('native_identity_unverified')
        return candidate

    def record_prequeue_intervention(self, source_id, claim_token, *, run_id, stage):
        """Trusted runtime only: an actual READ raised human intervention.

        UI readers cannot create Queue/model/import/send work. No generic
        collect/poll exception or expired lease is accepted as this evidence.
        """
        if (type(source_id) is not str or not re.fullmatch(r'native:[a-f0-9]{64}',source_id)
                or type(claim_token) is not str or not re.fullmatch(r'[a-f0-9]{32}',claim_token)
                or type(run_id) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',run_id)
                or type(stage) is not str or stage not in {'original_ui_lookup','choice_ui_read'}):
            raise NativeStoreError('native_interruption_receipt_invalid')
        now = self._clock()
        if type(now) not in (int,float) or not math.isfinite(now) or now <= 0:
            raise NativeStoreError('native_interruption_receipt_invalid')
        fingerprint = hashlib.sha256(claim_token.encode()).hexdigest()
        with self._connection(write=True) as db:
            row = db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
            if (row is None or row['state'] != 'claimed' or row['claim_token'] != claim_token
                    or row['claim_owner'] != run_id):
                raise NativeStoreError('native_claim_conflict')
            self._prequeue_identity(db,row)
            key = 'prequeue_intervention_v1:'+source_id+':'+str(row['attempts'])
            if db.execute('SELECT 1 FROM meta WHERE key=?',(key,)).fetchone():
                raise NativeStoreError('native_interruption_already_recorded')
            receipt_id = _digest({'source_id':source_id,'attempts':row['attempts'],
                'claim_fingerprint':fingerprint,'run_id':run_id,'stage':stage})
            receipt = {'version':1,'operation':'record_prequeue_intervention','receipt_id':receipt_id,
                'source_id':source_id,'attempts':row['attempts'],'run_id':run_id,'stage':stage,
                'claim_fingerprint':fingerprint,'immutable_hash':row['immutable_hash'],
                'identity_revision':row['identity_revision'],'source_first_seen':row['first_seen'],
                'recorded_at':float(now),'evidence_scope':'trusted_read_before_queue_return'}
            db.execute('INSERT INTO meta VALUES(?,?)',(key,_dump(receipt)))
            db.execute("UPDATE sources SET state='held',hold_code='native_prequeue_intervention',claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=?",(source_id,))
            return {'source_id':source_id,'receipt_id':receipt_id,'status':'prequeue_intervention_recorded',
                'attempts_preserved':row['attempts'],'dispatchable':False}

    def request_prequeue_resume(self, source_id, *, receipt_id, expected_attempts, operator_reason):
        """One explicit recovery per source, only with a runtime read receipt."""
        if (type(source_id) is not str or not re.fullmatch(r'native:[a-f0-9]{64}',source_id)
                or type(receipt_id) is not str or not re.fullmatch(r'[a-f0-9]{64}',receipt_id)
                or type(expected_attempts) is not int or expected_attempts <= 0
                or operator_reason != 'explicit_operator_after_prequeue_intervention'):
            raise NativeStoreError('native_prequeue_resume_invalid')
        with self._connection(write=True) as db:
            used = 'prequeue_resume_v1:'+source_id
            if db.execute('SELECT 1 FROM meta WHERE key=?',(used,)).fetchone():
                raise NativeStoreError('native_prequeue_resume_already_requested')
            row = db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
            if (row is None or row['state'] != 'held' or row['hold_code'] != 'native_prequeue_intervention'
                    or row['attempts'] != expected_attempts):
                raise NativeStoreError('native_interruption_receipt_required')
            saved = db.execute('SELECT value FROM meta WHERE key=?',
                ('prequeue_intervention_v1:'+source_id+':'+str(expected_attempts),)).fetchone()
            try: receipt = json.loads(saved[0]) if saved else {}
            except (ValueError,TypeError): receipt = {}
            if (type(receipt) is not dict or receipt.get('operation') != 'record_prequeue_intervention' or receipt.get('source_id') != source_id
                    or receipt.get('receipt_id') != receipt_id or receipt.get('attempts') != expected_attempts
                    or receipt.get('immutable_hash') != row['immutable_hash']
                    or receipt.get('identity_revision') != row['identity_revision']
                    or receipt.get('source_first_seen') != row['first_seen']):
                raise NativeStoreError('native_interruption_receipt_required')
            self._prequeue_identity(db,row,fresh=True)
            audit = {'version':1,'operation':'request_prequeue_resume','source_id':source_id,
                'receipt_id':receipt_id,'attempts_preserved':row['attempts'],'requested_at':self._clock(),
                'operator_reason':operator_reason,'maximum_per_source':1}
            db.execute('INSERT INTO meta VALUES(?,?)',(used,_dump(audit)))
            db.execute("UPDATE sources SET state='fresh',hold_code=NULL,claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=?",(source_id,))
            return {'source_id':source_id,'status':'prequeue_resume_requested',
                'attempts_preserved':row['attempts'],'dispatchable':False}

    def prequeue_recovery_status(self, source_id):
        """Fixed read-only discovery; receipt presence is not authorization."""
        if type(source_id) is not str or not re.fullmatch(r'native:[a-f0-9]{64}',source_id):
            raise NativeStoreError('native_prequeue_resume_invalid')
        with self._connection() as db:
            row=db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
            if row is None:return None
            saved=db.execute('SELECT value FROM meta WHERE key=?',
                ('prequeue_intervention_v1:'+source_id+':'+str(row['attempts']),)).fetchone()
            try:value=json.loads(saved[0]) if saved else None
            except (ValueError,TypeError):value=None
            receipt=None
            if (type(value) is dict and type(value.get('version')) is int and value['version']==1
                    and value.get('operation')=='record_prequeue_intervention' and value.get('source_id')==source_id
                    and type(value.get('attempts')) is int and value['attempts']==row['attempts']
                    and type(value.get('receipt_id')) is str and re.fullmatch(r'[a-f0-9]{64}',value['receipt_id'])
                    and value.get('stage') in ('original_ui_lookup','choice_ui_read')
                    and type(value.get('recorded_at')) in (int,float) and math.isfinite(value['recorded_at'])
                    and value.get('immutable_hash')==row['immutable_hash']
                    and value.get('identity_revision')==row['identity_revision']
                    and value.get('source_first_seen')==row['first_seen']):
                receipt={key:value[key] for key in ('receipt_id','attempts','stage','recorded_at')}
            return {'source_id':source_id,'state':row['state'],'hold_code':row['hold_code'],
                'attempts':row['attempts'],'historical':bool(row['historical']),
                'has_plan':row['plan'] is not None,'has_task':row['task_id'] is not None,
                'has_reference':row['reference_id'] is not None,'receipt':receipt,
                'resume_requested':db.execute('SELECT 1 FROM meta WHERE key=?',('prequeue_resume_v1:'+source_id,)).fetchone() is not None,
                'legacy_review_used':db.execute('SELECT 1 FROM meta WHERE key=?',('legacy_prequeue_review_v1:'+source_id,)).fetchone() is not None,
                'dispatchable':False}

    def _legacy_prequeue_snapshot(self, db, source_id):
        row = db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
        now = self._clock()
        if (row is None or type(now) not in (int,float) or not math.isfinite(now)
                or row['attempts'] != 3 or row['has_at'] != 0 or row['has_reply'] != 1
                or not (row['state']=='claimed' or row['state']=='held' and row['hold_code']=='native_unknown_lease')
                or type(row['lease_until']) not in (int,float) or not math.isfinite(row['lease_until'])
                or row['lease_until'] > now):
            raise NativeStoreError('native_legacy_review_not_allowed')
        candidate = self._prequeue_identity(db,row)
        audit_hashes = []
        for prefix,operation,attempt in (('ui_recheck_v1:','request_ui_recheck',1),
                ('ui_read_fix_recheck_v1:','request_ui_recheck_after_read_fix',2)):
            prior = db.execute('SELECT value FROM meta WHERE key=?',(prefix+source_id,)).fetchone()
            try: value = json.loads(prior[0]) if prior else {}
            except (ValueError,TypeError): value = {}
            if (type(value) is not dict or value.get('operation') != operation or value.get('source_id') != source_id
                    or value.get('old_attempts') != attempt or value.get('source_first_seen') != row['first_seen']):
                raise NativeStoreError('native_legacy_review_not_allowed')
            audit_hashes.append(_digest(value))
        return {'source_id':source_id,'state':row['state'],'hold_code':row['hold_code'],
            'attempts':row['attempts'],'immutable_hash':row['immutable_hash'],'identity_revision':row['identity_revision'],
            'first_seen':row['first_seen'],'lease_until':row['lease_until'],
            'claim_fingerprint':hashlib.sha256((row['claim_token'] or '').encode()).hexdigest(),
            'prior_audit_hashes':audit_hashes,'candidate':candidate}

    def legacy_prequeue_review_snapshot(self, source_id):
        if type(source_id) is not str or not re.fullmatch(r'native:[a-f0-9]{64}',source_id):
            raise NativeStoreError('native_legacy_review_not_allowed')
        with self._connection() as db:
            if db.execute('SELECT 1 FROM meta WHERE key=?',('legacy_prequeue_review_v1:'+source_id,)).fetchone():
                raise NativeStoreError('native_legacy_review_already_used')
            return self._legacy_prequeue_snapshot(db,source_id)

    def _commit_legacy_prequeue_resume(self, permit):
        if type(permit) is not _LegacyPrequeuePermit or permit.store is not self:
            raise NativeStoreError('native_legacy_review_required')
        snapshot,review = permit.snapshot,permit.review
        _safe_json(review)
        if (type(review) is not dict or set(review) != {'operation','reviewed_at','choice_id','root_task_id',
                'ui_reference_id','knowledge_base_id','scope_digest','cross_ledger_digest'}
                or review.get('operation') != 'owner_fresh_legacy_prequeue_review'):
            raise NativeStoreError('native_legacy_review_required')
        with self._connection(write=True) as db:
            source_id = snapshot['source_id']; key = 'legacy_prequeue_review_v1:'+source_id
            if db.execute('SELECT 1 FROM meta WHERE key=?',(key,)).fetchone():
                raise NativeStoreError('native_legacy_review_already_used')
            current = self._legacy_prequeue_snapshot(db,source_id)
            if current != snapshot:
                raise NativeStoreError('native_legacy_review_scope_changed')
            row = db.execute('SELECT * FROM sources WHERE source_id=?',(source_id,)).fetchone()
            self._prequeue_identity(db,row,fresh=True)
            now = self._clock()
            if (type(review['reviewed_at']) not in (int,float) or not math.isfinite(review['reviewed_at'])
                    or not -5 <= now-review['reviewed_at'] <= 120):
                raise NativeStoreError('native_legacy_review_expired')
            audit = {'version':1,'operation':'legacy_prequeue_review_resume','source_id':source_id,
                'attempts_preserved':row['attempts'],'immutable_hash':row['immutable_hash'],
                'prior_audit_hashes':snapshot['prior_audit_hashes'],'review':review,'requested_at':now,
                'evidence_boundary':'current_owner_review_not_historical_interruption_receipt'}
            db.execute('INSERT INTO meta VALUES(?,?)',(key,_dump(audit)))
            db.execute("UPDATE sources SET state='fresh',hold_code=NULL,claim_owner=NULL,claim_token=NULL,lease_until=NULL WHERE source_id=?",(source_id,))
            return {'source_id':source_id,'status':'legacy_prequeue_review_resumed',
                'attempts_preserved':row['attempts'],'dispatchable':False}

    def recoverable_ui_plans(self):
        with self._connection() as db:
            return [self._view(db, row) for row in db.execute("SELECT * FROM sources WHERE state='planned' AND plan IS NOT NULL ORDER BY first_seen")]

    def pending_count(self):
        with self._connection() as db:
            return db.execute("SELECT COUNT(*) FROM sources WHERE state IN ('fresh','claimed','planned')").fetchone()[0]

    def safe_status(self):
        with self._connection() as db:
            counts = dict(db.execute("SELECT state,COUNT(*) FROM sources GROUP BY state"))
            channels = [{"channel": r["channel"], "baseline_done": bool(r["baseline_done"]), "backlog_active": bool(r["active"])} for r in db.execute("SELECT * FROM channels ORDER BY channel")]
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('pause_code','armed_at')"))
            return {"counts": counts, "channels": channels, "armed": bool(meta["armed_at"]), "pause_code": meta["pause_code"] or None,
                    "unavailable_notices": db.execute("SELECT COUNT(*) FROM notice_skips").fetchone()[0]}
