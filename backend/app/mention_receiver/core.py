"""Parse public mention evidence into a separate SQLite inbox.

This module uses only the standard library.  A ``received`` result means that
the inbox has enough evidence to display a proposed command.  It never means
that a video has been downloaded, analysed, or added to a knowledge base.

Notification identities are local fingerprints, not Douyin comment IDs.  The
caller must supply visible card text without relative-time labels, and stable
card semantics (e.g. the associated public video URL or thumbnail URL path).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


STATUSES = frozenset({"pending", "retryable", "received", "needs_review"})
_PREVIEW_FLAGS = {
    "id_quality": "local_fingerprint", "preview_only": True, "import_enabled": False,
}
_ACTION = r"(?:加入(?:到|至)?|导入(?:到|至)?|上传(?:到|至))"
_QUOTES = (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("「", "」"), ("『", "』"))
_TARGET = re.compile(
    _ACTION + r"\s*(?:名为\s*|知识库\s*[:：]?\s*)?(?:"
    + "|".join(re.escape(left) + r"([^\r\n" + re.escape(left + right) + r"]*)" + re.escape(right)
               for left, right in _QUOTES)
    + ")"
)
_ACTION_RE = re.compile(_ACTION)
_MARKER = re.compile(r"测试编号\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,99})")
_QUOTE_CHARS = "“”‘’\"'「」『』"
_TIME_KEYS = {"time", "timestamp", "created_at", "updated_at", "seen_at", "relative_time", "date", "datetime", "age", "时间", "日期"}
_SECRET_KEYS = {"cookie", "cookies", "authorization", "password", "access_token", "refresh_token", "session_token", "storage_state", "credentials", "验证码", "密码"}
_QUARANTINE_DETAIL = "incomplete_candidate_quarantine"
_QUARANTINE_REASON_CODE = "incomplete_notification_fingerprint_v1"
_QUARANTINE_REASON = "通知卡片缺少稳定语义指纹；保留原始记录，停止自动重试，等待人工核对"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(namespace: str, parts: Any) -> str:
    return namespace + ":" + hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_public(value: Any, *, forbid_time: bool = False) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key).casefold()
            if key_name in _SECRET_KEYS:
                raise ValueError("The inbox accepts public evidence only; credential fields are forbidden")
            if forbid_time and (key_name in _TIME_KEYS or key_name.endswith("_timestamp")):
                raise ValueError("Time fields must not be part of a notification fingerprint")
            _check_public(item, forbid_time=forbid_time)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_public(item, forbid_time=forbid_time)


def _profile_url(value: str) -> str:
    """Keep only a public profile identity, excluding URL query credentials."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A public Douyin profile URL is required")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"www.douyin.com", "douyin.com"}:
        raise ValueError("Expected a public douyin.com profile URL")
    if parsed.username or parsed.password or not re.fullmatch(r"/user/[^/]+/?", parsed.path):
        raise ValueError("Expected a public Douyin user profile path")
    # /user/self is account-relative and cannot identify a sender across logins.
    if parsed.path.rstrip("/").endswith("/self"):
        raise ValueError("An account-relative /user/self URL is not a public identity")
    return urlunsplit(("https", "www.douyin.com", parsed.path.rstrip("/"), "", ""))


def _fingerprint(value: str | Mapping[str, Any] | None) -> str:
    if value is None or value == "" or value == {}:
        return ""
    if not isinstance(value, (str, Mapping)):
        raise TypeError("semantic_fingerprint must be public text or a mapping")
    _check_public(value, forbid_time=True)
    return value if isinstance(value, str) else _json(dict(value))


def notification_identity(source_profile_url: str, exact_text: str,
                          semantic_fingerprint: str | Mapping[str, Any] | None) -> str:
    if not isinstance(exact_text, str) or not exact_text.strip():
        raise ValueError("Exact visible notification text is required")
    return _hash("notification", [_profile_url(source_profile_url), exact_text, _fingerprint(semantic_fingerprint)])


def event_identity(sender_profile_url: str, video_id: str, command_text: str) -> str:
    if not isinstance(video_id, str) or not re.fullmatch(r"\d{5,30}", video_id):
        raise ValueError("A complete public video ID is required")
    if not isinstance(command_text, str) or not command_text.strip():
        raise ValueError("The complete command text is required")
    return _hash("event", [_profile_url(sender_profile_url), video_id, command_text])


@dataclass(frozen=True)
class CommandParse:
    status: str
    target_library_name: str | None = None
    marker: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **_PREVIEW_FLAGS}


