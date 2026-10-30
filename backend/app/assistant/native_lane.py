"""Native events drive fresh normal-page proof; they never impersonate it.

The native ledger owns history, identity pins, source uniqueness and durable
handoff plans. This owner-thread facade never opens credentials/network itself,
never injects a native CID into DOM detail, and never runs tools or publishes.
Only the existing actual UI builders create executable live_ui envelopes.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import math
import os
import re
import sqlite3
import threading

from app.mention_agent.contracts import Binding, canonical_json, public_profile
from app.mention_receiver.browser import (
    ReceiverDetailError, ReceiverInterventionRequired, ReceiverPageError,
)
from .choice_intent import parse_library_selection, selection_forms
from .native_lifecycle import project_native_session_status
from .native_source_store import NativeStoreError, _legacy_prequeue_permit, _FOLLOWUP_REVIEW_HOLD_CODES


_ID = re.compile(r"[1-9][0-9]{0,29}", re.ASCII)
_KINDS = frozenset({'native_notice_candidate', 'native_reply_notice_candidate'})
_LOGIN = frozenset({'session_expired', 'authorization_expired', 'session_invalid', 'invalid_session',
    'session_identity_mismatch', 'identity_not_verified', 'verification_required',
    'authorization_not_provisioned', 'capsule_identity_mismatch', 'capsule_invalid', 'capsule_unprotect_failed',
    'session_existing_draft', 'session_draft_state_unknown', 'session_context_changed'})
_ITEM_KEYS = frozenset({'binding', 'envelope', 'video_title', 'kind',
    'notice_fingerprint', 'user_id', 'device_id', 'authorization'})
_NOFX_LOG_TABLES = frozenset({'assistant_events', 'agent_steps', 'observations', 'audit'})
_NOFX_AUDIT_TABLES = frozenset({'semantic_plans', 'semantic_agent_links', 'semantic_ui_bindings',
    'semantic_budget_parents', 'usage_budget_requests', 'usage_budget_events', 'messages',
    'observations', 'audit', 'assistant_tasks', 'assistant_events', 'assistant_actions',
    'agent_tasks', 'agent_replies', 'agent_steps', 'agent_bindings', 'agent_command_aliases',
    'agent_task_ingestions', 'agent_ingestion_jobs'})
_NOFX_AUDIT_SUBSTAGES = frozenset('read_' + name for name in _NOFX_AUDIT_TABLES) | frozenset({
    'trusted_stores', 'source_time', 'semantic_records', 'semantic_links', 'budget_times',
    'native_records', 'native_observation_links', 'queue_attribution', 'queue_history_links',
    'agent_attribution', 'agent_history_links', 'agent_replies', 'agent_ingestions', 'witness_digest'})
_NOFX_AUDIT_EXCEPTION_TYPES = frozenset({'NativeLaneError', 'NativeStoreError', 'SemanticPlanError',
    'UsageBudgetError', 'NativeSemanticInboxError', 'StorageGuardError', 'SqliteOwnershipError',
    'OperationalError', 'DatabaseError', 'ValueError', 'TypeError', 'KeyError', 'OverflowError',
    'OSError', 'ReceiverPageError', 'ReceiverDetailError', 'ReceiverInterventionRequired'})
_NATIVE_READ_STAGES = frozenset({'original_ui_lookup', 'choice_ui_read', 'followup_prepare'})
_NATIVE_READ_CODES = frozenset({'native_read_checking', 'native_read_complete', 'native_read_no_result',
    'native_read_page_unready', 'native_read_detail_unverified', 'native_read_intervention',
    'native_read_unconfirmed', 'native_followup_no_resolution'})
_NATIVE_READ_EXCEPTION_TYPES = frozenset({'ReceiverPageError', 'ReceiverDetailError',
    'ReceiverInterventionRequired', 'NativeLaneError', 'TimeoutError', 'ValueError', 'TypeError', 'KeyError', 'local_error'})
_NATIVE_READ_VIEWPORT_KEYS = ('configured', 'expanded', 'restored', 'restore_failed')


class NativeLaneError(ReceiverPageError):
    """Fixed local error code; never copies a response, URL or exception."""
    def __init__(self, code='native_intake_unavailable'):
        self.code = code
        super().__init__(code)


class NativeSessionPaused(ReceiverInterventionRequired):
    """Current native failure provenance, for reporting only; never a retry."""
    def __init__(self, session_status):
        try:
            status = project_native_session_status(session_status)
            if status['state'] != 'paused':
                raise ValueError('native_pause_status_required')
        except Exception:
            status = None  # Unknown health still travels through the pause path.
        self.native_session_status = status
        self.native_failure_code = status['reason_code'] if status is not None else None
        super().__init__('本机会话或接口需要核对，自动操作已暂停；任务和草稿保留')


def _normal(value):
    return re.sub(r'\s+', '', value)


def _identity(value):
    result = deepcopy(value)
    result.pop('command_id', None)
    result['comment_text'] = _normal(result['comment_text'])
    proof = result.get('evidence', {})
    for key in ('observed_at_utc', 'diagnostics'):
        proof.pop(key, None)
    return result


class _InterruptionReadAdapter:
    """Delegate everything; observe only the existing READ method boundary."""
    def __init__(self, lane, claim):
        self._lane, self._claim = lane, claim

    def __getattr__(self, name):
        return getattr(self._lane.adapter, name)

    def read_choice_replies(self, *args, **kwargs):
        return self._lane._read_with_interruption_receipt(self._claim, 'choice_ui_read',
            lambda: self._lane.adapter.read_choice_replies(*args, **kwargs))


class NativeLane:
    """BrowserLane facade for an explicitly enabled native receiver.

    poll_native() is called ONCE by the owner loop before its bounded batch.
    collect() only consumes durable candidates; it never performs another GET.
    recover() should run before starting the tools worker. It only links exact
    already-existing Queue tasks, never recreates them from old UI evidence.
    """
    def __init__(self, browser_lane, native_receiver, source_store, *, max_candidates=3, session_lifecycle=None):
        if type(max_candidates) is not int or max_candidates != 3:
            raise NativeLaneError('invalid_native_candidate_limit')
        self.browser_lane, self.native_receiver, self.source_store = browser_lane, native_receiver, source_store
        self.session_lifecycle = session_lifecycle
        self.adapter, self.registry = browser_lane.adapter, browser_lane.registry
        self.queue, self.conversation = browser_lane.queue, browser_lane.conversation
        self.max_candidates = max_candidates
        self._owner_thread = threading.get_ident()
        self._pending = {}
        self._more = False
        self.last_native_issue_code = None
        self.last_native_read_diagnostic = {}
        self.visibility_hint_store = None  # Explicit local factory opt-in only.
        self.semantic_intake_ready = None
        self.semantic_candidate_action = None
        self.semantic_ui_source_ready = None
        self.followup_coordinator = None

    @property
    def run_id(self): return self.browser_lane.run_id
    @run_id.setter
    def run_id(self, value): self.browser_lane.run_id = value
    @property
    def should_stop(self): return self.browser_lane.should_stop
    @should_stop.setter
    def should_stop(self, value): self.browser_lane.should_stop = value
    @property
    def choice_controller(self): return self.browser_lane.choice_controller
    @choice_controller.setter
    def choice_controller(self, value): self.browser_lane.choice_controller = value
    @property
    def last_baseline_created(self): return self.browser_lane.last_baseline_created
    @property
    def last_detail_failure_count(self): return self.browser_lane.last_detail_failure_count
    @property
    def has_buffered_notices(self): return self._more

    def _owner(self):
        if threading.get_ident() != self._owner_thread:
            raise NativeLaneError('native_browser_owner_required')

    def _checkpoint(self):
        self._owner()
        if self.should_stop(): return False
        self.adapter.check_intervention()
        return not self.should_stop()

    def touch_devices(self):
        self._owner()
        return self.browser_lane.touch_devices()

    def poll_native(self):
        self._owner()
        if self.should_stop(): return {'received': 0, 'stopped': True}
        try:
            before = self.source_store.safe_status()
            if self.session_lifecycle is None:
                rows = self.native_receiver.poll_once()
            else:
                with self.session_lifecycle.cycle():
                    rows = self.native_receiver.poll_once()
            if not isinstance(rows, list):
                raise NativeLaneError('native_poll_result_invalid')
            status = self.source_store.safe_status()
            self.browser_lane.last_baseline_created = before.get('armed') is not True and status.get('armed') is True
            result = {'received': len(rows), 'status': status}
            session_status = self._public_native_session_status()
            if session_status is not None:
                result['native_session_status'] = session_status
            return result
        except ReceiverInterventionRequired as error:
            raise self._with_native_session_status(error) from None
        except Exception as error:
            code = getattr(error, 'code', None)
            if self.session_lifecycle is not None:
                # cycle() records exactly once and distinguishes bounded
                # transport cooldown from sticky authentication/local faults.
                session_status = self.session_lifecycle.safe_status()
                # Lifecycle can reject before NativeReceiver is entered;
                # deny new intake freshness without cancelling admitted intent.
                self.source_store.pause(code if type(code) is str and re.fullmatch(r'[a-z_]{1,80}', code)
                    else 'native_session_unavailable')
                if session_status['state'] == 'paused':
                    failure=NativeSessionPaused(session_status)
                    # Provenance belongs to THIS failure, never an older store
                    # pause or a visible string that might belong to a comment.
                    if code in {'invalid_native_visibility','invalid_notice_page','invalid_notice_schema',
                                'invalid_notice_type','invalid_notice_time','invalid_notice_identifier','unknown_comment_type','group_schema_unknown'}:
                        failure.native_intake_data_code=code
                    raise failure from None
                if session_status['state'] == 'cooldown':
                    raise self._with_native_session_status(NativeLaneError('native_session_cooldown')) from None
            if type(code) is str and code in _LOGIN:
                raise self._with_native_session_status(ReceiverInterventionRequired('原生收件登录或本机会话核验不可用，请本人处理后再开启助手')) from None
            raise self._with_native_session_status(NativeLaneError('native_poll_unavailable')) from None

    def _public_native_session_status(self):
        if self.session_lifecycle is None:
            return None
        return project_native_session_status(self.session_lifecycle.safe_status())

    def _with_native_session_status(self, error):
        status = self._public_native_session_status()
        if status is not None:
            error.native_session_status = status
        return error

    def resume_native_after_intervention(self):
        # Called only by main's existing explicit Start recovery gate.
        self._owner()
        if self.session_lifecycle is not None:
            try:
                self.session_lifecycle.resume_after_explicit_start()
            except Exception:
                raise ReceiverInterventionRequired('本机会话、验证或原草稿尚未核对，自动操作继续暂停') from None

    def _native_read_diagnostic(self, stage, code, error=None, *, capture_viewport=False):
        """Reporting only: no source transition, effect receipt or retry right."""
        previous = self.last_native_read_diagnostic if isinstance(self.last_native_read_diagnostic, dict) else {}
        raw = previous.get('viewport_counts', {})
        if capture_viewport:
            try:
                raw = getattr(self.adapter, 'last_choice_readback_viewport_diagnostics', {})
            except Exception:
                raw = {}
        raw = raw if isinstance(raw, dict) else {}
        counts = {key: raw.get(key) if type(raw.get(key)) is int and 0 <= raw[key] <= 1 else 0
            for key in _NATIVE_READ_VIEWPORT_KEYS}
        kind = None
        if error is not None:
            for cls in (ReceiverInterventionRequired, ReceiverDetailError, NativeLaneError, ReceiverPageError,
                        TimeoutError, ValueError, TypeError, KeyError):
                if isinstance(error, cls):
                    kind = cls.__name__
                    break
            kind = kind or 'local_error'
        diagnostic = {'stage':stage if type(stage) is str and stage in _NATIVE_READ_STAGES else 'original_ui_lookup',
            'code':code if type(code) is str and code in _NATIVE_READ_CODES else 'native_read_unconfirmed',
            'exception_type':kind, 'viewport_counts':counts}
        lookup_stage = previous.get('lookup_stage_index')
        if capture_viewport:
            try:
                lookup = getattr(self.adapter, 'last_original_lookup', {})
                lookup_stage = lookup.get('lookup_stage_index') if type(lookup) is dict else None
            except Exception:
                lookup_stage = None
        if type(lookup_stage) is int and 0 <= lookup_stage <= 10:
            diagnostic['lookup_stage_index'] = lookup_stage
        self.last_native_read_diagnostic = diagnostic

    def _read_with_interruption_receipt(self, claim, stage, read):
        self._owner()
        self.last_native_read_diagnostic = {}
        self._native_read_diagnostic(stage, 'native_read_checking')
        # A viewport-entry checkpoint can fail before the inner reader seeds its
        # own diagnostics. Never inherit a previous inspect's matched evidence.
        seed = {'stage':'followup_read_started','reason_code':'checking','counts':{}}
        for key, value in (('last_followup_diagnostic', seed), ('last_followup_diagnostics', seed),
                           ('last_original_lookup', {}), ('last_choice_readback_viewport_diagnostics',
                            {name:0 for name in _NATIVE_READ_VIEWPORT_KEYS})):
            try:
                setattr(self.adapter, key, value)
            except Exception:
                pass  # Optional diagnostics never replace or prevent the READ.
        try:
            result = read()
            self._native_read_diagnostic(stage, 'native_read_no_result' if result is None else 'native_read_complete',
                capture_viewport=True)
            return result
        except ReceiverInterventionRequired as error:
            self._native_read_diagnostic(stage, 'native_read_intervention', error, capture_viewport=True)
            try:
                receipt = self.source_store.record_prequeue_intervention(claim['source_id'],claim['claim_token'],
                    run_id=self.run_id or 'native-owner',stage=stage)
                error.native_prequeue_receipt = receipt
            except Exception:
                # Keep human pause even if durable proof cannot be written.
                # No proof means no generic prequeue resume is available.
                self.last_native_issue_code = 'native_interruption_receipt_unavailable'
            raise
        except Exception as error:
            code = ('native_read_detail_unverified' if isinstance(error, ReceiverDetailError) else
                    'native_read_page_unready' if isinstance(error, ReceiverPageError) else 'native_read_unconfirmed')
            self._native_read_diagnostic(stage, code, error, capture_viewport=True)
            raise  # Keep the original claim/exception/unknown-outcome behavior.

    def _recovery_identity(self):
        if not self._checkpoint():raise NativeLaneError('native_recovery_stopped')
        if self.session_lifecycle is None:
            valid = self.native_receiver.reverify_identity_checkpoint()
        else:
            with self.session_lifecycle.cycle(reset_failures_on_success=False):
                valid = self.native_receiver.reverify_identity_checkpoint()
        if valid is not True or not self._checkpoint():
            raise NativeLaneError('native_recovery_identity_unverified')

    def _visibility_scope_current(self, candidate):
        user = self.registry.find_user(candidate['sender_profile_url'], candidate['receiver_profile_url'])
        return (candidate['receiver_profile_url'] == self.adapter.expected_profile_url and user
            and user.get('active') is True and user.get('verification_status') == 'verified'
            and self.registry.intake_binding(candidate['sender_profile_url'], candidate['receiver_profile_url']) is not None)

    def request_visibility_review(self, hint_id):
        """Explicit run-owner action while processing is paused; never auto-run."""
        from .native_visibility_hint_store import validate_review_hint
        self._owner()
        ledger = self.visibility_hint_store
        if (ledger is None or type(hint_id) is not str
                or not re.fullmatch(r'native-hint:[a-f0-9]{64}', hint_id)):
            raise NativeLaneError('native_visibility_review_unavailable')
        def paused():
            state = self.queue.get_control()
            return (self.run_id is not None and state.get('run_id') == self.run_id
                    and state.get('state') == state.get('desired_state') == 'paused')
        if not paused(): raise NativeLaneError('native_visibility_review_requires_pause')
        self._recovery_identity()
        row = ledger.get_hint(hint_id)
        if not isinstance(row, dict):raise NativeLaneError('native_visibility_review_unavailable')
        hint = validate_review_hint(row['hint'])
        if not self._visibility_scope_current(hint) or not paused() or not self._checkpoint():
            raise NativeLaneError('native_visibility_review_scope_changed')
        ledger.request_review(hint_id, operator_reason='explicit_operator_unknown_visibility_ui_review')
        return {'source_id': hint_id, 'status': 'visibility_ui_review_requested', 'dispatchable': False}

    def request_followup_review(self, source_id):
        """Explicit owner repair of one never-dispatched ordinary reply.

        This only schedules one fresh READ. The old hold and its attempts are
        retained by the SourceStore audit; no old reply or task is re-published.
        """
        self._owner()
        self.last_native_recovery_diagnostic = {'stage': 'followup_review_scope', 'code': 'checking'}
        state = self.queue.get_control()
        if (self.followup_coordinator is None or self.run_id is None
                or state.get('run_id') != self.run_id
                or state.get('state') != state.get('desired_state')
                or state.get('state') != 'paused'):
            raise NativeLaneError('native_recovery_not_safe')
        self._recovery_identity()
        row = self.source_store.get_source(source_id)
        if (not isinstance(row, dict) or row.get('state') != 'held'
                or row.get('hold_code') not in _FOLLOWUP_REVIEW_HOLD_CODES
                or row.get('historical') is not False
                or any(row.get(key) is not None for key in ('plan', 'task_id', 'reference_id'))):
            raise NativeLaneError('native_recovery_scope_unavailable')
        candidate = self._candidate(row.get('candidate'))
        if (candidate['source_kind'] != 'native_reply_notice_candidate'
                or not self._visibility_scope_current(candidate)
                or not self._visibility_absent_from_effects(candidate)
                or len(self.followup_coordinator.anchors(candidate)) != 1
                or not self._checkpoint()):
            raise NativeLaneError('native_recovery_scope_unavailable')
        self.source_store.request_followup_review(source_id,
            operator_reason='explicit_operator_thread_followup_review')
        self.last_native_recovery_diagnostic = {'stage': 'followup_review_complete',
            'code': 'followup_ui_review_requested'}
        return {'source_id': source_id, 'status': 'followup_ui_review_requested',
            'dispatchable': False}

    def inspect_followup_source(self, source_id):
        """Read one held ordinary question before spending its explicit repair.

        It cannot claim, plan, enqueue, classify or publish anything.
        """
        self._owner()
        self.last_native_recovery_diagnostic = {'stage': 'followup_inspect_scope', 'code': 'checking'}
        state = self.queue.get_control()
        if (self.followup_coordinator is None or self.run_id is None
                or state.get('run_id') != self.run_id or state.get('state') != 'paused'
                or state.get('desired_state') != 'paused'
                or type(source_id) is not str or not re.fullmatch(r'native:[a-f0-9]{64}', source_id)):
            raise NativeLaneError('native_recovery_not_safe')
        self._recovery_identity()
        before = self.source_store.get_source(source_id)
        if (not isinstance(before, dict) or before.get('state') != 'held'
                or before.get('hold_code') not in _FOLLOWUP_REVIEW_HOLD_CODES
                or before.get('historical') is not False
                or any(before.get(key) is not None for key in ('plan', 'task_id', 'reference_id'))):
            raise NativeLaneError('native_recovery_scope_unavailable')
        candidate = self._candidate(before['candidate'])
        if (candidate['source_kind'] != 'native_reply_notice_candidate'
                or not self._visibility_scope_current(candidate)
                or not self._visibility_absent_from_effects(candidate)):
            raise NativeLaneError('native_recovery_scope_unavailable')

        def checkpoint():
            current = self.source_store.get_source(source_id)
            return (self._checkpoint() and current == before
                and self._visibility_scope_current(candidate))

        envelope = None
        read_started = False
        bridge = getattr(self.followup_coordinator, 'bridge', None)
        reset_anchor_diagnostic = getattr(bridge, 'reset_followup_anchor_diagnostic', None)
        fresh_anchor_diagnostic = callable(reset_anchor_diagnostic)
        if fresh_anchor_diagnostic:
            reset_anchor_diagnostic()
        def read_current(anchor, observed):
            nonlocal read_started
            read_started = True
            self.adapter.last_followup_diagnostic = {'stage': 'followup_read_started',
                'reason_code': 'checking', 'counts': {}}
            self.adapter.last_original_lookup = {}
            return self.adapter.read_followup_reply(anchor, observed, keep_open=True)
        try:
            _, envelope, _, failure = self.followup_coordinator.observe(candidate,
                checkpoint=checkpoint, read=read_current)
        except ReceiverDetailError:
            failure = 'native_followup_ui_unverified'
        if not checkpoint():
            raise NativeLaneError('native_recovery_scope_unavailable')
        self.last_native_recovery_diagnostic = {'stage': 'followup_inspect_complete',
            'code': failure or 'followup_original_matched'}
        if not read_started and failure == 'native_reply_parent_unresolved' and fresh_anchor_diagnostic:
            from .legacy_bridge import _FOLLOWUP_ANCHOR_REASONS, _FOLLOWUP_ANCHOR_COUNT_KEYS
            anchor_diagnostic = getattr(bridge, 'last_followup_anchor_diagnostic', None)
            anchor_diagnostic = anchor_diagnostic if isinstance(anchor_diagnostic, dict) else {}
            reason = anchor_diagnostic.get('code')
            self.last_native_recovery_diagnostic['code'] = (reason if type(reason) is str and reason in _FOLLOWUP_ANCHOR_REASONS
                else 'followup_anchor_not_checked')
            counts = anchor_diagnostic.get('counts')
            if isinstance(counts, dict):
                self.last_native_recovery_diagnostic['anchor_counts'] = {key: value for key, value in counts.items()
                    if key in _FOLLOWUP_ANCHOR_COUNT_KEYS and type(value) is int and 0 <= value <= 1000000}
        detail = getattr(self.adapter, 'last_followup_diagnostic', {}) if read_started else {}
        if isinstance(detail, dict):
            reason = detail.get('reason_code')
            if (type(reason) is str and ((envelope is not None and reason == 'matched')
                    or failure == 'native_followup_ui_unverified'
                    and re.fullmatch(r'followup_[a-z_]{1,65}', reason))):
                self.last_native_recovery_diagnostic['code'] = reason
            counts = detail.get('counts')
            if isinstance(counts, dict):
                keys = {'original_root_count', 'ask_count', 'selection_count', 'choice_sender_row_count',
                    'choice_receiver_target_row_count', 'body_candidate_count', 'readback_more_count',
                    'readback_unsupported_more_count', 'readback_loaded_row_count', 'root_count',
                    'prior_reply_count', 'followup_candidate_count', 'sender_row_count', 'reply_to_B_count',
                    'more_count', 'unsupported_more_count', 'loaded_row_count', 'detail_ok',
                    'sender_match_count', 'command_match_count', 'visible_comment_count', 'video_id_count'}
                self.last_native_recovery_diagnostic['followup_counts'] = {key: value for key, value in counts.items()
                    if key in keys and type(value) is int and 0 <= value <= 1000000}
        lookup = getattr(self.adapter, 'last_original_lookup', {}) if read_started else {}
        if isinstance(lookup, dict):
            self.last_native_recovery_diagnostic['lookup_counts'] = {key: value for key, value in lookup.items()
                if key in {'candidate_limit', 'candidate_count', 'checked_count', 'match_count', 'unreadable_count',
                    'final_rechecks', 'final_verified', 'snapshot_changes', 'page_open_count',
                    'lookup_stage_index', 'history_scrolls'} and type(value) is int and 0 <= value <= 1000000}
        return {'source_id': source_id, 'status': 'followup_source_inspected',
            'dispatchable': False, 'matched': envelope is not None}

    def inspect_visibility_hint(self, hint_id):
        """Explicit paused-owner READ only; never reclaim or revive a hint.

        This deliberately bypasses prepare_native_detail and every semantic,
        UI-plan and enqueue path. A successful lookup is diagnostic evidence,
        not an executable receipt, source claim or permission to send.
        """
        from .native_visibility_hint_store import validate_review_hint, hint_id_for
        self._owner()
        stage='inspect_scope'
        diagnostic={'stage':stage,'code':'checking'}
        self.last_native_recovery_diagnostic=diagnostic
        lookup_started=False
        def paused():
            state=self.queue.get_control()
            return (self.run_id is not None and state.get('run_id')==self.run_id
                    and state.get('state')==state.get('desired_state')=='paused')
        def unchanged(row):
            current=self.visibility_hint_store.get_hint(hint_id)
            return (isinstance(current,dict) and canonical_json(current)==canonical_json(row))
        try:
            ledger=self.visibility_hint_store
            controller=self.choice_controller
            if (ledger is None or type(hint_id) is not str
                    or not re.fullmatch(r'native-hint:[a-f0-9]{64}',hint_id)
                    or controller is None or not hasattr(controller,'admission_lock')
                    or not hasattr(controller,'agent') or not paused()):
                raise NativeLaneError('native_recovery_not_safe')
            row=ledger.get_hint(hint_id)
            if (not isinstance(row,dict) or row.get('state')!='held'
                    or row.get('admitted_source_id') is not None):
                raise NativeLaneError('native_recovery_scope_unavailable')
            hint=validate_review_hint(row['hint'])
            if hint_id_for(hint)!=hint_id:
                raise NativeLaneError('native_recovery_scope_unavailable')
            with controller.admission_lock:
                stage='inspect_identity';self._recovery_identity()
                stage='inspect_effects'
                self.source_store.visibility_hint_checkpoint(hint)
                if (not paused() or not unchanged(row) or not self._visibility_scope_current(hint)
                        or not self._visibility_absent_from_effects(hint) or not self._checkpoint()):
                    raise NativeLaneError('native_recovery_effects_unknown')
                target={'sender_profile_url':hint['sender_profile_url'],
                    'receiver_profile_url':hint['receiver_profile_url'],'video_id':hint['video_id'],
                    'comment_text':hint['content'],'native_comment_id':hint['native_comment_id'],
                    'reference_id':'native-inspection:'+hint_id.split(':',1)[1]}
                stage='inspect_original_lookup'
                self.adapter.last_original_lookup={}
                lookup_started=True
                detail=self.adapter.open_original_from_notifications(target,max_candidates=3,
                    native_candidate_correlation=True)
                # Do not return or persist detail, native UI references, text,
                # screenshots or a new envelope from this read-only operation.
                if (not isinstance(detail,dict) or detail.get('sender_profile_url')!=hint['sender_profile_url']
                        or detail.get('receiver_profile_url')!=hint['receiver_profile_url']
                        or detail.get('video_id')!=hint['video_id']
                        or type(detail.get('command_text')) is not str
                        or _normal(detail['command_text'])!=_normal(hint['content'])
                        or detail.get('native_comment_id') not in (None,hint['native_comment_id'])):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                stage='inspect_after_read'
                self._recovery_identity()
                self.source_store.visibility_hint_checkpoint(hint)
                if (not paused() or not unchanged(row) or not self._visibility_scope_current(hint)
                        or not self._visibility_absent_from_effects(hint) or not self._checkpoint()):
                    raise NativeLaneError('native_recovery_effects_unknown')
                stage='inspect_complete'
                diagnostic['code']='inspected'
                return {'source_id':hint_id,'status':'visibility_hint_inspected','dispatchable':False}
        except Exception as error:
            codes={'native_recovery_not_safe','native_recovery_scope_unavailable','native_recovery_effects_unknown',
                'native_recovery_stopped','native_recovery_identity_unverified','native_identity_unverified',
                'native_visibility_hint_scope_changed','native_visibility_hint_skip_required',
                'native_visibility_hint_source_exists','native_semantic_conflict'}
            code=getattr(error,'code',None)
            diagnostic['code']=code if type(code) is str and code in codes else 'native_recovery_unconfirmed'
            kinds={'ReceiverDetailError','ReceiverPageError','ReceiverInterventionRequired','NativeLaneError',
                   'NativeStoreError','NativeVisibilityHintError','TimeoutError','KeyError','TypeError','ValueError'}
            diagnostic['exception_type']=type(error).__name__ if type(error).__name__ in kinds else 'local_error'
            if isinstance(error,ReceiverInterventionRequired):raise
            raise NativeLaneError('native_recovery_not_safe') from None
        finally:
            diagnostic['stage']=stage
            if lookup_started:
                raw=getattr(self.adapter,'last_original_lookup',None)
                if type(raw) is dict:
                    keys={'candidate_limit','candidate_count','checked_count','match_count','unreadable_count',
                        'final_rechecks','final_verified','snapshot_changes','page_open_count',
                        'lookup_stage_index','history_scrolls'}
                    diagnostic['lookup_counts']={k:v for k,v in raw.items()
                        if k in keys and type(v) is int and 0<=v<=1000000}
                    reasons={'checking','matched','invalid_original_target','native_correlation_requires_source_id',
                        'receiver_binding_mismatch','original_notifications_unavailable','original_notification_not_unique',
                        'original_notification_candidates_exceed_limit','original_notification_changed',
                        'original_notification_not_found','original_candidate_unreadable','original_target_not_unique',
                        'original_native_semantic_ambiguous','original_target_changed','original_lookup_stopped',
                        'original_identity_not_ready','original_lookup_unavailable'}
                    if type(raw.get('reason_code')) is str and raw['reason_code'] in reasons:
                        diagnostic['lookup_reason_code']=raw['reason_code']

    def recover_visibility_hint(self,hint_id):
        """Register exactly one second READ of an already failed inert hint."""
        from .native_visibility_hint_store import validate_review_hint,hint_id_for,hint_source_id
        from .native_semantic_inbox import native_message_digest
        from .semantic_plan_store import decision_metadata
        from .semantic_resolution import catalog_snapshot_from_grants
        self._owner()
        stage='recovery_scope'
        self.last_native_recovery_diagnostic={'stage':stage,'code':'checking'}
        try:
            ledger=self.visibility_hint_store;controller=self.choice_controller
            state=self.queue.get_control()
            if (ledger is None or type(hint_id) is not str or not re.fullmatch(r'native-hint:[a-f0-9]{64}',hint_id)
                    or controller is None or not hasattr(controller,'admission_lock') or not hasattr(controller,'agent')
                    or self.run_id is None or state.get('run_id')!=self.run_id
                    or state.get('state')!=state.get('desired_state') or state.get('state')!='paused'):
                raise NativeLaneError('native_recovery_not_safe')
            with controller.admission_lock:
                row=ledger.get_hint(hint_id)
                if (not isinstance(row,dict) or row.get('state')!='held' or row.get('attempts')!=1
                        or row.get('hold_code')!='native_visibility_ui_unverified'
                        or row.get('admitted_source_id') is not None):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                hint=validate_review_hint(row['hint'])
                if hint_id_for(hint)!=hint_id:raise NativeLaneError('native_recovery_scope_unavailable')
                stage='recovery_identity';self._recovery_identity()
                stage='recovery_effects'
                self.source_store.visibility_hint_checkpoint(hint)
                if not self._visibility_scope_current(hint) or not self._visibility_absent_from_effects(hint):
                    raise NativeLaneError('native_recovery_effects_unknown')
                stage='recovery_meaning_scope'
                provider=getattr(self.browser_lane,'semantic_resolution_provider',None)
                inbox=getattr(provider,'native_inbox',None);store=getattr(provider,'store',None)
                context=getattr(provider,'_context',None)
                if inbox is None or store is None or not callable(context):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                source=hint_source_id(hint);meaning=inbox.lookup(source)
                receipt=store.get_native_decision(source)
                current=context(hint)
                if (not isinstance(meaning,dict) or meaning.get('state')!='understood'
                        or receipt is None or current is None):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                value=decision_metadata(receipt);user,device,grants=current
                owner={k:hint[k] for k in ('sender_profile_url','receiver_profile_url','video_id')}
                owner.update(user_id=user['user_id'],device_id=device['device_id'],instance_id=device['instance_id'],
                    reference_id=source,root_reference_id=source)
                if (value['mode']!='native' or value['source_key']!=source or value['owner_scope']!=owner
                        or value['catalog_snapshot']!=catalog_snapshot_from_grants(grants)
                        or value['native_facts_digest']!=native_message_digest(hint)
                        or native_message_digest(meaning['message'])!=value['native_facts_digest']
                        or meaning.get('facts_digest')!=value['native_facts_digest']
                        or meaning.get('plan_id')!=value['plan_id']):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                state=self.queue.get_control()
                if (state.get('run_id')!=self.run_id or state.get('state')!=state.get('desired_state')
                        or state.get('state')!='paused' or not self._checkpoint()
                        or canonical_json(ledger.get_hint(hint_id))!=canonical_json(row)
                        or not self._visibility_scope_current(hint)
                        or not self._visibility_absent_from_effects(hint)):
                    raise NativeLaneError('native_recovery_effects_unknown')
                stage='recovery_register'
                ledger.request_recovery_review(hint_id,
                    operator_reason='explicit_operator_held_visibility_ui_recovery',semantic_receipt=receipt)
                self.last_native_recovery_diagnostic={'stage':stage,'code':'recovery_requested'}
                return {'source_id':hint_id,'status':'visibility_hint_recovery_requested','dispatchable':False}
        except ReceiverInterventionRequired:
            self.last_native_recovery_diagnostic={'stage':stage,'code':'native_recovery_identity_unverified',
                'exception_type':'ReceiverInterventionRequired'}
            raise
        except Exception:
            self.last_native_recovery_diagnostic={'stage':stage,'code':'native_recovery_not_safe'}
            raise NativeLaneError('native_recovery_not_safe') from None

    def _visibility_check(self, claim):
        checked = self.visibility_hint_store.check_claim(claim['hint_id'], claim['claim_token'])
        return (isinstance(checked, dict) and checked.get('valid') is True
            and checked.get('semantic_conflict') is False
            and canonical_json(checked.get('candidate')) == canonical_json(claim['candidate'])
            and self.source_store.semantic_conflicts(claim['candidate']) is False)

    @staticmethod
    def _visibility_overlaps(envelope, candidate):
        if not isinstance(envelope, dict):raise NativeLaneError('native_visibility_effects_unknown')
        return (all(envelope.get(key) == candidate[key] for key in
                    ('sender_profile_url', 'receiver_profile_url', 'video_id'))
                and (envelope.get('native_comment_id') == candidate['native_comment_id']
                     or type(envelope.get('comment_text')) is str
                     and _normal(envelope['comment_text']) == _normal(candidate['content'])))

    def _visibility_absent_from_effects(self, candidate):
        """No equal-CID/semantic old task, even terminal, can become a new hint."""
        scanned = 0
        for page in range(1, 11):
            listing = self.queue.list_tasks(page=page, page_size=100)
            if type(listing.get('total')) is not int or listing['total'] > 1000:
                raise NativeLaneError('native_visibility_effects_unknown')
            for public in listing['items']:
                task = self.queue.get_task(public['id']); scanned += 1
                if not task:raise NativeLaneError('native_visibility_effects_unknown')
                if self._visibility_overlaps(task['envelope'], candidate):return False
            if scanned >= listing['total']:break
            if not listing['items']:raise NativeLaneError('native_visibility_effects_unknown')
        if scanned < listing['total']:raise NativeLaneError('native_visibility_effects_unknown')
        with self._recovery_agent_readonly() as db:
            rows = db.execute('SELECT envelope FROM agent_tasks LIMIT 1001').fetchall()
            if len(rows) > 1000:raise NativeLaneError('native_visibility_effects_unknown')
            if any(self._visibility_overlaps(json.loads(row['envelope']), candidate) for row in rows):return False
        return True

    def _visibility_hold(self, claim, code):
        self.last_native_issue_code = code
        self.browser_lane.last_detail_failure_count += 1
        try:self.visibility_hint_store.hold(claim['hint_id'], claim['claim_token'], code)
        except Exception:pass  # A partial committing/source plan is never retried.
        self.browser_lane.clear_buffer()

    def _collect_visibility_hint(self):
        ledger = self.visibility_hint_store
        if ledger is None:return []
        rows = ledger.list_hints(state='pending', limit=16)
        if not isinstance(rows, list) or len(rows) > 16:
            raise NativeLaneError('native_visibility_review_unavailable')
        if not rows:return []
        claim = ledger.claim_review(rows[0]['hint_id'], self.run_id or 'native-owner', lease_seconds=300)
        if claim is None:return []
        self._more = True
        review_stage='before_identity';review_error=None;review_code='native_review_unconfirmed';lookup_started=False
        try:
            claim = deepcopy(claim); candidate = self._candidate(claim['candidate'])
            claim['candidate'] = candidate
            provenance = candidate.get('hint_provenance', {})
            if (candidate.get('native_visibility') != 'unknown'
                    or candidate.get('visible_category') != 'other_int' or candidate.get('hidden_category') != 'int0'
                    or candidate.get('source_kind') != 'native_notice_candidate'
                    or provenance.get('hint_id') != claim['hint_id']
                    or provenance.get('original_source_kind') != 'native_visibility_review_hint'
                    or provenance.get('native_visibility') != 'unknown'):
                raise NativeLaneError('native_visibility_review_scope_changed')
            controller = self.choice_controller
            if controller is None or not hasattr(controller, 'admission_lock') or not hasattr(controller, 'agent'):
                raise NativeLaneError('native_visibility_effects_unknown')
            self._recovery_identity()
            with controller.admission_lock:
                if (not self._visibility_check(claim) or not self._visibility_scope_current(candidate)
                        or not self._legacy_guard(candidate) or not self._visibility_absent_from_effects(candidate)):
                    raise NativeLaneError('native_visibility_review_scope_changed')
            def checkpoint():
                nonlocal review_stage
                review_stage='semantic_prepare'
                self._recovery_identity()  # Actual fixed B GET after UI, not a boolean/timestamp grant.
                return self._visibility_check(claim) and self._visibility_scope_current(candidate)
            def read_original(target):
                nonlocal review_stage,lookup_started
                review_stage='original_lookup';lookup_started=True
                self.adapter.last_original_lookup={}
                result=self.adapter.open_original_from_notifications(target,
                    max_candidates=3,native_candidate_correlation=True)
                review_stage='after_ui'
                return result
            rows, thread, correlation, failure = self._prepare_mention_ui(candidate,
                read_original=read_original, source_checkpoint=checkpoint)
            if not rows:
                review_code=failure or 'native_visibility_ui_unverified'
                if self.should_stop():return []  # Unknown/expired claims are not unlocked.
                self._visibility_hold(claim, failure or 'native_visibility_ui_unverified');return []
            review_stage='source_plan'
            plan = self._ui_plan(rows[0], thread, correlation)
            with controller.admission_lock:
                selected = Binding.from_dict(plan['queue_item']['binding'])
                if (not self._checkpoint() or not self._visibility_check(claim)
                        or not self._visibility_scope_current(candidate) or not self._legacy_guard(candidate)
                        or not self._visibility_absent_from_effects(candidate)
                        or self.registry.authorize_snapshot(plan['queue_item']['authorization']) != selected):
                    if self.should_stop():return []
                    raise NativeLaneError('native_visibility_review_scope_changed')
                permit = ledger.mint_ui_plan_permit(claim['hint_id'], claim['claim_token'], plan)
                normal = self.source_store.accept_verified_hint_ui_plan(permit)
                if (not isinstance(normal, dict) or normal.get('source_id') != claim['source_id']
                        or canonical_json(normal.get('candidate')) != canonical_json(candidate)):
                    raise NativeLaneError('native_visibility_commit_unknown')
                self._pending[plan['reference_id']] = {'claim': deepcopy(normal), 'plan': plan}
            review_code='ui_plan_prepared'
            return rows
        except ReceiverInterventionRequired as error:
            review_error=error;review_code='native_visibility_review_intervention'
            self._visibility_hold(claim, 'native_visibility_review_intervention');raise
        except Exception as error:
            review_error=error
            if self.should_stop():return []
            code = getattr(error, 'code', None)
            lifecycle = self.session_lifecycle
            if (type(code) is str and code in _LOGIN
                    or lifecycle is not None and lifecycle.safe_status().get('state') == 'paused'):
                self._visibility_hold(claim, 'native_visibility_review_intervention')
                raise ReceiverInterventionRequired('本机抖音登录或核验需要本人处理，复核已保留') from None
            self._visibility_hold(claim, 'native_visibility_ui_unverified')
            return []
        finally:
            try:
                allowed={'ui_plan_prepared','native_visibility_ui_unverified','native_visibility_review_scope_changed',
                    'native_visibility_effects_unknown','native_visibility_commit_unknown','native_scope_changed',
                    'native_ui_unverified','native_semantic_conflict','native_recovery_stopped',
                    'native_identity_unverified','native_visibility_review_intervention','native_review_unconfirmed'}
                code=getattr(review_error,'code',None) if review_error is not None else review_code
                diagnostic={'stage':review_stage,'code':code if type(code) is str and code in allowed else 'native_review_unconfirmed'}
                if review_error is not None:
                    classes={'ReceiverDetailError','ReceiverPageError','ReceiverInterventionRequired','NativeLaneError',
                        'NativeStoreError','NativeVisibilityHintError','TimeoutError','KeyError','TypeError','ValueError'}
                    diagnostic['exception_type']=type(review_error).__name__ if type(review_error).__name__ in classes else 'local_error'
                raw=getattr(self.adapter,'last_original_lookup',None) if lookup_started else None
                if type(raw) is dict:
                    keys={'candidate_limit','candidate_count','checked_count','match_count','unreadable_count',
                        'final_rechecks','final_verified','snapshot_changes','page_open_count','lookup_stage_index','history_scrolls'}
                    diagnostic['lookup_counts']={k:v for k,v in raw.items() if k in keys and type(v) is int and 0<=v<=1000000}
                    reasons={'checking','matched','invalid_original_target','native_correlation_requires_source_id',
                        'receiver_binding_mismatch','original_notifications_unavailable','original_notification_not_unique',
                        'original_notification_candidates_exceed_limit','original_notification_changed',
                        'original_notification_not_found','original_candidate_unreadable','original_target_not_unique',
                        'original_native_semantic_ambiguous','original_target_changed','original_lookup_stopped',
                        'original_identity_not_ready','original_lookup_unavailable'}
                    if type(raw.get('reason_code')) is str and raw['reason_code'] in reasons:
                        diagnostic['lookup_reason_code']=raw['reason_code']
                ledger.record_ui_review_diagnostic(claim['hint_id'],claim['claim_token'],diagnostic)
            except Exception:
                pass  # A diagnostic sink never changes the original decision.

    def request_interrupted_prequeue_resume(self, source_id, *, receipt_id, expected_attempts):
        """Explicit owner action only, never called by Start/poll/timer."""
        self._owner(); self._recovery_identity()
        source = self.source_store.get_source(source_id)
        if source is None:raise NativeLaneError('native_recovery_scope_unavailable')
        candidate = self._candidate(source['candidate'])
        user = self.registry.find_user(candidate['sender_profile_url'],candidate['receiver_profile_url'])
        if (not user or not user['active'] or user['verification_status']!='verified'
                or self.registry.intake_binding(candidate['sender_profile_url'],candidate['receiver_profile_url']) is None):
            raise NativeLaneError('native_recovery_scope_unavailable')
        return self.source_store.request_prequeue_resume(source_id,receipt_id=receipt_id,
            expected_attempts=expected_attempts,operator_reason='explicit_operator_after_prequeue_intervention')

    @staticmethod
    def _overlaps_recovery_source(envelope, candidate, session, reference_id=None):
        if not isinstance(envelope,dict) or any(envelope.get(k)!=candidate.get(k)
                for k in ('sender_profile_url','receiver_profile_url','video_id')):return False
        proof = envelope.get('evidence',{})
        if not isinstance(proof,dict):return True
        texts = (envelope.get('comment_text'),proof.get('selection_value'))
        return (envelope.get('native_comment_id')==candidate['native_comment_id']
            or reference_id is not None and envelope.get('reference_id')==reference_id
            or proof.get('choice_id')==session['choice_id']
            or any(type(text) is str and _normal(text)==_normal(candidate['content']) for text in texts))

    @contextmanager
    def _recovery_agent_readonly(self):
        agent = self.choice_controller.agent
        # Normal AgentStore getters use BEGIN IMMEDIATE. Reuse its ownership
        # guard/lock, but never open a write connection for this audit.
        with agent._file_lock():
            agent._guard(); before=agent.path.stat()
            db=sqlite3.connect(agent.path.as_uri()+'?mode=ro',uri=True,timeout=5)
            db.row_factory=sqlite3.Row
            try:
                db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
                if not agent._owned(db):raise NativeLaneError('native_recovery_effects_unknown')
                yield db
                agent._guard(); after=agent.path.stat()
                if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino):
                    raise NativeLaneError('native_recovery_effects_unknown')
            finally:db.close()

    def _legacy_no_effects_audit(self, candidate, session, fresh_envelope=None):
        controller=self.choice_controller
        current=controller.store.get(session['choice_id'])
        if (not current or current['state']!='waiting_library'
                or any(current.get(k) is not None for k in ('selection_ref','selection_envelope','selected_kb_id',
                    'selected_authorization','child_task_id','child_key'))
                or controller._fresh_root(current) is None):
            raise NativeLaneError('native_recovery_effects_present')
        with controller.store._db() as db:
            if db.execute("SELECT COUNT(*) FROM choice_events WHERE choice_id=? AND event IN ('selection_reserved','handoff_claimed','child_linked')",
                    (session['choice_id'],)).fetchone()[0]:
                raise NativeLaneError('native_recovery_effects_present')
        reference=fresh_envelope.get('reference_id') if fresh_envelope else None
        scanned=0; root=None
        for page in range(1,11):
            listing=self.queue.list_tasks(page=page,page_size=100)
            if type(listing.get('total')) is not int or listing['total']>1000:
                raise NativeLaneError('native_recovery_effects_unknown')
            for public in listing['items']:
                task=self.queue.get_task(public['id']);scanned+=1
                if not task:raise NativeLaneError('native_recovery_effects_unknown')
                if task['id']==session['root_task_id']:
                    root=task
                    if (task.get('kind')!='clarify' or task.get('import_state') is not None
                            or _identity(task['envelope'])!=_identity(session['root_envelope'])):
                        raise NativeLaneError('native_recovery_effects_present')
                elif self._overlaps_recovery_source(task['envelope'],candidate,session,reference):
                    raise NativeLaneError('native_recovery_effects_present')
            if scanned>=listing['total']:break
            if not listing['items']:raise NativeLaneError('native_recovery_effects_unknown')
        if root is None or scanned<listing['total']:
            raise NativeLaneError('native_recovery_effects_unknown')
        with self._recovery_agent_readonly() as db:
            rows=list(db.execute('SELECT task_id,binding_id,envelope FROM agent_tasks LIMIT 1001'))
            if len(rows)>1000:raise NativeLaneError('native_recovery_effects_unknown')
            allowed_root=[]
            for row in rows:
                envelope=json.loads(row['envelope'])
                if row['binding_id']==root['binding_id'] and _identity(envelope)==_identity(root['envelope']):
                    allowed_root.append(row['task_id'])
                elif self._overlaps_recovery_source(envelope,candidate,session,reference):
                    raise NativeLaneError('native_recovery_effects_present')
            if root.get('agent_task_id') is not None:
                allowed_root=[key for key in allowed_root if key==root['agent_task_id']]
            if len(allowed_root)!=1:raise NativeLaneError('native_recovery_effects_unknown')
            root_agent=allowed_root[0]
            reply=db.execute('SELECT state,proposal,target,evidence,send_token IS NOT NULL AS has_barrier FROM agent_replies WHERE task_id=?',(root_agent,)).fetchone()
            if reply is None:raise NativeLaneError('native_recovery_effects_unknown')
            proposal=json.loads(reply['proposal']);target=json.loads(reply['target']);evidence=json.loads(reply['evidence'] or '{}')
            if (reply['state']!='b_view_visible' or not reply['has_barrier']
                    or proposal.get('kind')!='clarification' or proposal.get('text')!=session['ask_text']
                    or target.get('reference_id')!=root['envelope']['reference_id']
                    or evidence.get('submit_gate_passed') is not True
                    or evidence.get('exact_body_in_original_thread') is not True
                    or evidence.get('reply_sha256')!=hashlib.sha256(session['ask_text'].encode()).hexdigest()):
                raise NativeLaneError('native_recovery_effects_unknown')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_task_ingestions'").fetchone():
                if db.execute('SELECT 1 FROM agent_task_ingestions WHERE task_id=?',(root_agent,)).fetchone():
                    raise NativeLaneError('native_recovery_effects_present')
                bindings={option['authorization']['binding_id'] for option in session['options']}
                if bindings:
                    jobs=list(db.execute('SELECT job_id FROM agent_ingestion_jobs WHERE video_id=? AND binding_id IN ('+
                        ','.join('?' for _ in bindings)+')',(candidate['video_id'],*sorted(bindings))))
                    for job in jobs:
                        linked=list(db.execute('SELECT task_id FROM agent_task_ingestions WHERE job_id=?',(job['job_id'],)))
                        if not linked or any(link['task_id'] not in {row['task_id'] for row in rows} for link in linked):
                            raise NativeLaneError('native_recovery_effects_unknown')
        return {'choice_id':session['choice_id'],'root_task_id':root['id'],
            'allowed_historical_ask_agent_id':root_agent,'queue_selection_matches':0,'agent_selection_matches':0,
            'prior_choice_selection_events':0,'historical_ask_barrier_preserved':True}

    def review_and_resume_legacy_prequeue(self, source_id):
        """Explicit owner-only one-off audit, not a fabricated old receipt.

        No model/import/send or Choice reservation occurs here. Unreadable or
        ambiguous cross-ledger history rejects; no caller-provided proof bool.
        """
        self._owner()
        controller=self.choice_controller
        if controller is None:raise NativeLaneError('native_recovery_scope_unavailable')
        stage='snapshot'
        self.last_native_recovery_diagnostic={'stage':stage,'code':'started'}
        with controller.admission_lock:
            try:
                snapshot=self.source_store.legacy_prequeue_review_snapshot(source_id)
                candidate=self._candidate(snapshot['candidate'])
                stage='identity_before_read'
                self._recovery_identity()
                stage='find_original_choice'
                session=self._reply_session(candidate)
                if session is None:raise NativeLaneError('native_recovery_scope_unavailable')
                stage='effects_before_read'
                self._legacy_no_effects_audit(candidate,session)
                stage='fresh_original_thread_read'
                details=self.adapter.read_choice_replies(session,list(selection_forms(session['options'])),keep_open=True)
                if not self._checkpoint() or not isinstance(details,list) or len(details)!=1:
                    raise NativeLaneError('native_recovery_ui_unverified')
                detail=details[0];proof=detail.get('evidence',{})
                if not controller._fresh_time(proof):raise NativeLaneError('native_recovery_ui_unverified')
                option=parse_library_selection(proof.get('selection_value',detail.get('command_text')),session['options'])
                if option is None:raise NativeLaneError('native_recovery_ui_unverified')
                binding=self.registry.authorize_snapshot(option['authorization'],action='import')
                if binding is None:raise NativeLaneError('native_recovery_scope_unavailable')
                from app.mention_agent.choice_evidence import make_choice_envelope
                notice={'source_profile_url':detail['sender_profile_url'],'exact_text':detail['command_text'],
                    'semantic_fingerprint':'legacy-owner-fresh-read'}
                envelope=make_choice_envelope(notice,detail,binding,source_mode=session['root_envelope']['source_mode'])
                envelope['evidence']['observed_at_utc']=proof['observed_at_utc']
                if not self._choice_scope_matches(candidate,session,envelope):
                    raise NativeLaneError('native_recovery_ui_unverified')
                stage='identity_after_read'
                self._recovery_identity()
                if (not controller._fresh_time(proof) or controller._fresh_root(session) is None
                        or self.registry.authorize_snapshot(option['authorization'],action='import')!=binding):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                stage='effects_after_read'
                audit=self._legacy_no_effects_audit(candidate,session,envelope)
                if (not self._checkpoint() or not controller._fresh_time(proof)
                        or controller._fresh_root(session) is None
                        or self.registry.authorize_snapshot(option['authorization'],action='import')!=binding):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                reviewed_at=datetime.fromisoformat(proof['observed_at_utc'].replace('Z','+00:00')).timestamp()
                review={'operation':'owner_fresh_legacy_prequeue_review','reviewed_at':reviewed_at,
                    'choice_id':session['choice_id'],'root_task_id':session['root_task_id'],
                    'ui_reference_id':envelope['reference_id'],'knowledge_base_id':binding.knowledge_base_id,
                    'scope_digest':hashlib.sha256(canonical_json({'envelope':_identity(envelope),'authorization':option['authorization']}).encode()).hexdigest(),
                    'cross_ledger_digest':hashlib.sha256(canonical_json(audit).encode()).hexdigest()}
                stage='commit_review'
                result=self.source_store._commit_legacy_prequeue_resume(_legacy_prequeue_permit(self.source_store,snapshot,review))
                self.last_native_recovery_diagnostic={'stage':stage,'code':'reviewed'}
                return result
            except (NativeStoreError,NativeLaneError,ReceiverInterventionRequired) as error:
                code=getattr(error,'code',None)
                if code is None and isinstance(error,NativeLaneError):code=str(error)
                if type(code) is not str or not re.fullmatch(r'native_[a-z_]{1,70}',code):code='native_recovery_unconfirmed'
                self.last_native_recovery_diagnostic={'stage':stage,'code':code}
                raise
            except Exception as error:
                kind=type(error).__name__
                self.last_native_recovery_diagnostic={'stage':stage,'code':'native_recovery_effects_unknown',
                    'exception_type':kind if kind in {'KeyError','ValueError','TypeError','AttributeError','OperationalError',
                        'SessionLifecycleError','NativeReceiverError','ReceiverDetailError','ReceiverPageError',
                        'ReplyPreflightError','TimeoutError'} else 'local_error'}
                raise NativeLaneError('native_recovery_effects_unknown') from None

    def _followup_no_effects_scope(self):
        """Current owner scheduling facts, via an existing owned RO connection."""
        from .legacy_bridge import _followup_owned_ro
        self._owner()
        with _followup_owned_ro(self.queue) as db:
            row = db.execute("SELECT value FROM assistant_meta WHERE key='control'").fetchone()
            state = json.loads(row[0]) if row else {}
            busy = db.execute("SELECT COUNT(*) FROM assistant_tasks WHERE state IN "
                "('queued','waiting_desktop','executing','waiting_ingestion','ready_to_publish','publishing',"
                "'waiting_external','waiting_library','needs_reconcile')").fetchone()[0]
        manual = state.get('manual_verification')
        if (self.run_id is None or state.get('run_id') != self.run_id
                or state.get('pid') != os.getpid() or state.get('process_alive') is False
                or state.get('state') != 'paused' or state.get('desired_state') != 'paused'
                or state.get('worker_state') != 'idle' or type(manual) is not dict
                or manual.get('active') is not False or busy or self._pending
                or self.browser_lane.pending or not self._checkpoint()):
            raise NativeLaneError('native_recovery_not_safe')
        return True

    def _followup_no_effects_audit(self, candidate, snapshot, anchor, fresh_envelope=None):
        """Fail-closed whole-ledger audit; never use executable-only getters."""
        def substage(value):
            self._followup_no_effects_audit_stage = value
        substage('trusted_stores')
        from .legacy_bridge import _followup_owned_ro
        from .semantic_routing import SemanticIntentInterpreter
        from .semantic_plan_store import (SemanticPlanStore, _owner as semantic_owner,
            _text as semantic_text, _key as semantic_key, _catalog as semantic_catalog)
        from .usage_budget import UsageBudget
        from .native_semantic_inbox import (NativeSemanticInbox, validate_native_message,
            native_message_source_key, native_message_digest)
        from app.mention_agent.store import AgentStore
        provider = getattr(self.browser_lane, 'semantic_resolution_provider', None)
        bridge = self.followup_coordinator.bridge
        if (type(provider) is not SemanticIntentInterpreter or type(provider.store) is not SemanticPlanStore
                or type(provider.budget) is not UsageBudget or provider.store is not bridge.semantic_store
                or provider.budget is not bridge.usage_budget or type(bridge.agent) is not AgentStore):
            raise NativeLaneError('native_recovery_effects_unknown')
        substage('source_time')
        since = snapshot.get('first_seen')
        audit_now = self.source_store._clock()
        if (type(since) not in (int, float) or not math.isfinite(since) or since <= 0
                or type(audit_now) not in (int, float) or not math.isfinite(audit_now)
                or since > audit_now + 5):
            raise NativeLaneError('native_recovery_effects_unknown')
        refs = {fresh_envelope['reference_id']} if fresh_envelope else set()
        scope = ('sender_profile_url', 'receiver_profile_url', 'video_id')
        witness = {'store_identity': {}}
        def identity(name, store):
            observed = store.path.stat()
            witness['store_identity'][name] = [observed.st_dev, observed.st_ino]
        def rows(db, table):
            if table not in _NOFX_AUDIT_TABLES:
                raise NativeLaneError('native_recovery_effects_unknown')
            substage('read_' + table)
            if table in _NOFX_LOG_TABLES:
                cursor = db.execute('SELECT * FROM '+table+' ORDER BY rowid')
                value = []
                while True:
                    batch = cursor.fetchmany(500)
                    if not batch:
                        break
                    value.extend(dict(row) for row in batch)
                    # A refusal fence, never evidence that a truncated history is empty.
                    if len(value) >= 50000:
                        raise NativeLaneError('native_recovery_effects_unknown')
            else:
                value = [dict(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid LIMIT 1001')]
                if len(value) > 1000:
                    raise NativeLaneError('native_recovery_effects_unknown')
            return value
        def same_scope(value):
            if (type(value) is not dict
                    or any(type(value.get(k)) is not str or public_profile(value[k]) != value[k]
                        for k in ('sender_profile_url', 'receiver_profile_url'))
                    or value['sender_profile_url'] == value['receiver_profile_url']
                    or type(value.get('video_id')) is not str or not re.fullmatch(r'[0-9]{5,30}', value['video_id'])):
                raise NativeLaneError('native_recovery_effects_unknown')
            return all(value[k] == candidate[k] for k in scope)
        core = ('command_id', 'reference_id', 'reference_quality', 'native_comment_id',
            'sender_profile_url', 'receiver_profile_url', 'video_id', 'comment_text', 'source_mode')
        def checked_envelope(value):
            same_scope(value)  # Validate attribution even when it is another actor/video.
            if (not set(core) <= value.keys() or type(value.get('evidence')) is not dict
                    or value['source_mode'] not in {'live_ui', 'isolated', 'replay'}
                    or value['reference_quality'] not in {'native', 'verified_ui'}
                    or any(type(value[k]) is not str or not value[k].strip() or len(value[k]) > 512
                        or any(ord(ch) < 32 for ch in value[k]) for k in ('command_id', 'reference_id'))
                    or type(value['comment_text']) is not str or not value['comment_text'].strip()
                    or len(value['comment_text']) > 4000
                    or any(ord(ch) < 32 and ch not in '\r\n\t' for ch in value['comment_text'])
                    or value['native_comment_id'] is not None and (type(value['native_comment_id']) is not str
                        or not re.fullmatch(r'[1-9][0-9]{4,29}', value['native_comment_id']))
                    or value['reference_quality'] == 'native' and value['native_comment_id'] is None):
                raise NativeLaneError('native_recovery_effects_unknown')
            proof = value['evidence']
            if proof.get('source_kind') not in (None, 'library_choice_reply', 'conversation_followup'):
                raise NativeLaneError('native_recovery_effects_unknown')
            return value
        def overlaps(env):
            env = checked_envelope(env)
            proof = env['evidence']
            return (env['native_comment_id'] == candidate['native_comment_id'] or env['reference_id'] in refs
                or same_scope(env) and (_normal(env['comment_text']) == _normal(candidate['content'])
                or proof.get('root_reference_id') == anchor['root_envelope']['reference_id'])
            )
        def reject():
            raise NativeLaneError('native_recovery_effects_present')
        with provider.store._connection() as db:
            identity('semantic', provider.store)
            semantic_tables = {name: rows(db, name) for name in ('semantic_plans', 'semantic_agent_links',
                'semantic_ui_bindings', 'semantic_budget_parents')}
        substage('semantic_records')
        plans = {row['plan_id']: row for row in semantic_tables['semantic_plans']}
        for row in plans.values():
            if row['source_key'] == snapshot['source_id']:
                reject()
            owner = semantic_owner(json.loads(row['owner_scope']))
            text = semantic_text(row['original_text'])
            semantic_key(row['source_key']); semantic_key(row['budget_scope_id']); semantic_key(row['request_key'])
            semantic_catalog(json.loads(row['catalog']))
            if (row['mode'] not in {'root', 'choice', 'native'}
                    or row['state'] not in {'reserved', 'unknown', 'held', 'budget_blocked', 'complete'}
                    or type(row['ui_digest']) is not str or not re.fullmatch(r'[a-f0-9]{64}', row['ui_digest'])):
                raise NativeLaneError('native_recovery_effects_unknown')
            if row['mode'] == 'root' and owner['root_reference_id'] != owner['reference_id']:
                raise NativeLaneError('native_recovery_effects_unknown')
            if same_scope(owner) and (_normal(text) == _normal(candidate['content'])
                    or owner['reference_id'] in refs):
                reject()
            if row.get('native_facts') is not None:
                facts = validate_native_message(json.loads(row['native_facts']))
                if (row['mode'] != 'native' or native_message_source_key(facts) != row['source_key']
                        or native_message_digest(facts) != row['native_facts_digest']
                        or any(owner[key] != facts[key] for key in scope) or text != facts['content']
                        or owner['reference_id'] != row['source_key'] or owner['root_reference_id'] != row['source_key']):
                    raise NativeLaneError('native_recovery_effects_unknown')
                if facts['native_comment_id'] == candidate['native_comment_id'] or same_scope(facts) and _normal(facts['content']) == _normal(candidate['content']):
                    reject()
            elif row['mode'] == 'native' or row['native_facts_digest'] is not None:
                raise NativeLaneError('native_recovery_effects_unknown')
        substage('semantic_links')
        for name in ('semantic_agent_links', 'semantic_ui_bindings', 'semantic_budget_parents'):
            for row in semantic_tables[name]:
                if row['plan_id'] not in plans or name == 'semantic_budget_parents' and row['parent_plan_id'] not in plans:
                    raise NativeLaneError('native_recovery_effects_unknown')
                if 'budget_scope_id' in row and row['budget_scope_id'] != plans[row['plan_id']]['budget_scope_id']:
                    raise NativeLaneError('native_recovery_effects_unknown')
                if name == 'semantic_budget_parents' and row['budget_scope_id'] != plans[row['parent_plan_id']]['budget_scope_id']:
                    raise NativeLaneError('native_recovery_effects_unknown')
                if name == 'semantic_ui_bindings':
                    owner = semantic_owner(json.loads(row['owner_scope']))
                    if same_scope(owner) and owner['reference_id'] in refs:
                        reject()
        witness['semantic'] = semantic_tables
        with provider.budget._db() as db:
            identity('budget', provider.budget)
            budget_tables = {name: rows(db, name) for name in ('usage_budget_requests', 'usage_budget_events')}
        substage('budget_times')
        def epoch(value):
            if type(value) is not str:
                raise NativeLaneError('native_recovery_effects_unknown')
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise NativeLaneError('native_recovery_effects_unknown')
            value = parsed.timestamp()
            if not math.isfinite(value) or value <= 0 or value > audit_now + 5:
                raise NativeLaneError('native_recovery_effects_unknown')
            return value
        for row in budget_tables['usage_budget_requests']:
            if max(epoch(row['admitted_at_utc']), epoch(row['updated_at_utc'])) >= since:
                reject()
        for row in budget_tables['usage_budget_events']:
            if epoch(row['occurred_at_utc']) >= since:
                reject()
        witness['budget'] = budget_tables
        inbox = provider.native_inbox
        if inbox is not None:
            if type(inbox) is not NativeSemanticInbox:
                raise NativeLaneError('native_recovery_effects_unknown')
            if inbox.receiver_uid != candidate['recipient_uid'] or inbox.receiver_profile_url != candidate['receiver_profile_url']:
                raise NativeLaneError('native_recovery_effects_unknown')
            with inbox._connection() as db:
                identity('native_meaning', inbox)
                native_tables = {name: rows(db, name) for name in ('messages', 'observations', 'audit')}
            substage('native_records')
            for row in native_tables['messages']:
                facts = validate_native_message(json.loads(row['message']))
                if (native_message_source_key(facts) != row['source_key']
                        or native_message_digest(facts) != row['immutable_hash']):
                    raise NativeLaneError('native_recovery_effects_unknown')
                if (row['source_key'] == snapshot['source_id'] or facts['native_comment_id'] == candidate['native_comment_id']
                        or same_scope(facts) and _normal(facts['content']) == _normal(candidate['content'])):
                    reject()
            substage('native_observation_links')
            message_keys = {row['source_key'] for row in native_tables['messages']}
            for row in native_tables['observations']:
                if row['source_key'] not in message_keys:
                    raise NativeLaneError('native_recovery_effects_unknown')
                if row['source_key'] == snapshot['source_id'] or row['notice_id'] == candidate['native_notice_id']:
                    reject()
            witness['native_meaning'] = native_tables
        with _followup_owned_ro(self.queue) as db:
            identity('queue', self.queue)
            tasks = rows(db, 'assistant_tasks')
            queue_events = rows(db, 'assistant_events')
            actions = rows(db, 'assistant_actions')
        substage('queue_attribution')
        allowed = {}
        for row in tasks:
            task = self.queue._raw(row)
            checked_envelope(task['envelope'])
            historical = Binding.from_dict(task['binding'])
            if (task['binding_id'] != historical.binding_id
                    or historical.sender_profile_url != task['envelope']['sender_profile_url']
                    or historical.receiver_profile_url != task['envelope']['receiver_profile_url']):
                raise NativeLaneError('native_recovery_effects_unknown')
            if row['id'] == anchor['root_task_id']:
                if (task['state'] != 'b_view_visible' or task['agent_task_id'] != anchor['root_agent_task_id']
                        or not same_scope(task['envelope'])):
                    raise NativeLaneError('native_recovery_effects_unknown')
                allowed[task['agent_task_id']] = task
            elif _identity(task['envelope']) == _identity(anchor['root_envelope']):
                if task['kind'] != 'clarify' or not task.get('agent_task_id'):
                    raise NativeLaneError('native_recovery_effects_unknown')
                allowed[task['agent_task_id']] = task
            elif overlaps(task['envelope']):
                reject()
        if anchor['root_agent_task_id'] not in allowed:
            raise NativeLaneError('native_recovery_effects_unknown')
        substage('queue_history_links')
        task_ids = {row['id'] for row in tasks}
        if any(row['task_id'] not in task_ids for row in queue_events + actions):
            raise NativeLaneError('native_recovery_effects_unknown')
        witness['queue'] = {'tasks': tasks, 'events': queue_events, 'actions': actions}
        with _followup_owned_ro(bridge.agent) as db:
            identity('agent', bridge.agent)
            agents, replies, steps = rows(db, 'agent_tasks'), rows(db, 'agent_replies'), rows(db, 'agent_steps')
            bindings = rows(db, 'agent_bindings')
            extra = {}
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in ('agent_command_aliases', 'agent_task_ingestions', 'agent_ingestion_jobs'):
                if name in names:
                    extra[name] = rows(db, name)
        substage('agent_attribution')
        seen_allowed = set()
        all_agent_ids = {row['task_id'] for row in agents}
        if any(row['agent_task_id'] not in all_agent_ids for row in semantic_tables['semantic_agent_links']):
            raise NativeLaneError('native_recovery_effects_unknown')
        historical_bindings = {row['binding_id']: Binding.from_dict(json.loads(row['payload'])) for row in bindings}
        agent_envelopes = {}
        agent_scopes = {}
        for row in agents:
            env = checked_envelope(json.loads(row['envelope']))
            bound = historical_bindings.get(row['binding_id'])
            if (bound is None or bound.sender_profile_url != env['sender_profile_url']
                    or bound.receiver_profile_url != env['receiver_profile_url']):
                raise NativeLaneError('native_recovery_effects_unknown')
            agent_envelopes[row['task_id']] = env
            agent_scopes[row['task_id']] = bound
            if row['task_id'] in allowed:
                task = allowed[row['task_id']]
                if row['binding_id'] != task['binding_id'] or _identity(env) != _identity(task['envelope']):
                    raise NativeLaneError('native_recovery_effects_unknown')
                seen_allowed.add(row['task_id'])
            elif overlaps(env):
                reject()
        substage('agent_history_links')
        if seen_allowed != set(allowed) or any(row['task_id'] not in all_agent_ids for row in replies + steps):
            raise NativeLaneError('native_recovery_effects_unknown')
        substage('agent_replies')
        for row in replies:
            target = json.loads(row['target'])
            if type(target) is not dict:
                raise NativeLaneError('native_recovery_effects_unknown')
            linked = agent_envelopes[row['task_id']]
            proof = linked['evidence']
            bound = agent_scopes[row['task_id']]
            frozen_target = {key: linked[key] for key in core}
            frozen_target.update(binding_id=bound.binding_id, instance_id=bound.instance_id,
                knowledge_base_id=bound.knowledge_base_id)
            if bound.is_all_local:
                frozen_target['query_scope'] = 'all_local'
            if proof.get('source_kind') == 'conversation_followup':
                frozen_target['followup_evidence'] = proof
            elif proof.get('source_kind') == 'library_choice_reply':
                frozen_target['choice_evidence'] = proof
            if target != frozen_target:
                raise NativeLaneError('native_recovery_effects_unknown')
            target_proof = (target.get('followup_evidence') if proof.get('source_kind') == 'conversation_followup'
                else target.get('choice_evidence') if proof.get('source_kind') == 'library_choice_reply' else proof)
            attributed = {key: target.get(key) for key in core}
            attributed['evidence'] = target_proof
            checked_envelope(attributed)
            if (any(attributed[key] != linked[key] for key in core)
                    or _identity(attributed) != _identity(linked)
                    or proof.get('source_kind') is None and any(key in target for key in ('choice_evidence', 'followup_evidence'))):
                raise NativeLaneError('native_recovery_effects_unknown')
            if row['task_id'] not in allowed:
                if overlaps(attributed):
                    reject()
                continue
            task = allowed[row['task_id']]
            proposal, evidence = json.loads(row['proposal']), json.loads(row['evidence'] or '{}')
            expected = {k: v for k, v in task['envelope'].items() if k != 'evidence'}
            binding = Binding.from_dict(task['binding'])
            expected.update(binding_id=binding.binding_id, instance_id=binding.instance_id, knowledge_base_id=binding.knowledge_base_id)
            if binding.is_all_local:
                expected['query_scope'] = 'all_local'
            proof = task['envelope']['evidence']
            if proof.get('source_kind') == 'library_choice_reply':
                expected['choice_evidence'] = proof
            elif proof.get('source_kind') == 'conversation_followup':
                expected['followup_evidence'] = proof
            if (row['state'] != 'b_view_visible' or type(row['send_token']) is not str
                    or not re.fullmatch(r'[a-f0-9]{48}', row['send_token']) or target != expected
                    or evidence.get('submit_gate_passed') is not True
                    or evidence.get('exact_body_in_original_thread') is not True
                    or evidence.get('reply_sha256') != hashlib.sha256(proposal['text'].encode()).hexdigest()):
                raise NativeLaneError('native_recovery_effects_unknown')
            if row['task_id'] == anchor['root_agent_task_id']:
                if proposal.get('kind') not in {'answer', 'import_status'} or proposal.get('text') != anchor['prior_reply_text']:
                    raise NativeLaneError('native_recovery_effects_unknown')
            else:
                session = self.choice_controller.store.get_for_root_task(task['id']) if self.choice_controller else None
                if (not session or session.get('state') != 'handed_off'
                        or session.get('child_task_id') != anchor['root_task_id']
                        or proposal.get('kind') != 'clarification' or proposal.get('text') != session.get('ask_text')):
                    raise NativeLaneError('native_recovery_effects_unknown')
        if {row['task_id'] for row in replies if row['task_id'] in allowed} != set(allowed):
            raise NativeLaneError('native_recovery_effects_unknown')
        substage('agent_ingestions')
        links = extra.get('agent_task_ingestions', [])
        for job in extra.get('agent_ingestion_jobs', []):
            if job.get('video_id') == candidate['video_id']:
                linked = [row for row in links if row['job_id'] == job['job_id']]
                if not linked or any(row['task_id'] not in allowed for row in linked):
                    raise NativeLaneError('native_recovery_effects_unknown')
        if any(row['task_id'] not in all_agent_ids for row in extra.get('agent_command_aliases', []) + links):
            raise NativeLaneError('native_recovery_effects_unknown')
        witness['agent'] = {'tasks': agents, 'bindings': bindings, 'replies': replies, 'steps': steps, **extra}
        substage('witness_digest')
        return hashlib.sha256(canonical_json(witness).encode()).hexdigest()

    def review_no_effects_followup(self, source_id):
        """One explicit audited READ recovery; not a historical interruption receipt."""
        from .native_source_store import _followup_no_effects_permit
        from .semantic_plan_store import ui_semantic_digest
        self._owner()
        stage = 'followup_nofx_scope'
        self.last_native_recovery_diagnostic = {'stage': stage, 'code': 'checking'}
        bridge = getattr(self.followup_coordinator, 'bridge', None)
        lock = getattr(bridge, 'admission_lock', None)
        def failure_diagnostic(error, code):
            current_stage = stage
            substage = getattr(self, '_followup_no_effects_audit_stage', None)
            if (stage in {'followup_nofx_audit_before', 'followup_nofx_audit_after'}
                    and type(substage) is str and substage in _NOFX_AUDIT_SUBSTAGES):
                current_stage = stage + '_' + substage
            kind = type(error).__name__
            self.last_native_recovery_diagnostic = {'stage': current_stage, 'code': code,
                'exception_type': kind if kind in _NOFX_AUDIT_EXCEPTION_TYPES else 'local_error'}
        if self.followup_coordinator is None or lock is None:
            raise NativeLaneError('native_recovery_scope_unavailable')
        try:
            with lock:
                self._followup_no_effects_scope()
                self._recovery_identity()
                snapshot = self.source_store.followup_no_effects_review_snapshot(source_id)
                candidate = self._candidate(snapshot['candidate'])
                if (candidate['source_kind'] != 'native_reply_notice_candidate'
                        or not self._visibility_scope_current(candidate)):
                    raise NativeLaneError('native_recovery_scope_unavailable')
                anchors = self.followup_coordinator.anchors(candidate)
                if len(anchors) != 1:
                    raise NativeLaneError('native_recovery_scope_unavailable')
                anchor = anchors[0]
                def checkpoint():
                    self._followup_no_effects_scope()
                    return (self.source_store.followup_no_effects_review_snapshot(source_id) == snapshot
                        and self._visibility_scope_current(candidate)
                        and bridge.followup_anchor_current(anchor['root_task_id'], anchor['prior_reply_sha256']) is True)
                if not checkpoint():
                    raise NativeLaneError('native_recovery_scope_unavailable')
                stage = 'followup_nofx_audit_before'
                before = self._followup_no_effects_audit(candidate, snapshot, anchor)
                stage = 'followup_nofx_fresh_read'
                _, envelope, actual_anchor, failure = self.followup_coordinator.observe(candidate,
                    checkpoint=checkpoint, read=lambda found, observed: self.adapter.read_followup_reply(found, observed, keep_open=True))
                if envelope is None or failure is not None or actual_anchor != anchor:
                    raise NativeLaneError('native_recovery_scope_unavailable')
                self._recovery_identity()
                if not checkpoint():
                    raise NativeLaneError('native_recovery_scope_unavailable')
                stage = 'followup_nofx_audit_after'
                after = self._followup_no_effects_audit(candidate, snapshot, anchor, envelope)
                if before != after or not checkpoint():
                    raise NativeLaneError('native_recovery_effects_unknown')
                review = {'operation': 'owner_fresh_followup_no_effects_review',
                    'reviewed_at': datetime.fromisoformat(envelope['evidence']['observed_at_utc'].replace('Z', '+00:00')).timestamp(),
                    'root_task_id': anchor['root_task_id'], 'root_agent_task_id': anchor['root_agent_task_id'],
                    'ui_reference_id': envelope['reference_id'], 'ui_digest': ui_semantic_digest(envelope),
                    'cross_ledger_digest': after, 'operator_reason': 'explicit_operator_followup_no_effects_recovery'}
                stage = 'followup_nofx_commit'
                result = self.source_store._commit_followup_no_effects_review(
                    _followup_no_effects_permit(self.source_store, snapshot, review))
                self.last_native_recovery_diagnostic = {'stage': stage, 'code': 'reviewed'}
                return result
        except (ReceiverInterventionRequired, NativeLaneError) as error:
            code = getattr(error, 'code', None)
            codes = {'native_recovery_not_safe', 'native_recovery_scope_unavailable',
                'native_recovery_effects_present', 'native_recovery_effects_unknown',
                'native_recovery_stopped', 'native_recovery_identity_unverified'}
            failure_diagnostic(error, code if code in codes else 'native_recovery_identity_unverified')
            raise
        except Exception as error:
            failure_diagnostic(error, 'native_recovery_effects_unknown')
            raise NativeLaneError('native_recovery_effects_unknown') from None


    def clear_buffer(self):
        self._owner()
        # Durable plans are not deleted or unreserved. An enqueue may already
        # have committed before a later callback failed; recover() observes it.
        self._pending.clear()
        self._more = False
        self.browser_lane.clear_buffer()

    @staticmethod
    def _candidate(value):
        if not isinstance(value, dict): raise ValueError('native_candidate_invalid')
        value = deepcopy(value)
        for name in ('sender_uid', 'recipient_uid', 'native_comment_id', 'video_id'):
            if type(value.get(name)) is not str or not _ID.fullmatch(value[name]):
                raise ValueError('native_candidate_invalid')
        if (type(value.get('source_kind')) is not str or value['source_kind'] not in _KINDS
                or value.get('namespace') != 'douyin_web_v1' or value['sender_uid'] == value['recipient_uid']
                or type(value.get('content')) is not str or not value['content'].strip()
                or len(value['content']) > 4000 or type(value.get('sender_sec_uid')) is not str
                or type(value.get('native_source_key')) is not str or not value['native_source_key']
                or len(value['native_source_key']) > 256
                or type(value.get('identity_link_revision')) is not str
                or not _ID.fullmatch(value['identity_link_revision'])):
            raise ValueError('native_candidate_invalid')
        sender = public_profile(value.get('sender_profile_url'))
        receiver = public_profile(value.get('receiver_profile_url'))
        if sender != 'https://www.douyin.com/user/' + value['sender_sec_uid'] or sender == receiver:
            raise ValueError('native_candidate_invalid')
        if value.get('root_comment_id') is not None or value.get('parent_comment_id') is not None:
            raise ValueError('native_candidate_invalid')
        value['sender_profile_url'], value['receiver_profile_url'] = sender, receiver
        return value

    def _check(self, claim, *, reverify_identity=False):
        checked = self.source_store.check_candidate(claim['source_id'], claim['claim_token'])
        if not isinstance(checked, dict):
            return False
        candidate = self._candidate(checked.get('candidate'))
        if canonical_json(candidate) != canonical_json(claim['candidate']): return False
        if checked.get('valid') is not True:
            # A slow but fresh UI read must not invent a newer native identity
            # timestamp. Only a real fixed GET, while this same claim still has
            # a live lease and no other conflict/pause, may renew that evidence.
            allowed = getattr(self.source_store, 'candidate_identity_refresh_allowed', None)
            if (reverify_identity is not True or checked.get('reason') != 'native_identity_unverified'
                    or checked.get('semantic_conflict') is not False or not callable(allowed)
                    or allowed(claim['source_id'], claim['claim_token']) is not True
                    or not self._checkpoint()):
                return False
            try:
                lifecycle = getattr(self, 'session_lifecycle', None)
                if lifecycle is None:
                    verified = self.native_receiver.reverify_identity_checkpoint()
                else:
                    with lifecycle.cycle(reset_failures_on_success=False):
                        verified = self.native_receiver.reverify_identity_checkpoint()
            except Exception as error:
                code = getattr(error, 'code', None)
                self.source_store.pause(code if type(code) is str and re.fullmatch(r'[a-z_]{1,80}', code)
                    else 'native_identity_checkpoint_unavailable')
                lifecycle = getattr(self, 'session_lifecycle', None)
                if ((type(code) is str and code in _LOGIN)
                        or (lifecycle is not None and lifecycle.safe_status().get('state') == 'paused')):
                    raise ReceiverInterventionRequired('本机抖音身份需要核对，原任务和草稿已保留') from None
                return False
            if verified is not True or not self._checkpoint(): return False
            checked = self.source_store.check_candidate(claim['source_id'], claim['claim_token'])
            if not isinstance(checked, dict) or checked.get('valid') is not True: return False
            candidate = self._candidate(checked.get('candidate'))
            if canonical_json(candidate) != canonical_json(claim['candidate']): return False
        return checked.get('semantic_conflict') is False and self.source_store.semantic_conflicts(candidate) is False

    def _hold(self, claim, code):
        self.last_native_issue_code = code
        self.browser_lane.last_detail_failure_count += 1
        self.source_store.hold(claim['source_id'], claim['claim_token'], code)

    def _legacy_guard(self, candidate, *, root_reference=None):
        """Bounded, conservative potential-alias audit before opening UI.

        A native-equal prior task can be observed again, never requeued. An old
        UI-null-ID task or different native ID with the same semantics is not
        silently merged. Library binding/grant changes do not erase this fence.
        """
        count, page = 0, 1
        while True:
            values = self.queue.list_tasks(page=page, page_size=100)
            if type(values.get('total')) is not int or values['total'] > 1000:
                return False
            for item in values['items']:
                count += 1
                if (item.get('video_id') != candidate['video_id']
                        or _normal(item.get('comment_text', '')) != _normal(candidate['content'])):
                    continue
                task = self.queue.get_task(item['id'])
                if not task: return False
                env = task['envelope']
                if (env.get('sender_profile_url') != candidate['sender_profile_url']
                        or env.get('receiver_profile_url') != candidate['receiver_profile_url']):
                    continue
                if root_reference is not None and env.get('evidence', {}).get('root_reference_id') != root_reference:
                    continue
                if env.get('native_comment_id') != candidate['native_comment_id']:
                    return False
            if count >= values['total']: return True
            if not values['items'] or page >= 10: return False
            page += 1

    @staticmethod
    def _item(row):
        if not isinstance(row, dict) or set(row) - _ITEM_KEYS:
            raise ValueError('native_plan_conflict')
        result = deepcopy(row)
        if isinstance(result.get('binding'), Binding): result['binding'] = result['binding'].to_dict()
        result.setdefault('video_title', '')
        result.setdefault('notice_fingerprint', None)
        if set(result) != _ITEM_KEYS: raise ValueError('native_plan_conflict')
        return result

    @staticmethod
    def _task_matches(task, item):
        try:
            return (task['binding'] == item['binding'] and task.get('authorization') == item['authorization']
                and all(task.get(k) == item[k] for k in ('kind', 'user_id', 'device_id'))
                and _identity(task['envelope']) == _identity(item['envelope']))
        except (ValueError, TypeError, KeyError):
            return False

    def _ui_plan(self, row, thread, correlation):
        item = self._item(row)
        return {'task_id': None, 'reference_id': item['envelope']['reference_id'],
            'envelope': deepcopy(item['envelope']), 'thread': deepcopy(thread),
            'queue_item': item, 'native_correlation': deepcopy(correlation),
            'captured_at': item['envelope']['evidence']['observed_at_utc']}

    def _plan(self, claim, row, thread, correlation):
        plan = self._ui_plan(row, thread, correlation)
        self.source_store.record_ui_plan(claim['source_id'], claim['claim_token'], plan)
        self._pending[plan['reference_id']] = {'claim': deepcopy(claim), 'plan': plan}

    def _prepare_mention_ui(self, candidate, *, read_original, source_checkpoint):
        """Shared owner-page READ preparation; no source claim or admission.

        Ordinary sources keep their original check callbacks. Inert hints use
        their own ledger checks and can only persist a typed UI plan afterwards.
        """
        target = {'sender_profile_url': candidate['sender_profile_url'],
            'receiver_profile_url': candidate['receiver_profile_url'], 'video_id': candidate['video_id'],
            'comment_text': candidate['content'], 'native_comment_id': candidate['native_comment_id'],
            'reference_id': 'native-observation:' + candidate['native_source_key'][:180]}
        detail = read_original(target)
        if not self._checkpoint(): return [],None,None,None
        if not isinstance(detail,dict):return [],None,None,'native_ui_unverified'
        correlation = detail.get('native_correlation')
        observed = detail.get('native_comment_id')
        expected = {'source_native_comment_id': candidate['native_comment_id'],
            'observed_native_comment_id': observed,
            'quality': 'native_equal' if observed == candidate['native_comment_id'] else 'semantic_only'}
        if (correlation != expected or observed not in (None, candidate['native_comment_id'])
                or detail.get('sender_profile_url') != candidate['sender_profile_url']
                or detail.get('receiver_profile_url') != candidate['receiver_profile_url']
                or detail.get('video_id') != candidate['video_id']
                or _normal(detail.get('command_text', '')) != _normal(candidate['content'])):
            return [],None,None,'native_ui_unverified'
        if not source_checkpoint() or not self._legacy_guard(candidate):
            return [],None,None,'native_semantic_conflict'
        # Use the final actual UI notification, never synthesize its required
        # fingerprint from a native source key or notice ID.
        notice = detail.get('native_ui_notice')
        if (not isinstance(notice, dict) or set(notice) != {'source_profile_url', 'exact_text', 'semantic_fingerprint'}
                or notice.get('source_profile_url') != candidate['sender_profile_url']
                or type(notice.get('exact_text')) is not str
                or _normal(notice['exact_text']) != _normal(candidate['content'])
                or type(notice.get('semantic_fingerprint')) is not str or not notice['semantic_fingerprint'].strip()):
            return [],None,None,'native_ui_unverified'
        provider = getattr(self.browser_lane,'semantic_resolution_provider',None)
        if callable(provider):
            kwargs = {'semantic_checkpoint': source_checkpoint}
            if getattr(provider, 'native_inbox', None) is not None:
                kwargs['native_candidate'] = deepcopy(candidate)
            rows = self.browser_lane.prepare_native_detail(notice,detail,**kwargs)
        else:
            rows = self.browser_lane.prepare_native_detail(notice, detail)
        if not rows or len(rows)!=1:return [],None,None,'native_scope_changed'
        row = rows[0]
        pending = self.browser_lane.pending.get(row['envelope']['reference_id'])
        if pending is None or not source_checkpoint():
            self.browser_lane.clear_buffer()
            return [],None,None,'native_scope_changed'
        return rows,pending['thread'],correlation,None

    def _mention(self, claim):
        candidate = claim['candidate']
        if not self._legacy_guard(candidate):
            self._hold(claim, 'native_legacy_alias_ambiguous'); return []
        rows,thread,correlation,failure = self._prepare_mention_ui(candidate,
            read_original=lambda target:self._read_with_interruption_receipt(claim,'original_ui_lookup',lambda:
                self.adapter.open_original_from_notifications(target,max_candidates=3,native_candidate_correlation=True)),
            source_checkpoint=lambda:self._check(claim,reverify_identity=True))
        if not rows:
            if failure is not None:self._hold(claim,failure)
            return []
        self._plan(claim, rows[0], thread, correlation)
        return rows

    def _historical_root_locator(self, candidate, session, hint):
        """History may locate an already received UI conversation, never act.

        The fresh reply must still pass collect_pending's actual Root/ask/UI,
        source checkpoint and Registry gates. No historical alias is written.
        """
        env = session['root_envelope']
        binding = session.get('root_binding')
        if (not isinstance(binding, dict) or binding.get('active') is not True
                or binding.get('sender_profile_url') != candidate['sender_profile_url']
                or binding.get('receiver_profile_url') != candidate['receiver_profile_url']
                or env.get('native_comment_id') is not None):
            return False
        lookup = getattr(self.source_store, 'find_source_by_native_comment', None)
        if not callable(lookup):
            return False
        row = lookup(candidate['recipient_uid'], candidate['video_id'], hint)
        if (not isinstance(row, dict) or row.get('historical') is not True
                or row.get('state') != 'historical'
                or row.get('hold_code') not in (None, 'native_timestamp_unknown')
                or any(row.get(key) is not None for key in ('task_id', 'reference_id', 'plan'))):
            return False
        original = row.get('candidate')
        if (not isinstance(original, dict)
                or original.get('source_kind') != 'native_notice_candidate'
                or type(original.get('notice_type')) is not int or original['notice_type'] != 45
                or type(original.get('at_sub_type')) is not int or original['at_sub_type'] != 2
                or original.get('native_comment_id') != hint):
            return False
        identity = ('sender_uid', 'sender_sec_uid', 'sender_profile_url',
                    'recipient_uid', 'receiver_profile_url', 'video_id')
        if any(type(original.get(key)) is not str or original[key] != candidate.get(key)
               for key in identity):
            return False
        body, root_body = original.get('content'), env.get('comment_text')
        return (isinstance(body, str) and isinstance(root_body, str)
                and bool(_normal(body)) and _normal(body) == _normal(root_body)
                and self.source_store.semantic_conflicts(original) is False)

    def _reply_session(self, candidate):
        controller = self.choice_controller
        hint = candidate.get('parent_id_hint')
        if controller is None or type(hint) is not str or not _ID.fullmatch(hint): return None
        sessions = controller.store.list_waiting(candidate['receiver_profile_url'])
        sessions += controller.store.list_asks_to_observe(candidate['receiver_profile_url'])
        # An unspent selection can survive a failed cross-ledger handoff. It
        # may wake only this exact source, never sweep another saved choice.
        pending_handoffs = getattr(controller.store, 'list_pending_handoffs', None)
        if callable(pending_handoffs):
            sessions += [session for session in pending_handoffs()
                if session.get('state') in {'selected', 'handoff_claimed'}
                and session.get('child_task_id') is None
                and self._choice_scope_matches(candidate, session, session.get('selection_envelope'))]
        scoped = [session for session in sessions
            if session['root_envelope'].get('sender_profile_url') == candidate['sender_profile_url']
            and session['root_envelope'].get('receiver_profile_url') == candidate['receiver_profile_url']
            and session['root_envelope'].get('video_id') == candidate['video_id']]
        scoped_choices = {session['choice_id'] for session in scoped}
        matches = {}
        for session in scoped:
            env = session['root_envelope']
            original_row = self.source_store.lookup_source_for_task(session['root_task_id'])
            if original_row is not None:
                if (original_row.get('state') != 'admitted' or original_row.get('historical') is not False
                        or self.source_store.admission_current(original_row['source_id']) is not True):
                    continue
            elif self.source_store.find_sources_by_ui_reference(env['reference_id']):
                continue  # An unlinked native root is not unrelated legacy UI.
            original = original_row.get('candidate') if isinstance(original_row, dict) else None
            if original is not None:
                if (original.get('native_comment_id') != hint or any(original.get(k) != candidate.get(k)
                        for k in ('sender_uid', 'sender_sec_uid', 'recipient_uid', 'video_id'))): continue
            elif env.get('native_comment_id') is None:
                if (len(scoped_choices) != 1
                        or not self._historical_root_locator(candidate, session, hint)):
                    continue
            elif env.get('native_comment_id') != hint:
                continue
            matches[session['choice_id']] = session
        return next(iter(matches.values())) if len(matches) == 1 else None

    @staticmethod
    def _choice_scope_matches(candidate, session, envelope):
        if not isinstance(envelope, dict): return False
        proof = envelope.get('evidence', {})
        if not isinstance(proof, dict): return False
        text = proof.get('selection_value', envelope.get('comment_text', ''))
        return (proof.get('source_kind') == 'library_choice_reply'
            and proof.get('choice_id') == session['choice_id']
            and proof.get('root_reference_id') == session['root_envelope']['reference_id']
            and envelope.get('sender_profile_url') == candidate['sender_profile_url']
            and envelope.get('receiver_profile_url') == candidate['receiver_profile_url']
            and envelope.get('video_id') == candidate['video_id']
            and envelope.get('native_comment_id') in (None, candidate['native_comment_id'])
            and type(text) is str and _normal(text) == _normal(candidate['content']))

    def _choice_source_checkpoint(self, claim, session, envelope):
        # A native source's freshness alone must never reserve a different
        # valid-looking library reply found in the same original thread.
        return (self._checkpoint()
            and self._choice_scope_matches(claim['candidate'], session, envelope)
            and self._check(claim, reverify_identity=True))

    def _reply(self, claim, session=None):
        candidate = claim['candidate']
        session = session or self._reply_session(candidate)
        if session is None:
            if self.followup_coordinator is not None:
                return self._followup(claim)
            self._hold(claim, 'native_reply_parent_unresolved'); return []
        root_ref = session['root_envelope']['reference_id']
        if not self._legacy_guard(candidate, root_reference=root_ref):
            self._hold(claim, 'native_legacy_alias_ambiguous'); return []
        kwargs={'choice_id':session['choice_id'],
            'source_checkpoint':lambda envelope:self._choice_source_checkpoint(claim,session,envelope)}
        semantic=callable(getattr(self.choice_controller,'semantic_choice_planner',None))
        if semantic:
            kwargs['selection_candidate']={key:candidate[key] for key in
                ('sender_profile_url','receiver_profile_url','video_id','native_comment_id')}
            kwargs['selection_candidate'].update(comment_text=candidate['content'],choice_id=session['choice_id'],
                root_reference_id=root_ref)
        rows = self.choice_controller.collect_pending(_InterruptionReadAdapter(self,claim), self.run_id or 'native-owner',**kwargs)
        if not self._checkpoint(): return []
        if not rows:
            status=getattr(self.choice_controller,'last_semantic_choice_status',None)
            if (semantic and candidate['source_kind']=='native_notice_candidate' and isinstance(status,dict)
                    and status.get('choice_id')==session['choice_id'] and status.get('fallback_allowed') is True
                    and all(status.get(key) is False for key in ('parent_proved','model_started','selection_reserved'))
                    and self._check(claim,reverify_identity=True)):
                # No model or Choice reservation happened. The ordinary real-@
                # opener/builder still has to prove the new independent Root.
                return self._mention(claim)
            self._hold(claim, 'native_reply_not_matched'); return []
        if len(rows) != 1: raise ValueError('native_plan_conflict')
        row = rows[0]; env = row['envelope']; proof = env.get('evidence', {})
        observed = env.get('native_comment_id')
        if not self._choice_scope_matches(candidate, session, env) or not self._check(claim, reverify_identity=True):
            self.browser_lane.clear_buffer()
            self._hold(claim, 'native_reply_not_matched'); return []
        correlation = {'source_native_comment_id': candidate['native_comment_id'],
            'observed_native_comment_id': observed,
            'quality': 'native_equal' if observed == candidate['native_comment_id'] else 'semantic_only'}
        self._plan(claim, row, {'quality': 'verified_ancestor', 'root_reference_id': root_ref}, correlation)
        return rows

    def _followup(self, claim):
        candidate = claim['candidate']
        if not self._legacy_guard(candidate):
            self._hold(claim, 'native_legacy_alias_ambiguous')
            return []
        rows, thread, correlation, failure = self.followup_coordinator.prepare(claim,
            checkpoint=lambda: self._check(claim, reverify_identity=True),
            read=lambda anchor, observed: self._read_with_interruption_receipt(claim,
                'original_ui_lookup', lambda: self.adapter.read_followup_reply(anchor,
                    observed, keep_open=True)))
        if not rows:
            if failure is not None:
                self._hold(claim, failure)
            else:
                self._native_read_diagnostic('followup_prepare', 'native_followup_no_resolution')
            return []
        self._plan(claim, rows[0], thread, correlation)
        return rows

    def ready_persisted_ui_source(self):
        """A lossy scheduling hint only; no claim, API, page or authority grant."""
        self._owner()
        try:
            if (self.should_stop() or self._pending
                    or not callable(self.semantic_ui_source_ready)):
                return False
            control = self.queue.get_control()
            if (not self.run_id or control.get('run_id') != self.run_id
                    or control.get('state') != 'running' or control.get('desired_state') != 'running'):
                return False
            candidate = self.source_store.peek_ui_candidate()
            if not isinstance(candidate, dict) or candidate.get('source_kind') != 'native_notice_candidate':
                return False
            candidate = self._candidate(candidate)
            return (self._visibility_scope_current(candidate) is True
                    and self.semantic_ui_source_ready(deepcopy(candidate)) is True
                    and not self.should_stop())
        except Exception:
            # Races/changed owned stores fall back to the ordinary poll gate.
            return False

    def collect(self):
        self._owner()
        self.last_native_read_diagnostic = {}
        if self._pending: raise NativeLaneError('native_prior_admission_required')
        self._more = False
        self.browser_lane.last_detail_failure_count = 0
        if callable(self.semantic_intake_ready) and self.semantic_intake_ready() is not True:
            return []
        if (callable(getattr(self.browser_lane,'semantic_resolution_provider',None))
                or callable(getattr(self.choice_controller,'semantic_choice_planner',None))):
            # Native polling may still record free inert facts while paused,
            # but do not spend a once-only claim before a paid planner refuses.
            if self.queue.get_control().get('desired_state')!='running':return []
        if not self._checkpoint(): return []
        self.recover()
        try:
            claim = self.source_store.claim_ui_candidate(self.run_id or 'native-owner', lease_seconds=300)
            if claim is None: return self._collect_visibility_hint()
            self._more = True
            claim = deepcopy(claim)
            claim['candidate'] = self._candidate(claim['candidate'])
            if not self._check(claim):
                self._hold(claim, 'native_identity_unverified'); return []
            candidate = claim['candidate']
            user = self.registry.find_user(candidate['sender_profile_url'], candidate['receiver_profile_url'])
            if (candidate['receiver_profile_url'] != self.adapter.expected_profile_url or not user
                    or not user['active'] or user['verification_status'] != 'verified'
                    or self.registry.intake_binding(candidate['sender_profile_url'], candidate['receiver_profile_url']) is None):
                self._hold(claim, 'native_identity_unverified'); return []
            if candidate['source_kind'] == 'native_reply_notice_candidate':
                return self._reply(claim)
            session = self._reply_session(candidate)
            if session is not None:
                # Completed private meaning treats this actual AT as a new
                # instruction. This only selects a lane, not execution rights;
                # fresh UI/source/Registry/tool/send gates remain unchanged.
                if (callable(self.semantic_candidate_action)
                        and self.semantic_candidate_action(deepcopy(candidate))
                            in ('query', 'save', 'clarify', 'cancel')):
                    return self._mention(claim)
                if callable(getattr(self.choice_controller,'semantic_choice_planner',None)):
                    return self._reply(claim,session)
                if parse_library_selection(candidate['content'], session['options']) is not None:
                    return self._reply(claim, session)
                # A native AT event does not prove which displayed leading
                # label is B. Strip no label for positive selection. A likely
                # selection after such an unverified prefix is held rather
                # than falling through as a second independent save command.
                preview = re.sub(r'^\s*@[^\s@，,。:：]{1,80}\s*', '', candidate['content'], count=1)
                if preview != candidate['content'] and parse_library_selection(preview, session['options']) is not None:
                    self._hold(claim, 'native_mixed_role_unresolved'); return []
            return self._mention(claim)
        except ReceiverInterventionRequired:
            raise
        except ReceiverDetailError:
            if not self._checkpoint(): return []
            if 'claim' in locals(): self._hold(claim, 'native_ui_unverified')
            return []
        except (ValueError, KeyError, TypeError):
            if not self._checkpoint(): return []
            if 'claim' in locals() and isinstance(claim, dict) and {'source_id', 'claim_token'} <= claim.keys():
                self._hold(claim, 'native_candidate_invalid')
                self.browser_lane.clear_buffer()
                return []
            raise NativeLaneError('native_intake_unavailable') from None

    def admitted(self, tasks):
        self._owner()
        for task in tasks:
            pending = self._pending.get(task['envelope']['reference_id'])
            if pending is None or not self._task_matches(task, pending['plan']['queue_item']):
                raise NativeLaneError('native_admission_scope_conflict')
        self.browser_lane.admitted(tasks)
        for task in tasks:
            reference = task['envelope']['reference_id']
            pending = self._pending[reference]
            claim = pending['claim']
            self.source_store.finish_ui_admission(claim['source_id'], claim['claim_token'], task['id'], reference)
            self._pending.pop(reference, None)

    def recover(self):
        """Only observe existing immutable Queue rows; never re-enqueue a plan."""
        self._owner()
        recovered = 0
        plans = self.source_store.recoverable_ui_plans()
        if not isinstance(plans, list) or len(plans) > 100:
            raise NativeLaneError('native_recovery_unavailable')
        for claim in plans[:3]:
            if self.should_stop(): break
            plan = claim['plan']; item = plan['queue_item']; env = item['envelope']
            if plan['envelope'] != env or plan['reference_id'] != env['reference_id']:
                self._hold(claim, 'native_plan_conflict'); continue
            task = self.queue.lookup_by_source_reference(env['sender_profile_url'], env['receiver_profile_url'],
                env['source_mode'], env['reference_id'])
            if task is None:
                self._hold(claim, 'native_plan_missing_task'); continue
            if not self._task_matches(task, item):
                self._hold(claim, 'native_plan_conflict'); continue
            proof = plan['thread']
            if self.conversation.task_scope(task['id']) is None:
                self.conversation.bind_task(task['id'], Binding.from_dict(task['binding']), task['envelope'],
                    proof['root_reference_id'], proof)
            if env.get('evidence', {}).get('source_kind') == 'library_choice_reply' and self.choice_controller is not None:
                session = self.choice_controller.store.get(env['evidence']['choice_id'])
                if (session is None or session.get('state') not in {'selected', 'handoff_claimed', 'handed_off'}
                        or _identity(session.get('selection_envelope', {})) != _identity(env)):
                    self._hold(claim, 'native_plan_conflict'); continue
                self.choice_controller.collect_pending(self.adapter, self.run_id or 'native-owner',
                    choice_id=env['evidence']['choice_id'])
                session = self.choice_controller.store.get(env['evidence']['choice_id'])
                if session.get('state') != 'handed_off' or session.get('child_task_id') != task['id']:
                    # An earlier live ChoiceStore lease may still own this
                    # already-created child. Keep planned/non-executable until
                    # its normal durable handoff can be recovered, never steal.
                    continue
            elif env.get('evidence', {}).get('source_kind') == 'library_choice_reply':
                self._hold(claim, 'native_plan_conflict'); continue
            self.source_store.finish_ui_admission(claim['source_id'], claim['claim_token'], task['id'], env['reference_id'])
            recovered += 1
        return recovered

    def authority_current(self, task):
        """Read-only, thread-safe fresh provenance check for every effect gate.

        Registry authorization is still checked by LegacyBridge separately.
        A planned-but-unlinked source is NOT legacy and cannot start billing.
        Only a truly unrelated old UI task has no native provenance to revoke.
        """
        return self._authority_current(task, allow_choice_root=True)

    def _hint_authority_current(self, source):
        """A hint-derived admission retains its separate, current RO receipt.

        Ordinary native sources never depend on this optional ledger. A later
        hint payload conflict cannot be hidden by the already-admitted source.
        """
        candidate = source.get('candidate')
        if not isinstance(candidate, dict): return False
        if 'hint_provenance' not in candidate: return True
        from .native_visibility_hint_store import (hint_id_for, hint_source_id,
                                                   validate_review_hint)
        provenance = candidate['hint_provenance']
        if (not isinstance(provenance, dict) or set(provenance) != {'hint_id',
                'original_source_kind', 'native_visibility', 'hint_immutable_sha256'}
                or provenance['original_source_kind'] != 'native_visibility_review_hint'
                or provenance['native_visibility'] != 'unknown'
                or self.visibility_hint_store is None):
            return False
        row = self.visibility_hint_store.get_hint(provenance['hint_id'])
        if (not isinstance(row, dict) or row.get('state') != 'admitted'
                or row.get('hold_code') is not None
                or row.get('hint_id') != provenance['hint_id']
                or row.get('source_id') != source['source_id']
                or row.get('admitted_source_id') != source['source_id']):
            return False
        hint = validate_review_hint(row.get('hint'))
        if (hint_id_for(hint) != provenance['hint_id']
                or hint_source_id(hint) != source['source_id']
                or hashlib.sha256(canonical_json(hint).encode('utf-8')).hexdigest()
                    != provenance['hint_immutable_sha256']
                or candidate.get('source_kind') != 'native_notice_candidate'
                or any(candidate.get(key) != hint[key] for key in ('namespace',
                    'recipient_uid', 'sender_uid', 'sender_sec_uid', 'sender_profile_url',
                    'receiver_profile_url', 'video_id', 'native_comment_id', 'content'))):
            return False
        return True

    def _authority_current(self, task, *, allow_choice_root):
        try:
            if not isinstance(task, dict) or not isinstance(task.get('id'), str): return False
            env = task.get('envelope')
            if not isinstance(env, dict) or not isinstance(env.get('reference_id'), str): return False
            proof = env.get('evidence', {})
            if (proof.get('source_kind') == 'conversation_followup'
                    and (self.followup_coordinator is None
                         or not self.followup_coordinator.authority_current(task))):
                return False
            if proof.get('source_kind') == 'library_choice_reply':
                if not allow_choice_root or self.choice_controller is None: return False
                choice = self.choice_controller.store.get(proof.get('choice_id'))
                if (not isinstance(choice, dict) or choice.get('state') not in {'selected', 'handoff_claimed', 'handed_off'}
                        or choice.get('selection_ref') != env['reference_id']
                        or choice.get('child_task_id') not in (None, task['id'])):
                    return False
                root = self.queue.get_task(choice.get('root_task_id'))
                if (not root or root['id'] == task['id']
                        or root.get('envelope', {}).get('evidence', {}).get('source_kind') is not None
                        or root['envelope'].get('reference_id') != proof.get('root_reference_id')
                        or choice.get('root_envelope', {}).get('reference_id') != proof.get('root_reference_id')
                        or any(root['envelope'].get(k) != env.get(k)
                               for k in ('sender_profile_url', 'receiver_profile_url', 'video_id'))
                        or any(root.get(k) != task.get(k) for k in ('user_id', 'device_id'))
                        or root.get('binding', {}).get('instance_id') != task.get('binding', {}).get('instance_id')
                        or not self._authority_current(root, allow_choice_root=False)):
                    return False
            rows = self.source_store.find_sources_by_ui_reference(env['reference_id'])
            if not isinstance(rows, list) or len(rows) > 100: return False
            linked = self.source_store.lookup_source_for_task(task['id'])
            if linked is not None and all(row.get('source_id') != linked.get('source_id') for row in rows):
                rows.append(linked)
            if not rows:
                return env.get('source_mode') == 'live_ui'
            if len(rows) != 1: return False
            source = rows[0]
            if (source.get('state') != 'admitted' or source.get('historical') is not False
                    or source.get('task_id') != task['id'] or source.get('reference_id') != env['reference_id']):
                return False
            plan = source.get('plan')
            if (not isinstance(plan, dict) or plan.get('reference_id') != env['reference_id']
                    or plan.get('envelope') != plan.get('queue_item', {}).get('envelope')
                    or not self._task_matches(task, plan['queue_item'])):
                return False
            # Admission already froze actual UI proof and the original user
            # intent. Transport polling liveness is still required by intake,
            # but does not revoke a captured task during a long model request.
            return (self.source_store.admitted_source_current(source['source_id']) is True
                    and self._hint_authority_current(source))
        except Exception:
            return False  # Storage uncertainty is never a native admission.
