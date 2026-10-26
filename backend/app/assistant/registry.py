"""Local administration and authorization registry, independent of task stores.

Verification here is a recorded local-admin/pairing decision, not proof of
Douyin account ownership. No URL-based delivery or public listener is created.
Other-computer transport remains unsupported; signed messages are a foundation
tested with isolated local devices, not a deployed multi-computer channel.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
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
import threading
import time
import unicodedata
import uuid

from app.mention_agent.contracts import Binding, public_profile
from app.storage_guard import (StorageGuardError, SqliteOwnershipError, guarded_sqlite_file,
                               assert_no_orphan_sqlite_files, verified_readonly_connection)


DEFAULT_PATH = Path(__file__).resolve().parents[1] / "storage/assistant/registry.sqlite3"
COMPONENT = "DouyinMind.assistant.registry.v1"
REQUIRED_MARKER_NAME = "registry-required.json"
REQUIRED_MARKER_COMPONENT = "DouyinMind.assistant.registry-required"
REQUIRED_MARKER_VERSION = 1
_ACTIONS = {"query": "can_query", "save": "can_import", "import": "can_import"}


class RegistryError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def normalize_alias(value):
    if not isinstance(value, str):
        raise RegistryError("invalid_alias")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise RegistryError("invalid_alias")
    value = " ".join(unicodedata.normalize("NFKC", value).strip().split()).casefold()
    if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
        raise RegistryError("invalid_alias")
    return value


def _text(value, maximum=128):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        raise RegistryError("invalid_text")
    return value.strip()


def _identity(value):
    value = _text(value)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise RegistryError("invalid_identity")
    return value


def _bool(value):
    if type(value) is not bool:
        raise RegistryError("explicit_boolean_required")
    return int(value)


def _profile(value):
    try:
        return public_profile(value)
    except (ValueError, TypeError, AttributeError):
        raise RegistryError("invalid_public_profile") from None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise RegistryError("invalid_utc_timestamp") from None


_TASK_BASE_FIELDS = frozenset({"queue_task_id", "device_id", "instance_id", "user_id", "binding_id",
    "knowledge_base_id", "video_id", "command_id", "reference_id", "payload_sha256"})
_TASK_FIELDS = _TASK_BASE_FIELDS | {"protocol_version", "action", "authorization_sha256",
                                  "issued_at_utc", "expires_at_utc", "nonce", "signature"}
_AUTHORIZATION_FIELDS = frozenset({"registry_id", "user_id", "device_id", "grant_id", "user_revision",
    "device_revision", "grant_revision", "grant_version", "binding_id", "instance_id", "knowledge_base_id",
    "action", "public_reply_allowed"})
_ALL_AUTHORIZATION_FIELDS = _AUTHORIZATION_FIELDS | {"query_scope"}
_OWNER_COLUMNS = ("grant_id", "user_id", "device_id", "query_all_local", "catalog_read_allowed",
    "public_reply_allowed", "active", "version", "auth_revision", "verified_by", "verified_at")
_OWNER_SCHEMA = """CREATE TABLE assistant_local_owner_access(
    grant_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES assistant_users(user_id),
    device_id TEXT NOT NULL REFERENCES assistant_devices(device_id),
    query_all_local INTEGER NOT NULL,catalog_read_allowed INTEGER NOT NULL,
    public_reply_allowed INTEGER NOT NULL,active INTEGER NOT NULL,
    version INTEGER NOT NULL,auth_revision INTEGER NOT NULL,verified_by TEXT NOT NULL,
    verified_at REAL NOT NULL,UNIQUE(user_id,device_id))"""


def payload_sha256(payload):
    try:
        return _digest(payload)
    except (ValueError, TypeError, RecursionError):
        raise RegistryError("invalid_task_payload") from None


def _task_metadata(metadata):
    if not isinstance(metadata, dict) or set(metadata) != _TASK_BASE_FIELDS:
        raise RegistryError("invalid_signed_task_fields")
    result = dict(metadata)
    for key in ("queue_task_id", "command_id", "reference_id"):
        value = _text(result[key], 256)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value):
            raise RegistryError("invalid_signed_task_identity")
    for key in ("device_id", "instance_id", "user_id", "binding_id"):
        _identity(result[key])
    if type(result["knowledge_base_id"]) is not int or result["knowledge_base_id"] <= 0:
        raise RegistryError("invalid_signed_task_library")
    if not isinstance(result["video_id"], str) or not re.fullmatch(r"[0-9]{5,30}", result["video_id"]):
        raise RegistryError("invalid_signed_task_video")
    if not isinstance(result["payload_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", result["payload_sha256"]):
        raise RegistryError("invalid_task_payload_hash")
    return result


def _validate_task_window(packet, current):
    issued, expires = _utc(packet["issued_at_utc"]), _utc(packet["expires_at_utc"])
    if type(current) not in (int, float) or not math.isfinite(current):
        raise RegistryError("invalid_task_clock")
    if not 0 < expires - issued <= 3600 or issued - current > 5 or current >= expires:
        raise RegistryError("signed_task_expired_or_not_yet_valid")
    return expires


def verify_signed_task(packet, credential, *, expected_device_id, expected_instance_id,
                       expected_user_id, payload, now=None):
    """Pure protocol verification. Replay persistence/execution fences remain separate."""
    if not isinstance(packet, dict) or set(packet) != _TASK_FIELDS:
        raise RegistryError("invalid_signed_task_fields")
    _task_metadata({key: packet[key] for key in _TASK_BASE_FIELDS})
    if (type(packet["protocol_version"]) is not int or packet["protocol_version"] != 1
            or not isinstance(packet["action"], str) or packet["action"] not in {"query", "import"}
            or not isinstance(packet["signature"], str) or not re.fullmatch(r"[0-9a-f]{64}", packet["signature"])
            or not isinstance(packet["authorization_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", packet["authorization_sha256"])
            or not isinstance(packet["nonce"], str) or not re.fullmatch(r"[0-9a-f]{32}", packet["nonce"])):
        raise RegistryError("invalid_signed_task_protocol")
    if (packet["device_id"] != expected_device_id or packet["instance_id"] != expected_instance_id
            or packet["user_id"] != expected_user_id):
        raise RegistryError("signed_task_destination_mismatch")
    current = time.time() if now is None else now
    _validate_task_window(packet, current)
    if packet["payload_sha256"] != payload_sha256(payload):
        raise RegistryError("signed_task_payload_changed")
    if not isinstance(credential, str) or not re.fullmatch(r"[0-9a-f]{64}", credential):
        raise RegistryError("device_authentication_failed")
    unsigned = {key: value for key, value in packet.items() if key != "signature"}
    expected = hmac.new(hashlib.sha256(credential.encode()).digest(),
                        canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, packet["signature"]):
        raise RegistryError("invalid_task_signature")
    return unsigned


def read_local_catalog(database_path):
    """Read candidate IDs/names only. Candidates do not grant any permission."""
    path = Path(database_path).resolve()
    if not path.is_file():
        raise RegistryError("catalog_unavailable")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            return [{"id": row[0], "name": row[1]}
                    for row in db.execute("SELECT id,name FROM knowledge_bases ORDER BY id")]
    except sqlite3.Error:
        raise RegistryError("catalog_unavailable") from None


class Registry:
    def __init__(self, path=DEFAULT_PATH, *, catalog_reader=None, database_path=None,
                 protected_paths=(), clock=None, offline_seconds=90, allow_create=True):
        if str(path) == ":memory:":
            raise RegistryError("persistent_registry_required")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.required_marker_path = self.path.parent / REQUIRED_MARKER_NAME
        if self.path == self.required_marker_path:
            raise RegistryError("protected_registry_marker_path")
        self._marker_required = False
        self._creating_schema = False
        self._bootstrap_identity = None
        self.clock = clock or time.time
        if type(offline_seconds) not in (int, float) or not 1 <= offline_seconds <= 3600:
            raise RegistryError("invalid_offline_window")
        self.offline_seconds = offline_seconds
        self.catalog_reader = catalog_reader or (
            (lambda: read_local_catalog(database_path)) if database_path is not None else lambda: [])
        self.protected_paths = tuple(Path(item).resolve() for item in
                                     (*protected_paths, *((database_path,) if database_path is not None else ())))
        self._lock = threading.RLock()
        if type(allow_create) is not bool:
            raise RegistryError("explicit_boolean_required")
        self._initialize(allow_create=allow_create)

    def _guard_location(self):
        if self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent:
            raise RegistryError("redirected_registry_path")
        for protected in self.protected_paths:
            if self.path == protected or self.path.is_relative_to(protected) or protected.is_relative_to(self.path):
                raise RegistryError("protected_registry_path")

    @contextmanager
    def _file_lifecycle(self):
        # Check protected locations before creating the coordination file.
        # The full file/sidecar guard runs only after this process/thread lock.
        self._guard_location()
        try:
            with guarded_sqlite_file(self.path):
                yield
        except StorageGuardError as error:
            code = str(error)
            if code == "unsafe_storage_guard_database":
                code = "unsafe_registry_file"
            raise RegistryError(code) from None

    def _guard(self):
        self._guard_location()
        for path in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.resolve() != path:
                raise RegistryError("unsafe_registry_file")
        self._guard_required_marker(required=self._marker_required)

    def _guard_required_marker(self, *, required):
        marker = self.required_marker_path
        if marker.parent.resolve() != marker.parent:
            raise RegistryError("unsafe_registry_required_marker")
        for protected in self.protected_paths:
            if marker == protected or marker.is_relative_to(protected) or protected.is_relative_to(marker):
                raise RegistryError("protected_registry_marker_path")
        try:
            info = marker.lstat()
        except FileNotFoundError:
            if required:
                raise RegistryError("registry_required_marker_missing") from None
            return False
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or marker.resolve() != marker
                or info.st_size > 2048):
            raise RegistryError("unsafe_registry_required_marker")
        try:
            with marker.open("rb") as source:
                opened = os.fstat(source.fileno())
                if (opened.st_dev != info.st_dev or opened.st_ino != info.st_ino
                        or opened.st_nlink != 1):
                    raise RegistryError("unsafe_registry_required_marker")
                value = json.loads(source.read(2049).decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            raise RegistryError("invalid_registry_required_marker") from None
        if (not isinstance(value, dict) or set(value) != {"component", "version"}
                or value["component"] != REQUIRED_MARKER_COMPONENT
                or type(value["version"]) is not int or value["version"] != REQUIRED_MARKER_VERSION):
            raise RegistryError("invalid_registry_required_marker")
        return True

    def _ensure_required_marker(self):
        """Permanent fail-closed migration signal; never overwrite or remove it."""
        if self._guard_required_marker(required=False):
            self._marker_required = True
            return
        value = {"component": REQUIRED_MARKER_COMPONENT, "version": REQUIRED_MARKER_VERSION}
        try:
            descriptor = os.open(self.required_marker_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(descriptor, "wb") as output:
                info = os.fstat(output.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RegistryError("unsafe_registry_required_marker")
                output.write((canonical_json(value) + "\n").encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            self._guard_required_marker(required=True)
        except OSError:
            self._marker_required = True
            raise RegistryError("registry_required_marker_write_failed") from None
        self._marker_required = True
        self._guard_required_marker(required=True)

    def _initialize(self, *, allow_create):
        with self._lock:
            self._guard_location()
            if not self.path.parent.is_dir():
                if not allow_create:
                    raise RegistryError("registry_missing_or_empty")
                self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._file_lifecycle():
                self._initialize_locked(allow_create=allow_create)

    def _owner_check(self, db):
        """Stable format ownership only; never a mutable grant/permission check."""
        component = db.execute("SELECT value FROM registry_metadata WHERE key='component'").fetchone()
        required = db.execute("SELECT value FROM registry_metadata WHERE key='requires_marker'").fetchone()
        if component != (COMPONENT,) or required not in (None, ("1",)):
            return False
        if required == ("1",):
            # immutable precheck can discover this before rollback is allowed.
            # The recovery helper must then pass our normal marker guard.
            self._marker_required = True
        return True

    @contextmanager
    def _new_schema_connection(self, identity):
        """Bootstrap only the empty main created by this O_EXCL operation."""
        self._guard()
        info = self.path.lstat()
        if (info.st_size != 0 or (info.st_dev, info.st_ino) != identity):
            raise RegistryError("registry_bootstrap_file_changed")
        self._bootstrap_identity = identity
        self._creating_schema = True
        try:
            with self._db(write=True) as db:
                yield db
        finally:
            self._creating_schema = False
            self._bootstrap_identity = None

    def _initialize_locked(self, *, allow_create):
        with self._lock:
            self._guard()
            if self.path.exists() and self.path.stat().st_size:
                try:
                    with verified_readonly_connection(self.path, self._owner_check, guard=self._guard) as check:
                        row = check.execute("SELECT value FROM registry_metadata WHERE key='component'").fetchone()
                        marker_required = check.execute("SELECT value FROM registry_metadata WHERE key='requires_marker'").fetchone()
                    if row != (COMPONENT,):
                        raise RegistryError("foreign_registry_database")
                    if marker_required not in (None, ("1",)):
                        raise RegistryError("foreign_registry_database")
                except (sqlite3.Error, SqliteOwnershipError):
                    raise RegistryError("foreign_registry_database") from None
                # Opening an existing registry is validation, not a migration
                # or repair operation. In particular a GET cannot write DDL.
                self._marker_required = marker_required == ("1",)
                self._guard()
                return
            if self._guard_required_marker(required=False):
                # A permanent signal with a lost DB is not a fresh setup.
                raise RegistryError("registry_missing_after_initialization")
            if self.path.exists():
                # An existing empty file is not proof of ownership. Never
                # adopt it, including during an explicit create request.
                raise RegistryError("registry_missing_or_empty")
            if not allow_create:
                raise RegistryError("registry_missing_or_empty")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            assert_no_orphan_sqlite_files(self.path)
            try:
                descriptor = os.open(self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            except FileExistsError:
                # Another actor's new file cannot be our bootstrap token.
                raise RegistryError("registry_bootstrap_file_changed") from None
            try:
                created = os.fstat(descriptor)
                if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1:
                    raise RegistryError("unsafe_registry_file")
                identity = (created.st_dev, created.st_ino)
            finally:
                os.close(descriptor)
            with self._new_schema_connection(identity) as db:
                # executescript ends an existing transaction; begin explicitly
                # in the script so schema and metadata are committed together.
                db.executescript("""BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS registry_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS assistant_users(
                      user_id TEXT PRIMARY KEY,label TEXT NOT NULL,sender_profile_url TEXT NOT NULL,
                      receiver_profile_url TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
                      verification_status TEXT NOT NULL DEFAULT 'unverified',verification_method TEXT,
                      verified_by TEXT,verified_at REAL,default_device_id TEXT,
                      version INTEGER NOT NULL DEFAULT 1,auth_revision INTEGER NOT NULL DEFAULT 1,
                      UNIQUE(sender_profile_url,receiver_profile_url));
                    CREATE TABLE IF NOT EXISTS assistant_devices(
                      device_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES assistant_users(user_id),
                      instance_id TEXT NOT NULL,label TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
                      revoked INTEGER NOT NULL DEFAULT 0,verification_status TEXT NOT NULL DEFAULT 'unverified',
                      verification_method TEXT,verified_by TEXT,verified_at REAL,last_seen REAL,
                      credential_hash BLOB,version INTEGER NOT NULL DEFAULT 1,auth_revision INTEGER NOT NULL DEFAULT 1,
                      UNIQUE(user_id,instance_id));
                    CREATE TABLE IF NOT EXISTS assistant_grants(
                      grant_id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES assistant_users(user_id),
                      device_id TEXT NOT NULL REFERENCES assistant_devices(device_id),knowledge_base_id INTEGER NOT NULL,
                      aliases_json TEXT NOT NULL,is_default INTEGER NOT NULL DEFAULT 0,
                      can_query INTEGER NOT NULL,can_import INTEGER NOT NULL,public_reply_allowed INTEGER NOT NULL,
                      active INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
                      auth_revision INTEGER NOT NULL DEFAULT 1,legacy_binding_id TEXT,
                      UNIQUE(user_id,device_id,knowledge_base_id));
                    CREATE UNIQUE INDEX IF NOT EXISTS one_default_library ON assistant_grants(user_id,device_id)
                      WHERE active=1 AND is_default=1;
                    CREATE TABLE IF NOT EXISTS legacy_imports(
                      binding_id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,user_id TEXT NOT NULL,
                      device_id TEXT NOT NULL,grant_id TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS device_task_nonces(
                      device_id TEXT NOT NULL,nonce TEXT NOT NULL,expires_at REAL NOT NULL,
                      PRIMARY KEY(device_id,nonce));
                """)
                db.execute("INSERT OR IGNORE INTO registry_metadata VALUES('component',?)", (COMPONENT,))
                db.execute("INSERT OR IGNORE INTO registry_metadata VALUES('registry_id',?)", (uuid.uuid4().hex,))
                db.execute("INSERT OR IGNORE INTO registry_metadata VALUES('local_instance_id',?)", (uuid.uuid4().hex,))
                db.execute("INSERT OR IGNORE INTO registry_metadata VALUES('requires_marker','1')")
            self._ensure_required_marker()

    @contextmanager
    def _db(self, *, write=False):
        with self._lock, self._file_lifecycle():
            self._guard()
            if not self._creating_schema:
                try:
                    # A cached Registry also needs this after another writer
                    # crashes; constructor-only checks cannot recover that case.
                    with verified_readonly_connection(self.path, self._owner_check, guard=self._guard):
                        pass
                except (sqlite3.Error, SqliteOwnershipError):
                    raise RegistryError("foreign_registry_database") from None
                self._guard()
            db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=True, timeout=10, isolation_level=None)
            try:
                if self._creating_schema:
                    self._guard()
                    info = self.path.lstat()
                    if ((info.st_dev, info.st_ino) != self._bootstrap_identity
                            or info.st_size != 0):
                        raise RegistryError("registry_bootstrap_file_changed")
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA synchronous=FULL" if write else "PRAGMA query_only=ON")
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _required(db, table, column, value):
        row = db.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()
        if row is None:
            raise RegistryError("record_not_found")
        return dict(row)

    @property
    def local_instance_id(self):
        with self._db() as db:
            return db.execute("SELECT value FROM registry_metadata WHERE key='local_instance_id'").fetchone()[0]

    def catalog(self):
        try:
            raw = self.catalog_reader()
            result = []
            seen = set()
            for item in raw:
                if (not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] <= 0
                        or item["id"] in seen):
                    raise RegistryError("invalid_catalog")
                result.append({"id": item["id"], "name": _text(item.get("name"))})
                seen.add(item["id"])
            return result
        except RegistryError:
            raise
        except Exception:
            raise RegistryError("catalog_unavailable") from None

    @staticmethod
    def _owner_rows(db, *, prepare=False):
        table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='assistant_local_owner_access'").fetchone()
        version = db.execute("SELECT value FROM registry_metadata WHERE key='local_owner_access_schema'").fetchone()
        if table is None and version is None:
            if not prepare:
                return []
            db.execute(_OWNER_SCHEMA)
            db.execute("INSERT INTO registry_metadata VALUES('local_owner_access_schema','1')")
        elif table is None or version is None or version[0] != "1":
            raise RegistryError("invalid_local_owner_access_schema")
        if tuple(row[1] for row in db.execute("PRAGMA table_info(assistant_local_owner_access)")) != _OWNER_COLUMNS:
            raise RegistryError("invalid_local_owner_access_schema")
        rows=[dict(row) for row in db.execute("SELECT * FROM assistant_local_owner_access")]
        if any(any(type(row[key]) is not int or row[key] not in (0,1) for key in
                   ("query_all_local","catalog_read_allowed","public_reply_allowed","active"))
               or any(type(row[key]) is not int or row[key]<=0 for key in ("version","auth_revision")) for row in rows):
            raise RegistryError("invalid_local_owner_access_schema")
        return rows

    @staticmethod
    def _owner_binding_id(user, device, grant):
        return "all-local-" + _digest({"user_id": user["user_id"], "device_id": device["device_id"],
            "instance_id": device["instance_id"], "grant_id": grant["grant_id"],
            "user_revision": user["auth_revision"], "device_revision": device["auth_revision"],
            "grant_revision": grant["auth_revision"], "query_scope": "all_local"})[:40]

    def set_local_owner_access(self, user_id, device_id, *, query_all_local=False,
                               catalog_read_allowed=False, public_reply_allowed=False,
                               grant_current_libraries=False, verified_by):
        """Explicit local administrator declaration, never profile ownership proof.

        Query-all is independent of library grants and includes unfiled ready
        sources. The final flag grants only the catalog present at this explicit
        operation; subsequently created libraries gain no import grant by it.
        """
        flags = tuple(_bool(value) for value in
            (query_all_local, catalog_read_allowed, public_reply_allowed, grant_current_libraries))
        self._verification("local_admin", verified_by)
        if flags[3] and not flags[1]:
            raise RegistryError("catalog_owner_confirmation_required")
        if flags[2] and not (flags[0] or flags[1]):
            raise RegistryError("local_owner_scope_required")
        catalog = self.catalog() if flags[3] else []
        with self._db(write=True) as db:
            user = self._required(db,"assistant_users","user_id",user_id)
            device = self._required(db,"assistant_devices","device_id",device_id)
            local = db.execute("SELECT value FROM registry_metadata WHERE key='local_instance_id'").fetchone()[0]
            if device["user_id"] != user_id:
                raise RegistryError("device_owner_mismatch")
            if (device["instance_id"] != local or not user["active"] or user["verification_status"] != "verified"
                    or not device["enabled"] or device["revoked"] or device["verification_status"] != "verified"):
                raise RegistryError("local_owner_verified_device_required")
            owners = self._owner_rows(db,prepare=True)
            old = next((row for row in owners if row["user_id"]==user_id and row["device_id"]==device_id),None)
            active = int(bool(flags[0] or flags[1]))
            if old is None:
                db.execute("INSERT INTO assistant_local_owner_access VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    ("owner-"+uuid.uuid4().hex,user_id,device_id,*flags[:3],active,1,1,_text(verified_by),self.clock()))
            elif any(old[key] != value for key,value in zip(
                    ("query_all_local","catalog_read_allowed","public_reply_allowed","active"),(*flags[:3],active))):
                db.execute("UPDATE assistant_local_owner_access SET query_all_local=?,catalog_read_allowed=?,public_reply_allowed=?,active=?,version=version+1,auth_revision=auth_revision+1,verified_by=?,verified_at=? WHERE grant_id=?",
                    (*flags[:3],active,_text(verified_by),self.clock(),old["grant_id"]))
            if flags[3]:
                for item in catalog:
                    previous = db.execute("SELECT * FROM assistant_grants WHERE user_id=? AND device_id=? AND knowledge_base_id=?",
                        (user_id,device_id,item["id"])).fetchone()
                    if previous is None:
                        db.execute("INSERT INTO assistant_grants(grant_id,user_id,device_id,knowledge_base_id,aliases_json,is_default,can_query,can_import,public_reply_allowed) VALUES(?,?,?,?,?,0,1,1,?)",
                            ("grant-"+uuid.uuid4().hex,user_id,device_id,item["id"],"[]",flags[2]))
                    elif not previous["active"] or previous["can_query"]!=1 or previous["can_import"]!=1 or previous["public_reply_allowed"]!=flags[2]:
                        db.execute("UPDATE assistant_grants SET can_query=1,can_import=1,public_reply_allowed=?,active=1,version=version+1,auth_revision=auth_revision+1 WHERE grant_id=?",
                            (flags[2],previous["grant_id"]))
        return self.get_device(device_id)

    def authorize_all_local(self, user_id, device_id):
        found = self._all_local_scope(user_id,device_id)
        if found is None:
            return None
        _,user,device,grant = found
        return Binding(self._owner_binding_id(user,device,grant),device["instance_id"],
            user["sender_profile_url"],user["receiver_profile_url"],None,grant["public_reply_allowed"],True,
            query_scope="all_local")

    def _all_local_scope(self, user_id, device_id):
        try:
            _identity(user_id);_identity(device_id)
            snapshot = self.snapshot()
        except (RegistryError,sqlite3.Error,OSError):
            return None
        for user in snapshot["users"]:
            if user["user_id"]!=user_id or not user["active"] or user["verification_status"]!="verified":
                continue
            for device in user["devices"]:
                grant = device["local_owner_access"]
                if (device["device_id"]==device_id and device["is_local"] and device["enabled"] and not device["revoked"]
                        and device["verification_status"]=="verified" and grant["active"] and grant["query_all_local"]):
                    return snapshot,user,device,grant
        return None

    def all_authorization_snapshot(self, user_id, device_id):
        found = self._all_local_scope(user_id,device_id)
        if found is None:
            return None
        registry,user,device,grant = found
        return {"registry_id":registry["registry_id"],"user_id":user_id,"device_id":device_id,
            "grant_id":grant["grant_id"],"user_revision":user["auth_revision"],"device_revision":device["auth_revision"],
            "grant_revision":grant["auth_revision"],"grant_version":grant["version"],
            "binding_id":self._owner_binding_id(user,device,grant),"instance_id":device["instance_id"],
            "knowledge_base_id":None,"action":"query","public_reply_allowed":grant["public_reply_allowed"],"query_scope":"all_local"}

    def library_choices(self, user_id, device_id):
        """Only this verified user's selected local device catalog/grants."""
        snapshot = self.snapshot()
        user = next((item for item in snapshot["users"] if item["user_id"]==user_id),None)
        device = next((item for item in user["devices"] if item["device_id"]==device_id),None) if user else None
        if (not user or not user["active"] or user["verification_status"]!="verified" or not device
                or not device["is_local"] or not device["enabled"] or device["revoked"] or device["verification_status"]!="verified"):
            raise RegistryError("local_owner_verified_device_required")
        owner = device["local_owner_access"]
        full = owner["active"] and owner["catalog_read_allowed"]
        grants = {item["knowledge_base_id"]:item for item in device["libraries"] if item["active"] and item["library_exists"]}
        candidates = snapshot["local_catalog"] if full else [item for item in snapshot["local_catalog"] if item["id"] in grants]
        options=[]
        for ordinal,item in enumerate(sorted(candidates,key=lambda item:item["id"]),1):
            grant=grants.get(item["id"])
            binding=self.authorize_binding(grant["binding_id"],device["instance_id"],item["id"],"import") if grant else None
            authorization=self.authorization_snapshot(grant["binding_id"],device["instance_id"],item["id"],"import") if binding else None
            options.append({"knowledge_base_id":item["id"],"ordinal":ordinal,"name":item["name"],"aliases":grant["aliases"] if grant else [],
                "can_import":binding is not None and authorization is not None,
                "public_reply_allowed":grant["public_reply_allowed"] if grant else False,
                "binding":binding.to_dict() if binding and authorization else None,"authorization":authorization})
        return {"catalog_count":len(options),"catalog_scope":"all_local" if full else "authorized_libraries",
            "catalog_public_reply_allowed":bool(owner["public_reply_allowed"] if full else options and all(item["public_reply_allowed"] for item in options)),
            "choice_options":options,"options_sha256":_digest(options)}

    def register_user(self, sender_profile_url, receiver_profile_url, label=""):
        sender, receiver = _profile(sender_profile_url), _profile(receiver_profile_url)
        if sender == receiver:
            raise RegistryError("sender_must_differ_from_receiver")
        user_id = "user-" + uuid.uuid4().hex
        with self._db(write=True) as db:
            try:
                db.execute("""INSERT INTO assistant_users(user_id,label,sender_profile_url,receiver_profile_url)
                              VALUES(?,?,?,?)""", (user_id, _text(label or "新账号"), sender, receiver))
            except sqlite3.IntegrityError:
                raise RegistryError("profile_already_registered") from None
        return self.get_user(user_id)

    def verify_user(self, user_id, *, verified_by, method="local_admin"):
        self._verification(method, verified_by)
        with self._db(write=True) as db:
            self._required(db, "assistant_users", "user_id", user_id)
            db.execute("""UPDATE assistant_users SET active=1,verification_status='verified',
                verification_method=?,verified_by=?,verified_at=?,version=version+1,
                auth_revision=auth_revision+1 WHERE user_id=?""",
                       (method, _text(verified_by), self.clock(), user_id))
        return self.get_user(user_id)

    @staticmethod
    def _verification(method, verified_by):
        if method not in {"local_admin", "pairing", "legacy_local_admin_import"}:
            raise RegistryError("invalid_verification_method")
        _text(verified_by)

    def revoke_user(self, user_id):
        with self._db(write=True) as db:
            self._required(db, "assistant_users", "user_id", user_id)
            db.execute("""UPDATE assistant_users SET active=0,verification_status='revoked',
                version=version+1,auth_revision=auth_revision+1 WHERE user_id=?""", (user_id,))
            if self._owner_rows(db):
                db.execute("UPDATE assistant_local_owner_access SET query_all_local=0,catalog_read_allowed=0,public_reply_allowed=0,active=0,version=version+1,auth_revision=auth_revision+1 WHERE user_id=? AND active=1",(user_id,))
        return self.get_user(user_id)

    def register_device(self, user_id, *, instance_id=None, label="本机设备", is_default=False):
        instance = _identity(instance_id or self.local_instance_id)
        device_id = "device-" + uuid.uuid4().hex
        with self._db(write=True) as db:
            self._required(db, "assistant_users", "user_id", user_id)
            try:
                db.execute("INSERT INTO assistant_devices(device_id,user_id,instance_id,label) VALUES(?,?,?,?)",
                           (device_id, user_id, instance, _text(label)))
            except sqlite3.IntegrityError:
                raise RegistryError("device_instance_already_registered") from None
            if _bool(is_default):
                db.execute("UPDATE assistant_users SET default_device_id=?,version=version+1 WHERE user_id=?",
                           (device_id, user_id))
        return self.get_device(device_id)

    def verify_device(self, device_id, *, verified_by, method="local_admin"):
        self._verification(method, verified_by)
        with self._db(write=True) as db:
            self._required(db, "assistant_devices", "device_id", device_id)
            db.execute("""UPDATE assistant_devices SET enabled=1,revoked=0,verification_status='verified',
                verification_method=?,verified_by=?,verified_at=?,version=version+1,
                auth_revision=auth_revision+1 WHERE device_id=?""",
                       (method, _text(verified_by), self.clock(), device_id))
        return self.get_device(device_id)

    def revoke_device(self, device_id):
        with self._db(write=True) as db:
            self._required(db, "assistant_devices", "device_id", device_id)
            db.execute("""UPDATE assistant_devices SET enabled=0,revoked=1,verification_status='revoked',
                credential_hash=NULL,version=version+1,auth_revision=auth_revision+1 WHERE device_id=?""", (device_id,))
            if self._owner_rows(db):
                db.execute("UPDATE assistant_local_owner_access SET query_all_local=0,catalog_read_allowed=0,public_reply_allowed=0,active=0,version=version+1,auth_revision=auth_revision+1 WHERE device_id=? AND active=1",(device_id,))
        return self.get_device(device_id)

    def set_default_device(self, user_id, device_id):
        with self._db(write=True) as db:
            device = self._required(db, "assistant_devices", "device_id", device_id)
            if device["user_id"] != user_id or device["revoked"]:
                raise RegistryError("device_owner_mismatch")
            self._required(db, "assistant_users", "user_id", user_id)
            db.execute("UPDATE assistant_users SET default_device_id=?,version=version+1 WHERE user_id=?",
                       (device_id, user_id))
        return self.get_user(user_id)

    def grant_library(self, user_id, device_id, knowledge_base_id, *, aliases=(), can_query=True,
                      can_import=False, public_reply_allowed=False, is_default=False):
        catalog = {item["id"]: item for item in self.catalog()}
        if type(knowledge_base_id) is not int or knowledge_base_id not in catalog:
            raise RegistryError("library_not_in_local_catalog")
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 20:
            raise RegistryError("invalid_aliases")
        aliases = list(dict.fromkeys(normalize_alias(value) for value in aliases))
        flags = tuple(_bool(value) for value in (can_query, can_import, public_reply_allowed, is_default))
        with self._db(write=True) as db:
            device = self._required(db, "assistant_devices", "device_id", device_id)
            self._required(db, "assistant_users", "user_id", user_id)
            if device["user_id"] != user_id:
                raise RegistryError("device_owner_mismatch")
            local = db.execute("SELECT value FROM registry_metadata WHERE key='local_instance_id'").fetchone()[0]
            if device["instance_id"] != local:
                raise RegistryError("remote_catalog_not_verified")
            previous = db.execute("""SELECT * FROM assistant_grants
                WHERE user_id=? AND device_id=? AND knowledge_base_id=?""",
                                  (user_id, device_id, knowledge_base_id)).fetchone()
            if flags[3]:
                db.execute("""UPDATE assistant_grants SET is_default=0,version=version+1
                    WHERE user_id=? AND device_id=? AND is_default=1 AND knowledge_base_id!=?""",
                           (user_id, device_id, knowledge_base_id))
            if previous:
                scope_changed = any(previous[key] != flag for key, flag in zip(
                    ("can_query", "can_import", "public_reply_allowed"), flags[:3])) or not previous["active"]
                db.execute("""UPDATE assistant_grants SET aliases_json=?,can_query=?,can_import=?,
                    public_reply_allowed=?,is_default=?,active=1,version=version+1,
                    auth_revision=auth_revision+? WHERE grant_id=?""",
                    (canonical_json(aliases), *flags, int(scope_changed), previous["grant_id"]))
                grant_id = previous["grant_id"]
            else:
                grant_id = "grant-" + uuid.uuid4().hex
                db.execute("""INSERT INTO assistant_grants(grant_id,user_id,device_id,knowledge_base_id,
                    aliases_json,can_query,can_import,public_reply_allowed,is_default) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (grant_id, user_id, device_id, knowledge_base_id, canonical_json(aliases), *flags))
        return self.get_grant(grant_id)

    def set_default_library(self, user_id, device_id, knowledge_base_id):
        with self._db(write=True) as db:
            row = db.execute("""SELECT * FROM assistant_grants WHERE user_id=? AND device_id=?
                AND knowledge_base_id=? AND active=1""", (user_id, device_id, knowledge_base_id)).fetchone()
            if row is None:
                raise RegistryError("library_not_granted")
            db.execute("""UPDATE assistant_grants SET is_default=0,version=version+1
                WHERE user_id=? AND device_id=? AND is_default=1""", (user_id, device_id))
            db.execute("UPDATE assistant_grants SET is_default=1,version=version+1 WHERE grant_id=?",
                       (row["grant_id"],))
        return self.get_grant(row["grant_id"])

    def revoke_grant(self, grant_id):
        with self._db(write=True) as db:
            self._required(db, "assistant_grants", "grant_id", grant_id)
            db.execute("""UPDATE assistant_grants SET active=0,is_default=0,version=version+1,
                auth_revision=auth_revision+1 WHERE grant_id=?""", (grant_id,))
        return self.get_grant(grant_id)

    def revoke_library(self, knowledge_base_id, *, reason="library_deleted"):
        """Fence every local grant before an ordinary product library deletion.

        This intentionally commits independently before deleting business data.
        A failed business deletion can leave grants revoked, never the reverse.
        It cannot detect external DB edits or restoring an older registry copy.
        """
        if type(knowledge_base_id) is not int or knowledge_base_id <= 0:
            raise RegistryError("invalid_library_id")
        if not isinstance(reason, str) or reason not in {"library_deleted", "library_revoked"}:
            raise RegistryError("invalid_library_revocation_reason")
        with self._db(write=True) as db:
            local = db.execute("SELECT value FROM registry_metadata WHERE key='local_instance_id'").fetchone()[0]
            cursor = db.execute("""UPDATE assistant_grants SET active=0,is_default=0,
                auth_revision=auth_revision+1,version=version+1
                WHERE knowledge_base_id=? AND (active=1 OR is_default=1)
                AND device_id IN (SELECT device_id FROM assistant_devices WHERE instance_id=?)""",
                (knowledge_base_id, local))
            revoked = cursor.rowcount
        return {"knowledge_base_id": knowledge_base_id, "reason": reason, "revoked_count": revoked}

    @staticmethod
    def _binding_id(user, device, grant):
        if (grant["legacy_binding_id"] and user["auth_revision"] == 1
                and device["auth_revision"] == 1 and grant["auth_revision"] == 1):
            return grant["legacy_binding_id"]
        return "assistant-" + _digest({"user_id": user["user_id"], "device_id": device["device_id"],
            "grant_id": grant["grant_id"], "user_revision": user["auth_revision"],
            "device_revision": device["auth_revision"], "grant_revision": grant["auth_revision"]})[:40]

    def _public_device(self, row, local):
        return {key: row[key] for key in ("device_id", "user_id", "instance_id", "label",
            "verification_status", "verification_method", "verified_by", "version", "auth_revision")} | {
            "enabled": bool(row["enabled"]), "revoked": bool(row["revoked"]),
            "last_seen_at": _iso(row["last_seen"]) if row["last_seen"] is not None else None,
            "online": bool(row["enabled"] and not row["revoked"] and row["last_seen"] is not None
                           and 0 <= self.clock() - row["last_seen"] <= self.offline_seconds),
            "is_local": row["instance_id"] == local, "remote_transport_verified": False,
            "credential_provisioned": row["credential_hash"] is not None}

    def _public_grant(self, user, device, grant, catalog):
        item = catalog.get(grant["knowledge_base_id"])
        return {key: grant[key] for key in ("grant_id", "user_id", "device_id", "knowledge_base_id",
                "version", "auth_revision")} | {
            "name": item["name"] if item else "", "library_exists": item is not None,
            "aliases": json.loads(grant["aliases_json"]), "is_default": bool(grant["is_default"]),
            "can_query": bool(grant["can_query"]), "can_import": bool(grant["can_import"]),
            "public_reply_allowed": bool(grant["public_reply_allowed"]), "active": bool(grant["active"]),
            "binding_id": self._binding_id(user, device, grant), "legacy": bool(grant["legacy_binding_id"])}

    def snapshot(self, *, allow_unavailable_catalog=False):
        catalog_error = None
        try:
            catalog = {item["id"]: item for item in self.catalog()}
        except RegistryError:
            if not allow_unavailable_catalog:
                raise
            catalog, catalog_error = {}, "catalog_unavailable"
        with self._db() as db:
            metadata = dict(db.execute("SELECT key,value FROM registry_metadata").fetchall())
            users = [dict(row) for row in db.execute("SELECT * FROM assistant_users ORDER BY rowid")]
            devices = [dict(row) for row in db.execute("SELECT * FROM assistant_devices ORDER BY rowid")]
            grants = [dict(row) for row in db.execute("SELECT * FROM assistant_grants ORDER BY rowid")]
            owners = self._owner_rows(db)
        public = []
        for user in users:
            value = {key: user[key] for key in ("user_id", "label", "sender_profile_url", "receiver_profile_url",
                "verification_status", "verification_method", "verified_by", "default_device_id", "version", "auth_revision")}
            value["active"] = bool(user["active"])
            value["devices"] = []
            for device in devices:
                if device["user_id"] != user["user_id"]:
                    continue
                item = self._public_device(device, metadata["local_instance_id"])
                item["libraries"] = [self._public_grant(user, device, grant, catalog) for grant in grants
                                     if grant["device_id"] == device["device_id"]]
                owner = next((row for row in owners if row["user_id"]==user["user_id"] and row["device_id"]==device["device_id"]),None)
                item["local_owner_access"] = ({"configured":True,
                    **{key:owner[key] for key in ("grant_id","version","auth_revision","verified_by")},
                    **{key:bool(owner[key]) for key in ("query_all_local","catalog_read_allowed","public_reply_allowed","active")},
                    "verified_at_utc":_iso(owner["verified_at"]),
                    "binding_id":self._owner_binding_id(user,device,owner)} if owner else
                    {"configured":False,"grant_id":None,"version":0,"auth_revision":0,"verified_by":None,
                     "query_all_local":False,"catalog_read_allowed":False,"public_reply_allowed":False,
                     "active":False,"verified_at_utc":None,"binding_id":None})
                value["devices"].append(item)
            public.append(value)
        return {"registry_id": metadata["registry_id"], "local_instance_id": metadata["local_instance_id"],
                "users": public, "local_catalog": list(catalog.values()),
                "catalog_error": catalog_error,
                "verification_scope": "local_admin_or_pairing_not_platform_ownership_proof",
                "cross_device_status": "local_protocol_only_not_remote_verified"}

    def get_user(self, user_id):
        return next((item for item in self.snapshot(allow_unavailable_catalog=True)["users"] if item["user_id"] == user_id), None)

    def get_device(self, device_id):
        return next((device for user in self.snapshot(allow_unavailable_catalog=True)["users"] for device in user["devices"]
                     if device["device_id"] == device_id), None)

    def get_grant(self, grant_id):
        return next((grant for user in self.snapshot(allow_unavailable_catalog=True)["users"] for device in user["devices"]
                     for grant in device["libraries"] if grant["grant_id"] == grant_id), None)

    def find_user(self, sender_profile_url, receiver_profile_url):
        sender, receiver = _profile(sender_profile_url), _profile(receiver_profile_url)
        return next((user for user in self.snapshot()["users"] if user["sender_profile_url"] == sender
                     and user["receiver_profile_url"] == receiver), None)

    def default_binding(self, sender_profile_url, receiver_profile_url, action="query"):
        """Trusted intake identity only; resolution still selects/fences the final grant."""
        user = self.find_user(sender_profile_url, receiver_profile_url)
        if not user or not user["active"] or user["verification_status"] != "verified":
            return None
        devices = [device for device in user["devices"] if device["is_local"] and device["enabled"]
                   and not device["revoked"] and device["verification_status"] == "verified"]
        preferred = next((device for device in devices if device["device_id"] == user["default_device_id"]), None)
        if preferred:
            devices = [preferred]
        elif user["default_device_id"]:
            return None
        elif len(devices) != 1:
            return None
        for device in devices:
            if action == "query":
                all_binding = self.authorize_all_local(user["user_id"],device["device_id"])
                if all_binding is not None:
                    return all_binding
            grants = sorted(device["libraries"], key=lambda grant: not grant["is_default"])
            for grant in grants:
                selected = self.authorize_binding(grant["binding_id"], device["instance_id"],
                                                  grant["knowledge_base_id"], action)
                if selected:
                    return selected
        return None

    def intake_binding(self, sender_profile_url, receiver_profile_url):
        """Identity evidence for intake only, including an import-only grant.

        This does not select a command's library or authorize any operation;
        CommandResolver and the executor must still freshly check that scope.
        """
        return (self.default_binding(sender_profile_url, receiver_profile_url, "query")
                or self.default_binding(sender_profile_url, receiver_profile_url, "import"))

    def import_legacy(self, source, *, verified_by, allow_import=False, aliases=()):
        """Explicit local-admin migration; repeat imports never restore revoked scope."""
        self._verification("legacy_local_admin_import", verified_by)
        _bool(allow_import)
        try:
            if isinstance(source, Binding):
                legacy = Binding.from_dict(source.to_dict())
            else:
                raw = source if isinstance(source, dict) else json.loads(Path(source).read_text(encoding="utf-8"))
                legacy = Binding.from_dict(raw)
        except (OSError, UnicodeError, ValueError, TypeError):
            raise RegistryError("invalid_legacy_binding") from None
        if not legacy.active:
            raise RegistryError("legacy_binding_inactive")
        if legacy.knowledge_base_id not in {item["id"] for item in self.catalog()}:
            raise RegistryError("legacy_library_missing")
        alias_values = list(dict.fromkeys(normalize_alias(value) for value in aliases))
        payload_hash = _digest(legacy.to_dict())
        with self._db(write=True) as db:
            # Explicit migration is the only repair path for a pre-marker
            # registry; hold the lifecycle lock for its marker as well.
            self._ensure_required_marker()
            db.execute("INSERT OR REPLACE INTO registry_metadata VALUES('requires_marker','1')")
            old = db.execute("SELECT * FROM legacy_imports WHERE binding_id=?", (legacy.binding_id,)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise RegistryError("legacy_binding_conflict")
                result = dict(old)
                imported = False
            else:
                if db.execute("SELECT 1 FROM assistant_devices LIMIT 1").fetchone():
                    raise RegistryError("legacy_migration_requires_empty_devices")
                if db.execute("SELECT 1 FROM assistant_users WHERE sender_profile_url=? AND receiver_profile_url=?",
                              (legacy.sender_profile_url, legacy.receiver_profile_url)).fetchone():
                    raise RegistryError("legacy_profile_already_registered")
                db.execute("UPDATE registry_metadata SET value=? WHERE key='local_instance_id'", (legacy.instance_id,))
                user_id, device_id, grant_id = ("user-" + uuid.uuid4().hex, "device-" + uuid.uuid4().hex,
                                               "grant-" + uuid.uuid4().hex)
                now = self.clock()
                db.execute("""INSERT INTO assistant_users(user_id,label,sender_profile_url,receiver_profile_url,
                    verification_status,verification_method,verified_by,verified_at,default_device_id)
                    VALUES(?,?,?,?,'verified','legacy_local_admin_import',?,?,?)""",
                    (user_id, "已有授权账号", legacy.sender_profile_url, legacy.receiver_profile_url,
                     _text(verified_by), now, device_id))
                db.execute("""INSERT INTO assistant_devices(device_id,user_id,instance_id,label,verification_status,
                    verification_method,verified_by,verified_at,last_seen) VALUES(?,?,?,?,'verified',
                    'legacy_local_admin_import',?,?,?)""",
                    (device_id, user_id, legacy.instance_id, "本机已有设备", _text(verified_by), now, now))
                db.execute("""INSERT INTO assistant_grants(grant_id,user_id,device_id,knowledge_base_id,aliases_json,
                    is_default,can_query,can_import,public_reply_allowed,legacy_binding_id)
                    VALUES(?,?,?,?,?,1,1,?,?,?)""",
                    (grant_id, user_id, device_id, legacy.knowledge_base_id, canonical_json(alias_values),
                     int(allow_import), int(legacy.public_reply_allowed), legacy.binding_id))
                db.execute("INSERT INTO legacy_imports VALUES(?,?,?,?,?)",
                           (legacy.binding_id, payload_hash, user_id, device_id, grant_id))
                result = {"binding_id": legacy.binding_id, "payload_hash": payload_hash,
                          "user_id": user_id, "device_id": device_id, "grant_id": grant_id}
                imported = True
        return {key: result[key] for key in ("user_id", "device_id", "grant_id")} | {
            "imported": imported, "grant": self.get_grant(result["grant_id"])}

    def _authorization(self, binding_id, instance_id, knowledge_base_id, action, require_local):
        if (not isinstance(action, str) or action not in _ACTIONS
                or type(knowledge_base_id) is not int or knowledge_base_id <= 0
                or type(require_local) is not bool):
            return None
        try:
            _identity(binding_id)
            _identity(instance_id)
            snapshot = self.snapshot()
        except (RegistryError, sqlite3.Error, OSError):
            return None
        for user in snapshot["users"]:
            if not user["active"] or user["verification_status"] != "verified":
                continue
            for device in user["devices"]:
                if (device["instance_id"] != instance_id or not device["enabled"] or device["revoked"]
                        or device["verification_status"] != "verified"
                        or (require_local and not device["is_local"])):
                    continue
                for grant in device["libraries"]:
                    if (grant["binding_id"] != binding_id or grant["knowledge_base_id"] != knowledge_base_id
                            or not grant["active"] or not grant["library_exists"] or not grant[_ACTIONS[action]]):
                        continue
                    return snapshot, user, device, grant
        return None

    def authorize_binding(self, binding_id, instance_id, knowledge_base_id, action="import", *, require_local=True):
        found = self._authorization(binding_id, instance_id, knowledge_base_id, action, require_local)
        if not found:
            return None
        _, user, _, grant = found
        return Binding(binding_id=binding_id, instance_id=instance_id,
            sender_profile_url=user["sender_profile_url"], receiver_profile_url=user["receiver_profile_url"],
            knowledge_base_id=knowledge_base_id, public_reply_allowed=grant["public_reply_allowed"], active=True)

    def authorization_snapshot(self, binding_id, instance_id, knowledge_base_id, action="query"):
        found = self._authorization(binding_id, instance_id, knowledge_base_id, action, True)
        if not found:
            return None
        registry, user, device, grant = found
        return {"registry_id": registry["registry_id"], "user_id": user["user_id"], "device_id": device["device_id"],
            "grant_id": grant["grant_id"], "user_revision": user["auth_revision"],
            "device_revision": device["auth_revision"], "grant_revision": grant["auth_revision"],
            "grant_version": grant["version"], "binding_id": binding_id, "instance_id": instance_id,
            "knowledge_base_id": knowledge_base_id, "action": action,
            "public_reply_allowed": grant["public_reply_allowed"]}

    def authorize_snapshot(self, authorization, *, action=None):
        if isinstance(authorization,dict) and set(authorization)==_ALL_AUTHORIZATION_FIELDS:
            if (authorization.get("query_scope")!="all_local" or authorization.get("knowledge_base_id") is not None
                    or authorization.get("action")!="query" or action not in (None,"query")
                    or type(authorization.get("public_reply_allowed")) is not bool
                    or any(type(authorization[key]) is not int or authorization[key]<=0
                           for key in ("user_revision","device_revision","grant_revision","grant_version"))):
                return None
            try:
                for key in ("registry_id","user_id","device_id","grant_id","binding_id","instance_id"):
                    _identity(authorization[key])
                fresh=self.all_authorization_snapshot(authorization["user_id"],authorization["device_id"])
            except (RegistryError,TypeError):
                return None
            if fresh!=authorization:
                return None
            return self.authorize_all_local(authorization["user_id"],authorization["device_id"])
        if not isinstance(authorization, dict) or set(authorization) != _AUTHORIZATION_FIELDS:
            return None
        if (type(authorization["public_reply_allowed"]) is not bool
                or any(type(authorization[key]) is not int or authorization[key] <= 0
                       for key in ("user_revision", "device_revision", "grant_revision", "grant_version", "knowledge_base_id"))
                or not isinstance(authorization["action"], str) or authorization["action"] not in _ACTIONS):
            return None
        try:
            for key in ("registry_id", "user_id", "device_id", "grant_id", "binding_id", "instance_id"):
                _identity(authorization[key])
        except RegistryError:
            return None
        requested = authorization["action"] if action is None else action
        if requested != authorization.get("action"):
            return None
        fresh = self.authorization_snapshot(authorization.get("binding_id"), authorization.get("instance_id"),
                                             authorization.get("knowledge_base_id"), requested)
        if fresh is None or fresh != authorization:
            return None
        return self.authorize_binding(fresh["binding_id"], fresh["instance_id"],
                                      fresh["knowledge_base_id"], requested)

    def mark_local_device_online(self, device_id):
        """Trusted local runtime hook, never an unauthenticated device endpoint."""
        with self._db(write=True) as db:
            row = self._required(db, "assistant_devices", "device_id", device_id)
            local = db.execute("SELECT value FROM registry_metadata WHERE key='local_instance_id'").fetchone()[0]
            if (row["instance_id"] != local or row["revoked"] or not row["enabled"]
                    or row["verification_status"] != "verified"):
                raise RegistryError("device_not_local_verified")
            db.execute("UPDATE assistant_devices SET last_seen=? WHERE device_id=?", (self.clock(), device_id))
        return self.get_device(device_id)

    def provision_device_credential(self, device_id):
        """Admin-only: return a fresh credential once; snapshots never contain it."""
        credential = secrets.token_hex(32)
        key = hashlib.sha256(credential.encode()).digest()
        with self._db(write=True) as db:
            row = self._required(db, "assistant_devices", "device_id", device_id)
            if row["revoked"] or not row["enabled"] or row["verification_status"] != "verified":
                raise RegistryError("device_not_verified")
            db.execute("""UPDATE assistant_devices SET credential_hash=?,version=version+1,
                auth_revision=auth_revision+? WHERE device_id=?""",
                       (key, int(row["credential_hash"] is not None), device_id))
        return {"device_id": device_id, "credential": credential}

    def authenticate_device(self, device_id, credential):
        if not isinstance(credential, str):
            return None
        try:
            with self._db() as db:
                row = self._required(db, "assistant_devices", "device_id", device_id)
                user = self._required(db, "assistant_users", "user_id", row["user_id"])
        except RegistryError:
            return None
        if (not row["credential_hash"] or row["revoked"] or not row["enabled"]
                or row["verification_status"] != "verified" or not user["active"]
                or user["verification_status"] != "verified"):
            return None
        if not hmac.compare_digest(bytes(row["credential_hash"]), hashlib.sha256(credential.encode()).digest()):
            return None
        return self.get_device(device_id)

    def heartbeat_device(self, device_id, credential):
        if self.authenticate_device(device_id, credential) is None:
            raise RegistryError("device_authentication_failed")
        with self._db(write=True) as db:
            row = self._required(db, "assistant_devices", "device_id", device_id)
            user = self._required(db, "assistant_users", "user_id", row["user_id"])
            if (row["revoked"] or not row["enabled"] or not row["credential_hash"]
                    or row["verification_status"] != "verified" or not user["active"]
                    or user["verification_status"] != "verified"
                    or not hmac.compare_digest(bytes(row["credential_hash"]), hashlib.sha256(credential.encode()).digest())):
                raise RegistryError("device_authentication_failed")
            db.execute("UPDATE assistant_devices SET last_seen=? WHERE device_id=?", (self.clock(), device_id))
        return self.get_device(device_id)

    def sign_task(self, metadata, *, action="query", ttl_seconds=300):
        """Trusted scheduler only: seal a frozen queue payload hash, never an arbitrary URL."""
        metadata = _task_metadata(metadata)
        action = "import" if action == "save" else action
        if not isinstance(action, str) or action not in {"query", "import"} or type(ttl_seconds) not in (int, float) or not 1 <= ttl_seconds <= 3600:
            raise RegistryError("invalid_signed_task_policy")
        authorization = self.authorization_snapshot(metadata["binding_id"], metadata["instance_id"],
                                                      metadata["knowledge_base_id"], action)
        if (not authorization or authorization["device_id"] != metadata["device_id"]
                or authorization["user_id"] != metadata["user_id"]):
            raise RegistryError("signed_task_scope_not_authorized")
        with self._db() as db:
            device = self._required(db, "assistant_devices", "device_id", metadata["device_id"])
            key = device["credential_hash"]
        if not key:
            raise RegistryError("device_credential_not_provisioned")
        now = self.clock()
        unsigned = {**metadata, "protocol_version": 1, "action": action,
            "authorization_sha256": _digest(authorization), "issued_at_utc": _iso(now),
            "expires_at_utc": _iso(now + ttl_seconds), "nonce": secrets.token_hex(16)}
        signature = hmac.new(bytes(key), canonical_json(unsigned).encode(), hashlib.sha256).hexdigest()
        return {**unsigned, "signature": signature}

    def verify_device_task(self, packet, credential, *, payload):
        """Authenticated local protocol acceptance with durable nonce protection.

        This authenticates the sealed message, not its eventual business effect.
        The queue must still claim/fence work and freshly authorize each tool/send.
        """
        if not isinstance(packet, dict):
            raise RegistryError("invalid_signed_task_fields")
        device = self.authenticate_device(packet.get("device_id"), credential)
        if device is None:
            raise RegistryError("device_authentication_failed")
        checked = verify_signed_task(packet, credential, expected_device_id=device["device_id"],
            expected_instance_id=device["instance_id"], expected_user_id=device["user_id"],
            payload=payload, now=self.clock())
        authorization = self.authorization_snapshot(checked["binding_id"], checked["instance_id"],
                                                      checked["knowledge_base_id"], checked["action"])
        if not authorization or _digest(authorization) != checked["authorization_sha256"]:
            raise RegistryError("signed_task_authorization_changed")
        with self._db(write=True) as db:
            user = self._required(db, "assistant_users", "user_id", authorization["user_id"])
            current_device = self._required(db, "assistant_devices", "device_id", authorization["device_id"])
            grant = self._required(db, "assistant_grants", "grant_id", authorization["grant_id"])
            if (not user["active"] or user["verification_status"] != "verified"
                    or current_device["revoked"] or not current_device["enabled"]
                    or current_device["verification_status"] != "verified" or not grant["active"]
                    or user["auth_revision"] != authorization["user_revision"]
                    or current_device["auth_revision"] != authorization["device_revision"]
                    or grant["auth_revision"] != authorization["grant_revision"]
                    or grant["version"] != authorization["grant_version"]
                    or self._binding_id(user, current_device, grant) != authorization["binding_id"]):
                raise RegistryError("signed_task_authorization_changed")
            current = self.clock()
            expires = _validate_task_window(checked, current)
            db.execute("DELETE FROM device_task_nonces WHERE expires_at<=?", (current,))
            try:
                db.execute("INSERT INTO device_task_nonces VALUES(?,?,?)",
                           (device["device_id"], checked["nonce"], expires))
            except sqlite3.IntegrityError:
                raise RegistryError("task_nonce_replayed") from None
        return checked