def parse_command(text: str) -> CommandParse:
    """Accept one explicit paired-quoted target, otherwise request review.

    Requiring paired quotes deliberately avoids guessing where an unquoted
    knowledge-base name ends. A parsed command is still only an inbox preview.
    """
    if not isinstance(text, str) or not text.strip():
        return CommandParse("needs_review", reason="缺少完整指令")
    markers = _MARKER.findall(text)
    marker = markers[0] if len(markers) == 1 else None
    targets = list(_TARGET.finditer(text))
    if len(targets) != 1:
        return CommandParse("needs_review", marker=marker, reason="需要一个使用成对引号的明确知识库名称")
    match = targets[0]
    name = next(value for value in match.groups() if value is not None).strip()
    if not name:
        return CommandParse("needs_review", marker=marker, reason="知识库名称为空")
    prefix = text[:match.start()]
    if not re.search(r"(?:这个|这条|本|该|此)\s*视频", prefix):
        return CommandParse("needs_review", marker=marker, reason="指令未明确引用这个视频，需人工核对")
    remaining = text[:match.start()] + " " + text[match.end():]
    tail = text[match.end():]
    # A second unquoted target (or mixed/missing closing quote) is never guessed.
    if _ACTION_RE.search(remaining) or any(char in tail for char in _QUOTE_CHARS):
        return CommandParse("needs_review", marker=marker, reason="出现多个目标或额外引号，需人工核对")
    tail_before_marker = re.split(r"测试编号", tail, maxsplit=1)[0]
    if re.match(r"[\s，,、]*(?:和|及|或|以及|并|再)", tail_before_marker) or re.match(r"\s*、", tail_before_marker):
        return CommandParse("needs_review", marker=marker, reason="可能有多个知识库目标，需人工核对")
    if re.search(r"(?:不要|别|取消|禁止|不必|无需)", prefix):
        return CommandParse("needs_review", marker=marker, reason="指令包含否定，需人工核对")
    if len(markers) > 1:
        return CommandParse("needs_review", marker=marker, reason="出现多个测试编号，需人工核对")
    return CommandParse("ready", target_library_name=name, marker=marker)


def match_library(name: str | None, libraries: Iterable[Mapping[str, Any]] | None) -> dict[str, Any]:
    matches = [dict(item) for item in (libraries or []) if item.get("name") == name] if name else []
    if len(matches) != 1:
        return {"status": "needs_review", "library_id": None,
                "reason": "知识库不存在或未提供知识库清单" if not matches else "存在同名知识库，需人工选择"}
    library_id = matches[0].get("id")
    if library_id is None or library_id == "" or isinstance(library_id, bool):
        return {"status": "needs_review", "library_id": None, "reason": "匹配的知识库缺少有效编号"}
    return {"status": "ready", "library_id": library_id, "reason": None}


@dataclass(frozen=True)
class NotificationCandidate:
    notification_key: str
    source_profile_url: str
    exact_text: str
    semantic_fingerprint: str
    status: str
    attempts: int
    event_key: str | None
    details: dict[str, Any]
    last_error: str | None
    identity_note: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), **_PREVIEW_FLAGS}


@dataclass(frozen=True)
class InboxEvent:
    event_key: str
    sender_profile_url: str
    video_id: str
    command_text: str
    target_library_name: str | None
    library_id: Any
    marker: str | None
    status: str
    reason: str | None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    @property
    def video_title(self) -> str | None:
        return self.metadata.get("video_title")

    @property
    def author_display_name(self) -> str | None:
        return self.metadata.get("author_display_name")

    @property
    def source_url(self) -> str | None:
        return self.metadata.get("source_url")

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "video_title": self.video_title,
                "author_display_name": self.author_display_name, "source_url": self.source_url,
                **_PREVIEW_FLAGS}


