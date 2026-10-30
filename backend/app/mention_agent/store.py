"""Isolated durable tasks and reply intentions. No model, browser or app DB.

Received evidence, an answer draft, and an observed B-side comment are distinct
states. An expired paid-model attempt is quarantined, never automatically rerun.
"""
from __future__ import annotations

from contextlib import contextmanager
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
import time
from urllib.parse import urlsplit

from app.storage_guard import (guarded_sqlite_file, StorageGuardError,
    verified_readonly_connection, SqliteOwnershipError, assert_no_orphan_sqlite_files)
from app.assistant.reply_style import normalize_reply_prefix, style_reply_text


STEP_KINDS = frozenset({"model_request", "model_response", "tool_call", "tool_result", "decision", "error"})
SEND_OUTCOMES = frozenset({"b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit"})
SECRET_KEYS = frozenset({"api_key", "cookie", "cookies", "password", "authorization", "reasoning_content",
    "raw_messages", "access_token", "refresh_token", "client_secret", "credentials", "storage_state"})
_BINDING_KEYS = frozenset({"binding_id", "instance_id", "sender_profile_url", "receiver_profile_url",
                         "knowledge_base_id", "public_reply_allowed", "active"})
_ENVELOPE_KEYS = frozenset({"command_id", "reference_id", "reference_quality", "native_comment_id",
    "sender_profile_url", "receiver_profile_url", "video_id", "comment_text", "source_mode", "evidence"})
_PROPOSAL_KEYS = frozenset({"text", "source_ids", "kind", "sources"})
INGESTION_STATES = frozenset({"queued", "reserved", "accepted", "processing", "submission_unknown",
                            "succeeded", "failed_no_submit", "failed", "needs_review"})
_INGESTION_BACKEND_STATES = frozenset({"pending", "accepted", "processing", "succeeded", "failed", "unknown"})
_INGESTION_OBSERVATION_KEYS = frozenset({"backend_request_id", "binding_id", "instance_id",
    "knowledge_base_id", "video_id", "backend_state", "membership_exists", "cache_status", "indexed_content_chunks"})
_COMPONENT = "DouyinMind.mention_agent.store.v1"
_READBACK_KIND = 'reply_readback_confirmation'
_READBACK_MAC_KEY = secrets.token_bytes(32)
_READBACK_ISSUER = object()
_READBACK_FIELDS = frozenset({'outcome','exact_body_in_original_thread','verification',
    'reply_sha256','observed_at_utc','counts','readback_quality'})
_READBACK_COUNT_KEYS = frozenset({'matching_reply_count','existing_matching_reply_count',
    'strict_matching_reply_count','weak_matching_reply_count','verified_root_count',
    'verified_selection_count','selected_source_count'})
_OPERATOR_QUERY_DRAFT_BRAND = object()
_OPERATOR_QUERY_DRAFT_MAC = secrets.token_bytes(32)
_OPERATOR_QUERY_DRAFT_PROOF_KEYS = frozenset({'operation','operator_reason','queue_task_id','queue_kind',
    'run_id','current_pid','reviewed_at','authorization_current','fresh_ui_verified','fresh_ui_digest',
    'evidence_sha_verified','reply_validated','budget_requests_settled','budget_snapshot_digest',
    'reply_style_key','prefix_sha256'})


class AgentStoreError(ValueError):
    """A fixed, non-sensitive state/validation error code."""


def _operator_query_draft_proof(value):
    if (type(value) is not dict or set(value) != _OPERATOR_QUERY_DRAFT_PROOF_KEYS
            or value.get('operation') != 'operator_finalize_query_draft'
            or value.get('operator_reason') != 'explicit_operator_grounded_first_reply'
            or value.get('queue_kind') != 'query'
            or any(type(value.get(key)) is not str or re.fullmatch(r'[a-f0-9]{32}',value[key]) is None
                for key in ('queue_task_id','run_id'))
            or type(value.get('current_pid')) is not int or value['current_pid'] <= 0
            or type(value.get('reviewed_at')) not in (int,float) or not math.isfinite(value['reviewed_at'])
            or value['reviewed_at'] <= 0
            or any(value.get(key) is not True for key in ('authorization_current','fresh_ui_verified',
                'evidence_sha_verified','reply_validated','budget_requests_settled'))
            or any(type(value.get(key)) is not str or re.fullmatch(r'[a-f0-9]{64}',value[key]) is None
                for key in ('fresh_ui_digest','budget_snapshot_digest','prefix_sha256'))
            or type(value.get('reply_style_key')) is not str
            or re.fullmatch(r'(?:assistant|agent):[a-f0-9]{32}',value['reply_style_key']) is None):
        raise AgentStoreError('operator_query_draft_proof_required')
    return json.loads(_public_json(value,8192))


class _OperatorQueryDraftPermit:
    """Owner-issued first-draft attestation, never a JSON/request flag."""
    __slots__ = ('store','snapshot','proposal','proof','_seal')

    def __init__(self,store,snapshot,proposal,proof,*,_brand=None):
        if _brand is not _OPERATOR_QUERY_DRAFT_BRAND or type(snapshot) is not dict:
            raise AgentStoreError('operator_query_draft_permit_required')
        self.store = store
        self.snapshot = json.loads(_public_json(snapshot,262144))
        self.proposal = _proposal(proposal)
        self.proof = _operator_query_draft_proof(proof)
        self._seal = self._signature()

    def _signature(self):
        value = {'store':id(self.store),'snapshot':self.snapshot,'proposal':self.proposal,'proof':self.proof}
        return hmac.new(_OPERATOR_QUERY_DRAFT_MAC,_public_json(value,393216).encode(),hashlib.sha256).digest()


def _operator_query_draft_permit(store,snapshot,proposal,proof):
    # Only the paused page owner may issue this after fresh authorization/UI,
    # settled-budget, recorded-chunk SHA and full public-answer validation.
    return _OperatorQueryDraftPermit(store,snapshot,proposal,proof,_brand=_OPERATOR_QUERY_DRAFT_BRAND)


class _VerifiedReplyReadback(dict):
    """In-process attestation, not an API/send credential or serialized secret."""
    __slots__ = ('_readback_mac',)

    def __init__(self, value, *, _issuer=None):
        if _issuer is not _READBACK_ISSUER:
            raise AgentStoreError('unverified_readback_confirmation')
        super().__init__(value)
        self._readback_mac = hmac.new(_READBACK_MAC_KEY, _public_json(value,65536).encode(), hashlib.sha256).digest()


def verified_reply_readback_metadata(value):
    """Reject plain mappings, altered payloads, or cross-process old objects."""
    if type(value) is not _VerifiedReplyReadback:
        raise AgentStoreError('unverified_readback_confirmation')
    expected = hmac.new(_READBACK_MAC_KEY, _public_json(dict(value),65536).encode(), hashlib.sha256).digest()
    mark=getattr(value,'_readback_mac',None)
    if not isinstance(mark,bytes) or not hmac.compare_digest(expected,mark):
        raise AgentStoreError('readback_confirmation_changed')
    return json.loads(_public_json(dict(value),65536))


def _text(value, maximum=256, *, preserve=False):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise AgentStoreError("invalid_text")
    return value if preserve else value.strip()


def _profile(value):
    value = _text(value, 2048)
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname != "www.douyin.com" or url.username or url.password
                or url.port not in (None, 443) or url.query or url.fragment
                or not re.fullmatch(r"/user/[^/?#]+", url.path) or url.path == "/user/self"):
            raise ValueError
    except ValueError:
        raise AgentStoreError("invalid_public_profile") from None
    return "https://www.douyin.com" + url.path


def _public_json(value, maximum=8192):
    def inspect(item, depth=0):
        if depth > 10:
            raise AgentStoreError("metadata_too_deep")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or key.casefold() in SECRET_KEYS:
                    raise AgentStoreError("forbidden_metadata_field")
                inspect(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                inspect(child, depth + 1)
    inspect(value)
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise AgentStoreError("invalid_json_metadata") from None
    if len(result) > maximum:
        raise AgentStoreError("metadata_too_large")
    return result


def _utc(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise AgentStoreError("invalid_utc_timestamp") from None


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="microseconds")


def _binding(value):
    if isinstance(value, dict) and set(value) == _BINDING_KEYS | {"query_scope"}:
        from .contracts import Binding
        try:
            return Binding.from_dict(value).to_dict()
        except ValueError:
            raise AgentStoreError("invalid_binding_value") from None
    if not isinstance(value, dict) or set(value) != _BINDING_KEYS:
        raise AgentStoreError("invalid_binding_fields")
    result = dict(value)
    for key in ("binding_id", "instance_id"):
        result[key] = _text(result[key], 128)
    for key in ("sender_profile_url", "receiver_profile_url"):
        result[key] = _profile(result[key])
    if (type(result["knowledge_base_id"]) is not int or result["knowledge_base_id"] <= 0
            or type(result["active"]) is not bool or type(result["public_reply_allowed"]) is not bool):
        raise AgentStoreError("invalid_binding_value")
    return result


def _envelope(value):
    if not isinstance(value, dict) or set(value) != _ENVELOPE_KEYS:
        raise AgentStoreError("invalid_envelope_fields")
    result = dict(value)
    for key in ("command_id", "reference_id"):
        result[key] = _text(result[key], 512)
    for key in ("sender_profile_url", "receiver_profile_url"):
        result[key] = _profile(result[key])
    if not isinstance(result["reference_quality"], str) or result["reference_quality"] not in {"native", "verified_ui"}:
        raise AgentStoreError("invalid_reference_quality")
    if not isinstance(result["source_mode"], str) or result["source_mode"] not in {"live_ui", "replay", "isolated"}:
        raise AgentStoreError("invalid_source_mode")
    native = result["native_comment_id"]
    if native is not None:
        result["native_comment_id"] = _text(native, 512)
    if result["reference_quality"] == "native" and native is None:
        raise AgentStoreError("native_comment_id_required")
    if not isinstance(result["video_id"], str) or not re.fullmatch(r"\d{5,30}", result["video_id"]):
        raise AgentStoreError("invalid_video_id")
    result["comment_text"] = _text(result["comment_text"], 8000, preserve=True)
    proof = result["evidence"]
    if isinstance(proof, dict) and proof.get('source_kind') == 'conversation_followup':
        from .followup_evidence import validate_followup_envelope
        try:
            result = validate_followup_envelope(result)
        except ValueError:
            raise AgentStoreError('invalid_followup_evidence') from None
        _utc(proof.get('observed_at_utc'))
        _public_json(proof, 32768)
        return result
    if isinstance(proof, dict) and proof.get("source_kind") == "library_choice_reply":
        from .choice_evidence import validate_choice_envelope
        try:
            result = validate_choice_envelope(result)
        except ValueError:
            raise AgentStoreError("invalid_choice_evidence") from None
        _utc(proof.get("observed_at_utc"))
        _public_json(proof, 16384)
        return result
    if isinstance(proof, dict) and "source_kind" in proof:
        raise AgentStoreError("invalid_evidence_kind")
    if (not isinstance(proof, dict) or proof.get("real_mention") is not True
            or type(proof.get("unique_count")) is not int or proof["unique_count"] != 1
            or proof.get("sender_verified") is not True or proof.get("video_verified") is not True):
        raise AgentStoreError("incomplete_mention_evidence")
    _utc(proof.get("observed_at_utc"))
    _public_json(proof)
    return result


def _proposal(value):
    if not isinstance(value, dict):
        raise AgentStoreError("invalid_proposal_fields")
    is_import = value.get("kind") == "import_status"
    expected = _PROPOSAL_KEYS | {"import_job_id", "import_state"} if is_import else _PROPOSAL_KEYS
    if set(value) != expected:
        raise AgentStoreError("invalid_proposal_fields")
    result = dict(value)
    result["text"] = _text(value["text"], 8000, preserve=True)
    if not isinstance(value["kind"], str) or value["kind"] not in {"answer", "clarification", "import_status"}:
        raise AgentStoreError("invalid_proposal_kind")
    if (not isinstance(value["source_ids"], list) or len(value["source_ids"]) > 64
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in value["source_ids"])):
        raise AgentStoreError("invalid_source_ids")
    if not isinstance(value["sources"], list) or len(value["sources"]) > 64 or any(not isinstance(item, dict) for item in value["sources"]):
        raise AgentStoreError("invalid_sources")
    if is_import:
        result["import_job_id"] = _text(value["import_job_id"], 128)
        if (not isinstance(value["import_state"], str) or value["import_state"] not in INGESTION_STATES
                or value["source_ids"] or value["sources"]):
            raise AgentStoreError("invalid_import_proposal")
    _public_json(result, 65536)
    return result


