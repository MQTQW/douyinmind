"""Product intake queue, separate from legacy agent and business databases.

An execution/publication reservation survives a crash. Recovery observes the
legacy receipt; it never assumes an interrupted operation did not happen.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from app.mention_agent.contracts import Binding, public_profile, validate_envelope
from app.mention_agent.store import AgentStoreError, verified_reply_readback_metadata
from app.storage_guard import (guarded_sqlite_file, StorageGuardError, SqliteOwnershipError,
    verified_readonly_connection, assert_no_orphan_sqlite_files)


COMPONENT = "DouyinMind.assistant.queue.v1"
STATES = frozenset({"queued", "waiting_desktop", "executing", "waiting_ingestion", "ready_to_publish", "publishing",
    "waiting_external", "waiting_library", "needs_reconcile", "needs_review", "b_view_visible", "delivery_unknown",
    "verification_required", "failed_no_submit", "failed", "cancelled", "completed_private"})
TERMINAL = frozenset({"b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit", "failed", "cancelled", "completed_private"})
SEND_TERMINAL = frozenset({"b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit"})
LABELS = {"queued": "等待处理", "waiting_desktop": "对应电脑尚未连接，任务尚未执行", "executing": "正在处理", "waiting_ingestion": "正在入库，完成后回复",
    "ready_to_publish": "等待回复", "publishing": "正在核对并回复", "waiting_external": "等待后台状态",
    "waiting_library": "已询问，等待你选择知识库",
    "needs_reconcile": "需要核对处理结果", "needs_review": "需要人工确认", "b_view_visible": "助手账号可见",
    "delivery_unknown": "发送结果未知，禁止重复发送", "verification_required": "需要人工验证",
    "failed_no_submit": "未提交回复", "failed": "处理未完成", "cancelled": "已停止此任务", "completed_private": "已处理，未公开回复"}
ISSUE_LABELS = {"remote_device_not_connected": "对应电脑尚未连接，这条提醒未执行。",
    "remote_catalog_unavailable": "对应电脑的知识库尚未接通，这条提醒未执行。",
    "library_not_resolved": "尚未明确可用的知识库，这条提醒未执行。",
    "authorization_unavailable": "授权信息尚不完整，这条提醒未执行。",
    "verified_local_binding_required": "还没有已确认的本机知识库授权，这条提醒未执行。",
    "unknown_library": "没有匹配到允许使用的知识库，这条提醒未执行。",
    "default_library_required": "请先选择默认知识库，这条提醒未执行。",
    "multiple_libraries": "这条提醒涉及多个知识库，请明确选择一个。",
    "multiple_targets": "这条提醒涉及多个目标，请明确选择当前视频和一个库。",
    "ambiguous_library_alias": "知识库别名存在歧义，这条提醒未执行。",
    "library_action_not_allowed": "当前知识库授权不允许这项操作，这条提醒未执行。",
    "import_not_allowed": "当前知识库未允许入库，这条提醒未执行。",
    "device_required": "请先选择对应电脑，这条提醒未执行。",
    "no_verified_local_device": "还没有已确认的本机设备，这条提醒未执行。",
    "default_device_not_verified": "默认设备尚未确认，这条提醒未执行。",
    "invalid_comment": "这条提醒暂时无法核对，尚未执行。",
    "empty_comment": "这条提醒没有明确内容，尚未执行。",
    "external_target_forbidden": "当前只处理原评论所在视频，这条提醒未执行。"}
ISSUE_LABELS.update({
    'all_local_not_allowed': '请先在本机确认本人全部资料查询权限，或明确指定已授权知识库。',
    'all_local_authorization_required': '请先在本机确认本人全部资料查询权限，或明确指定已授权知识库。',
    'catalog_public_reply_not_allowed': '尚未允许在评论区展示知识库清单，这条提醒未执行。',
})
_ISSUE_TABLE = """CREATE TABLE IF NOT EXISTS assistant_intake_issues(
  id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,sender_profile_url TEXT NOT NULL,receiver_profile_url TEXT NOT NULL,
  comment_text TEXT NOT NULL,video_id TEXT,reason_code TEXT NOT NULL,evidence TEXT NOT NULL,state TEXT NOT NULL,
  resolved_task_id TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
  UNIQUE(fingerprint,sender_profile_url,receiver_profile_url))"""
FORBIDDEN = frozenset({"api_key", "cookie", "cookies", "password", "authorization", "reasoning_content",
    "raw_messages", "credentials", "storage_state", "access_token", "refresh_token", "client_secret", "send_token",
    "claim_token", "execution_token", "publication_token", "poll_token", "lease_until"})
RESULT_KEYS = frozenset({"state", "agent_task_id", "reason_code", "usage", "import_state", "reply_state",
    "model_started", "ingestion_submitted", "send_token_present", "safe_to_execute", "safe_to_publish",
    "binding_active", "public_reply_allowed", "can_operator_resume", "can_operator_retry_query", "details"})
DELIVERY_READBACK_FAILURE_REASONS = frozenset({'readback_not_confirmed','readback_scope_changed',
    'readback_interrupted','readback_page_unavailable','readback_verification_required',
    'readback_confirmation_invalid','readback_result_unconfirmed','readback_unsupported',
    'readback_not_unique','readback_failed','readback_timeout','readback_auth_changed',
    'readback_stopped','readback_observer_unavailable','readback_original_not_verified',
    'readback_control_paused','readback_unconfirmed','readback_not_visible'})
_READBACK_RETRY_SECONDS = 120


def _verified_library_wait(result, kind):
    choice = result.get('details', {}).get('library_choice', {})
    return (kind == 'clarify' and result.get('state') == 'waiting_library'
            and result.get('reply_state') == 'b_view_visible'
            and result.get('send_token_present') is True
            and result.get('safe_to_publish') is False
            and result.get('binding_active') is True and result.get('public_reply_allowed') is True
            and isinstance(choice, dict) and choice.get('state') in {'waiting_library', 'selected', 'handoff_claimed'}
            and isinstance(choice.get('choice_id'), str)
            and re.fullmatch(r'[a-f0-9]{32,64}', choice['choice_id']) is not None)


class QueueError(ValueError):
    pass


def utc(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat(timespec="microseconds")


def text(value, limit=256, *, blank=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value or (not blank and not value.strip()):
        raise QueueError("invalid_text")
    return value.strip()


def code(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value):
        raise QueueError("invalid_reason_code")
    return value


def encoded(value, limit=65536):
    def inspect(item, depth=0):
        if depth > 12:
            raise QueueError("metadata_too_deep")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.casefold() in FORBIDDEN:
                    raise QueueError("forbidden_metadata")
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child, depth + 1)
    inspect(value)
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise QueueError("invalid_metadata") from None
    if len(result) > limit:
        raise QueueError("metadata_too_large")
    return result


def _validate_timings(timings):
    if not isinstance(timings, dict) or set(timings) - {"execution_ms", "model_ms", "retrieval_ms", "video_processing"}:
        raise QueueError("invalid_task_timings")
    for key in ("execution_ms", "model_ms", "retrieval_ms"):
        if key in timings and (type(timings[key]) is not int or timings[key] < 0):
            raise QueueError("invalid_task_timings")
    if "video_processing" not in timings:
        return
    processing = timings["video_processing"]
    if not isinstance(processing, dict) or set(processing) != {"run_id", "request_id", "video_id", "stages_seconds"}:
        raise QueueError("invalid_task_timings")
    for key in ("run_id", "request_id"):
        if not isinstance(processing[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", processing[key]):
            raise QueueError("invalid_task_timings")
    if not isinstance(processing["video_id"], str) or not re.fullmatch(r"[0-9]{5,30}", processing["video_id"]):
        raise QueueError("invalid_task_timings")
    stages = processing["stages_seconds"]
    if not isinstance(stages, dict) or set(stages) - {"download", "recognition", "indexing"}:
        raise QueueError("invalid_task_timings")
    for value in stages.values():
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise QueueError("invalid_task_timings")


def normalized_result(value):
    if not isinstance(value, dict) or set(value) - RESULT_KEYS or "state" not in value:
        raise QueueError("invalid_adapter_result")
    result = dict(value)
    state = result["state"]
    if not isinstance(state, str) or state not in STATES | {"answer_ready", "processing", "preflight_blocked", "not_sendable"}:
        raise QueueError("invalid_adapter_state")
    for name in ("agent_task_id", "import_state", "reply_state"):
        if result.get(name) is not None:
            text(result[name], 128)
    if result.get("reason_code") is not None:
        code(result["reason_code"])
    for name in ("model_started", "ingestion_submitted", "send_token_present", "safe_to_execute", "safe_to_publish",
                 "binding_active", "public_reply_allowed", "can_operator_resume", "can_operator_retry_query"):
        if name in result and type(result[name]) is not bool:
            raise QueueError("invalid_adapter_proof")
    usage = result.get("usage", {})
    if not isinstance(usage, dict) or set(usage) - {"input_tokens", "output_tokens", "total_tokens", "model_responses", "elapsed_ms"}:
        raise QueueError("invalid_usage")
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in usage.values()):
        raise QueueError("invalid_usage")
    if "details" in result:
        details = result["details"]
        if not isinstance(details, dict) or set(details) - {"reply_text", "library_name", "sources", "steps", "timings", "library_choice"}:
            raise QueueError("invalid_task_details")
        if 'library_choice' in details:
            choice = details['library_choice']
            allowed = {'choice_id', 'state', 'option_count', 'catalog_count', 'catalog_scope',
                       'selected_knowledge_base_id', 'selected_library_name', 'child_task_id'}
            if (not isinstance(choice, dict) or not {'choice_id', 'state'} <= set(choice) or set(choice) - allowed
                    or not isinstance(choice['choice_id'], str) or not re.fullmatch(r'[a-f0-9]{32,64}', choice['choice_id'])
                    or choice['state'] not in {'awaiting_ask', 'waiting_library', 'ask_unknown', 'ask_held',
                        'selected', 'handoff_claimed', 'handed_off', 'needs_review', 'cancelled'}):
                raise QueueError('invalid_public_library_choice')
            for key in ('option_count', 'catalog_count'):
                if key in choice and (type(choice[key]) is not int or not 0 <= choice[key] <= 1000000):
                    raise QueueError('invalid_public_library_choice')
            if choice.get('selected_knowledge_base_id') is not None and (
                    type(choice['selected_knowledge_base_id']) is not int or choice['selected_knowledge_base_id'] <= 0):
                raise QueueError('invalid_public_library_choice')
            if choice.get('child_task_id') is not None and (not isinstance(choice['child_task_id'], str)
                    or not re.fullmatch(r'[a-f0-9]{32}', choice['child_task_id'])):
                raise QueueError('invalid_public_library_choice')
            if choice.get('selected_library_name') is not None:
                text(choice['selected_library_name'], 128)
            if 'catalog_scope' in choice and choice['catalog_scope'] not in {'all_local', 'authorized_libraries'}:
                raise QueueError('invalid_public_library_choice')
        if "timings" in details:
            _validate_timings(details["timings"])
        for name, maximum in (("reply_text", 8000), ("library_name", 128)):
            if name in details:
                text(details[name], maximum, blank=True)
        sources = details.get("sources", [])
        if not isinstance(sources, list) or len(sources) > 8:
            raise QueueError("invalid_task_sources")
        for source in sources:
            if not isinstance(source, dict) or set(source) - {"source_id", "id", "title", "url", "platform_item_id", "chunk_id", "source_type", "time_range"}:
                raise QueueError("invalid_task_sources")
            for name, item in source.items():
                text(item, 64 if name == "time_range" else 2048, blank=True)
        steps = details.get("steps", [])
        if not isinstance(steps, list) or len(steps) > 32:
            raise QueueError("invalid_task_steps")
        allowed_metadata = {"round", "tool_name", "name", "status", "source_count", "elapsed_ms", "knowledge_base_id",
                            "code", "error_type", "http_status", "source_ids", "usage", "kind", "count"}
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"kind", "metadata", "created_at"}:
                raise QueueError("invalid_task_steps")
            if step.get("kind") not in {"model_request", "model_response", "tool_call", "tool_result", "decision", "error", "reply_readback_confirmation"}:
                raise QueueError("invalid_task_steps")
            if not isinstance(step.get("metadata"), dict) or set(step["metadata"]) - allowed_metadata:
                raise QueueError("invalid_task_steps")
    encoded(result)
    return result


def _delivery_readback_shape(task):
    """Only a frozen live choice reply, not another execution/publication."""
    try:
        if task['state']!='delivery_unknown' or task.get('reply_state')!='delivery_unknown' or task['kind']!='save':return None
        binding=Binding.from_dict(task['binding']);env=validate_envelope(task['envelope'],binding)
        if (env['source_mode']!='live_ui' or env['evidence'].get('source_kind')!='library_choice_reply'
                or binding.is_all_local or not binding.public_reply_allowed or not binding.active
                or not isinstance(task.get('agent_task_id'),str) or not re.fullmatch(r'[a-f0-9]{32}',task['agent_task_id'])):return None
        body=task.get('details',{}).get('reply_text')
        if not isinstance(body,str) or not body.strip() or len(body)>8000:return None
        target={key:value for key,value in env.items() if key!='evidence'}
        target.update(binding_id=binding.binding_id,instance_id=binding.instance_id,
            knowledge_base_id=binding.knowledge_base_id,choice_evidence=env['evidence'])
        return {'agent_task_id':task['agent_task_id'],'source_reference_id':env['reference_id'],
            'reply_sha256':hashlib.sha256(body.encode()).hexdigest(),
            'target_sha256':hashlib.sha256(encoded(target).encode()).hexdigest(),
            'binding_sha256':hashlib.sha256(encoded(binding.to_dict()).encode()).hexdigest(),
            'immutable_sha256':hashlib.sha256(task['immutable'].encode()).hexdigest(),
            'execution_attempts':task['execution_attempts'],'publish_attempts':task['publish_attempts']}
    except (KeyError,TypeError,ValueError):return None


def _readback_matches(metadata,shape):
    return (shape is not None and metadata.get('version')==1 and metadata.get('source_mode')=='live_ui'
        and metadata.get('scope')=='b_view_only' and metadata.get('outcome')=='b_view_visible'
        and metadata.get('original_reply_state')=='delivery_unknown'
        and all(metadata.get(key)==shape[key] for key in (
            'agent_task_id','source_reference_id','reply_sha256','target_sha256','binding_sha256')))


class AssistantQueue:
    def __init__(self, path, protected_paths=()):
        if str(path) == ":memory:":
            raise QueueError("persistent_queue_required")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.protected_paths = tuple(Path(item).resolve() for item in protected_paths)
        self._bootstrap_identity = None
        self._validate_location()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock():
            self._initialize()

    def _initialize(self):
        self._guard(missing_ok=True)
        assert_no_orphan_sqlite_files(self.path)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self._check_existing()
            with self._tx() as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(assistant_tasks)")}
                if "details" not in columns:
                    db.execute("ALTER TABLE assistant_tasks ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")
                if "authorization" not in columns:
                    db.execute("ALTER TABLE assistant_tasks ADD COLUMN authorization TEXT")
                db.execute(_ISSUE_TABLE)
        else:
            created = os.fstat(descriptor)
            self._bootstrap_identity = (created.st_dev, created.st_ino)
            os.close(descriptor)
            with self._connection(check_owner=False) as db:
                db.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE assistant_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE assistant_tasks(
                  id TEXT PRIMARY KEY,reference_key TEXT UNIQUE NOT NULL,immutable TEXT NOT NULL,
                  binding_id TEXT NOT NULL,binding TEXT NOT NULL,envelope TEXT NOT NULL,kind TEXT NOT NULL,
                  user_id TEXT,device_id TEXT,authorization TEXT,video_title TEXT NOT NULL,state TEXT NOT NULL,
                  agent_task_id TEXT,reason_code TEXT,import_state TEXT,reply_state TEXT,usage TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}',
                  execution_token TEXT,publication_token TEXT,owner_run_id TEXT,lease_until REAL,
                  execution_attempts INTEGER NOT NULL DEFAULT 0,publish_attempts INTEGER NOT NULL DEFAULT 0,
                  reconciliation TEXT,reconciled_at REAL,next_poll_at REAL NOT NULL DEFAULT 0,
                  poll_token TEXT,poll_until REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE assistant_events(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,
                  kind TEXT NOT NULL,metadata TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TABLE assistant_seen(binding_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                  reason TEXT NOT NULL,task_id TEXT,created_at REAL NOT NULL,PRIMARY KEY(binding_id,fingerprint));
                CREATE TABLE assistant_baselines(binding_id TEXT PRIMARY KEY,created_at REAL NOT NULL);
                CREATE TABLE assistant_actions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,action TEXT NOT NULL,
                  state TEXT NOT NULL,reason_code TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                INSERT INTO assistant_meta VALUES('component','DouyinMind.assistant.queue.v1');
                COMMIT;""")
                db.execute(_ISSUE_TABLE)

    def _validate_location(self):
        if self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent:
            raise QueueError("redirected_queue_path")
        for item in self.protected_paths:
            if self.path == item or self.path.is_relative_to(item) or item.is_relative_to(self.path):
                raise QueueError("protected_queue_path")

    def _guard(self, missing_ok=False):
        self._validate_location()
        for candidate in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                if candidate == self.path and not missing_ok:
                    raise QueueError("queue_missing") from None
                continue
            if candidate != self.path and stat.S_ISREG(info.st_mode) and info.st_nlink == 0:
                # Windows may expose a just-unlinked SQLite journal with zero
                # links while another connection commits. Re-stat immediately;
                # only a vanished sidecar is harmless. A replacement is still
                # subject to every regular-file/link/redirect check below.
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 0 and not candidate.exists():
                    continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or candidate.resolve() != candidate:
                raise QueueError("unsafe_queue_file")

    @staticmethod
    def _owned(db):
        try:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='component'").fetchone()
            return row is not None and row[0] == COMPONENT
        except sqlite3.DatabaseError:
            return False

    def _check_existing(self):
        with self._file_lock():
            try:
                with verified_readonly_connection(self.path, self._owned, guard=self._guard):
                    pass
            except (SqliteOwnershipError, sqlite3.DatabaseError):
                raise QueueError("foreign_queue_database") from None

    @contextmanager
    def _file_lock(self):
        try:
            with guarded_sqlite_file(self.path):
                yield
        except StorageGuardError as error:
            reason = "unsafe_queue_file" if str(error) == "unsafe_storage_guard_database" else str(error)
            raise QueueError(reason) from None

    @contextmanager
    def _connection(self, check_owner=True):
        with self._file_lock():
            self._guard()
            observed = self.path.lstat()
            identity = (observed.st_dev, observed.st_ino)
            if check_owner:
                self._check_existing()
            else:
                if self._bootstrap_identity != identity or observed.st_size != 0:
                    raise QueueError("queue_bootstrap_not_new")
                # A single exclusive-created main may initialize its schema.
                # This flag can never bypass ownership on a cached object.
                self._bootstrap_identity = None
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                self._guard()
                current = self.path.lstat()
                if (current.st_dev, current.st_ino) != identity:
                    raise QueueError("queue_identity_changed")
                if check_owner and not self._owned(db):
                    raise QueueError("foreign_queue_database")
                yield db
            finally:
                db.close()

    @contextmanager
    def _tx(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _raw(row):
        if row is None:
            return None
        value = dict(row)
        for name in ("binding", "envelope", "usage", "reconciliation", "details", "authorization"):
            value[name] = json.loads(value[name]) if value[name] is not None else None
        value["knowledge_base_id"] = value["binding"]["knowledge_base_id"]
        value["video_id"] = value["envelope"]["video_id"]
        return value

    @staticmethod
    def _event(db, task_id, kind, metadata):
        db.execute("INSERT INTO assistant_events(task_id,kind,metadata,created_at) VALUES(?,?,?,?)",
                   (task_id, kind, encoded(metadata, 8192), time.time()))

    def enqueue(self, binding, envelope, *, video_title="", kind="unknown", notice_fingerprint=None, user_id=None, device_id=None, authorization=None, initial_state="queued"):
        binding = binding if isinstance(binding, Binding) else Binding.from_dict(binding)
        envelope = validate_envelope(envelope, binding)
        if not isinstance(kind, str) or kind not in {"unknown", "query", "save", "status", "clarify"}:
            raise QueueError("invalid_task_kind")
        if not isinstance(initial_state, str) or initial_state not in {"queued", "waiting_desktop"}:
            raise QueueError("invalid_initial_state")
        video_title = text(video_title, 512, blank=True)
        for value in (user_id, device_id, notice_fingerprint):
            if value is not None:
                text(value, 256)
        if authorization is not None:
            expected = {"registry_id", "user_id", "device_id", "grant_id", "user_revision", "device_revision", "grant_revision",
                        "grant_version", "binding_id", "instance_id", "knowledge_base_id", "action", "public_reply_allowed"}
            if binding.is_all_local:
                expected.add('query_scope')
            if not isinstance(authorization, dict) or set(authorization) != expected:
                raise QueueError("invalid_authorization_snapshot")
            if (any(authorization[name] != getattr(binding, name) for name in ("binding_id", "instance_id", "knowledge_base_id", "public_reply_allowed"))
                    or (authorization['knowledge_base_id'] is not None if binding.is_all_local
                        else type(authorization["knowledge_base_id"]) is not int)
                    or binding.is_all_local and (authorization.get('query_scope') != 'all_local' or authorization.get('action') != 'query')
                    or type(authorization["public_reply_allowed"]) is not bool
                    or not isinstance(authorization["action"], str) or authorization["action"] not in {"query", "import"}
                    or (user_id is not None and user_id != authorization["user_id"])
                    or (device_id is not None and device_id != authorization["device_id"])):
                raise QueueError("authorization_scope_mismatch")
            for name in ("registry_id", "user_id", "device_id", "grant_id"):
                text(authorization[name], 128)
            for name in ("user_revision", "device_revision", "grant_revision", "grant_version"):
                if type(authorization[name]) is not int or authorization[name] < 1:
                    raise QueueError("invalid_authorization_snapshot")
        reference = {name: envelope[name] for name in ("sender_profile_url", "receiver_profile_url", "source_mode", "reference_id")}
        reference_key = hashlib.sha256(encoded(reference).encode()).hexdigest()
        immutable_envelope = {name: value for name, value in envelope.items() if name not in {"command_id", "evidence"}}
        immutable_envelope["comment_text"] = re.sub(r"\s+", "", immutable_envelope["comment_text"])
        proof = envelope['evidence']
        if proof.get('source_kind') == 'conversation_followup':
            # Re-observation can refresh time, never exchange the published
            # anchor, parent quality, account identity or exact prior receipt.
            immutable_envelope['followup_parent'] = {name: value for name, value in proof.items()
                if name != 'observed_at_utc'}
        if proof.get('source_kind') == 'library_choice_reply':
            # An observed-at refresh is allowed; changing the original parent,
            # prompt or selected visible value cannot borrow a queued reply.
            immutable_envelope['choice_parent'] = {name: proof[name] for name in (
                'source_kind', 'evidence_version', 'choice_id', 'instance_id', 'root_reference_id',
                'root_comment_text', 'ask_ref', 'ask_reply_ref', 'ask_text', 'parent_scope',
                'direct_reply_to_ask', 'selection_text')}
            for name in ('selection_value', 'receiver_reply_label', 'root_native_comment_id', 'ask_native_comment_id'):
                if name in proof:
                    immutable_envelope['choice_parent'][name] = proof[name]
        immutable_value = {"binding": binding.to_dict(), "envelope": immutable_envelope, "user_id": user_id, "device_id": device_id}
        if authorization is not None:
            immutable_value["permission_snapshot"] = authorization
        immutable = encoded(immutable_value)
        with self._tx() as db:
            row = db.execute("SELECT * FROM assistant_tasks WHERE reference_key=?", (reference_key,)).fetchone()
            if row and row["immutable"] != immutable:
                raise QueueError("original_comment_conflict")
            if row is None:
                now, task_id = time.time(), secrets.token_hex(16)
                db.execute("""INSERT INTO assistant_tasks(id,reference_key,immutable,binding_id,binding,envelope,kind,
                  user_id,device_id,authorization,video_title,state,usage,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'{}',?,?)""",
                  (task_id, reference_key, immutable, binding.binding_id, encoded(binding.to_dict()), encoded(envelope),
                   kind, user_id, device_id, encoded(authorization) if authorization is not None else None, video_title, initial_state, now, now))
                self._event(db, task_id, "received", {"source_mode": envelope["source_mode"], "kind": kind, "initial_state": initial_state})
                row = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if notice_fingerprint is not None:
                self._remember(db, binding.binding_id, notice_fingerprint, "received", row["id"])
            return self._raw(row)

    def get_task(self, task_id):
        with self._connection() as db:
            return self._raw(db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone())

    def lookup_by_source_reference(self, sender_profile_url, receiver_profile_url, source_mode, reference_id):
        """Find an original task without changing its state or frozen authority.

        This is an internal recovery observation, not an admission/authentication
        proof. A caller must compare the frozen payload or reuse enqueue's
        conflict checks before acknowledging any separate durable inbox row.
        Returned claims remain internal and must never be projected directly.
        """
        if (not isinstance(source_mode, str) or source_mode not in {"live_ui", "replay", "isolated"}
                or not isinstance(reference_id, str) or not reference_id or len(reference_id) > 256):
            raise QueueError("invalid_source_reference")
        try:
            sender = public_profile(sender_profile_url)
            receiver = public_profile(receiver_profile_url)
        except ValueError:
            raise QueueError("invalid_source_reference") from None
        reference = {"sender_profile_url": sender, "receiver_profile_url": receiver,
                     "source_mode": source_mode, "reference_id": reference_id}
        reference_key = hashlib.sha256(encoded(reference).encode()).hexdigest()
        with self._connection() as db:
            return self._raw(db.execute("SELECT * FROM assistant_tasks WHERE reference_key=?", (reference_key,)).fetchone())

    @staticmethod
    def _resume_phase(task, now):
        proof = task.get("reconciliation")
        if (task["state"] not in {"needs_review", "needs_reconcile"} or not isinstance(proof, dict)
                or task.get("reconciled_at") is None or not 0 <= now - task["reconciled_at"] <= 120
                or proof.get("binding_active") is not True or proof.get("send_token_present") is not False
                or proof.get("state") in {"delivery_unknown", "verification_required", "b_view_visible"}):
            return None
        if (proof.get("safe_to_execute") is True and proof.get("model_started") is False
                and proof.get("ingestion_submitted") is False and proof.get("state") == "queued"
                and not task.get("publication_token")):
            return "execute"
        if (proof.get("safe_to_publish") is True and proof.get("state") in {"answer_ready", "ready_to_publish"}
                and proof.get("reply_state") == "ready" and proof.get("public_reply_allowed") is True):
            return "publish"
        if (proof.get("can_operator_resume") is True and proof.get("state") == "needs_review"
                and proof.get("reason_code") in {"preflight_failed", "preflight_verification_required"} and proof.get("reply_state") == "needs_review"
                and proof.get("public_reply_allowed") is True):
            return "operator"
        return None

    def public_task(self, task, *, detail=False):
        if task is None:
            return None
        value = {name: task.get(name) for name in ("id", "kind", "state", "binding_id", "user_id", "device_id", "knowledge_base_id",
            "video_id", "video_title", "agent_task_id", "reason_code", "import_state", "reply_state", "usage")}
        value['query_scope'] = task.get('binding', {}).get('query_scope', 'library')
        value['scope_label'] = '全部入库资料' if value['query_scope'] == 'all_local' else '指定知识库'
        if task.get("import_state"):
            processing_stage = task["import_state"]
        elif task["state"] in {"ready_to_publish", "publishing", "b_view_visible", "completed_private"}:
            processing_stage = "done"
        elif task["state"] in {"needs_review", "needs_reconcile", "waiting_external", "delivery_unknown", "verification_required", "failed_no_submit", "failed", "cancelled"}:
            processing_stage = "not_confirmed"
        else:
            processing_stage = task["state"]
        value.update({"comment_text": task["envelope"]["comment_text"], "status_label": LABELS[task["state"]],
            "created_at": utc(task["created_at"]), "updated_at": utc(task["updated_at"]),
            "actions": {"can_reconcile": task["state"] in {"needs_reconcile", "needs_review", "waiting_external", "waiting_ingestion", "waiting_library"},
                        "can_resume": self._resume_phase(task, time.time()) is not None,
                        "can_retry_query": self._query_retry_allowed(task, time.time()) and self.get_query_retry(task['id']) is None},
            "stages": {"intake": "received", "processing": processing_stage,
                       "reply": task["state"] if task["state"] in SEND_TERMINAL else task.get("reply_state") or "not_confirmed"},
            "visibility_scope": "b_view_only"})
        if task["state"] == "waiting_desktop":
            value["stages"]["processing"] = "not_started"
            value["stages"]["reply"] = "not_submitted"
            value["execution_note"] = "对应电脑尚未连接；尚未调用模型或读取该电脑的资料。"
        if task["state"] == "completed_private":
            value["stages"]["reply"] = "not_public"
        if task['state'] == 'waiting_library':
            value['stages']['processing'] = 'not_started'
            value['execution_note'] = '已在原评论询问目标知识库；确认选择后再保存，当前尚未提交入库。'
            choice = task.get('details', {}).get('library_choice', {})
            if choice.get('state') in {'selected', 'handoff_claimed'}:
                value['status_label'] = '已选择，准备保存'
                value['execution_note'] = '已记录你的选择，正在接续保存任务；不会重新发送问库评论。'
        if detail:
            value["timeline"] = self.list_events(task["id"])
            value["details"] = task.get("details", {})
        return value

    def list_tasks(self, *, state=None, q=None, page=1, page_size=20, binding_id=None):
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise QueueError("invalid_pagination")
        if state is not None and state not in STATES:
            raise QueueError("invalid_task_state")
        query = text(q, 256, blank=True) if q is not None else None
        where, params = [], []
        for key, value in (("state", state), ("binding_id", binding_id)):
            if value is not None:
                where.append(key + "=?")
                params.append(value)
        if query:
            where.append("(video_title LIKE ? ESCAPE '\\' OR envelope LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([pattern] * 3)
        suffix = " WHERE " + " AND ".join(where) if where else ""
        with self._connection() as db:
            total = db.execute("SELECT COUNT(*) FROM assistant_tasks" + suffix, params).fetchone()[0]
            rows = db.execute("SELECT * FROM assistant_tasks" + suffix + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                              (*params, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [self.public_task(self._raw(row)) for row in rows], "total": total, "page": page, "page_size": page_size}

    def list_events(self, task_id):
        with self._connection() as db:
            rows = db.execute("SELECT kind,metadata,created_at FROM assistant_events WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            return [{"kind": row["kind"], "metadata": json.loads(row["metadata"]), "at": utc(row["created_at"])} for row in rows]

    def counts(self):
        result = {state: 0 for state in sorted(STATES)}
        with self._connection() as db:
            for row in db.execute("SELECT state,COUNT(*) AS total FROM assistant_tasks GROUP BY state"):
                result[row["state"]] = row["total"]
        return result

    def claim_next(self, phase="execute", *, run_id="local", lease_seconds=1800):
        if phase not in {"execute", "publish"} or type(lease_seconds) not in (int, float) or not 1 <= lease_seconds <= 7200:
            raise QueueError("invalid_claim")
        run_id = text(run_id, 128)
        source, target = ("queued", "executing") if phase == "execute" else ("ready_to_publish", "publishing")
        token_column = "execution_token" if phase == "execute" else "publication_token"
        attempts_column = "execution_attempts" if phase == "execute" else "publish_attempts"
        with self._tx() as db:
            if db.execute("SELECT 1 FROM assistant_tasks WHERE state=? LIMIT 1", (target,)).fetchone():
                return None
            candidates = db.execute("SELECT * FROM assistant_tasks WHERE state=? ORDER BY created_at,id", (source,)).fetchall()
            row = None
            for candidate in candidates:
                retry = self._query_retry(db, candidate['id']) if phase == 'execute' else None
                if retry is not None and (retry['state'] != 'queued' or candidate['immutable'] != retry['immutable']
                        or candidate['kind'] != 'query' or candidate['agent_task_id'] != retry['agent_task_id']
                        or candidate['publication_token']):
                    db.execute("UPDATE assistant_tasks SET state='needs_review',reason_code='query_retry_not_safe',updated_at=? WHERE id=?", (time.time(),candidate['id']))
                    continue
                if phase == "publish":
                    envelope, binding = json.loads(candidate["envelope"]), json.loads(candidate["binding"])
                    if envelope["source_mode"] != "live_ui" or binding["public_reply_allowed"] is not True:
                        db.execute("UPDATE assistant_tasks SET state='needs_review',reason_code='public_send_not_authorized',updated_at=? WHERE id=?",
                                   (time.time(), candidate["id"]))
                        self._event(db, candidate["id"], "publication_blocked", {"reason_code": "public_send_not_authorized"})
                        continue
                row = candidate
                break
            if row is None:
                return None
            now, token = time.time(), secrets.token_hex(24)
            db.execute("UPDATE assistant_tasks SET state=?," + token_column + "=?," + attempts_column + "=" + attempts_column + "+1,owner_run_id=?,lease_until=?,updated_at=? WHERE id=?",
                       (target, token, run_id, now + lease_seconds, now, row["id"]))
            self._event(db, row["id"], "reserved_" + phase, {"run_id": run_id})
            result = self._raw(db.execute("SELECT * FROM assistant_tasks WHERE id=?", (row["id"],)).fetchone())
            retry = self._query_retry(db, row['id']) if phase == 'execute' else None
            if retry is not None:
                db.execute("UPDATE assistant_query_retries SET state='claimed',updated_at=? WHERE task_id=? AND state='queued'", (now,row['id']))
                result['_query_retry_action_id'] = retry['action_id']
            return result

    def finish(self, task_id, token, phase, result):
        result = normalized_result(result)
        if phase not in {"execute", "publish"}:
            raise QueueError("invalid_phase")
        expected = "executing" if phase == "execute" else "publishing"
        column = "execution_token" if phase == "execute" else "publication_token"
        with self._tx() as db:
            row = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row["state"] != expected or not isinstance(token, str) or not row[column] or not secrets.compare_digest(row[column], token):
                raise QueueError("stale_task_claim")
            state = {"answer_ready": "ready_to_publish", "processing": "waiting_external", "queued": "needs_reconcile",
                     "preflight_blocked": "needs_review", "not_sendable": "needs_reconcile"}.get(result["state"], result["state"])
            readonly_import_wait = (phase == "publish" and state == "waiting_ingestion"
                and result.get("send_token_present") is False and result.get("reply_state") == "ready"
                and result.get("binding_active") is True and result.get("ingestion_submitted") is True)
            readonly_library_wait = phase == 'publish' and _verified_library_wait(result, row['kind'])
            if phase == "publish" and not readonly_import_wait and not readonly_library_wait and state not in TERMINAL | {"needs_review", "needs_reconcile"}:
                state = "needs_reconcile"
            now = time.time()
            db.execute("""UPDATE assistant_tasks SET state=?,agent_task_id=COALESCE(?,agent_task_id),reason_code=?,
              import_state=COALESCE(?,import_state),reply_state=COALESCE(?,reply_state),usage=?,details=?,lease_until=NULL,
              reconciliation=NULL,reconciled_at=NULL,next_poll_at=?,updated_at=? WHERE id=?""",
              (state, result.get("agent_task_id"), result.get("reason_code"), result.get("import_state"), result.get("reply_state", state if state in SEND_TERMINAL else None),
               encoded(result.get("usage", json.loads(row["usage"]))), encoded(result.get("details", json.loads(row["details"]))),
               now + (45 if state == 'waiting_library' else 10) if state in {'waiting_ingestion', 'waiting_library'} else 0, now, task_id))
            self._event(db, task_id, "finished_" + phase, {key: value for key, value in result.items() if key != "details"})
            if phase == 'execute' and self._query_retry(db, task_id) is not None:
                db.execute("UPDATE assistant_query_retries SET state='finished',updated_at=? WHERE task_id=? AND state='claimed'", (now,task_id))
            return self._raw(db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone())

    def recover_interrupted(self, *, current_run_id=None):
        with self._tx() as db:
            rows = db.execute("SELECT id,state FROM assistant_tasks WHERE state IN ('executing','publishing') AND (? IS NULL OR owner_run_id!=?)",
                              (current_run_id, current_run_id)).fetchall()
            for row in rows:
                db.execute("UPDATE assistant_tasks SET state='needs_reconcile',reason_code='interrupted_operation',lease_until=NULL,reconciliation=NULL,reconciled_at=NULL,updated_at=? WHERE id=?",
                           (time.time(), row["id"]))
                self._event(db, row["id"], "restart_fence", {"previous_state": row["state"]})
            db.execute("UPDATE assistant_actions SET state='failed',reason_code='interrupted_action',updated_at=? WHERE state='processing'", (time.time(),))
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_query_retries'").fetchone():
                interrupted = db.execute("SELECT task_id FROM assistant_query_retries WHERE state IN ('reserved','claimed')").fetchall()
                for retry in interrupted:
                    db.execute("UPDATE assistant_query_retries SET state='held',updated_at=? WHERE task_id=?", (time.time(),retry['task_id']))
                    db.execute("UPDATE assistant_tasks SET state='needs_review',reason_code='query_retry_interrupted',updated_at=? WHERE id=? AND state NOT IN ('b_view_visible','delivery_unknown','verification_required','failed_no_submit','completed_private')", (time.time(),retry['task_id']))
            return [row["id"] for row in rows]

    def claim_waiting(self, *, poll_seconds=10, lease_seconds=30):
        if not 1 <= poll_seconds <= 300 or not 1 <= lease_seconds <= 300:
            raise QueueError("invalid_poll_budget")
        now = time.time()
        with self._tx() as db:
            row = db.execute("""SELECT * FROM assistant_tasks WHERE state IN ('waiting_ingestion','waiting_library') AND next_poll_at<=?
              AND (poll_until IS NULL OR poll_until<=?) ORDER BY next_poll_at,created_at,id LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return None
            interval = max(45, poll_seconds) if row['state'] == 'waiting_library' else poll_seconds
            db.execute("UPDATE assistant_tasks SET poll_token=?,poll_until=?,next_poll_at=? WHERE id=?",
                       (secrets.token_hex(24), now + lease_seconds, now + interval, row["id"]))
            return self._raw(db.execute("SELECT * FROM assistant_tasks WHERE id=?", (row["id"],)).fetchone())

    def claim_delivery_readback(self, run_id, lease_seconds=90):
        """Lease an existing uncertain choice receipt for observation only."""
        run_id=text(run_id,128)
        if type(lease_seconds) not in (int,float) or not math.isfinite(lease_seconds) or not 1<=lease_seconds<=300:
            raise QueueError('invalid_readback_lease')
        now=time.time()
        with self._tx() as db:
            control=db.execute("SELECT value FROM assistant_meta WHERE key='control'").fetchone()
            control=json.loads(control[0]) if control else {}
            if control.get('state')!='running' or control.get('desired_state')!='running' or control.get('run_id')!=run_id:
                return None
            # Incoming execution/publication always has priority over an old
            # optional readback. A readback never claims those lanes.
            if db.execute("SELECT 1 FROM assistant_tasks WHERE state IN ('queued','executing','ready_to_publish','publishing') LIMIT 1").fetchone():return None
            if db.execute("SELECT 1 FROM assistant_tasks WHERE state='delivery_unknown' AND poll_token IS NOT NULL AND poll_until>? LIMIT 1",(now,)).fetchone():return None
            rows=db.execute("""SELECT * FROM assistant_tasks WHERE state='delivery_unknown' AND next_poll_at<=?
                AND (poll_until IS NULL OR poll_until<=?) ORDER BY created_at DESC,id DESC""",(now,now)).fetchall()
            for row in rows:
                shape=_delivery_readback_shape(self._raw(row))
                if shape is None:continue
                attempt=db.execute("SELECT COUNT(*) FROM assistant_events WHERE task_id=? AND kind='delivery_readback_claimed'",(row['id'],)).fetchone()[0]+1
                if attempt>3:continue
                token=secrets.token_hex(24)
                db.execute('UPDATE assistant_tasks SET poll_token=?,poll_until=?,next_poll_at=? WHERE id=?',
                    (token,now+lease_seconds,now+_READBACK_RETRY_SECONDS,row['id']))
                self._event(db,row['id'],'delivery_readback_claimed',{**shape,'attempt':attempt,'run_id':run_id,
                    'claimed_at_utc':utc(now),'expires_at_utc':utc(now+lease_seconds)})
                result=self._raw(db.execute('SELECT * FROM assistant_tasks WHERE id=?',(row['id'],)).fetchone())
                result['_delivery_readback_claim_token']=token
                result['_delivery_readback_attempt']=attempt
                return result
            return None

    def list_delivery_readback_candidates(self,limit=3,*,before=None):
        """Local proof recovery candidates, including exhausted observations.

        This neither reserves an observation nor advances its attempts/backoff.
        The helper may only load an already-persisted Agent proof for these.
        """
        if type(limit) is not int or not 1<=limit<=24:raise QueueError('invalid_readback_limit')
        if before is not None and (not isinstance(before,(tuple,list)) or len(before)!=2
                or type(before[0]) not in (int,float) or not math.isfinite(before[0]) or before[0]<0
                or not isinstance(before[1],str) or re.fullmatch(r'[a-f0-9]{32}',before[1]) is None):
            raise QueueError('invalid_readback_cursor')
        now=time.time();result=[]
        with self._connection() as db:
            where='';arguments=[now]
            if before is not None:
                where=' AND (created_at<? OR (created_at=? AND id<?))'
                arguments.extend((before[0],before[0],before[1]))
            for row in db.execute("""SELECT * FROM assistant_tasks WHERE state='delivery_unknown'
                    AND reply_state='delivery_unknown' AND kind='save'
                    AND (poll_until IS NULL OR poll_until<=?)"""+where+' ORDER BY created_at DESC,id DESC',arguments):
                raw=self._raw(row)
                if _delivery_readback_shape(raw) is not None:
                    result.append(raw)
                    if len(result)>=limit:break
        return result

    def project_recorded_delivery_readback(self,task_id,credential,*,run_id=None):
        """Recover a durable Agent fact locally, without a fourth observation."""
        if run_id is not None:run_id=text(run_id,128)
        try:metadata=verified_reply_readback_metadata(credential)
        except AgentStoreError:raise QueueError('unverified_readback_confirmation') from None
        now=time.time()
        with self._tx() as db:
            row=db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone()
            if row is None:raise QueueError('task_not_found')
            old=db.execute("SELECT metadata FROM assistant_events WHERE task_id=? AND kind IN ('delivery_readback_confirmed','delivery_readback_recovered') ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
            if old and row['state']=='b_view_visible':
                previous=json.loads(old[0]);shape=_delivery_readback_shape({**self._raw(row),'state':'delivery_unknown','reply_state':'delivery_unknown'})
                if previous.get('confirmation')==metadata and previous.get('snapshot')==shape:return self._raw(row)
                raise QueueError('readback_confirmation_conflict')
            if row['state']!='delivery_unknown':raise QueueError('readback_not_unknown')
            if row['poll_until'] is not None and row['poll_until']>now:raise QueueError('readback_observation_busy')
            shape=_delivery_readback_shape(self._raw(row))
            if not _readback_matches(metadata,shape):raise QueueError('readback_confirmation_mismatch')
            control=db.execute("SELECT value FROM assistant_meta WHERE key='control'").fetchone()
            control=json.loads(control[0]) if control else {}
            if (control.get('state')!='running' or control.get('desired_state')!='running'
                    or (run_id is not None and control.get('run_id')!=run_id)):
                raise QueueError('readback_control_paused')
            db.execute("UPDATE assistant_tasks SET state='b_view_visible',reply_state='b_view_visible',poll_token=NULL,poll_until=NULL,next_poll_at=0,updated_at=? WHERE id=?",(now,task_id))
            self._event(db,task_id,'delivery_readback_recovered',{'confirmation':metadata,'snapshot':shape,
                'recovery':'persisted_agent_readback_only'})
            return self._raw(db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone())

    def finish_delivery_readback(self, task_id, claim_token, confirmation=None, reason_code=None):
        """Project a verified B-only observation; preserve every send barrier."""
        if confirmation is not None:
            if reason_code is not None:raise QueueError('conflicting_readback_result')
            try:metadata=verified_reply_readback_metadata(confirmation)
            except AgentStoreError:raise QueueError('unverified_readback_confirmation') from None
        else:
            metadata=None
            if reason_code is None:reason_code='readback_not_confirmed'
            if not isinstance(reason_code,str) or reason_code not in DELIVERY_READBACK_FAILURE_REASONS:raise QueueError('invalid_readback_reason')
        now=time.time()
        with self._tx() as db:
            row=db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone()
            if row is None:raise QueueError('task_not_found')
            old=db.execute("SELECT metadata FROM assistant_events WHERE task_id=? AND kind IN ('delivery_readback_confirmed','delivery_readback_recovered') ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
            if old and metadata is not None and row['state']=='b_view_visible':
                previous=json.loads(old[0])
                shape=_delivery_readback_shape({**self._raw(row),'state':'delivery_unknown','reply_state':'delivery_unknown'})
                if previous.get('confirmation')==metadata and previous.get('snapshot')==shape:return self._raw(row)
                raise QueueError('readback_confirmation_conflict')
            if (row['state']!='delivery_unknown' or not isinstance(claim_token,str) or not row['poll_token']
                    or not secrets.compare_digest(claim_token,row['poll_token']) or row['poll_until'] is None or row['poll_until']<=now):
                raise QueueError('stale_readback_claim')
            event=db.execute("SELECT metadata FROM assistant_events WHERE task_id=? AND kind='delivery_readback_claimed' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
            if event is None:raise QueueError('readback_claim_missing')
            claimed=json.loads(event[0]);shape=_delivery_readback_shape(self._raw(row))
            if shape is None or any(claimed.get(key)!=value for key,value in shape.items()):raise QueueError('readback_scope_changed')
            if metadata is not None:
                control=db.execute("SELECT value FROM assistant_meta WHERE key='control'").fetchone()
                control=json.loads(control[0]) if control else {}
                if control.get('state')!='running' or control.get('desired_state')!='running' or control.get('run_id')!=claimed.get('run_id'):
                    raise QueueError('readback_control_paused')
                if not _readback_matches(metadata,shape):
                    raise QueueError('readback_confirmation_mismatch')
                db.execute("UPDATE assistant_tasks SET state='b_view_visible',reply_state='b_view_visible',poll_token=NULL,poll_until=NULL,next_poll_at=0,updated_at=? WHERE id=?",(now,task_id))
                self._event(db,task_id,'delivery_readback_confirmed',{'attempt':claimed['attempt'],'confirmation':metadata,'snapshot':shape})
            else:
                db.execute('UPDATE assistant_tasks SET poll_token=NULL,poll_until=NULL,next_poll_at=?,updated_at=? WHERE id=?',
                    (max(row['next_poll_at'],now+_READBACK_RETRY_SECONDS),now,task_id))
                self._event(db,task_id,'delivery_readback_failed',{'attempt':claimed['attempt'],'reason_code':reason_code})
            return self._raw(db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone())

    def apply_reconciliation(self, task_id, result, *, poll_token=None):
        result = normalized_result(result)
        with self._tx() as db:
            row = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise QueueError("task_not_found")
            if poll_token is not None and (row["poll_token"] != poll_token or row["state"] not in {'waiting_ingestion','waiting_library'}
                    or row["poll_until"] is None or row["poll_until"] <= time.time()):
                raise QueueError("stale_poll_claim")
            if row["state"] in TERMINAL:
                return self._raw(row)  # No late callback can reopen a terminal send barrier.
            if row["state"] in {"executing", "publishing"}:
                raise QueueError("task_busy")
            reported = result["state"]
            state = reported if reported in TERMINAL else "needs_review"
            if _verified_library_wait(result, row['kind']):
                state = 'waiting_library'
            if row["state"] == "waiting_desktop":
                # There is no cross-device transport in this increment. Neither
                # a model nor reconciliation can silently adopt a remote task.
                state = "waiting_desktop"
            if row["state"] == "waiting_ingestion":
                if (reported in {"answer_ready", "ready_to_publish"} and result.get("safe_to_publish") is True
                        and result.get("send_token_present") is False and result.get("reply_state") == "ready"
                        and result.get("binding_active") is True and result.get("public_reply_allowed") is True
                        and result.get("import_state") in {"succeeded", "failed", "failed_no_submit"}):
                    state = "ready_to_publish"
                elif reported in {"waiting_ingestion", "processing", "waiting_external"}:
                    state = "waiting_ingestion"
            now = time.time()
            db.execute("""UPDATE assistant_tasks SET state=?,agent_task_id=COALESCE(?,agent_task_id),reason_code=?,
              import_state=COALESCE(?,import_state),reply_state=COALESCE(?,reply_state),details=?,reconciliation=?,reconciled_at=?,
              poll_token=NULL,poll_until=NULL,updated_at=? WHERE id=?""",
              (state, result.get("agent_task_id"), result.get("reason_code"), result.get("import_state"), result.get("reply_state", state if state in SEND_TERMINAL else None),
               encoded(result.get("details", json.loads(row["details"]))), encoded({key: value for key, value in result.items() if key != "details"}), now, now, task_id))
            self._event(db, task_id, "reconciled", {key: value for key, value in result.items() if key != "details"})
            return self._raw(db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone())

    def resume(self, task_id):
        with self._tx() as db:
            row = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise QueueError("task_not_found")
            phase = self._resume_phase(self._raw(row), time.time())
            if phase is None:
                raise QueueError("resume_not_safe")
            if phase == "operator":
                raise QueueError("resume_requires_operator_adapter")
            state = "queued" if phase == "execute" else "ready_to_publish"
            db.execute("UPDATE assistant_tasks SET state=?,reason_code=NULL,reconciliation=NULL,reconciled_at=NULL,updated_at=? WHERE id=?",
                       (state, time.time(), task_id))
            self._event(db, task_id, "operator_resume", {"phase": phase})
            return self._raw(db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone())

    @staticmethod
    def _remember(db, binding_id, fingerprint, reason, task_id=None):
        db.execute("INSERT OR IGNORE INTO assistant_seen VALUES(?,?,?,?,?)", (binding_id, fingerprint, reason, task_id, time.time()))

    def ensure_capture_baseline(self, binding_id, fingerprints, *, ready=False):
        binding_id = text(binding_id, 128)
        if ready is not True or not isinstance(fingerprints, (list, tuple)):
            raise QueueError("capture_snapshot_not_ready")
        values = list(dict.fromkeys(text(value, 256) for value in fingerprints))
        with self._tx() as db:
            if db.execute("SELECT 1 FROM assistant_baselines WHERE binding_id=?", (binding_id,)).fetchone():
                return {"initialized": True, "created": False, "ignored_count": 0}
            db.execute("INSERT INTO assistant_baselines VALUES(?,?)", (binding_id, time.time()))
            for value in values:
                self._remember(db, binding_id, value, "baseline")
            return {"initialized": True, "created": True, "ignored_count": len(values)}

    def has_seen_notice(self, binding_id, fingerprint):
        with self._connection() as db:
            return db.execute("SELECT 1 FROM assistant_seen WHERE binding_id=? AND fingerprint=?", (binding_id, fingerprint)).fetchone() is not None

    def remember_notice(self, binding_id, fingerprint, *, task_id=None, reason="received"):
        binding_id, fingerprint = text(binding_id, 128), text(fingerprint, 256)
        if reason not in {"received", "baseline", "ignored_unbound", "ignored_command", "sender_not_verified"} | ISSUE_LABELS.keys():
            raise QueueError("invalid_notice_reason")
        with self._tx() as db:
            if task_id is not None:
                row = db.execute("SELECT binding_id FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
                if not row or row[0] != binding_id:
                    raise QueueError("notice_task_mismatch")
            self._remember(db, binding_id, fingerprint, reason, task_id)

    def record_intake_issue(self, fingerprint, sender_profile_url, receiver_profile_url, comment_text,
                            video_id=None, reason_code="remote_device_not_connected", *, evidence):
        """Remember a verified @ without inventing a library or executable task."""
        fingerprint = text(fingerprint, 256)
        sender, receiver = public_profile(sender_profile_url), public_profile(receiver_profile_url)
        body = text(comment_text, 4000)
        if not isinstance(reason_code, str) or reason_code not in ISSUE_LABELS:
            raise QueueError("invalid_intake_issue_reason")
        if video_id is not None and (not isinstance(video_id, str) or not re.fullmatch(r"\d{5,30}", video_id)):
            raise QueueError("invalid_video_id")
        if (not isinstance(evidence, dict) or any(evidence.get(name) is not True for name in
                ("real_mention", "sender_verified", "receiver_verified"))
                or type(evidence.get("unique_count")) is not int or evidence["unique_count"] != 1
                or (video_id is not None and evidence.get("video_verified") is not True)):
            raise QueueError("incomplete_intake_issue_evidence")
        safe_evidence = {name: evidence.get(name) for name in ("real_mention", "sender_verified", "receiver_verified", "unique_count", "video_verified")}
        with self._tx() as db:
            old = db.execute("SELECT * FROM assistant_intake_issues WHERE fingerprint=? AND sender_profile_url=? AND receiver_profile_url=?",
                             (fingerprint, sender, receiver)).fetchone()
            if old:
                if old["video_id"] != video_id or re.sub(r"\s+", "", old["comment_text"]) != re.sub(r"\s+", "", body):
                    raise QueueError("intake_issue_conflict")
                return self._public_issue(old)
            now, issue_id = time.time(), secrets.token_hex(16)
            db.execute("INSERT INTO assistant_intake_issues VALUES(?,?,?,?,?,?,?,?,'not_executed',NULL,?,?)",
                       (issue_id, fingerprint, sender, receiver, body, video_id, reason_code, encoded(safe_evidence), now, now))
            return self._public_issue(db.execute("SELECT * FROM assistant_intake_issues WHERE id=?", (issue_id,)).fetchone())

    @staticmethod
    def _public_issue(row):
        return {"id": row["id"], "sender_profile_url": row["sender_profile_url"], "receiver_profile_url": row["receiver_profile_url"],
                "video_id": row["video_id"], "comment_excerpt": row["comment_text"][:200], "reason_code": row["reason_code"],
                "message": ISSUE_LABELS[row["reason_code"]], "state": row["state"], "resolved_task_id": row["resolved_task_id"],
                "created_at": utc(row["created_at"]), "updated_at": utc(row["updated_at"])}

    def intake_issue_summary(self, limit=10):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise QueueError("invalid_issue_limit")
        with self._connection() as db:
            count = db.execute("SELECT COUNT(*) FROM assistant_intake_issues WHERE state='not_executed'").fetchone()[0]
            rows = db.execute("SELECT * FROM assistant_intake_issues WHERE state='not_executed' ORDER BY created_at DESC,id LIMIT ?", (limit,)).fetchall()
            return {"unresolved_count": count, "recent": [self._public_issue(row) for row in rows]}

    def resolve_intake_issue(self, issue_id, task_id):
        """Preserve the issue; resolve only against a newly verified real intake."""
        with self._tx() as db:
            issue = db.execute("SELECT * FROM assistant_intake_issues WHERE id=?", (issue_id,)).fetchone()
            task = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if issue is None or task is None:
                raise QueueError("intake_issue_or_task_missing")
            if issue["state"] == "resolved":
                if issue["resolved_task_id"] != task_id:
                    raise QueueError("intake_issue_resolution_conflict")
                return self._public_issue(issue)
            envelope = json.loads(task["envelope"])
            try:
                observed = datetime.fromisoformat(envelope["evidence"]["observed_at_utc"]).timestamp()
            except (ValueError, KeyError, TypeError):
                raise QueueError("fresh_intake_required") from None
            if (any(envelope[name] != issue[name] for name in ("sender_profile_url", "receiver_profile_url"))
                    or (issue["video_id"] is not None and envelope["video_id"] != issue["video_id"])
                    or re.sub(r"\s+", "", envelope["comment_text"]) != re.sub(r"\s+", "", issue["comment_text"])
                    or task["created_at"] < issue["created_at"] or observed < issue["created_at"]
                    or envelope["source_mode"] != "live_ui" or task["state"] == "waiting_desktop"):
                raise QueueError("fresh_intake_required")
            if issue["video_id"] is None and not db.execute("SELECT 1 FROM assistant_seen WHERE fingerprint=? AND task_id=?", (issue["fingerprint"], task_id)).fetchone():
                raise QueueError("fresh_intake_required")
            db.execute("UPDATE assistant_intake_issues SET state='resolved',resolved_task_id=?,updated_at=? WHERE id=?",
                       (task_id, time.time(), issue_id))
            self._event(db, task_id, "resolved_intake_issue", {"issue_id": issue_id})
            return self._public_issue(db.execute("SELECT * FROM assistant_intake_issues WHERE id=?", (issue_id,)).fetchone())

    def defer_unverified_notice(self, capture_id, fingerprint, *, seconds=120, now=None):
        """Scheduling only: no admission/seen receipt or proof of a real comment."""
        capture_id = text(capture_id, 128)
        if not isinstance(fingerprint, str) or not re.fullmatch('[a-f0-9]{64}', fingerprint):
            raise QueueError('invalid_notice_fingerprint')
        if type(seconds) is not int or not 30 <= seconds <= 600:
            raise QueueError('invalid_notice_deferral')
        now = time.time() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise QueueError('invalid_notice_deferral')
        with self._tx() as db:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='notice_deferrals'").fetchone()
            records = json.loads(row[0]) if row else []
            if not isinstance(records, list): raise QueueError('invalid_notice_deferral_state')
            records = [item for item in records if isinstance(item, dict)
                       and item.get('retry_after', 0) > now
                       and not (item.get('capture_id') == capture_id and item.get('fingerprint') == fingerprint)]
            if len(records) >= 256:
                # Fail closed; do not silently evict a still-active hold.
                raise QueueError('notice_deferral_capacity')
            records.append({'capture_id': capture_id, 'fingerprint': fingerprint, 'retry_after': now + seconds})
            db.execute("INSERT OR REPLACE INTO assistant_meta VALUES('notice_deferrals',?)", (encoded(records),))

    def notice_is_deferred(self, capture_id, fingerprint, *, now=None):
        now = time.time() if now is None else now
        with self._connection() as db:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='notice_deferrals'").fetchone()
        records = json.loads(row[0]) if row else []
        if not isinstance(records, list): raise QueueError('invalid_notice_deferral_state')
        return any(isinstance(item, dict) and item.get('capture_id') == capture_id
                   and item.get('fingerprint') == fingerprint and item.get('retry_after', 0) > now
                   for item in records)

    def notice_deferral_summary(self):
        with self._connection() as db:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='notice_deferrals'").fetchone()
        records = json.loads(row[0]) if row else []
        if not isinstance(records, list): raise QueueError('invalid_notice_deferral_state')
        now = time.time()
        return {'pending_rechecks': sum(1 for item in records if isinstance(item, dict)
                                      and item.get('retry_after', 0) > now)}

    def get_control(self):
        with self._connection() as db:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='control'").fetchone()
            return json.loads(row[0]) if row else {"state": "stopped", "desired_state": "stopped", "run_id": None,
                "pid": None, "heartbeat_at": None, "reason_code": None}

    def update_control(self, transform):
        with self._tx() as db:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='control'").fetchone()
            current = json.loads(row[0]) if row else {"state": "stopped", "desired_state": "stopped", "run_id": None,
                "pid": None, "heartbeat_at": None, "reason_code": None}
            result = transform(dict(current))
            db.execute("INSERT OR REPLACE INTO assistant_meta VALUES('control',?)", (encoded(result),))
            return result

    def request_action(self, task_id, action):
        if action not in {"reconcile", "resume", "retry_query"}:
            raise QueueError("invalid_task_action")
        with self._tx() as db:
            task = db.execute("SELECT * FROM assistant_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise QueueError("task_not_found")
            if action == 'retry_query':
                prior = self._query_retry(db, task_id)
                if prior is not None:
                    return dict(db.execute('SELECT * FROM assistant_actions WHERE id=?', (prior['action_id'],)).fetchone())
                if not self._query_retry_allowed(self._raw(task), time.time()):
                    raise QueueError('query_retry_not_safe')
            if action == "resume" and self._resume_phase(self._raw(task), time.time()) is None:
                raise QueueError("resume_not_safe")
            old = db.execute("SELECT * FROM assistant_actions WHERE task_id=? AND action=? AND state='queued'", (task_id, action)).fetchone()
            if old:
                return dict(old)
            now, action_id = time.time(), secrets.token_hex(16)
            db.execute("INSERT INTO assistant_actions VALUES(?,?,?,'queued',NULL,?,?)", (action_id, task_id, action, now, now))
            if action == 'retry_query':
                db.execute("""CREATE TABLE IF NOT EXISTS assistant_query_retries(
                  task_id TEXT PRIMARY KEY,action_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,
                  immutable TEXT NOT NULL,agent_task_id TEXT NOT NULL,snapshot TEXT NOT NULL,
                  action_token TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL)""")
                snapshot = {name:self._raw(task).get(name) for name in ('state','reason_code','usage','details','execution_attempts')}
                db.execute("INSERT INTO assistant_query_retries VALUES(?,?,'requested',?,?,?,NULL,?,?)", (
                    task_id,action_id,task['immutable'],task['agent_task_id'],encoded(snapshot),now,now))
                self._event(db, task_id, 'query_retry_requested', {'action_id':action_id,'reason_code':'operator_retry_readonly_query'})
            return dict(db.execute("SELECT * FROM assistant_actions WHERE id=?", (action_id,)).fetchone())

    @staticmethod
    def _query_retry(db, task_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_query_retries'").fetchone():
            return None
        return db.execute('SELECT * FROM assistant_query_retries WHERE task_id=?', (task_id,)).fetchone()

    def get_query_retry(self, task_id):
        with self._connection() as db:
            row = self._query_retry(db, task_id)
            return ({name:row[name] for name in ('task_id','action_id','state','agent_task_id','created_at','updated_at')}
                    if row is not None else None)

    @staticmethod
    def _query_retry_allowed(task, now):
        proof = task.get('reconciliation')
        return bool(task['kind']=='query' and task['state']=='needs_review'
            and task.get('reason_code')=='knowledge_query_failed' and not task.get('publication_token')
            and isinstance(proof,dict) and task.get('reconciled_at') is not None
            and 0 <= now-task['reconciled_at'] <= 120
            and proof.get('state')=='needs_review' and proof.get('reason_code')=='knowledge_query_failed'
            and proof.get('agent_task_id')==task.get('agent_task_id') and bool(task.get('agent_task_id'))
            and proof.get('can_operator_retry_query') is True and proof.get('binding_active') is True
            and proof.get('model_started') is True and proof.get('send_token_present') is False
            and proof.get('ingestion_submitted') is False)

    @staticmethod
    def _reserve_query_retry_action(db, action):
        retry = AssistantQueue._query_retry(db, action['task_id'])
        if (retry is None or retry['action_id']!=action['id'] or retry['state']!='requested'
                or action['state']!='queued'):
            return None
        token, now = secrets.token_hex(24), time.time()
        db.execute("UPDATE assistant_query_retries SET state='reserved',action_token=?,updated_at=? WHERE task_id=?", (token,now,action['task_id']))
        db.execute("UPDATE assistant_actions SET state='processing',updated_at=? WHERE id=?", (now,action['id']))
        return dict(action) | {'state':'processing','_query_retry_token':token}

    def take_query_retry_action(self, task_id, action_id):
        with self._tx() as db:
            action = db.execute("SELECT * FROM assistant_actions WHERE id=? AND task_id=? AND action='retry_query'", (action_id,task_id)).fetchone()
            return self._reserve_query_retry_action(db,action) if action is not None else None

    def query_retry_action_current(self, task_id, action_id, token):
        with self._connection() as db:
            retry=self._query_retry(db,task_id)
            return bool(retry is not None and retry['state']=='reserved' and retry['action_id']==action_id
                and isinstance(token,str) and secrets.compare_digest(token,retry['action_token'] or ''))

    def query_retry_execution_current(self, task_id, action_id, execution_token):
        with self._connection() as db:
            retry=self._query_retry(db,task_id)
            task=db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone()
            return bool(retry is not None and retry['state']=='claimed' and retry['action_id']==action_id
                and task is not None and task['state']=='executing' and task['kind']=='query'
                and task['immutable']==retry['immutable'] and task['agent_task_id']==retry['agent_task_id']
                and task['lease_until'] is not None and task['lease_until']>time.time()
                and isinstance(execution_token,str)
                and secrets.compare_digest(execution_token,task['execution_token'] or ''))

    def activate_query_retry(self, task_id, action_id, token, result):
        result = normalized_result(result)
        with self._tx() as db:
            retry, task = self._query_retry(db,task_id), db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone()
            if (retry is None or retry['state']!='reserved' or retry['action_id']!=action_id
                    or not isinstance(token,str) or not secrets.compare_digest(token,retry['action_token'] or '')
                    or task is None or task['immutable']!=retry['immutable'] or task['kind']!='query'
                    or task['state']!='needs_review' or task['reason_code']!='knowledge_query_failed'
                    or task['publication_token'] or result.get('agent_task_id')!=retry['agent_task_id']
                    or result['state']!='queued' or result.get('binding_active') is not True
                    or result.get('send_token_present') is not False or result.get('ingestion_submitted') is not False):
                raise QueueError('query_retry_not_safe')
            now=time.time()
            db.execute("UPDATE assistant_tasks SET state='queued',reason_code='operator_query_retry_armed',reconciliation=NULL,reconciled_at=NULL,updated_at=? WHERE id=?", (now,task_id))
            db.execute("UPDATE assistant_query_retries SET state='queued',updated_at=? WHERE task_id=?", (now,task_id))
            db.execute("UPDATE assistant_actions SET state='done',updated_at=? WHERE id=? AND state='processing'", (now,action_id))
            self._event(db,task_id,'query_retry_armed',{'action_id':action_id,'agent_task_id':retry['agent_task_id']})
            return self._raw(db.execute('SELECT * FROM assistant_tasks WHERE id=?',(task_id,)).fetchone())

    def hold_query_retry(self, task_id, action_id, reason='query_retry_unconfirmed'):
        code(reason)
        with self._tx() as db:
            retry=self._query_retry(db,task_id)
            if retry is not None and retry['action_id']==action_id and retry['state']=='reserved':
                db.execute("UPDATE assistant_query_retries SET state='held',updated_at=? WHERE task_id=?",(time.time(),task_id))
                db.execute("UPDATE assistant_tasks SET state='needs_review',reason_code=?,updated_at=? WHERE id=? AND state='needs_review'",(reason,time.time(),task_id))

    def take_action(self):
        with self._tx() as db:
            row = db.execute("SELECT * FROM assistant_actions WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            if row['action']=='retry_query':
                reserved=self._reserve_query_retry_action(db,row)
                if reserved is not None:
                    return reserved
                db.execute("UPDATE assistant_actions SET state='failed',reason_code='query_retry_already_used',updated_at=? WHERE id=?",(time.time(),row['id']))
                return dict(row) | {'state':'failed','reason_code':'query_retry_already_used'}
            db.execute("UPDATE assistant_actions SET state='processing',updated_at=? WHERE id=?", (time.time(), row["id"]))
            return dict(row)

    def latest_action(self, task_id):
        """Read-only action progress, with no claims, receipts or credentials."""
        with self._connection() as db:
            row = db.execute("""SELECT id,task_id,action,state,reason_code,created_at,updated_at
                FROM assistant_actions WHERE task_id=? ORDER BY created_at DESC,id DESC LIMIT 1""", (task_id,)).fetchone()
            return dict(row) if row is not None else None

    def finish_action(self, action_id, reason_code=None):
        if reason_code is not None:
            code(reason_code)
        with self._tx() as db:
            db.execute("UPDATE assistant_actions SET state=?,reason_code=?,updated_at=? WHERE id=? AND state='processing'",
                       ("failed" if reason_code else "done", reason_code, time.time(), action_id))