class SQLiteInbox:
    """Durable inbox in an explicitly supplied database, isolated from the app.

    Each operation opens its own connection. SQLite unique constraints plus
    BEGIN IMMEDIATE transactions make duplicate observations/completions safe
    across restarts and cooperating processes. This is local deduplication,
    not a platform-level exactly-once delivery guarantee.
    """

    def __init__(self, db_path: str | Path):
        if str(db_path) == ":memory:":
            raise ValueError("A persistent inbox database path is required")
        self.db_path = Path(db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mention_candidates (
                notification_key TEXT PRIMARY KEY,
                source_profile_url TEXT NOT NULL, exact_text TEXT NOT NULL,
                semantic_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','retryable','received','needs_review')),
                attempts INTEGER NOT NULL DEFAULT 0, event_key TEXT,
                details_json TEXT NOT NULL DEFAULT '{}', last_error TEXT, identity_note TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(event_key) REFERENCES mention_events(event_key))""")
            db.execute("""CREATE TABLE IF NOT EXISTS mention_events (
                event_key TEXT PRIMARY KEY, sender_profile_url TEXT NOT NULL,
                video_id TEXT NOT NULL, command_text TEXT NOT NULL,
                target_library_name TEXT, library_id_json TEXT, marker TEXT,
                status TEXT NOT NULL CHECK(status IN ('received','needs_review')),
                reason TEXT, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL)""")

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _candidate(row: sqlite3.Row) -> NotificationCandidate:
        data = dict(row)
        data["details"] = json.loads(data.pop("details_json"))
        return NotificationCandidate(**data)

    @staticmethod
    def _event(row: sqlite3.Row) -> InboxEvent:
        data = dict(row)
        data["library_id"] = json.loads(data.pop("library_id_json"))
        data["metadata"] = json.loads(data.pop("metadata_json"))
        return InboxEvent(**data)

    @staticmethod
    def _required(db: sqlite3.Connection, key: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM mention_candidates WHERE notification_key = ?", (key,)).fetchone()
        if row is None:
            raise KeyError("Persist the notification before updating its status")
        return row

    @staticmethod
    def _is_quarantined(row: sqlite3.Row) -> bool:
        """Only our explicit audit marker makes an incomplete row terminal."""
        audit = json.loads(row["details_json"]).get(_QUARANTINE_DETAIL)
        return (row["status"] == "needs_review" and row["event_key"] is None
                and not row["semantic_fingerprint"].strip()
                and isinstance(audit, dict)
                and audit.get("reason_code") == _QUARANTINE_REASON_CODE
                and audit.get("reason") == _QUARANTINE_REASON
                and audit.get("previous_status") in {"pending", "retryable"}
                and audit.get("quarantined_at") == row["updated_at"]
                and (row["identity_note"] or "").endswith(_QUARANTINE_REASON))

    def quarantine_incomplete_candidates(self) -> list[NotificationCandidate]:
        """Preserve incomplete startup cards without retrying or guessing links.

        Only unlinked pending/retryable rows with a blank semantic fingerprint
        are moved to review. Existing details and their original status/time
        remain in the audit record. Repeated calls make no further changes.
        Complete events and other review decisions are deliberately untouched.
        """
        quarantined = []
        with self._transaction() as db:
            rows = db.execute("""SELECT * FROM mention_candidates
                WHERE event_key IS NULL AND status IN ('pending','retryable')
                ORDER BY created_at, notification_key""").fetchall()
            for row in rows:
                if row["semantic_fingerprint"].strip() or self._is_quarantined(row):
                    continue
                now = _now()
                details = json.loads(row["details_json"])
                audit = {
                    "reason_code": _QUARANTINE_REASON_CODE,
                    "reason": _QUARANTINE_REASON,
                    "quarantined_at": now,
                    "previous_status": row["status"],
                    "previous_updated_at": row["updated_at"],
                    "previous_identity_note": row["identity_note"],
                }
                # A caller's earlier public metadata must not be overwritten.
                if _QUARANTINE_DETAIL in details:
                    audit["previous_value"] = details[_QUARANTINE_DETAIL]
                details[_QUARANTINE_DETAIL] = audit
                note = ((row["identity_note"] + "；") if row["identity_note"] else "") + _QUARANTINE_REASON
                db.execute("""UPDATE mention_candidates SET status='needs_review',
                    details_json=?,identity_note=?,updated_at=? WHERE notification_key=?""",
                           (_json(details), note, now, row["notification_key"]))
                quarantined.append(self._candidate(self._required(db, row["notification_key"])))
        return quarantined

    def observe_notification(self, source_profile_url: str, exact_text: str,
                             semantic_fingerprint: str | Mapping[str, Any] | None = None) -> NotificationCandidate:
        source = _profile_url(source_profile_url)
        fingerprint = _fingerprint(semantic_fingerprint)
        key = notification_identity(source, exact_text, semantic_fingerprint)
        now = _now()
        note = None if fingerprint.strip() else "缺少稳定卡片语义指纹；同文通知可能碰撞，需人工核对"
        with self._transaction() as db:
            db.execute("""INSERT OR IGNORE INTO mention_candidates
                (notification_key,source_profile_url,exact_text,semantic_fingerprint,status,identity_note,created_at,updated_at)
                VALUES (?,?,?,?,'pending',?,?,?)""", (key, source, exact_text, fingerprint, note, now, now))
            row = self._required(db, key)
        return self._candidate(row)

    def mark_retryable(self, notification_key: str, error: str) -> NotificationCandidate:
        with self._transaction() as db:
            row = self._required(db, notification_key)
            # A later navigation hiccup must not undo a completed result.
            if row["event_key"] is None and not self._is_quarantined(row):
                db.execute("""UPDATE mention_candidates SET status='retryable',
                    attempts=attempts+1,last_error=?,updated_at=? WHERE notification_key=?""",
                           (str(error)[:1000], _now(), notification_key))
            row = self._required(db, notification_key)
        return self._candidate(row)

    def complete_notification(self, notification_key: str, *,
                              sender_profile_url: str | None = None,
                              video_id: str | None = None,
                              command_text: str | None = None,
                              libraries: Iterable[Mapping[str, Any]] | None = None,
                              video_title: str | None = None,
                              author_display_name: str | None = None,
                              source_url: str | None = None,
                              extra_metadata: Mapping[str, Any] | None = None) -> NotificationCandidate:
        """Persist details and result atomically; return the durable candidate.

        Missing detail fields remain retryable. Complete but ambiguous commands
        become saved review events. Calling this again with the same evidence
        cannot create another event or automatically promote a review decision.
        """
        metadata = dict(extra_metadata or {})
        metadata.update({key: value for key, value in {
            "video_title": video_title, "author_display_name": author_display_name,
            "source_url": source_url,
        }.items() if value is not None})
        _check_public(metadata)
        details = {**metadata, "sender_profile_url": sender_profile_url,
                   "video_id": video_id, "command_text": command_text}
        try:
            key = event_identity(sender_profile_url, video_id, command_text)
            sender = _profile_url(sender_profile_url)
            validation_error = None
        except (ValueError, TypeError) as exc:
            key, sender, validation_error = None, None, str(exc)
        with self._transaction() as db:
            row = self._required(db, notification_key)
            if self._is_quarantined(row):
                return self._candidate(row)
            if key is None:
                if row["event_key"] is None:
                    db.execute("""UPDATE mention_candidates SET status='retryable',attempts=attempts+1,
                        details_json=?,last_error=?,updated_at=? WHERE notification_key=?""",
                               (_json(details), validation_error, _now(), notification_key))
            elif row["event_key"] and row["event_key"] != key:
                conflict_details = json.loads(row["details_json"])
                conflict_details["conflicting_detail"] = details
                db.execute("""UPDATE mention_candidates SET status='needs_review',details_json=?,
                    last_error=?,updated_at=? WHERE notification_key=?""",
                           (_json(conflict_details), "同一通知候选关联到了不同事件，保留原结果并等待人工核对", _now(), notification_key))
            elif row["event_key"] is None:
                parsed = parse_command(command_text)
                matched = match_library(parsed.target_library_name, libraries) if parsed.status == "ready" else None
                reason = parsed.reason or (matched["reason"] if matched else None) or row["identity_note"]
                status = "received" if parsed.status == "ready" and matched["status"] == "ready" and not row["identity_note"] else "needs_review"
                db.execute("""INSERT OR IGNORE INTO mention_events
                    (event_key,sender_profile_url,video_id,command_text,target_library_name,library_id_json,
                     marker,status,reason,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (key, sender, video_id, command_text, parsed.target_library_name,
                            _json(matched["library_id"] if matched else None), parsed.marker,
                            status, reason, _json(metadata), _now()))
                # Another notification may already have saved the same event.
                persisted = db.execute("SELECT * FROM mention_events WHERE event_key=?", (key,)).fetchone()
                candidate_status = "needs_review" if row["identity_note"] else persisted["status"]
                db.execute("""UPDATE mention_candidates SET status=?,attempts=attempts+1,event_key=?,
                    details_json=?,last_error=NULL,updated_at=? WHERE notification_key=?""",
                           (candidate_status, key, _json(details), _now(), notification_key))
            row = self._required(db, notification_key)
        return self._candidate(row)

    def list_candidates(self, statuses: Iterable[str] | None = None) -> list[NotificationCandidate]:
        selected = tuple(statuses) if statuses is not None else ()
        if any(status not in STATUSES for status in selected):
            raise ValueError("Unknown inbox status")
        query = "SELECT * FROM mention_candidates"
        if selected:
            query += " WHERE status IN (" + ",".join("?" for _ in selected) + ")"
        query += " ORDER BY created_at, notification_key"
        with self._transaction() as db:
            rows = db.execute(query, selected).fetchall()
        return [self._candidate(row) for row in rows]

    def list_events(self) -> list[InboxEvent]:
        with self._transaction() as db:
            rows = db.execute("SELECT * FROM mention_events ORDER BY created_at,event_key").fetchall()
        return [self._event(row) for row in rows]