def _canonical_reference(envelope):
    """Compare rendered identity without rewriting the original observation.

    This deliberately follows the existing DOM comment comparator's whitespace
    handling. Punctuation, case, source mode and every destination remain exact.
    """
    try:
        value = {key: envelope[key] for key in _ENVELOPE_KEYS - {"command_id", "evidence"}}
        value["reference_id"] = _text(value["reference_id"], 512)
        value["comment_text"] = re.sub(r"\s+", "", value["comment_text"])
        proof = envelope.get("evidence")
        if isinstance(proof, dict) and proof.get('source_kind') == 'conversation_followup':
            from .followup_evidence import PROOF_REQUIRED
            value['followup_parent'] = {key: proof[key] for key in PROOF_REQUIRED - {'observed_at_utc'}}
        if isinstance(proof, dict) and proof.get("source_kind") == "library_choice_reply":
            # Observation time may change; the verified original parent may not.
            value["choice_parent"] = {key: proof[key] for key in (
                "source_kind", "evidence_version", "choice_id", "instance_id", "root_reference_id",
                "root_comment_text", "ask_ref", "ask_reply_ref", "ask_text", "parent_scope",
                "direct_reply_to_ask", "real_mention")}
            value["choice_parent"].update({key:proof[key] for key in (
                "root_native_comment_id", "ask_native_comment_id", "selection_value", "receiver_reply_label") if key in proof})
        return value
    except (KeyError, TypeError):
        raise AgentStoreError("invalid_stored_envelope") from None


def _checked_choice(envelope, binding):
    proof = envelope.get("evidence")
    if not isinstance(proof, dict) or proof.get("source_kind") != "library_choice_reply":
        return False
    from .choice_evidence import validate_choice_envelope
    from .contracts import Binding
    try:
        validate_choice_envelope(envelope, Binding.from_dict(binding))
    except ValueError:
        raise AgentStoreError("invalid_choice_binding") from None
    return True


def _checked_followup(envelope, binding):
    proof = envelope.get('evidence')
    if not isinstance(proof, dict) or proof.get('source_kind') != 'conversation_followup':
        return False
    from .followup_evidence import validate_followup_envelope
    from .contracts import Binding
    try:
        validate_followup_envelope(envelope, Binding.from_dict(binding))
    except ValueError:
        raise AgentStoreError('invalid_followup_binding') from None
    return True


def _verified_target(target, verification, binding):
    """Fresh publication proof; ordinary mentions retain the original contract."""
    if not isinstance(verification, dict):
        return False
    if any(verification.get(key) != target[key] for key in (
            "sender_profile_url", "receiver_profile_url", "video_id", "comment_text", "reference_id")):
        return False
    if target["native_comment_id"] is not None and verification.get("native_comment_id") != target["native_comment_id"]:
        return False
    if "native_comment_id" in verification and verification["native_comment_id"] != target["native_comment_id"]:
        return False
    if (type(verification.get("unique_count")) is not int or verification["unique_count"] != 1
            or verification.get("current_receiver_verified") is not True):
        return False
    if 'followup_evidence' in target:
        if 'choice_evidence' in target:
            return False
        proof = target['followup_evidence']
        envelope = {key: target[key] for key in _ENVELOPE_KEYS - {'evidence'}}
        envelope['evidence'] = proof
        try:
            if not _checked_followup(envelope, binding):
                return False
            from .followup_evidence import PROOF_REQUIRED
            frozen = PROOF_REQUIRED - {'observed_at_utc'}
            return all(verification.get(key) == proof[key] and type(verification.get(key)) is type(proof[key]) for key in frozen)
        except (ValueError, KeyError, TypeError):
            return False
    proof = target.get("choice_evidence")
    if proof is None:
        return verification.get("real_mention") is True and verification.get("source_kind") is None
    envelope = {key: target[key] for key in _ENVELOPE_KEYS - {"evidence"}}
    envelope["evidence"] = proof
    try:
        if not _checked_choice(envelope, binding):
            return False
    except (AgentStoreError, KeyError, TypeError):
        return False
    frozen = ("source_kind", "evidence_version", "choice_id", "instance_id", "root_reference_id",
        "root_comment_text", "ask_ref", "ask_reply_ref", "ask_text", "parent_scope", "direct_reply_to_ask",
        "real_mention")
    frozen += tuple(key for key in ("root_native_comment_id", "ask_native_comment_id", "selection_value",
        "receiver_reply_label") if key in proof)
    return (all(verification.get(key) == proof[key] and type(verification.get(key)) is type(proof[key])
                for key in frozen)
            and all(verification.get(key) is True for key in (
                "root_chain_verified", "ask_visible_unique", "receiver_reply_target_verified")))


def _readback_observation(value, target, binding, *, recorded_at):
    if not isinstance(value,dict) or set(value)!=_READBACK_FIELDS:
        raise AgentStoreError('invalid_readback_observation')
    _public_json(value,32768)
    def no_capabilities(item):
        if isinstance(item,dict):
            for key,child in item.items():
                if isinstance(key,str) and (key.casefold() in {'token','nonce'} or key.casefold().endswith(('_token','_nonce'))):
                    raise AgentStoreError('readback_private_field_forbidden')
                no_capabilities(child)
        elif isinstance(item,(list,tuple)):
            for child in item:no_capabilities(child)
    no_capabilities(value)
    if value['outcome']!='b_view_visible' or value['exact_body_in_original_thread'] is not True:
        raise AgentStoreError('readback_not_confirmed')
    quality=value['readback_quality']
    if quality not in {'direct_selected_parent','weak_unique_root'}:
        raise AgentStoreError('readback_quality_not_confirmed')
    counts=value['counts']
    required={'matching_reply_count','existing_matching_reply_count','strict_matching_reply_count',
              'weak_matching_reply_count','verified_root_count','verified_selection_count'}
    if (not isinstance(counts,dict) or not required<=set(counts) or set(counts)-_READBACK_COUNT_KEYS
            or any(type(number) is not int or not 0<=number<=10000 for number in counts.values())
            or any(counts[key]!=1 for key in ('matching_reply_count','existing_matching_reply_count',
                                            'verified_root_count','verified_selection_count'))):
        raise AgentStoreError('readback_not_unique')
    strict,weak=(1,0) if quality=='direct_selected_parent' else (0,1)
    if counts['strict_matching_reply_count']!=strict or counts['weak_matching_reply_count']!=weak:
        raise AgentStoreError('readback_quality_overclaimed')
    if quality=='weak_unique_root' and counts.get('selected_source_count')!=1:
        raise AgentStoreError('readback_source_not_unique')
    if 'selected_source_count' in counts and counts['selected_source_count']<1:
        raise AgentStoreError('readback_source_not_unique')
    if not _verified_target(target,value['verification'],binding):
        raise AgentStoreError('readback_target_not_verified')
    observed=_utc(value['observed_at_utc'])
    verified=_utc(value['verification'].get('observed_at_utc'))
    if not -5<=recorded_at-observed<=120 or not -5<=recorded_at-verified<=120:
        raise AgentStoreError('readback_observation_not_fresh')
    return json.loads(_public_json(value,32768))


def _readback_scope(task,reply,binding):
    if (task is None or reply is None or binding is None or task['state']!='delivery_unknown'
            or reply['state']!='delivery_unknown' or not isinstance(reply['send_token'],str)
            or not re.fullmatch(r'[a-f0-9]{48}',reply['send_token'])):
        raise AgentStoreError('readback_original_not_unknown_reserved')
    try:
        envelope=json.loads(task['envelope']);target=json.loads(reply['target']);proposal=json.loads(reply['proposal'])
        evidence=json.loads(reply['evidence']) if reply['evidence'] else {}
    except (TypeError,ValueError):
        raise AgentStoreError('readback_scope_invalid') from None
    if any(not isinstance(value,dict) for value in (envelope,target,proposal,evidence)):
        raise AgentStoreError('readback_scope_invalid')
    if (envelope['source_mode']!='live_ui' or target.get('source_mode')!='live_ui'
            or not _checked_choice(envelope,binding) or target.get('choice_evidence')!=envelope['evidence']
            or evidence.get('submit_gate_passed') is not True or binding.get('public_reply_allowed') is not True
            or binding.get('active') is not True
            or not isinstance(proposal.get('text'),str) or not proposal['text'].strip()):
        raise AgentStoreError('readback_scope_not_supported')
    expected={key:value for key,value in envelope.items() if key!='evidence'}
    expected.update(binding_id=task['binding_id'],instance_id=binding['instance_id'],
                    knowledge_base_id=binding['knowledge_base_id'],choice_evidence=envelope['evidence'])
    if target!=expected:
        raise AgentStoreError('readback_frozen_target_changed')
    if 'reply_sha256' in evidence and evidence['reply_sha256']!=hashlib.sha256(proposal['text'].encode()).hexdigest():
        raise AgentStoreError('readback_original_body_changed')
    return target,proposal,{
        'binding_sha256':hashlib.sha256(_public_json(binding).encode()).hexdigest(),
        'target_sha256':hashlib.sha256(reply['target'].encode()).hexdigest(),
        'proposal_sha256':hashlib.sha256(reply['proposal'].encode()).hexdigest(),
        'original_evidence_sha256':hashlib.sha256(reply['evidence'].encode()).hexdigest(),
    }


def _readback_record(task,reply,binding,observation,recorded_at):
    target,proposal,hashes=_readback_scope(task,reply,binding)
    observed=_readback_observation(observation,target,binding,recorded_at=recorded_at)
    body_hash=hashlib.sha256(proposal['text'].encode()).hexdigest()
    if observed['reply_sha256']!=body_hash:
        raise AgentStoreError('readback_body_changed')
    source={
        'agent_task_id':task['task_id'],'source_reference_id':target['reference_id'],
        'reply_sha256':body_hash,**hashes,
    }
    identity=hashlib.sha256(_public_json(source).encode()).hexdigest()
    return {'version':1,'confirmation_id':identity,**source,'source_mode':'live_ui','scope':'b_view_only',
        'outcome':'b_view_visible','original_reply_state':'delivery_unknown',
        'observed_at_utc':observed['observed_at_utc'],'recorded_at_utc':_iso(recorded_at),
        'readback_quality':observed['readback_quality'],'counts':observed['counts'],'observation':observed}


def _readback_certificate(record):
    # The original verification remains in the protected Agent step. Callers
    # receive only bounded public metadata plus a non-serialized integrity mark.
    public={key:value for key,value in record.items() if key!='observation'}
    return _VerifiedReplyReadback(public,_issuer=_READBACK_ISSUER)


