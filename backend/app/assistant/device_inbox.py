"""Experimental durable device inbox; no transport or execution integration.

The caller explicitly prepares a Registry schema. Packet acceptance consumes a
nonce and saves the complete frozen task in one Registry transaction. Handoff
only creates waiting_desktop queue records and never promotes or executes them.
HMAC/source flags do not prove a platform comment or physical device identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3

from app.mention_agent.contracts import Binding, validate_envelope
from .registry import Registry, RegistryError, canonical_json, verify_signed_task, _validate_task_window
from .queue import QueueError


COMPONENT = "DouyinMind.assistant.device-inbox.v1"
SCHEMA_VERSION = "1"
PAYLOAD_LIMIT = 32768
ENVELOPE_FIELDS = frozenset({"command_id", "reference_id", "reference_quality", "native_comment_id",
    "sender_profile_url", "receiver_profile_url", "video_id", "comment_text", "source_mode", "evidence"})
EVIDENCE_FIELDS = frozenset({"real_mention", "sender_verified", "video_verified", "unique_count", "observed_at_utc"})
AUTH_FIELDS = frozenset({"registry_id", "user_id", "device_id", "grant_id", "user_revision", "device_revision",
    "grant_revision", "grant_version", "binding_id", "instance_id", "knowledge_base_id", "action", "public_reply_allowed"})
META = "assistant_device_inbox_meta"
INBOX = "assistant_device_inbox"
NONCES = "assistant_device_inbox_nonces"
_COLUMNS = {
    META: {"key", "value"},
    INBOX: {"receipt_id", "registry_id", "remote_task_id", "device_id", "user_id", "instance_id",
        "binding_id", "knowledge_base_id", "video_id", "command_id", "reference_id", "packet_json",
        "packet_sha256", "business_sha256", "payload_json", "binding_json", "authorization_json",
        "state", "local_task_id", "handoff_token", "owner_run_id", "lease_until", "reason_code",
        "created_at", "updated_at"},
    NONCES: {"device_id", "nonce", "receipt_id", "packet_sha256", "expires_at"},
}
_FIXED_REASONS = frozenset({"authorization_changed", "queue_original_conflict", "queue_unavailable"})
_DDL = {
    META: "CREATE TABLE assistant_device_inbox_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
    INBOX: """CREATE TABLE assistant_device_inbox(
                receipt_id TEXT PRIMARY KEY,registry_id TEXT NOT NULL,remote_task_id TEXT NOT NULL,
                device_id TEXT NOT NULL,user_id TEXT NOT NULL,instance_id TEXT NOT NULL,
                binding_id TEXT NOT NULL,knowledge_base_id INTEGER NOT NULL,video_id TEXT NOT NULL,
                command_id TEXT NOT NULL,reference_id TEXT NOT NULL,packet_json TEXT NOT NULL,
                packet_sha256 TEXT NOT NULL,business_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL,
                binding_json TEXT NOT NULL,authorization_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('received','handoff_reserved','handed_off','held')),
                local_task_id TEXT,handoff_token TEXT,owner_run_id TEXT,lease_until REAL,reason_code TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL,
                UNIQUE(registry_id,device_id,remote_task_id))""",
    NONCES: """CREATE TABLE assistant_device_inbox_nonces(
                device_id TEXT NOT NULL,nonce TEXT NOT NULL,receipt_id TEXT NOT NULL
                REFERENCES assistant_device_inbox(receipt_id),packet_sha256 TEXT NOT NULL,
                expires_at REAL NOT NULL,PRIMARY KEY(device_id,nonce))""",
}


class DeviceInboxError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _sha(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _identity(value, maximum=128):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1," + str(maximum) + r"}", value):
        raise DeviceInboxError("invalid_inbox_identity")
    return value


def _payload(value):
    if (not isinstance(value, dict) or set(value) != ENVELOPE_FIELDS
            or not isinstance(value.get("evidence"), dict) or set(value["evidence"]) != EVIDENCE_FIELDS):
        raise DeviceInboxError("invalid_inbox_payload")
    if not isinstance(value.get("source_mode"), str) or value["source_mode"] not in {"isolated", "replay"}:
        # A device knows its HMAC key. Its own live_ui flag is not Hub/UI proof.
        raise DeviceInboxError("device_platform_origin_not_verified")
    if (any(not isinstance(value[name], str) or len(value[name]) > 2048
            for name in ("sender_profile_url", "receiver_profile_url"))
            or not isinstance(value["evidence"].get("observed_at_utc"), str)
            or len(value["evidence"]["observed_at_utc"]) > 64):
        raise DeviceInboxError("invalid_inbox_payload")
    try:
        encoded = canonical_json(value).encode("utf-8")
        if len(encoded) > PAYLOAD_LIMIT:
            raise DeviceInboxError("inbox_payload_too_large")
        return json.loads(encoded)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, DeviceInboxError):
            raise
        raise DeviceInboxError("invalid_inbox_payload") from None


class DeviceInbox:
    """Internal foundation only; construction and reads never prepare schemas."""

    def __init__(self, registry: Registry):
        if not isinstance(registry, Registry):
            raise DeviceInboxError("registry_required")
        self.registry = registry

    @staticmethod
    def _metadata(db):
        return dict(db.execute("SELECT key,value FROM registry_metadata").fetchall())

    def _require_schema(self, db, *, allow_absent=False):
        objects = {row[0]: (row[1], row[2]) for row in db.execute(
            "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)", (META, INBOX, NONCES))}
        found = set(objects)
        if not found:
            if allow_absent:
                return False
            raise DeviceInboxError("device_inbox_not_prepared")
        if found != set(_COLUMNS):
            raise DeviceInboxError("foreign_device_inbox_schema")
        try:
            owner = dict(db.execute("SELECT key,value FROM " + META).fetchall())
            expected = {"component": COMPONENT, "version": SCHEMA_VERSION,
                        "registry_id": self._metadata(db)["registry_id"]}
            if owner != expected:
                raise DeviceInboxError("foreign_device_inbox_schema")
            for table, columns in _COLUMNS.items():
                actual_type, actual_sql = objects[table]
                normalized = lambda value: " ".join(value.strip().rstrip(";").split()).casefold()
                if (actual_type != "table" or not isinstance(actual_sql, str)
                        or normalized(actual_sql) != normalized(_DDL[table])
                        or {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")} != columns):
                    raise DeviceInboxError("foreign_device_inbox_schema")
        except sqlite3.Error:
            raise DeviceInboxError("foreign_device_inbox_schema") from None
        return True

    def prepare_schema(self):
        """Explicit opt-in; never adopts partial/unmarked same-name tables."""
        with self.registry._db(write=True) as db:
            if self._require_schema(db, allow_absent=True):
                return {"prepared": True, "created": False}
            for ddl in _DDL.values():
                db.execute(ddl)
            owner = {"component": COMPONENT, "version": SCHEMA_VERSION,
                     "registry_id": self._metadata(db)["registry_id"]}
            db.executemany("INSERT INTO " + META + " VALUES(?,?)", owner.items())
            self._require_schema(db)
        return {"prepared": True, "created": True}

    @staticmethod
    def _raw(row):
        if row is None:
            return None
        value = dict(row)
        for name in ("packet", "payload", "binding", "authorization"):
            value[name] = json.loads(value.pop(name + "_json"))
        return value

    def _public(self, raw, *, created=False, replayed=False):
        binding = Binding.from_dict(raw["binding"])
        current = self.registry.authorize_snapshot(raw["authorization"])
        return {key: raw[key] for key in ("receipt_id", "remote_task_id", "device_id", "user_id",
            "instance_id", "binding_id", "knowledge_base_id", "state", "local_task_id", "reason_code")} | {
            "created": created, "replayed": replayed, "currently_authorized": current == binding,
            "source_mode": raw["payload"]["source_mode"], "platform_origin_verified": False,
            "execution_enabled": False}

    def _identity_in_transaction(self, db, device, credential, checked, payload):
        user = self.registry._required(db, "assistant_users", "user_id", device["user_id"])
        current = self.registry._required(db, "assistant_devices", "device_id", device["device_id"])
        if (not user["active"] or user["verification_status"] != "verified"
                or not current["enabled"] or current["revoked"] or current["verification_status"] != "verified"
                or current["user_id"] != checked["user_id"] or current["instance_id"] != checked["instance_id"]
                or not current["credential_hash"]
                or not hmac.compare_digest(bytes(current["credential_hash"]), hashlib.sha256(credential.encode()).digest())
                or user["sender_profile_url"] != payload["sender_profile_url"]
                or user["receiver_profile_url"] != payload["receiver_profile_url"]):
            raise DeviceInboxError("device_authentication_failed")
        return user, current

    def _scope_in_transaction(self, db, authorization, binding, checked):
        if not isinstance(authorization, dict) or set(authorization) != AUTH_FIELDS:
            raise DeviceInboxError("device_inbox_authorization_changed")
        user = self.registry._required(db, "assistant_users", "user_id", authorization["user_id"])
        device = self.registry._required(db, "assistant_devices", "device_id", authorization["device_id"])
        grant = self.registry._required(db, "assistant_grants", "grant_id", authorization["grant_id"])
        permission = "can_import" if checked["action"] == "import" else "can_query"
        if (not user["active"] or user["verification_status"] != "verified"
                or not device["enabled"] or device["revoked"] or device["verification_status"] != "verified"
                or not grant["active"] or not grant[permission]
                or device["user_id"] != user["user_id"] or grant["user_id"] != user["user_id"]
                or grant["device_id"] != device["device_id"]
                or device["instance_id"] != checked["instance_id"]
                or grant["knowledge_base_id"] != checked["knowledge_base_id"]
                or user["auth_revision"] != authorization["user_revision"]
                or device["auth_revision"] != authorization["device_revision"]
                or grant["auth_revision"] != authorization["grant_revision"]
                or grant["version"] != authorization["grant_version"]
                or self.registry._binding_id(user, device, grant) != checked["binding_id"]
                or bool(grant["public_reply_allowed"]) != authorization["public_reply_allowed"]
                or binding.public_reply_allowed != authorization["public_reply_allowed"]
                or checked["authorization_sha256"] != _sha(authorization)):
            raise DeviceInboxError("device_inbox_authorization_changed")

    def _save_nonce(self, db, checked, receipt_id, packet_sha, expires):
        # Both writes participate in the same caller-owned Registry transaction.
        db.execute("INSERT INTO device_task_nonces VALUES(?,?,?)",
                   (checked["device_id"], checked["nonce"], expires))
        db.execute("INSERT INTO " + NONCES + " VALUES(?,?,?,?,?)",
                   (checked["device_id"], checked["nonce"], receipt_id, packet_sha, expires))

    def accept(self, packet, credential, *, payload):
        """Persist a fixed task+nonce atomically; never enqueues or executes it."""
        payload = _payload(payload)
        if not isinstance(packet, dict):
            raise DeviceInboxError("invalid_inbox_packet")
        device = self.registry.authenticate_device(packet.get("device_id"), credential)
        if device is None:
            raise DeviceInboxError("device_authentication_failed")
        try:
            checked = verify_signed_task(packet, credential, expected_device_id=device["device_id"],
                expected_instance_id=device["instance_id"], expected_user_id=device["user_id"],
                payload=payload, now=self.registry.clock())
            for name in ("queue_task_id", "command_id", "reference_id"):
                _identity(checked[name], 256)
            user = self.registry.get_user(device["user_id"])
            provisional = Binding.from_dict({"binding_id": checked["binding_id"],
                "instance_id": checked["instance_id"], "sender_profile_url": user["sender_profile_url"],
                "receiver_profile_url": user["receiver_profile_url"],
                "knowledge_base_id": checked["knowledge_base_id"], "public_reply_allowed": False, "active": True})
            validated = validate_envelope(payload, provisional)
            if validated != payload or any(payload[name] != checked[name]
                    for name in ("video_id", "command_id", "reference_id")):
                raise DeviceInboxError("inbox_payload_scope_mismatch")
        except (RegistryError, ValueError, TypeError, KeyError, AttributeError) as error:
            if isinstance(error, DeviceInboxError):
                raise
            raise DeviceInboxError(str(error) if isinstance(error, RegistryError) else "invalid_inbox_payload") from None
        packet = {**checked, "signature": packet["signature"]}
        packet_sha = _sha(packet)
        business = {key: value for key, value in checked.items()
                    if key not in {"nonce", "issued_at_utc", "expires_at_utc"}}
        business_sha = _sha(business)
        authorization = self.registry.authorization_snapshot(checked["binding_id"], checked["instance_id"],
            checked["knowledge_base_id"], checked["action"])
        binding = (Binding.from_dict({**provisional.to_dict(),
                   "public_reply_allowed": authorization["public_reply_allowed"]}) if authorization else None)
        created = False
        with self.registry._db(write=True) as db:
            self._require_schema(db)
            self._identity_in_transaction(db, device, credential, checked, payload)
            expires = _validate_task_window(checked, self.registry.clock())
            nonce = db.execute("SELECT * FROM " + NONCES + " WHERE device_id=? AND nonce=?",
                               (device["device_id"], checked["nonce"])).fetchone()
            if nonce:
                if nonce["packet_sha256"] != packet_sha:
                    raise DeviceInboxError("device_inbox_nonce_conflict")
                row = db.execute("SELECT * FROM " + INBOX + " WHERE receipt_id=?", (nonce["receipt_id"],)).fetchone()
                if row is None or row["business_sha256"] != business_sha:
                    raise DeviceInboxError("device_inbox_nonce_conflict")
            else:
                if db.execute("SELECT 1 FROM device_task_nonces WHERE device_id=? AND nonce=?",
                              (device["device_id"], checked["nonce"])).fetchone():
                    raise DeviceInboxError("legacy_nonce_without_inbox")
                registry_id = self._metadata(db)["registry_id"]
                row = db.execute("SELECT * FROM " + INBOX + " WHERE registry_id=? AND device_id=? AND remote_task_id=?",
                    (registry_id, device["device_id"], checked["queue_task_id"])).fetchone()
                if row and row["business_sha256"] != business_sha:
                    raise DeviceInboxError("device_inbox_scope_conflict")
                if row is None:
                    if binding is None:
                        raise DeviceInboxError("device_inbox_authorization_changed")
                    self._scope_in_transaction(db, authorization, binding, checked)
                    receipt_id, now = secrets.token_hex(16), self.registry.clock()
                    db.execute("""INSERT INTO assistant_device_inbox(
                        receipt_id,registry_id,remote_task_id,device_id,user_id,instance_id,binding_id,
                        knowledge_base_id,video_id,command_id,reference_id,packet_json,packet_sha256,
                        business_sha256,payload_json,binding_json,authorization_json,state,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'received',?,?)""",
                        (receipt_id, registry_id, checked["queue_task_id"], device["device_id"], checked["user_id"],
                         checked["instance_id"], checked["binding_id"], checked["knowledge_base_id"],
                         checked["video_id"], checked["command_id"], checked["reference_id"], canonical_json(packet),
                         packet_sha, business_sha, canonical_json(payload), canonical_json(binding.to_dict()),
                         canonical_json(authorization), now, now))
                    created = True
                    row = db.execute("SELECT * FROM " + INBOX + " WHERE receipt_id=?", (receipt_id,)).fetchone()
                self._save_nonce(db, checked, row["receipt_id"], packet_sha, expires)
            raw = self._raw(row)
        return self._public(raw, created=created, replayed=not created)

    def get_receipt(self, receipt_id, credential, *, expected_device_id):
        """Current authenticated device may read old status; packet TTL is irrelevant."""
        _identity(receipt_id)
        _identity(expected_device_id)
        device = self.registry.authenticate_device(expected_device_id, credential)
        if device is None:
            raise DeviceInboxError("device_authentication_failed")
        with self.registry._db() as db:
            self._require_schema(db)
            row = db.execute("SELECT * FROM " + INBOX + " WHERE receipt_id=? AND device_id=? AND user_id=?",
                (receipt_id, expected_device_id, device["user_id"])).fetchone()
            if row is None:
                raise DeviceInboxError("device_inbox_receipt_not_found")
            raw = self._raw(row)
            self._identity_in_transaction(db, device, credential, raw["packet"], raw["payload"])
        return self._public(raw)

    def get_receipt_by_remote_task(self, remote_task_id, credential, *, expected_device_id):
        """Read the original ACK after a lost response, including after packet expiry."""
        _identity(remote_task_id, 256)
        _identity(expected_device_id)
        device = self.registry.authenticate_device(expected_device_id, credential)
        if device is None:
            raise DeviceInboxError("device_authentication_failed")
        with self.registry._db() as db:
            self._require_schema(db)
            registry_id = self._metadata(db)["registry_id"]
            row = db.execute("SELECT * FROM " + INBOX + """ WHERE registry_id=? AND device_id=?
                AND user_id=? AND remote_task_id=?""",
                (registry_id, expected_device_id, device["user_id"], remote_task_id)).fetchone()
            if row is None:
                raise DeviceInboxError("device_inbox_receipt_not_found")
            raw = self._raw(row)
            self._identity_in_transaction(db, device, credential, raw["packet"], raw["payload"])
        return self._public(raw)

    def claim_handoff(self, *, run_id, lease_seconds=30):
        _identity(run_id)
        if type(lease_seconds) not in (int, float) or not 1 <= lease_seconds <= 300:
            raise DeviceInboxError("invalid_handoff_lease")
        with self.registry._db(write=True) as db:
            self._require_schema(db)
            now = self.registry.clock()
            row = db.execute("SELECT * FROM " + INBOX + """ WHERE state='received'
                OR (state='handoff_reserved' AND lease_until<=?) ORDER BY created_at,receipt_id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(24)
            db.execute("UPDATE " + INBOX + """ SET state='handoff_reserved',handoff_token=?,owner_run_id=?,
                lease_until=?,updated_at=? WHERE receipt_id=?""", (token, run_id, now + lease_seconds, now, row["receipt_id"]))
            return self._raw(db.execute("SELECT * FROM " + INBOX + " WHERE receipt_id=?", (row["receipt_id"],)).fetchone())

    def finish_handoff(self, receipt_id, token, *, local_task_id=None, state="handed_off", reason_code=None):
        _identity(receipt_id)
        if (not isinstance(state, str) or state not in {"handed_off", "held"}
                or (state == "handed_off" and (not local_task_id or reason_code is not None))
                or (state == "held" and (local_task_id is not None or reason_code is None))):
            raise DeviceInboxError("invalid_handoff_result")
        if local_task_id is not None:
            _identity(local_task_id)
        if reason_code is not None and (not isinstance(reason_code, str) or reason_code not in _FIXED_REASONS):
            raise DeviceInboxError("invalid_handoff_result")
        with self.registry._db(write=True) as db:
            self._require_schema(db)
            row = db.execute("SELECT * FROM " + INBOX + " WHERE receipt_id=?", (receipt_id,)).fetchone()
            if (row is None or row["state"] != "handoff_reserved" or not isinstance(token, str)
                    or not row["handoff_token"] or not hmac.compare_digest(row["handoff_token"], token)
                    or row["lease_until"] is None or row["lease_until"] <= self.registry.clock()):
                raise DeviceInboxError("stale_handoff_claim")
            db.execute("UPDATE " + INBOX + """ SET state=?,local_task_id=?,reason_code=?,handoff_token=NULL,
                owner_run_id=NULL,lease_until=NULL,updated_at=? WHERE receipt_id=?""",
                (state, local_task_id, reason_code, self.registry.clock(), receipt_id))
            raw = self._raw(db.execute("SELECT * FROM " + INBOX + " WHERE receipt_id=?", (receipt_id,)).fetchone())
        return self._public(raw)

    @staticmethod
    def _matches(task, frozen):
        def immutable(envelope):
            result = {key: value for key, value in envelope.items() if key not in {"command_id", "evidence"}}
            result["comment_text"] = re.sub(r"\s+", "", result["comment_text"])
            return result
        try:
            expected_kind = "save" if frozen["authorization"]["action"] == "import" else "query"
            return (task["kind"] == expected_kind
                and task["binding"] == frozen["binding"] and task["user_id"] == frozen["user_id"]
                and task["device_id"] == frozen["device_id"] and task["authorization"] == frozen["authorization"]
                and immutable(task["envelope"]) == immutable(frozen["payload"]))
        except (KeyError, TypeError, AttributeError):
            return False

    def handoff_once(self, queue, *, run_id, lease_seconds=30):
        """Recoverably map to a queue record. New records cannot auto-execute."""
        frozen = self.claim_handoff(run_id=run_id, lease_seconds=lease_seconds)
        if frozen is None:
            return None
        envelope = frozen["payload"]
        try:
            # No Registry DB/OS lock remains held during any Queue operation.
            task = queue.lookup_by_source_reference(envelope["sender_profile_url"], envelope["receiver_profile_url"],
                                                   envelope["source_mode"], envelope["reference_id"])
            if task is not None:
                if not self._matches(task, frozen):
                    return self.finish_handoff(frozen["receipt_id"], frozen["handoff_token"],
                        state="held", reason_code="queue_original_conflict")
            else:
                binding = Binding.from_dict(frozen["binding"])
                if self.registry.authorize_snapshot(frozen["authorization"]) != binding:
                    return self.finish_handoff(frozen["receipt_id"], frozen["handoff_token"],
                        state="held", reason_code="authorization_changed")
                task = queue.enqueue(binding, envelope, kind="save" if frozen["authorization"]["action"] == "import" else "query",
                    user_id=frozen["user_id"], device_id=frozen["device_id"], authorization=frozen["authorization"],
                    initial_state="waiting_desktop")
            return self.finish_handoff(frozen["receipt_id"], frozen["handoff_token"], local_task_id=task["id"])
        except QueueError as error:
            if str(error) == "original_comment_conflict":
                return self.finish_handoff(frozen["receipt_id"], frozen["handoff_token"],
                    state="held", reason_code="queue_original_conflict")
            raise DeviceInboxError("device_inbox_handoff_unknown") from None
        except DeviceInboxError:
            raise
        except Exception:
            # Admission may already have committed. Keep the leased original,
            # then recover by read-only lookup after expiry; do not reset it.
            raise DeviceInboxError("device_inbox_handoff_unknown") from None