class AgentStore:
    def __init__(self, path, protected_paths=()):
        if str(path) == ":memory:":
            raise AgentStoreError("persistent_store_required")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.protected_paths = tuple(Path(item).resolve() for item in protected_paths)
        self._guard_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock():
            self._guard(missing_ok=True)
            assert_no_orphan_sqlite_files(self.path)
            try:
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self._verify_existing_readonly()
            else:
                created = os.fstat(descriptor)
                os.close(descriptor)
                self._create((created.st_dev, created.st_ino))

    def _guard_path(self):
        if self.path.resolve() != self.path or self.path.parent.resolve() != self.path.parent:
            raise AgentStoreError("redirected_store_path")
        for protected in self.protected_paths:
            if self.path == protected or self.path.is_relative_to(protected) or protected.is_relative_to(self.path):
                raise AgentStoreError("protected_store_path")

    @contextmanager
    def _file_lock(self):
        try:
            with guarded_sqlite_file(self.path, timeout=10):
                yield
        except StorageGuardError as error:
            code = 'unsafe_store_file' if str(error) == 'unsafe_storage_guard_database' else str(error)
            raise AgentStoreError(code) from None

    def _guard(self, *, missing_ok=False):
        self._guard_path()
        for candidate in (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm"))):
            try:
                item = candidate.lstat()
            except FileNotFoundError:
                if candidate == self.path and not missing_ok:
                    raise AgentStoreError("store_missing") from None
                continue
            if not stat.S_ISREG(item.st_mode) or item.st_nlink != 1 or candidate.resolve() != candidate:
                raise AgentStoreError("unsafe_store_file")

    @staticmethod
    def _owned(db):
        try:
            row = db.execute("SELECT value FROM agent_meta WHERE key='component'").fetchone()
            return row is not None and row[0] == _COMPONENT
        except sqlite3.DatabaseError:
            return False

    def _verify_existing_readonly(self):
        self._guard()
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                if not self._owned(db):
                    raise AgentStoreError("foreign_database")
            finally:
                db.close()
        except sqlite3.DatabaseError:
            raise AgentStoreError("foreign_database") from None

    def _create(self, created_identity):
        self._guard()
        created = self.path.stat()
        if (created.st_dev, created.st_ino) != created_identity or created.st_size != 0:
            raise AgentStoreError('store_creation_conflict')
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True)
        try:
            current = self.path.stat()
            if (current.st_dev, current.st_ino) != created_identity or current.st_size != 0:
                raise AgentStoreError('store_creation_conflict')
            db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE agent_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE agent_bindings(binding_id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE agent_tasks(task_id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,command_id TEXT UNIQUE NOT NULL,
              envelope TEXT NOT NULL,immutable_hash TEXT NOT NULL,state TEXT NOT NULL,
              claim_token TEXT,lease_until REAL,attempts INTEGER NOT NULL DEFAULT 0,
              reason_code TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE agent_steps(id INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,
              kind TEXT NOT NULL,metadata TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE agent_replies(reply_id TEXT PRIMARY KEY,task_id TEXT UNIQUE NOT NULL,
              proposal TEXT NOT NULL,target TEXT NOT NULL,state TEXT NOT NULL,created_at REAL NOT NULL,
              send_token TEXT,verification TEXT,evidence TEXT);
            INSERT INTO agent_meta(key,value) VALUES('component','DouyinMind.mention_agent.store.v1');
            COMMIT;
            """)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _tx(self):
        with self._file_lock():
            with self._tx_locked() as db:
                yield db

    @contextmanager
    def _tx_locked(self):
        self._guard()
        before = self.path.stat()
        identity = (before.st_dev,before.st_ino)
        try:
            with verified_readonly_connection(self.path, self._owned, guard=self._guard):
                pass
        except SqliteOwnershipError:
            raise AgentStoreError('foreign_database') from None
        db = sqlite3.connect(self.path.as_uri()+'?mode=rw', uri=True, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            self._guard()
            current = self.path.stat()
            if (current.st_dev,current.st_ino) != identity:
                raise AgentStoreError('store_identity_changed')
            if not self._owned(db):
                raise AgentStoreError("foreign_database")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _binding_row(db, binding_id):
        row = db.execute("SELECT payload FROM agent_bindings WHERE binding_id=?", (binding_id,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _task(row):
        if row is None:
            return None
        result = dict(row)
        result["envelope"] = json.loads(result["envelope"])
        result.pop("immutable_hash", None)
        result["lease_expires_at_utc"] = _iso(result["lease_until"]) if result["lease_until"] is not None else None
        return result

    @staticmethod
    def _reply(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("proposal", "target", "verification", "evidence"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        result.update(result["proposal"])
        if result["state"] == "needs_review" and isinstance(result["evidence"], dict):
            result["reason_code"] = result["evidence"].get("reason_code")
        return result

    @staticmethod
    def _alias_task(db, command_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_command_aliases'").fetchone():
            return None
        return db.execute("""SELECT t.* FROM agent_command_aliases a JOIN agent_tasks t ON t.task_id=a.task_id
          WHERE a.command_id=?""", (command_id,)).fetchone()

    @staticmethod
    def _reference_rows(db, binding_id, reference_id):
        rows = db.execute("SELECT * FROM agent_tasks WHERE binding_id=? ORDER BY created_at,task_id", (binding_id,)).fetchall()
        return [row for row in rows if _text(json.loads(row["envelope"])["reference_id"], 512) == reference_id]

    def _reference_choice(self, db, rows, incoming=None):
        if not rows:
            return None
        canonical = _canonical_reference(json.loads(rows[0]["envelope"]))
        if (incoming is not None and canonical != _canonical_reference(incoming)) or any(
                _canonical_reference(json.loads(row["envelope"])) != canonical for row in rows[1:]):
            raise AgentStoreError("reference_conflict")
        if len(rows) > 1:
            ids = tuple(row["task_id"] for row in rows)
            placeholders = ",".join("?" for _ in ids)
            replies = db.execute("SELECT state FROM agent_replies WHERE task_id IN (" + placeholders + ")", ids).fetchall()
            if (any(row["state"] in SEND_OUTCOMES or row["state"] == "send_reserved" for row in rows)
                    or any(row["state"] in SEND_OUTCOMES or row["state"] == "reserved" for row in replies)):
                raise AgentStoreError("reference_delivery_conflict")
        return rows[0]

    def put_binding(self, value):
        value = _binding(value)
        payload = _public_json(value)
        with self._tx() as db:
            old = self._binding_row(db, value["binding_id"])
            if old is not None and old != value:
                raise AgentStoreError("binding_conflict")
            db.execute("INSERT OR IGNORE INTO agent_bindings VALUES(?,?)", (value["binding_id"], payload))
        return value

    def get_binding(self, binding_id):
        with self._tx() as db:
            return self._binding_row(db, binding_id)

    def deactivate(self, binding_id):
        with self._tx() as db:
            value = self._binding_row(db, binding_id)
            if value is None:
                raise AgentStoreError("binding_not_found")
            value["active"] = False
            db.execute("UPDATE agent_bindings SET payload=? WHERE binding_id=?", (_public_json(value), binding_id))
            db.execute("""UPDATE agent_tasks SET state='needs_review',reason_code='binding_inactive',
              claim_token=NULL,lease_until=NULL,updated_at=? WHERE binding_id=? AND state IN ('queued','processing')""",
              (time.time(), binding_id))
            return value

    deactivate_binding = deactivate

    def enqueue(self, binding_id, envelope):
        envelope = _envelope(envelope)
        immutable = {key: value for key, value in envelope.items() if key != "evidence"}
        fingerprint = hashlib.sha256(_public_json(immutable, 16384).encode("utf-8")).hexdigest()
        with self._tx() as db:
            binding = self._binding_row(db, binding_id)
            if binding is None or not binding["active"]:
                raise AgentStoreError("binding_inactive_or_missing")
            if any(binding[key] != envelope[key] for key in ("sender_profile_url", "receiver_profile_url")):
                raise AgentStoreError("envelope_binding_mismatch")
            _checked_choice(envelope, binding)
            _checked_followup(envelope, binding)
            old = db.execute("SELECT * FROM agent_tasks WHERE command_id=?", (envelope["command_id"],)).fetchone()
            alias = self._alias_task(db, envelope["command_id"])
            if old and alias and old["task_id"] != alias["task_id"]:
                raise AgentStoreError("command_conflict")
            prior = old or alias
            if prior:
                if prior["binding_id"] != binding_id or _canonical_reference(json.loads(prior["envelope"])) != _canonical_reference(envelope):
                    raise AgentStoreError("command_conflict")
            rows = self._reference_rows(db, binding_id, envelope["reference_id"])
            chosen = self._reference_choice(db, rows, envelope)
            if chosen:
                if prior is None:
                    db.execute("""CREATE TABLE IF NOT EXISTS agent_command_aliases(
                      command_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,created_at REAL NOT NULL)""")
                    db.execute("INSERT INTO agent_command_aliases VALUES(?,?,?)", (envelope["command_id"], chosen["task_id"], time.time()))
                return self._task(chosen)
            task_id = secrets.token_hex(16)
            now = time.time()
            db.execute("""INSERT INTO agent_tasks(task_id,binding_id,command_id,envelope,immutable_hash,state,created_at,updated_at)
              VALUES(?,?,?,?,?,'queued',?,?)""", (task_id, binding_id, envelope["command_id"],
                  _public_json(envelope, 32768), fingerprint, now, now))
            return self._task(db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())

    def claim(self, task_id, lease_seconds=180, *, query_retry_action_id=None):
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float)) or not math.isfinite(lease_seconds) or lease_seconds <= 0 or lease_seconds > 3600:
            raise AgentStoreError("invalid_lease")
        with self._tx() as db:
            row = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                return None
            envelope = json.loads(row["envelope"])
            peers = self._reference_rows(db, row["binding_id"], _text(envelope["reference_id"], 512))
            chosen = self._reference_choice(db, peers)
            if chosen["task_id"] != task_id:
                return None
            if any(peer["state"] != "queued" for peer in peers[1:]):
                raise AgentStoreError("reference_execution_conflict")
            now = time.time()
            if row["state"] == "processing" and row["lease_until"] <= now:
                db.execute("UPDATE agent_tasks SET state='needs_review',reason_code='processing_lease_expired',updated_at=? WHERE task_id=?", (now, task_id))
                return None
            if row["state"] != "queued":
                return None
            retry = self._query_retry_row(db, task_id)
            if retry is not None:
                if (retry["state"] != "armed" or query_retry_action_id != retry["action_id"]
                        or row['binding_id']!=retry['binding_id'] or row['immutable_hash']!=retry['immutable_hash']):
                    return None
            elif query_retry_action_id is not None:
                return None
            binding = self._binding_row(db, row["binding_id"])
            if not binding or not binding["active"]:
                db.execute("UPDATE agent_tasks SET state='needs_review',reason_code='binding_inactive',updated_at=? WHERE task_id=?", (now, task_id))
                return None
            db.execute("""UPDATE agent_tasks SET state='processing',claim_token=?,lease_until=?,
              attempts=attempts+1,updated_at=? WHERE task_id=?""", (secrets.token_hex(24), now + lease_seconds, now, task_id))
            if retry is not None:
                db.execute("UPDATE agent_query_retries SET state='consumed',updated_at=? WHERE task_id=? AND state='armed'", (now, task_id))
            return self._task(db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())

    @staticmethod
    def _query_retry_row(db, task_id):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_query_retries'").fetchone():
            return None
        return db.execute("SELECT * FROM agent_query_retries WHERE task_id=?", (task_id,)).fetchone()

    def get_query_retry(self, task_id):
        with self._tx() as db:
            row = self._query_retry_row(db, task_id)
            return dict(row) if row is not None else None

    def _failed_query_retry_eligible(self, db, task_id):
        task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        if (task is None or task['state'] != 'needs_review' or task['reason_code'] != 'knowledge_query_failed'
                or self._query_retry_row(db, task_id) is not None
                or db.execute('SELECT 1 FROM agent_replies WHERE task_id=?', (task_id,)).fetchone()):
            return False
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_task_ingestions'").fetchone():
            if db.execute('SELECT 1 FROM agent_task_ingestions WHERE task_id=?', (task_id,)).fetchone():
                return False
        steps = db.execute('SELECT * FROM agent_steps WHERE task_id=? ORDER BY id', (task_id,)).fetchall()
        if not steps or steps[-1]['kind'] != 'error' or json.loads(steps[-1]['metadata']).get('code') != 'knowledge_query_failed':
            return False
        pending, completed, last_tool = None, 0, None
        for step in steps:
            meta = json.loads(step['metadata'])
            if step['kind'] == 'model_request':
                if pending is not None or type(meta.get('round')) is not int or meta['round']<1:
                    return False
                pending = meta['round']
            elif step['kind'] == 'model_response':
                if pending is None or type(meta.get('round')) is not int or pending != meta.get('round'):
                    return False
                completed += 1
                pending = None
            elif step['kind'] in {'tool_call', 'tool_result'}:
                if pending is not None or meta.get('tool_name') not in {'list_knowledge_bases', 'query_knowledge_base'}:
                    return False
                if step['kind'] == 'tool_call':
                    last_tool = meta['tool_name']
            elif step['kind'] == 'decision':
                return False
        peers = self._reference_rows(db, task['binding_id'], json.loads(task['envelope'])['reference_id'])
        return bool(completed and pending is None and last_tool == 'query_knowledge_base'
                    and self._reference_choice(db, peers)['task_id'] == task_id)

    def can_retry_failed_query(self, task_id):
        with self._tx() as db:
            return self._failed_query_retry_eligible(db, task_id)

    def _unstarted_budget_eligible(self, db, task_id, *, queued_noop=False):
        """Local evidence only; the caller must separately check the budget ledger.

        A completed denial retains its old claim lease. Wait for that lease to
        expire, rather than reopening a possibly live owner for convenience.
        """
        task = db.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
        if task is None or type(task['attempts']) is not int or task['attempts'] < 1:
            return False
        queued = queued_noop and task['state'] == 'queued' and task['reason_code'] is None
        if not queued and (task['state'] != 'needs_review' or task['reason_code'] != 'model_budget_exceeded'):
            return False
        token, lease = task['claim_token'], task['lease_until']
        if token is None and lease is None:
            pass
        elif (queued or not isinstance(token, str) or not token
                or isinstance(lease, bool) or not isinstance(lease, (int, float))
                or not math.isfinite(lease) or lease > time.time()):
            return False
        binding = self._binding_row(db, task['binding_id'])
        if not binding or binding.get('active') is not True:
            return False
        if db.execute('SELECT 1 FROM agent_replies WHERE task_id=?', (task_id,)).fetchone():
            return False
        # These tables are additive/lazy. Observation must not create them.
        for table in ('agent_task_ingestions', 'agent_query_retries', 'agent_reply_hold_releases',
                      'agent_reply_revisions', 'agent_import_reply_updates'):
            if db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():
                if db.execute('SELECT 1 FROM '+table+' WHERE task_id=?', (task_id,)).fetchone():
                    return False
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_ingestion_events'").fetchone():
            events = db.execute('SELECT metadata FROM agent_ingestion_events').fetchall()
            if any(json.loads(event['metadata']).get('task_id') == task_id for event in events):
                return False
        steps = db.execute('SELECT kind,metadata FROM agent_steps WHERE task_id=? ORDER BY id', (task_id,)).fetchall()
        if not steps:
            return False
        for step in steps:
            metadata = json.loads(step['metadata'])
            if (step['kind'] != 'error' or not isinstance(metadata, dict)
                    or metadata.get('code') != 'model_budget_exceeded'
                    or set(metadata) - {'code', 'round', 'error_type'}
                    or ('round' in metadata and (type(metadata['round']) is not int or metadata['round'] != 1))
                    or ('error_type' in metadata and (not isinstance(metadata['error_type'], str)
                        or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', metadata['error_type']) is None))):
                return False
        envelope = json.loads(task['envelope'])
        if any(binding[key] != envelope[key] for key in ('sender_profile_url', 'receiver_profile_url')):
            return False
        peers = self._reference_rows(db, task['binding_id'], envelope['reference_id'])
        # An ambiguous historical duplicate is not a safe original-task restart.
        return len(peers) == 1 and peers[0]['task_id'] == task_id

    def can_resume_unstarted_budget(self, task_id):
        """Whether local facts allow a first-request denial to be resumed.

        This does not prove a fresh external authorization or zero budget-ledger
        reservations; the product admission lock must verify both separately.
        """
        with self._tx() as db:
            return self._unstarted_budget_eligible(db, task_id)

    def resume_unstarted_budget(self, task_id, *, binding, envelope):
        """Explicitly requeue the same never-started task, preserving all history.

        This is not the paid-query retry mechanism. After any model/tool event,
        reply, ingestion, or retry intent it is permanently ineligible here.
        """
        expected_binding, expected_envelope = _binding(binding), _envelope(envelope)
        if binding != expected_binding or envelope != expected_envelope:
            raise AgentStoreError('unstarted_budget_scope_changed')
        with self._tx() as db:
            task = db.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if (task is None or not expected_binding['active']
                    or task['binding_id'] != expected_binding['binding_id']
                    or self._binding_row(db, task['binding_id']) != expected_binding
                    or json.loads(task['envelope']) != expected_envelope):
                raise AgentStoreError('unstarted_budget_scope_changed')
            if not self._unstarted_budget_eligible(db, task_id, queued_noop=True):
                raise AgentStoreError('unstarted_budget_not_safe')
            if task['state'] == 'queued':
                return self._task(task)  # No timestamp/history/attempt mutation.
            db.execute("""UPDATE agent_tasks SET state='queued',claim_token=NULL,lease_until=NULL,
              reason_code=NULL,updated_at=? WHERE task_id=?""", (time.time(), task_id))
            return self._task(db.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone())

    def arm_failed_query_retry(self, task_id, action_id, *, binding, envelope):
        """One explicit query-only revision; preserve all old paid steps.

        The separate product queue must durably authorize the same action ID.
        Only claim(action_id) consumes it. A crash after consumption never re-arms.
        """
        if not isinstance(action_id, str) or not re.fullmatch(r'[a-f0-9]{32}', action_id):
            raise AgentStoreError('invalid_query_retry_action')
        expected_binding, expected_envelope = _binding(binding), _envelope(envelope)
        with self._tx() as db:
            task = db.execute('SELECT * FROM agent_tasks WHERE task_id=?', (task_id,)).fetchone()
            if (task is None or self._binding_row(db, task['binding_id']) != expected_binding
                    or not expected_binding['active'] or task['binding_id'] != expected_binding['binding_id']
                    or json.loads(task['envelope']) != expected_envelope):
                raise AgentStoreError('query_retry_scope_changed')
            prior = self._query_retry_row(db, task_id)
            if prior is not None:
                if prior['action_id'] != action_id:
                    raise AgentStoreError('query_retry_already_used')
                return dict(prior)  # Observation only, never reset an old intent.
            if not self._failed_query_retry_eligible(db, task_id):
                raise AgentStoreError('query_retry_not_safe')
            db.execute("""CREATE TABLE IF NOT EXISTS agent_query_retries(
              task_id TEXT PRIMARY KEY,action_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,
              binding_id TEXT NOT NULL,immutable_hash TEXT NOT NULL,snapshot TEXT NOT NULL,
              created_at REAL NOT NULL,updated_at REAL NOT NULL)""")
            now = time.time()
            snapshot = {'state':task['state'], 'reason_code':task['reason_code'], 'attempts':task['attempts'],
                        'last_step_id':db.execute('SELECT MAX(id) FROM agent_steps WHERE task_id=?', (task_id,)).fetchone()[0]}
            db.execute("INSERT INTO agent_query_retries VALUES(?,?,'armed',?,?,?,?,?)", (
                task_id,action_id,task['binding_id'],task['immutable_hash'],_public_json(snapshot),now,now))
            db.execute("UPDATE agent_tasks SET state='queued',claim_token=NULL,lease_until=NULL,reason_code='operator_query_retry_armed',updated_at=? WHERE task_id=?", (now,task_id))
            return dict(self._query_retry_row(db, task_id))

    def _checked_claim(self, db, task_id, token):
        row = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["state"] != "processing":
            return None, "task_not_processing"
        if row["lease_until"] <= time.time():
            db.execute("UPDATE agent_tasks SET state='needs_review',reason_code='processing_lease_expired',updated_at=? WHERE task_id=?", (time.time(), task_id))
            return None, "claim_expired"
        if not isinstance(token, str) or not secrets.compare_digest(row["claim_token"], token):
            return None, "stale_claim_token"
        return row, None

    def _operator_query_draft_snapshot(self, db, task_id):
        if type(task_id) is not str or re.fullmatch(r'[a-f0-9]{32}',task_id) is None:
            raise AgentStoreError('operator_query_draft_not_eligible')
        marker = 'operator_query_draft_v1:' + task_id
        if db.execute('SELECT 1 FROM agent_meta WHERE key=?',(marker,)).fetchone():
            raise AgentStoreError('operator_query_draft_already_used')
        task = db.execute('SELECT * FROM agent_tasks WHERE task_id=?',(task_id,)).fetchone()
        if (task is None or task['state'] != 'needs_review' or task['reason_code'] != 'model_round_budget_exceeded'
                or type(task['attempts']) is not int or task['attempts'] < 1
                or type(task['claim_token']) is not str or re.fullmatch(r'[a-f0-9]{48}',task['claim_token']) is None
                or type(task['lease_until']) not in (int,float) or not math.isfinite(task['lease_until'])
                or task['lease_until'] <= 0
                or db.execute('SELECT 1 FROM agent_replies WHERE task_id=?',(task_id,)).fetchone()):
            raise AgentStoreError('operator_query_draft_not_eligible')
        for table in ('agent_task_ingestions','agent_query_retries','agent_reply_hold_releases',
                      'agent_reply_revisions','agent_import_reply_updates'):
            if (db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
                    and db.execute('SELECT 1 FROM '+table+' WHERE task_id=?',(task_id,)).fetchone()):
                raise AgentStoreError('operator_query_draft_prior_effect')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_ingestion_events'").fetchone():
            if db.execute("SELECT 1 FROM agent_ingestion_events WHERE json_extract(metadata,'$.task_id')=?",(task_id,)).fetchone():
                raise AgentStoreError('operator_query_draft_prior_effect')
        envelope = _envelope(json.loads(task['envelope']))
        binding = _binding(self._binding_row(db,task['binding_id']))
        immutable = {key:value for key,value in envelope.items() if key != 'evidence'}
        if (binding['active'] is not True or binding['public_reply_allowed'] is not True
                or any(binding[key] != envelope[key] for key in ('sender_profile_url','receiver_profile_url'))
                or envelope['source_mode'] != 'live_ui'
                or envelope['evidence'].get('source_kind') == 'library_choice_reply'
                or hashlib.sha256(_public_json(immutable,16384).encode()).hexdigest() != task['immutable_hash']):
            raise AgentStoreError('operator_query_draft_scope_changed')
        _checked_followup(envelope,binding)
        peers = self._reference_rows(db,task['binding_id'],envelope['reference_id'])
        if len(peers) != 1 or peers[0]['task_id'] != task_id:
            raise AgentStoreError('operator_query_draft_reference_ambiguous')
        steps = db.execute('SELECT * FROM agent_steps WHERE task_id=? ORDER BY id LIMIT 1001',(task_id,)).fetchall()
        if not steps or len(steps) > 1000:
            raise AgentStoreError('operator_query_draft_steps_unconfirmed')
        pending_model = pending_tool = None
        completed, request_ids, refs, rejected = 0, [], {}, 0
        allowed = {'list_knowledge_bases','query_knowledge_base','propose_reply'}
        for step in steps:
            meta = json.loads(step['metadata'])
            if type(meta) is not dict or type(meta.get('round')) is not int or not 1 <= meta['round'] <= 6:
                raise AgentStoreError('operator_query_draft_steps_unconfirmed')
            kind = step['kind']
            if kind == 'model_request':
                request = meta.get('budget_request_id')
                if (pending_model is not None or pending_tool is not None or meta['round'] != completed+1
                        or type(request) is not str or re.fullmatch(r'live-[a-f0-9]{48}',request) is None
                        or request in request_ids):
                    raise AgentStoreError('operator_query_draft_request_unconfirmed')
                pending_model = (meta['round'],request)
                request_ids.append(request)
            elif kind == 'model_response':
                usage = meta.get('usage')
                if (pending_model != (meta['round'],meta.get('budget_request_id'))
                        or type(usage) is not dict or type(usage.get('total_tokens')) is not int
                        or usage['total_tokens'] < 0
                        or any(key in usage and (type(usage[key]) is not int or usage[key] < 0)
                               for key in ('prompt_tokens','completion_tokens'))):
                    raise AgentStoreError('operator_query_draft_request_unconfirmed')
                completed += 1
                pending_model = None
            elif kind == 'tool_call':
                if (pending_model is not None or pending_tool is not None or meta['round'] != completed
                        or meta.get('tool_name') not in allowed):
                    raise AgentStoreError('operator_query_draft_tool_unconfirmed')
                pending_tool = (meta['round'],meta['tool_name'])
            elif kind == 'tool_result':
                name = meta.get('tool_name')
                if pending_model is not None or pending_tool != (meta['round'],name):
                    raise AgentStoreError('operator_query_draft_tool_unconfirmed')
                pending_tool = None
                if name == 'propose_reply':
                    if meta.get('status') != 'error' or meta.get('code') != 'invalid_public_reply':
                        raise AgentStoreError('operator_query_draft_prior_decision')
                    rejected += 1
                elif meta.get('status') != 'ok':
                    raise AgentStoreError('operator_query_draft_tool_unconfirmed')
                elif name == 'query_knowledge_base':
                    evidence = meta.get('evidence_refs')
                    if (type(evidence) is not list or not 1 <= len(evidence) <= 8
                            or meta.get('knowledge_base_id') != binding['knowledge_base_id']
                            or binding['knowledge_base_id'] is not None and type(meta.get('knowledge_base_id')) is not int):
                        raise AgentStoreError('operator_query_draft_evidence_unconfirmed')
                    for ref in evidence:
                        if (type(ref) is not dict or set(ref) != {'source_id','platform_item_id','chunk_id','text_sha256'}
                                or type(ref.get('source_id')) is not str or not ref['source_id'].strip() or len(ref['source_id']) > 128
                                or type(ref.get('platform_item_id')) is not str or re.fullmatch(r'[0-9]{5,30}',ref['platform_item_id']) is None
                                or type(ref.get('chunk_id')) is not str or not ref['chunk_id'].strip() or len(ref['chunk_id']) > 256
                                or type(ref.get('text_sha256')) is not str or re.fullmatch(r'[a-f0-9]{64}',ref['text_sha256']) is None):
                            raise AgentStoreError('operator_query_draft_evidence_unconfirmed')
                        key = ref['source_id']
                        if key in refs and refs[key] != ref:
                            raise AgentStoreError('operator_query_draft_evidence_conflict')
                        refs[key] = dict(ref)
            else:
                raise AgentStoreError('operator_query_draft_steps_unconfirmed')
        last = json.loads(steps[-1]['metadata'])
        if (pending_model is not None or pending_tool is not None or completed != len(request_ids)
                or not 1 <= completed <= 6 or not refs or len(refs) > 64 or not rejected
                or steps[-1]['kind'] != 'tool_result' or last.get('tool_name') != 'propose_reply'
                or last.get('code') != 'invalid_public_reply'):
            raise AgentStoreError('operator_query_draft_steps_unconfirmed')
        observed = self.path.stat()
        archived = dict(task)
        archived['claim_fingerprint'] = hashlib.sha256(archived.pop('claim_token').encode()).hexdigest()
        snapshot = {'version':1,'task_id':task_id,'store_identity':{'component':_COMPONENT,
            'device':observed.st_dev,'inode':observed.st_ino},'task':archived,
            'task_sha256':hashlib.sha256(_public_json(dict(task),65536).encode()).hexdigest(),
            'steps_sha256':hashlib.sha256(_public_json([dict(step) for step in steps],5242880).encode()).hexdigest(),
            'envelope':envelope,'binding':binding,'recorded_refs':list(refs.values()),
            'budget_request_ids':request_ids,'completed_model_rounds':completed,
            'agent_prefix':self._reply_prefix(db,'agent:'+task_id)}
        return json.loads(_public_json(snapshot,262144))

    def operator_query_draft_snapshot(self, task_id):
        """Read eligibility without a write transaction, model or budget action."""
        with self._file_lock():
            self._guard()
            before = self.path.stat()
            with verified_readonly_connection(self.path,self._owned,guard=self._guard) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                result = self._operator_query_draft_snapshot(db,task_id)
            self._guard()
            after = self.path.stat()
            if (before.st_dev,before.st_ino) != (after.st_dev,after.st_ino):
                raise AgentStoreError('operator_query_draft_store_changed')
            return result

    def _commit_operator_query_draft(self, permit):
        """Create the FIRST ready answer only; never reclaim or rerun paid work."""
        try:
            valid = (type(permit) is _OperatorQueryDraftPermit and permit.store is self
                and type(permit._seal) is bytes and hmac.compare_digest(permit._seal,permit._signature()))
        except Exception:
            valid = False
        if not valid:
            raise AgentStoreError('operator_query_draft_permit_required')
        proof = _operator_query_draft_proof(permit.proof)
        proposal = _proposal(permit.proposal)
        if (proposal['kind'] != 'answer' or not 1 <= len(proposal['source_ids']) <= 8
                or len(set(proposal['source_ids'])) != len(proposal['source_ids'])):
            raise AgentStoreError('operator_query_draft_answer_required')
        with self._tx() as db:
            snapshot = self._operator_query_draft_snapshot(db,permit.snapshot.get('task_id'))
            if snapshot != permit.snapshot:
                raise AgentStoreError('operator_query_draft_snapshot_changed')
            now = time.time()
            if (proof['current_pid'] != os.getpid() or not -5 <= now-proof['reviewed_at'] <= 120
                    or proof['reviewed_at'] < snapshot['task']['updated_at']-1):
                raise AgentStoreError('operator_query_draft_review_expired')
            task_id = snapshot['task_id']
            if proof['reply_style_key'] not in {'assistant:'+proof['queue_task_id'],'agent:'+task_id}:
                raise AgentStoreError('operator_query_draft_style_changed')
            prefix = self._reply_prefix(db,proof['reply_style_key'])
            if (prefix is None or hashlib.sha256(prefix.encode()).hexdigest() != proof['prefix_sha256']
                    or snapshot['agent_prefix'] is not None and snapshot['agent_prefix'] != prefix):
                raise AgentStoreError('operator_query_draft_style_changed')
            recorded = {ref['source_id']:ref for ref in snapshot['recorded_refs']}
            if (not set(proposal['source_ids']) <= recorded.keys()
                    or len(proposal['sources']) != len(proposal['source_ids'])):
                raise AgentStoreError('operator_query_draft_evidence_changed')
            chosen = {}
            for source in proposal['sources']:
                key = source.get('source_id')
                ref = recorded.get(key)
                if (key in chosen or key not in proposal['source_ids'] or ref is None
                        or source.get('platform_item_id') != ref['platform_item_id']
                        or source.get('chunk_id') != ref['chunk_id']
                        or 'text_sha256' in source and source['text_sha256'] != ref['text_sha256']
                        or source.get('url') != 'https://www.douyin.com/video/'+ref['platform_item_id']
                        or type(source.get('title')) is not str or not source['title'].strip() or len(source['title']) > 512):
                    raise AgentStoreError('operator_query_draft_evidence_changed')
                chosen[key] = source
            if set(chosen) != set(proposal['source_ids']):
                raise AgentStoreError('operator_query_draft_evidence_changed')
            styled = self._styled_proposal(proposal,prefix)
            if (len(styled['text']) > 320 or '@' in styled['text'] or re.search(r'https?://',styled['text'],re.I)
                    or any(ord(char) < 32 and char not in '\n\t' for char in styled['text'])):
                raise AgentStoreError('operator_query_draft_public_reply_invalid')
            envelope, binding = snapshot['envelope'], snapshot['binding']
            target = {key:value for key,value in envelope.items() if key != 'evidence'}
            if _checked_followup(envelope,binding):
                target['followup_evidence'] = envelope['evidence']
            target.update(binding_id=binding['binding_id'],instance_id=binding['instance_id'],
                knowledge_base_id=binding['knowledge_base_id'])
            if binding.get('query_scope') == 'all_local':
                target['query_scope'] = 'all_local'
            reply_id = secrets.token_hex(16)
            audit = {'version':1,'operation':'operator_authored_first_query_draft','maximum_requests':1,
                'task_id':task_id,'reply_id':reply_id,'created_at':now,'snapshot':snapshot,'proof':proof,
                'proposal_sha256':hashlib.sha256(_public_json(styled,65536).encode()).hexdigest(),
                'provenance':'operator_authored_grounded_answer_not_recorded_model_draft',
                'model_replayed':False,'query_replayed':False,'ingestion_submitted':False,'dispatchable':False}
            db.execute('INSERT INTO agent_meta(key,value) VALUES(?,?)',
                ('operator_query_draft_v1:'+task_id,_public_json(audit,393216)))
            if snapshot['agent_prefix'] is None:
                db.execute('INSERT INTO agent_meta(key,value) VALUES(?,?)',
                    (self._reply_style_key('agent:'+task_id),prefix))
            db.execute("INSERT INTO agent_replies(reply_id,task_id,proposal,target,state,created_at) VALUES(?,?,?,?,'ready',?)",
                (reply_id,task_id,_public_json(styled,65536),_public_json(target,32768),now))
            db.execute("UPDATE agent_tasks SET state='answer_ready',updated_at=? WHERE task_id=?",(now,task_id))
            return {'task_id':task_id,'reply_id':reply_id,'status':'operator_query_draft_ready',
                'state':'answer_ready','dispatchable':False,'audit_recorded':True}


    def record_step(self, task_id, claim_token, kind, metadata):
        if not isinstance(kind, str) or kind not in STEP_KINDS or not isinstance(metadata, dict):
            raise AgentStoreError("invalid_step")
        payload = _public_json(metadata, 4096)
        with self._tx() as db:
            row, error = self._checked_claim(db, task_id, claim_token)
            if error is None:
                db.execute("INSERT INTO agent_steps(task_id,kind,metadata,created_at) VALUES(?,?,?,?)", (task_id, kind, payload, time.time()))
        if error:
            raise AgentStoreError(error)

    @staticmethod
    def _reply_style_key(key):
        if type(key) is not str or re.fullmatch(r'(?:assistant|agent):[a-f0-9]{32,64}', key) is None:
            raise AgentStoreError('invalid_reply_style_key')
        return 'reply_prefix:' + key

    @staticmethod
    def _reply_prefix(db, key):
        row = db.execute('SELECT value FROM agent_meta WHERE key=?', (AgentStore._reply_style_key(key),)).fetchone()
        if row is None:
            return None
        try:
            prefix = normalize_reply_prefix(row[0])
            if prefix != row[0]:
                raise ValueError('invalid_reply_prefix')
            return prefix
        except ValueError:
            raise AgentStoreError('invalid_reply_style') from None

    def reply_prefix(self, key):
        with self._tx() as db:
            return self._reply_prefix(db, key)

    def freeze_reply_prefix(self, key, prefix):
        try:
            prefix = normalize_reply_prefix(prefix)
        except ValueError:
            raise AgentStoreError('invalid_reply_style') from None
        with self._tx() as db:
            current = self._reply_prefix(db, key)
            if current is not None:
                return current
            db.execute('INSERT INTO agent_meta(key,value) VALUES(?,?)', (self._reply_style_key(key), prefix))
            return prefix

    @staticmethod
    def _styled_proposal(proposal, prefix):
        try:
            return _proposal({**proposal, 'text': style_reply_text(proposal['text'], prefix, maximum=8000)})
        except ValueError as error:
            raise AgentStoreError('invalid_reply_style_text') from error

    def complete(self, task_id, claim_token, proposal, *, reply_prefix=''):
        proposal = _proposal(proposal)
        with self._tx() as db:
            existing = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if existing:
                frozen_prefix = self._reply_prefix(db, 'agent:' + task_id) or ''
                if task["claim_token"] == claim_token and json.loads(existing["proposal"]) == self._styled_proposal(proposal, frozen_prefix):
                    return self._reply(existing)
                raise AgentStoreError("reply_conflict")
            task, error = self._checked_claim(db, task_id, claim_token)
            if error is None:
                frozen_prefix = self._reply_prefix(db, 'agent:' + task_id)
                if frozen_prefix is None:
                    try:
                        frozen_prefix = normalize_reply_prefix(reply_prefix)
                    except ValueError:
                        raise AgentStoreError('invalid_reply_style') from None
                    db.execute('INSERT INTO agent_meta(key,value) VALUES(?,?)', (self._reply_style_key('agent:' + task_id), frozen_prefix))
                proposal = self._styled_proposal(proposal, frozen_prefix)
                envelope = json.loads(task["envelope"])
                peers = self._reference_rows(db, task["binding_id"], _text(envelope["reference_id"], 512))
                if self._reference_choice(db, peers)["task_id"] != task_id:
                    raise AgentStoreError("reference_noncanonical_task")
                target = {key: value for key, value in envelope.items() if key != "evidence"}
                binding = self._binding_row(db, task["binding_id"])
                is_choice = _checked_choice(envelope, binding)
                is_followup = _checked_followup(envelope, binding)
                if is_choice:
                    target["choice_evidence"] = envelope["evidence"]
                if is_followup:
                    if proposal['kind'] == 'import_status':
                        raise AgentStoreError('followup_query_only')
                    target['followup_evidence'] = envelope['evidence']
                if proposal["kind"] == "import_status":
                    self._checked_import_proposal(db, task_id, proposal)
                target.update({"binding_id": task["binding_id"], "instance_id": binding["instance_id"],
                               "knowledge_base_id": binding["knowledge_base_id"]})
                if binding.get("query_scope") == "all_local":
                    target["query_scope"] = "all_local"
                now = time.time()
                db.execute("""INSERT INTO agent_replies(reply_id,task_id,proposal,target,state,created_at)
                  VALUES(?,?,?,?,'ready',?)""", (secrets.token_hex(16), task_id, _public_json(proposal, 65536), _public_json(target, 32768 if is_choice or is_followup else 16384), now))
                db.execute("UPDATE agent_tasks SET state='answer_ready',updated_at=? WHERE task_id=?", (now, task_id))
                result = self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())
        if error:
            raise AgentStoreError(error)
        return result

    def needs_review(self, task_id, claim_token, reason_code):
        reason = _text(reason_code, 128)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason):
            raise AgentStoreError("invalid_reason_code")
        with self._tx() as db:
            row, error = self._checked_claim(db, task_id, claim_token)
            if error is None:
                db.execute("UPDATE agent_tasks SET state='needs_review',reason_code=?,updated_at=? WHERE task_id=?", (reason, time.time(), task_id))
        if error:
            raise AgentStoreError(error)

    def get_task(self, task_id):
        with self._tx() as db:
            return self._task(db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())

    def lookup_existing(self, binding_id, envelope):
        """Observe a durable command/reference without creating or claiming it."""
        envelope = _envelope(envelope)
        with self._tx() as db:
            direct = db.execute("SELECT * FROM agent_tasks WHERE command_id=?", (envelope["command_id"],)).fetchone()
            alias = self._alias_task(db, envelope["command_id"])
            if direct and alias and direct["task_id"] != alias["task_id"]:
                raise AgentStoreError("command_conflict")
            prior = direct or alias
            if prior and (prior["binding_id"] != binding_id
                    or _canonical_reference(json.loads(prior["envelope"])) != _canonical_reference(envelope)):
                raise AgentStoreError("command_conflict")
            rows = self._reference_rows(db, binding_id, envelope["reference_id"])
            chosen = self._reference_choice(db, rows, envelope)
            if prior and chosen and prior["task_id"] != chosen["task_id"]:
                raise AgentStoreError("reference_conflict")
            return self._task(chosen or prior)

    def list_tasks(self, binding_id=None):
        with self._tx() as db:
            rows = db.execute("SELECT * FROM agent_tasks WHERE (? IS NULL OR binding_id=?) ORDER BY created_at,task_id", (binding_id, binding_id)).fetchall()
            return [self._task(row) for row in rows]

    def get_reply(self, task_id):
        with self._tx() as db:
            return self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())

    def _reply_readback_rows(self,db,task_id):
        return db.execute('SELECT id,metadata,created_at FROM agent_steps WHERE task_id=? AND kind=? ORDER BY id',
                          (task_id,_READBACK_KIND)).fetchall()

    def _checked_reply_readback(self,db,task_id):
        rows=self._reply_readback_rows(db,task_id)
        if not rows:return None
        if len(rows)!=1:raise AgentStoreError('readback_confirmation_conflict')
        row=rows[0]
        task=db.execute('SELECT * FROM agent_tasks WHERE task_id=?',(task_id,)).fetchone()
        reply=db.execute('SELECT * FROM agent_replies WHERE task_id=?',(task_id,)).fetchone()
        binding=self._binding_row(db,task['binding_id']) if task else None
        try:
            stored=json.loads(row['metadata'])
            expected=_readback_record(task,reply,binding,stored['observation'],row['created_at'])
            if _public_json(stored,65536)!=_public_json(expected,65536):
                raise AgentStoreError('readback_confirmation_changed')
        except (KeyError,TypeError,ValueError) as error:
            if isinstance(error,AgentStoreError):raise
            raise AgentStoreError('readback_confirmation_invalid') from None
        return _readback_certificate(stored)

    def record_reply_readback(self,task_id,observation):
        """Append one verified read-only fact; never mutate the old send result."""
        task_id=_text(task_id,128)
        with self._tx() as db:
            task=db.execute('SELECT * FROM agent_tasks WHERE task_id=?',(task_id,)).fetchone()
            reply=db.execute('SELECT * FROM agent_replies WHERE task_id=?',(task_id,)).fetchone()
            binding=self._binding_row(db,task['binding_id']) if task else None
            now=time.time()
            record=_readback_record(task,reply,binding,observation,now)
            old=self._checked_reply_readback(db,task_id)
            if old is not None:return old
            db.execute('INSERT INTO agent_steps(task_id,kind,metadata,created_at) VALUES(?,?,?,?)',
                       (task_id,_READBACK_KIND,_public_json(record,65536),now))
            return _readback_certificate(record)

    def get_reply_readback(self,task_id):
        """Revalidate persistent proof and mint a fresh in-process attestation."""
        task_id=_text(task_id,128)
        with self._tx() as db:return self._checked_reply_readback(db,task_id)

    def list_steps(self, task_id):
        with self._tx() as db:
            rows = db.execute("SELECT * FROM agent_steps WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def recovery_snapshot(self, binding_id=None):
        """Observe restart barriers; never reset leases, drafts or send tokens."""
        with self._tx() as db:
            tasks = db.execute("SELECT * FROM agent_tasks WHERE (? IS NULL OR binding_id=?) ORDER BY created_at,task_id", (binding_id, binding_id)).fetchall()
            now = time.time()
            reserved = []
            for task in tasks:
                reply = db.execute("SELECT state FROM agent_replies WHERE task_id=?", (task["task_id"],)).fetchone()
                if task["state"] == "send_reserved" or (reply and reply["state"] == "reserved"):
                    reserved.append(task["task_id"])
            groups = {}
            for task in tasks:
                ref = _text(json.loads(task["envelope"])["reference_id"], 512)
                groups.setdefault((task["binding_id"], ref), []).append(task)
            duplicate_groups = []
            for (bound, ref), peers in groups.items():
                if len(peers) < 2:
                    continue
                error = None
                try:
                    self._reference_choice(db, peers)
                except AgentStoreError as exc:
                    error = str(exc)
                duplicate_groups.append({"binding_id": bound, "reference_id": ref,
                    "task_ids": [peer["task_id"] for peer in peers],
                    "canonical_task_id": peers[0]["task_id"] if error is None else None,
                    "barrier_reason": error})
            return {"observed_at_utc": _iso(now),
                "queued_task_ids": [row["task_id"] for row in tasks if row["state"] == "queued"],
                "expired_processing_task_ids": [row["task_id"] for row in tasks if row["state"] == "processing" and row["lease_until"] <= now],
                "reserved_unconfirmed_task_ids": reserved,
                "needs_review_task_ids": [row["task_id"] for row in tasks if row["state"] == "needs_review"],
                "terminal_send_task_ids": {outcome: [row["task_id"] for row in tasks if row["state"] == outcome] for outcome in sorted(SEND_OUTCOMES)},
                "duplicate_reference_groups": duplicate_groups,
                "ingestion_job_ids_by_state": self._ingestion_summary(db, binding_id)}

    @staticmethod
    def _has_ingestions(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_ingestion_jobs'").fetchone() is not None

    @staticmethod
    def _create_ingestion_tables(db):
        # Lazy additive schema: opening or observing an old ledger never rewrites it.
        db.execute("""CREATE TABLE IF NOT EXISTS agent_ingestion_jobs(
          job_id TEXT PRIMARY KEY,binding_id TEXT NOT NULL,instance_id TEXT NOT NULL,
          source_mode TEXT NOT NULL,video_id TEXT NOT NULL,knowledge_base_id INTEGER NOT NULL,
          state TEXT NOT NULL,dispatch_token TEXT,backend_task_id TEXT,submission_outcome TEXT,
          submission_evidence TEXT,observation TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
          UNIQUE(binding_id,source_mode,video_id,knowledge_base_id))""")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_task_ingestions(
          task_id TEXT PRIMARY KEY,job_id TEXT NOT NULL,created_at REAL NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_ingestion_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT NOT NULL,kind TEXT NOT NULL,
          metadata TEXT NOT NULL,created_at REAL NOT NULL)""")

    @staticmethod
    def _ingestion(row):
        if row is None:
            return None
        value = dict(row)
        for key in ("submission_evidence", "observation"):
            value[key] = json.loads(value[key]) if value[key] is not None else None
        value["backend_request_id"] = value["job_id"]
        return value

    @staticmethod
    def _ingestion_event(db, job_id, kind, metadata):
        db.execute("INSERT INTO agent_ingestion_events(job_id,kind,metadata,created_at) VALUES(?,?,?,?)",
                   (job_id, kind, _public_json(metadata, 8192), time.time()))

    def _ingestion_summary(self, db, binding_id):
        summary = {state: [] for state in sorted(INGESTION_STATES)}
        if self._has_ingestions(db):
            rows = db.execute("""SELECT job_id,state FROM agent_ingestion_jobs
              WHERE (? IS NULL OR binding_id=?) ORDER BY created_at,job_id""", (binding_id, binding_id)).fetchall()
            for row in rows:
                summary[row["state"]].append(row["job_id"])
        return summary

    def _ingestion_claim(self, db, task_id, claim_token):
        task, error = self._checked_claim(db, task_id, claim_token)
        if error:
            return None, None, None, error
        envelope = json.loads(task["envelope"])
        if envelope.get('evidence', {}).get('source_kind') == 'conversation_followup':
            raise AgentStoreError('followup_query_only')
        peers = self._reference_rows(db, task["binding_id"], _text(envelope["reference_id"], 512))
        if self._reference_choice(db, peers)["task_id"] != task_id:
            raise AgentStoreError("reference_noncanonical_task")
        binding = self._binding_row(db, task["binding_id"])
        if binding is None or not binding["active"]:
            raise AgentStoreError("binding_inactive")
        if any(binding[key] != envelope[key] for key in ("sender_profile_url", "receiver_profile_url")):
            raise AgentStoreError("envelope_binding_mismatch")
        return task, binding, envelope, None

    def ensure_ingestion(self, task_id, claim_token):
        """Persist one fixed video/library intention, without submitting work.

        The caller must separately validate the user's positive save intent.
        Model arguments cannot choose a video, library, tenant or device here.
        """
        result = None
        with self._tx() as db:
            task, binding, envelope, error = self._ingestion_claim(db, task_id, claim_token)
            if error is None:
                self._create_ingestion_tables(db)
                linked = db.execute("SELECT job_id FROM agent_task_ingestions WHERE task_id=?", (task_id,)).fetchone()
                row = db.execute("""SELECT * FROM agent_ingestion_jobs WHERE binding_id=? AND source_mode=?
                  AND video_id=? AND knowledge_base_id=?""", (task["binding_id"], envelope["source_mode"],
                      envelope["video_id"], binding["knowledge_base_id"])).fetchone()
                if linked and (row is None or linked["job_id"] != row["job_id"]):
                    raise AgentStoreError("ingestion_target_conflict")
                if row and row["instance_id"] != binding["instance_id"]:
                    raise AgentStoreError("ingestion_target_conflict")
                now = time.time()
                if row is None:
                    job_id = secrets.token_hex(16)
                    db.execute("""INSERT INTO agent_ingestion_jobs(job_id,binding_id,instance_id,source_mode,video_id,
                      knowledge_base_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'queued',?,?)""",
                      (job_id, task["binding_id"], binding["instance_id"], envelope["source_mode"],
                       envelope["video_id"], binding["knowledge_base_id"], now, now))
                    self._ingestion_event(db, job_id, "created", {"task_id": task_id})
                    row = db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (job_id,)).fetchone()
                if linked is None:
                    db.execute("INSERT INTO agent_task_ingestions VALUES(?,?,?)", (task_id, row["job_id"], now))
                    self._ingestion_event(db, row["job_id"], "task_linked", {"task_id": task_id})
                result = self._ingestion(row)
        if error:
            raise AgentStoreError(error)
        return result

    def get_ingestion(self, job_id):
        with self._tx() as db:
            if not self._has_ingestions(db):
                return None
            return self._ingestion(db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (job_id,)).fetchone())

    def get_task_ingestion(self, task_id):
        with self._tx() as db:
            if not self._has_ingestions(db):
                return None
            return self._ingestion(db.execute("""SELECT j.* FROM agent_task_ingestions t
              JOIN agent_ingestion_jobs j ON j.job_id=t.job_id WHERE t.task_id=?""", (task_id,)).fetchone())

    def list_ingestions(self, binding_id=None):
        with self._tx() as db:
            if not self._has_ingestions(db):
                return []
            rows = db.execute("""SELECT * FROM agent_ingestion_jobs WHERE (? IS NULL OR binding_id=?)
              ORDER BY created_at,job_id""", (binding_id, binding_id)).fetchall()
            return [self._ingestion(row) for row in rows]

    def list_ingestion_events(self, job_id):
        with self._tx() as db:
            if not self._has_ingestions(db):
                return []
            rows = db.execute("SELECT * FROM agent_ingestion_events WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
            return [{**dict(row), "metadata": json.loads(row["metadata"])} for row in rows]

    def reserve_ingestion(self, task_id, claim_token):
        """Write a durable one-shot dispatch barrier before any HTTP request.

        A crash after this commit leaves 'reserved'. No lease can reopen it;
        recovery may only query the backend using the stable job/request ID.
        """
        result = None
        with self._tx() as db:
            task, binding, envelope, error = self._ingestion_claim(db, task_id, claim_token)
            if error is None and envelope["source_mode"] == "live_ui" and self._has_ingestions(db):
                row = db.execute("""SELECT j.* FROM agent_task_ingestions t JOIN agent_ingestion_jobs j
                  ON j.job_id=t.job_id WHERE t.task_id=?""", (task_id,)).fetchone()
                if row and (row["binding_id"] != task["binding_id"] or row["instance_id"] != binding["instance_id"]
                        or row["video_id"] != envelope["video_id"] or row["knowledge_base_id"] != binding["knowledge_base_id"]
                        or row["source_mode"] != envelope["source_mode"]):
                    raise AgentStoreError("ingestion_target_conflict")
                if row and row["state"] == "queued" and row["dispatch_token"] is None:
                    db.execute("UPDATE agent_ingestion_jobs SET state='reserved',dispatch_token=?,updated_at=? WHERE job_id=?",
                               (secrets.token_hex(24), time.time(), row["job_id"]))
                    self._ingestion_event(db, row["job_id"], "dispatch_reserved", {"task_id": task_id})
                    result = self._ingestion(db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (row["job_id"],)).fetchone())
        if error:
            raise AgentStoreError(error)
        return result

    def finish_ingestion_submission(self, job_id, dispatch_token, outcome, evidence):
        if not isinstance(outcome, str) or outcome not in {"accepted", "submission_unknown", "failed_no_submit"}:
            raise AgentStoreError("invalid_ingestion_submission_outcome")
        if (not isinstance(evidence, dict) or "backend_request_id" not in evidence
                or set(evidence) - {"backend_request_id", "backend_task_id", "reason_code", "http_status"}):
            raise AgentStoreError("invalid_ingestion_submission_evidence")
        if evidence["backend_request_id"] != job_id:
            raise AgentStoreError("ingestion_target_conflict")
        backend_task_id = evidence.get("backend_task_id")
        if backend_task_id is not None:
            _text(backend_task_id, 128)
        reason = evidence.get("reason_code")
        if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason)):
            raise AgentStoreError("invalid_reason_code")
        status = evidence.get("http_status")
        if status is not None and (type(status) is not int or not 100 <= status <= 599):
            raise AgentStoreError("invalid_http_status")
        payload = _public_json(evidence)
        with self._tx() as db:
            row = db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (job_id,)).fetchone() if self._has_ingestions(db) else None
            if (not row or not isinstance(dispatch_token, str) or not row["dispatch_token"]
                    or not secrets.compare_digest(row["dispatch_token"], dispatch_token)):
                raise AgentStoreError("stale_ingestion_dispatch_token")
            if row["submission_outcome"]:
                if row["submission_outcome"] == outcome and row["submission_evidence"] == payload:
                    return self._ingestion(row)
                raise AgentStoreError("ingestion_submission_conflict")
            state = row["state"] if row["state"] in {"processing", "succeeded", "failed", "needs_review"} else outcome
            db.execute("""UPDATE agent_ingestion_jobs SET state=?,submission_outcome=?,submission_evidence=?,
              backend_task_id=?,updated_at=? WHERE job_id=?""", (state, outcome, payload, backend_task_id, time.time(), job_id))
            self._ingestion_event(db, job_id, "submission_result", {"outcome": outcome, **evidence})
            return self._ingestion(db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (job_id,)).fetchone())

    def observe_ingestion(self, job_id, evidence):
        """Record trusted local reconciliation, never a model-reported success.

        indexed_content_chunks excludes the title-only metadata chunk. A
        backend ACK, transcript, or worker-task disappearance is insufficient.
        """
        if not isinstance(evidence, dict) or set(evidence) != _INGESTION_OBSERVATION_KEYS:
            raise AgentStoreError("invalid_ingestion_observation")
        if (not isinstance(evidence["backend_state"], str) or evidence["backend_state"] not in _INGESTION_BACKEND_STATES
                or type(evidence["membership_exists"]) is not bool
                or type(evidence["indexed_content_chunks"]) is not int or not 0 <= evidence["indexed_content_chunks"] <= 1000000
                or not isinstance(evidence["cache_status"], str)
                or evidence["cache_status"] not in {"missing", "pending", "downloading", "transcribing", "embedding", "done", "failed"}
                or type(evidence["knowledge_base_id"]) is not int):
            raise AgentStoreError("invalid_ingestion_observation")
        payload = _public_json(evidence)
        with self._tx() as db:
            row = db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (job_id,)).fetchone() if self._has_ingestions(db) else None
            if row is None:
                raise AgentStoreError("ingestion_not_found")
            if (evidence["backend_request_id"] != job_id or any(evidence[key] != row[key]
                    for key in ("binding_id", "instance_id", "knowledge_base_id", "video_id"))):
                raise AgentStoreError("ingestion_target_conflict")
            backend_state = evidence["backend_state"]
            if backend_state == "succeeded" and not (evidence["membership_exists"] and evidence["cache_status"] == "done"
                                                    and evidence["indexed_content_chunks"] > 0):
                raise AgentStoreError("incomplete_ingestion_success_evidence")
            if backend_state == "succeeded":
                state = "succeeded"
            elif row["state"] == "succeeded":
                raise AgentStoreError("ingestion_observation_conflict")
            elif row["state"] in {"failed", "failed_no_submit", "needs_review"}:
                state = row["state"]
            elif backend_state == "pending":
                state = row["state"]  # Pending alone must never reopen a dispatch barrier.
            elif backend_state == "unknown":
                state = "submission_unknown" if row["dispatch_token"] is not None else "needs_review"
            else:
                state = backend_state
            db.execute("UPDATE agent_ingestion_jobs SET state=?,observation=?,updated_at=? WHERE job_id=?",
                       (state, payload, time.time(), job_id))
            self._ingestion_event(db, job_id, "observation", evidence)
            return self._ingestion(db.execute("SELECT * FROM agent_ingestion_jobs WHERE job_id=?", (job_id,)).fetchone())

    def _checked_import_proposal(self, db, task_id, proposal):
        row = db.execute("""SELECT j.* FROM agent_task_ingestions t JOIN agent_ingestion_jobs j
          ON j.job_id=t.job_id WHERE t.task_id=?""", (task_id,)).fetchone() if self._has_ingestions(db) else None
        if row is None or row["job_id"] != proposal["import_job_id"]:
            raise AgentStoreError("import_proposal_job_mismatch")
        if row["state"] != proposal["import_state"]:
            raise AgentStoreError("import_proposal_state_mismatch")

    def hold_reply(self, task_id, reason_code):
        """Quarantine an unsent ready draft after publisher preflight fails."""
        reason = _text(reason_code, 128)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason):
            raise AgentStoreError("invalid_reason_code")
        with self._tx() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            reply = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            if (not task or not reply or task["state"] != "answer_ready" or reply["state"] != "ready"
                    or reply["send_token"] not in (None, "")):
                raise AgentStoreError("reply_not_unsent_ready")
            now = time.time()
            db.execute("UPDATE agent_tasks SET state='needs_review',reason_code=?,updated_at=? WHERE task_id=?", (reason, now, task_id))
            db.execute("UPDATE agent_replies SET state='needs_review',evidence=? WHERE task_id=?", (
                _public_json({"reason_code": reason, "held_at_utc": _iso(now)}), task_id))
            return self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())

    def release_held_reply(self, task_id, reason_code, *, verification=None):
        """Operator-only, one release of an unsent preflight-held original draft.

        This never requeues model or ingestion work. The publisher must acquire
        and verify the original comment afresh; another hold cannot be released
        a second time, and no reserved or delivery-unknown reply is eligible.
        """
        reason = _text(reason_code, 128)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason):
            raise AgentStoreError("invalid_reason_code")
        with self._tx() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            reply = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            if (not task or not reply or task["state"] != "needs_review" or reply["state"] != "needs_review"
                    or reply["send_token"] not in (None, "")):
                raise AgentStoreError("reply_not_held_unsent")
            previous_evidence = json.loads(reply["evidence"]) if reply["evidence"] is not None else None
            held_reason = previous_evidence.get("reason_code") if isinstance(previous_evidence, dict) else None
            if (held_reason not in {"preflight_failed", "preflight_verification_required", "original_notification_not_unique"}
                    or task["reason_code"] != held_reason):
                raise AgentStoreError("hold_reason_not_releasable")
            binding = self._binding_row(db, task["binding_id"])
            if binding is None or not binding["active"]:
                raise AgentStoreError("binding_inactive")
            if held_reason in {"preflight_verification_required", "original_notification_not_unique"}:
                target = json.loads(reply["target"])
                if not _verified_target(target, verification, binding):
                    raise AgentStoreError("hold_reason_not_releasable")
                observed = _utc(verification.get("observed_at_utc"))
                if observed < reply["created_at"] - 1 or not -5 <= time.time() - observed <= 120:
                    raise AgentStoreError("hold_reason_not_releasable")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_reply_hold_releases(
              release_id TEXT PRIMARY KEY,task_id TEXT UNIQUE NOT NULL,reason_code TEXT NOT NULL,
              released_at REAL NOT NULL,reply_snapshot TEXT NOT NULL)""")
            if db.execute("SELECT 1 FROM agent_reply_hold_releases WHERE task_id=?", (task_id,)).fetchone():
                raise AgentStoreError("hold_release_limit_reached")
            now = time.time()
            release_id = secrets.token_hex(16)
            db.execute("INSERT INTO agent_reply_hold_releases VALUES(?,?,?,?,?)", (
                release_id, task_id, reason, now, _public_json(dict(reply), 262144)))
            db.execute("UPDATE agent_replies SET state='ready',evidence=? WHERE task_id=?", (
                _public_json({"release_id": release_id, "reason_code": reason, "released_at_utc": _iso(now),
                              **({"verification": verification} if verification is not None else {})},
                             32768 if any(json.loads(reply['target']).get(key) for key in
                                 ('choice_evidence','followup_evidence')) else 8192), task_id))
            db.execute("UPDATE agent_tasks SET state='answer_ready',reason_code=?,updated_at=? WHERE task_id=?", (
                reason, now, task_id))
            return self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())

    def list_reply_hold_releases(self, task_id):
        """Read the archived hold and explicit release reason without migration."""
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_reply_hold_releases'").fetchone():
                return []
            rows = db.execute("SELECT * FROM agent_reply_hold_releases WHERE task_id=? ORDER BY released_at,release_id", (task_id,)).fetchall()
            return [{"release_id": row["release_id"], "task_id": row["task_id"], "reason_code": row["reason_code"],
                     "released_at": row["released_at"], "reply": self._reply(json.loads(row["reply_snapshot"]))} for row in rows]

    def refresh_unsent_import_reply(self, task_id, proposal):
        """Replace an unsent processing receipt after trusted backend reconciliation.

        This program-only operation neither claims model work nor requeues a
        submission. It archives every old receipt and can never reopen a reply
        with a send reservation, uncertain delivery, or a quarantined draft.
        """
        proposal = _proposal(proposal)
        if proposal["kind"] != "import_status":
            raise AgentStoreError("import_proposal_required")
        with self._tx() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            reply = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            if (not task or not reply or task["state"] != "answer_ready" or reply["state"] != "ready"
                    or reply["send_token"] not in (None, "")):
                raise AgentStoreError("reply_not_unsent_ready")
            binding = self._binding_row(db, task["binding_id"])
            if not binding or not binding["active"]:
                raise AgentStoreError("binding_inactive")
            previous = json.loads(reply["proposal"])
            if (previous["kind"] != "import_status"
                    or previous["import_job_id"] != proposal["import_job_id"]):
                raise AgentStoreError("import_proposal_job_mismatch")
            self._checked_import_proposal(db, task_id, proposal)
            proposal = self._styled_proposal(proposal, self._reply_prefix(db, 'agent:' + task_id) or '')
            if previous == proposal:
                return self._reply(reply)
            if previous["import_state"] in {"succeeded", "failed", "failed_no_submit", "needs_review"}:
                raise AgentStoreError("import_receipt_terminal")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_import_reply_updates(
              update_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,observed_state TEXT NOT NULL,
              updated_at REAL NOT NULL,reply_snapshot TEXT NOT NULL)""")
            db.execute("INSERT INTO agent_import_reply_updates VALUES(?,?,?,?,?)", (
                secrets.token_hex(16), task_id, proposal["import_state"], time.time(),
                _public_json(dict(reply), 262144)))
            db.execute("UPDATE agent_replies SET proposal=? WHERE task_id=?", (
                _public_json(proposal, 65536), task_id))
            db.execute("UPDATE agent_tasks SET updated_at=? WHERE task_id=?", (time.time(), task_id))
            return self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())

    def list_import_reply_updates(self, task_id):
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_import_reply_updates'").fetchone():
                return []
            return [{"update_id": row["update_id"], "observed_state": row["observed_state"],
                     "updated_at": row["updated_at"], "reply": self._reply(json.loads(row["reply_snapshot"]))}
                    for row in db.execute("SELECT * FROM agent_import_reply_updates WHERE task_id=? ORDER BY updated_at,update_id", (task_id,)).fetchall()]

    def requeue_unsent(self, task_id, reason_code):
        """Operator-only, one explicit revision of a draft that was never sent.

        The original command/envelope stays immutable. Its prior reply is kept
        in this ledger before the active reply is removed. This is not a model
        tool and is never invoked by expiry or automatic retry handling.
        """
        reason = _text(reason_code, 128)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason):
            raise AgentStoreError("invalid_reason_code")
        with self._tx() as db:
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            reply = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise AgentStoreError("task_not_found")
            binding = self._binding_row(db, task["binding_id"])
            if binding is None or not binding["active"]:
                raise AgentStoreError("binding_inactive")
            if (task["state"] != "answer_ready" or reply is None or reply["state"] != "ready"
                    or reply["send_token"] not in (None, "")):
                raise AgentStoreError("reply_not_unsent_ready")
            envelope = json.loads(task["envelope"])
            peers = self._reference_rows(db, task["binding_id"], _text(envelope["reference_id"], 512))
            if self._reference_choice(db, peers)["task_id"] != task_id:
                raise AgentStoreError("reference_noncanonical_task")
            db.execute("""CREATE TABLE IF NOT EXISTS agent_reply_revisions(
              revision_id TEXT PRIMARY KEY,task_id TEXT UNIQUE NOT NULL,reason_code TEXT NOT NULL,
              archived_at REAL NOT NULL,reply_snapshot TEXT NOT NULL)""")
            if db.execute("SELECT 1 FROM agent_reply_revisions WHERE task_id=?", (task_id,)).fetchone():
                raise AgentStoreError("revision_limit_reached")
            now = time.time()
            db.execute("INSERT INTO agent_reply_revisions VALUES(?,?,?,?,?)", (
                secrets.token_hex(16), task_id, reason, now, _public_json(dict(reply), 262144)))
            db.execute("DELETE FROM agent_replies WHERE task_id=?", (task_id,))
            db.execute("""UPDATE agent_tasks SET state='queued',claim_token=NULL,lease_until=NULL,
              reason_code=?,updated_at=? WHERE task_id=?""", (reason, now, task_id))
            return self._task(db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone())

    def list_reply_revisions(self, task_id):
        """Read archived drafts without creating a table or changing task state."""
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_reply_revisions'").fetchone():
                return []
            rows = db.execute("SELECT * FROM agent_reply_revisions WHERE task_id=? ORDER BY archived_at,revision_id", (task_id,)).fetchall()
            result = []
            for row in rows:
                old = self._reply(json.loads(row["reply_snapshot"]))
                result.append({"revision_id": row["revision_id"], "task_id": row["task_id"],
                    "reason_code": row["reason_code"], "archived_at": row["archived_at"],
                    "reply": old, "proposal": old["proposal"], "target": old["target"]})
            return result

    def reserve_send(self, task_id, verification):
        if not isinstance(verification, dict):
            return None
        payload = _public_json(verification, 32768 if verification.get("source_kind") in
            {"library_choice_reply", "conversation_followup"} else 8192)
        with self._tx() as db:
            reply = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            task = db.execute("SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
            if not reply or not task or reply["state"] != "ready" or task["state"] != "answer_ready":
                return None
            envelope = json.loads(task["envelope"])
            peers = self._reference_rows(db, task["binding_id"], _text(envelope["reference_id"], 512))
            chosen = self._reference_choice(db, peers)
            if chosen["task_id"] != task_id:
                return None
            binding = self._binding_row(db, task["binding_id"])
            target = json.loads(reply["target"])
            if not binding or not binding["active"] or not binding["public_reply_allowed"] or target["source_mode"] != "live_ui":
                return None
            proposed = json.loads(reply["proposal"])
            if proposed["kind"] == "import_status":
                try:
                    self._checked_import_proposal(db, task_id, proposed)
                except AgentStoreError:
                    return None
            if not _verified_target(target, verification, binding):
                return None
            try:
                observed = _utc(verification.get("observed_at_utc"))
            except AgentStoreError:
                return None
            now = time.time()
            if observed < reply["created_at"] - 1 or not -5 <= now - observed <= 120:
                return None
            token = secrets.token_hex(24)
            db.execute("UPDATE agent_replies SET state='reserved',send_token=?,verification=? WHERE task_id=?", (token, payload, task_id))
            db.execute("UPDATE agent_tasks SET state='send_reserved',updated_at=? WHERE task_id=?", (now, task_id))
            return self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())

    def finish_send(self, task_id, send_token, outcome, evidence):
        if not isinstance(outcome, str) or outcome not in SEND_OUTCOMES or not isinstance(evidence, dict):
            raise AgentStoreError("invalid_send_outcome")
        payload = _public_json(evidence)
        with self._tx() as db:
            row = db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone()
            if not row or not isinstance(send_token, str) or not row["send_token"] or not secrets.compare_digest(row["send_token"], send_token):
                raise AgentStoreError("stale_send_token")
            if row["state"] in SEND_OUTCOMES:
                if row["state"] == outcome and row["evidence"] == payload:
                    return self._reply(row)
                raise AgentStoreError("send_result_conflict")
            if row["state"] != "reserved":
                raise AgentStoreError("send_not_reserved")
            db.execute("UPDATE agent_replies SET state=?,evidence=? WHERE task_id=?", (outcome, payload, task_id))
            db.execute("UPDATE agent_tasks SET state=?,updated_at=? WHERE task_id=?", (outcome, time.time(), task_id))
            return self._reply(db.execute("SELECT * FROM agent_replies WHERE task_id=?", (task_id,)).fetchone())
