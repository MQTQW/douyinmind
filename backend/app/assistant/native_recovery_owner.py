"""Explicit local operator reviews, performed by the registered page owner.

This class cannot execute a model, import or send. It only invokes the existing
strict NativeLane review methods and leaves normal task handling paused.
"""
import threading
import re
from datetime import datetime

from app.mention_receiver.browser import ReceiverInterventionRequired


class NativeRecoveryOwner:
    def __init__(self,lane,control,queue,run_id,admission_lock,hold,owner_review,worker_inflight,manual_pending,*,reply_inspector=None,draft_finalizer=None):
        self.lane,self.control,self.queue,self.run_id=lane,control,queue,run_id
        self.lock,self.hold,self.review=admission_lock,hold,owner_review
        self.worker_inflight,self.manual_pending=worker_inflight,manual_pending
        self.thread=threading.get_ident()
        if reply_inspector is not None and not callable(reply_inspector):raise ValueError('native_recovery_not_safe')
        self.reply_inspector=reply_inspector
        if draft_finalizer is not None and not callable(draft_finalizer):raise ValueError('native_recovery_not_safe')
        self.draft_finalizer=draft_finalizer

    def __call__(self,operation,source_id):
        if threading.get_ident()!=self.thread:raise ValueError('native_recovery_not_safe')
        with self.lock:
            current=self.control.snapshot();counts=self.queue.counts()
            if (current.get('run_id')!=self.run_id or current.get('state')!='paused'
                    or current.get('desired_state')!='paused' or not current.get('process_alive')
                    or current.get('worker_state')!='idle' or self.worker_inflight.is_set()
                    or self.manual_pending() or self.hold.is_set()
                    or counts.get('executing',0) or counts.get('publishing',0)):
                return {'ok':False,'code':'native_recovery_not_safe'}
            if operation in {'review_unexecuted_followup','finalize_query_draft'}:
                manual=current.get('manual_verification')
                active={'queued','waiting_desktop','executing','waiting_ingestion',
                        'ready_to_publish','publishing','waiting_external','waiting_library','needs_reconcile'}
                if (type(manual) is not dict or manual.get('active') is not False
                        or any(counts.get(key,0) for key in active)):
                    return {'ok':False,'code':'native_recovery_not_safe'}
            self.hold.set();self.review.set()
            try:
                if operation=='review_legacy_prequeue':
                    result=self.lane.review_and_resume_legacy_prequeue(source_id)
                elif operation=='resume_interrupted_prequeue':
                    source=self.lane.source_store.prequeue_recovery_status(source_id)
                    receipt=source.get('receipt') if isinstance(source,dict) else None
                    if not isinstance(receipt,dict):
                        return {'ok':False,'code':'native_recovery_scope_unavailable'}
                    result=self.lane.request_interrupted_prequeue_resume(source_id,
                        receipt_id=receipt['receipt_id'],expected_attempts=source['attempts'])
                elif operation=='request_visibility_ui_review':
                    result=self.lane.request_visibility_review(source_id)
                elif operation=='inspect_visibility_hint':
                    result=self.lane.inspect_visibility_hint(source_id)
                elif operation=='recover_visibility_hint':
                    result=self.lane.recover_visibility_hint(source_id)
                elif operation=='request_followup_ui_review':
                    result=self.lane.request_followup_review(source_id)
                elif operation=='review_unexecuted_followup':
                    result=self.lane.review_no_effects_followup(source_id)
                elif operation=='inspect_thread_followup':
                    result=self.lane.inspect_followup_source(source_id)
                elif operation=='finalize_query_draft':
                    matched = re.fullmatch(r'assistant-draft:([a-f0-9]{32}):([a-f0-9]{64})',source_id) if type(source_id) is str else None
                    if matched is None or self.draft_finalizer is None:
                        return {'ok':False,'code':'native_recovery_scope_unavailable'}
                    self.lane._recovery_identity()
                    result=self.draft_finalizer(matched[1],expected_candidate_sha256=matched[2],
                        reverify_identity=self.lane._recovery_identity)
                elif operation=='inspect_reply':
                    if (type(source_id) is not str or not re.fullmatch(r'assistant-task:[a-f0-9]{32}',source_id)
                            or self.reply_inspector is None):
                        return {'ok':False,'code':'native_recovery_scope_unavailable'}
                    inspector=getattr(self.reply_inspector,'__self__',None)
                    if inspector is not None:
                        # A failed identity check must not attach the prior
                        # task's positive B-view observation to this request.
                        inspector.last_reply_inspection_diagnostic={'stage':'reply_inspect_identity',
                            'code':'identity_check_pending','scope':'b_view_only',
                            'b_view_visible_now':False,'a_visibility_verified':False}
                    self.lane._recovery_identity()
                    result=self.reply_inspector(source_id.split(':',1)[1])
                else:
                    return {'ok':False,'code':'native_recovery_scope_unavailable'}
                if (not isinstance(result,dict) or result.get('dispatchable') is not False
                        or result.get('source_id')!=source_id
                        or result.get('status') not in {'legacy_prequeue_review_resumed','prequeue_resume_requested',
                                                     'visibility_ui_review_requested','visibility_hint_inspected',
                                                     'visibility_hint_recovery_requested','assistant_reply_inspected',
                                                     'followup_ui_review_requested','followup_source_inspected',
                                                     'followup_no_effects_review_queued','operator_query_draft_finalized'}):
                    return {'ok':False,'code':'native_recovery_effects_unknown'}
                return {'ok':True,'reviewed':True,'queued_for_fresh_ui':operation not in {'inspect_visibility_hint','inspect_reply','inspect_thread_followup','finalize_query_draft'},
                        'historical_preserved':True,'dispatchable':False}
            except ReceiverInterventionRequired:
                self.control.set_receiver_health(self.run_id,'verification_required',reason_code='human_intervention_required')
                return {'ok':False,'code':'native_recovery_identity_unverified'}
            except Exception as error:
                fixed={'native_recovery_not_safe','native_recovery_scope_unavailable','native_recovery_effects_unknown',
                       'native_recovery_stopped','native_recovery_identity_unverified','native_legacy_recovery_incomplete',
                       'native_recovery_effects_present'}
                code=getattr(error,'code',None)
                if code not in fixed:code=str(error) if type(error).__module__.startswith('app.assistant.') else None
                return {'ok':False,'code':code if code in fixed else 'native_recovery_not_safe'}
            finally:
                diagnostic=(getattr(getattr(self.reply_inspector,'__self__',None),'last_reply_inspection_diagnostic',None)
                    if operation=='inspect_reply' else
                    getattr(getattr(self.draft_finalizer,'__self__',None),'last_operator_draft_diagnostic',None)
                    if operation=='finalize_query_draft' else getattr(self.lane,'last_native_recovery_diagnostic',None))
                if isinstance(diagnostic,dict):
                    safe={k:v for k,v in diagnostic.items() if k in {'stage','code','exception_type'}
                          and type(v) is str and len(v)<100}
                    counts=diagnostic.get('lookup_counts')
                    if type(counts) is dict:
                        keys={'candidate_limit','candidate_count','checked_count','match_count','unreadable_count',
                              'final_rechecks','final_verified','snapshot_changes','page_open_count',
                              'lookup_stage_index','history_scrolls'}
                        safe['lookup_counts']={k:v for k,v in counts.items()
                            if k in keys and type(v) is int and 0<=v<=1000000}
                    followup_counts=diagnostic.get('followup_counts')
                    if operation=='inspect_thread_followup' and type(followup_counts) is dict:
                        keys={'original_root_count','ask_count','selection_count','choice_sender_row_count',
                            'choice_receiver_target_row_count','body_candidate_count','readback_more_count',
                            'readback_unsupported_more_count','readback_loaded_row_count','root_count',
                            'prior_reply_count','followup_candidate_count','sender_row_count','reply_to_B_count',
                            'more_count','unsupported_more_count','loaded_row_count','detail_ok',
                            'sender_match_count','command_match_count','visible_comment_count','video_id_count'}
                        safe['followup_counts']={k:v for k,v in followup_counts.items()
                            if k in keys and type(v) is int and 0<=v<=1000000}
                    anchor_counts=diagnostic.get('anchor_counts')
                    if operation=='inspect_thread_followup' and type(anchor_counts) is dict:
                        keys={'user_count','device_count','root_task_count','origin_count'}
                        safe['anchor_counts']={k:v for k,v in anchor_counts.items()
                            if k in keys and type(v) is int and 0<=v<=1000000}
                    reasons={'checking','matched','invalid_original_target','native_correlation_requires_source_id',
                        'receiver_binding_mismatch','original_notifications_unavailable','original_notification_not_unique',
                        'original_notification_candidates_exceed_limit','original_notification_changed',
                        'original_notification_not_found','original_candidate_unreadable','original_target_not_unique',
                        'original_native_semantic_ambiguous','original_target_changed','original_lookup_stopped',
                        'original_identity_not_ready','original_lookup_unavailable'}
                    if type(diagnostic.get('lookup_reason_code')) is str and diagnostic['lookup_reason_code'] in reasons:
                        safe['lookup_reason_code']=diagnostic['lookup_reason_code']
                    if operation=='inspect_reply':
                        for key in ('b_view_visible_now','a_visibility_verified'):
                            if type(diagnostic.get(key)) is bool and (key!='a_visibility_verified' or diagnostic[key] is False):
                                safe[key]=diagnostic[key]
                        if diagnostic.get('scope')=='b_view_only':safe['scope']='b_view_only'
                        if type(diagnostic.get('reply_sha256')) is str and re.fullmatch(r'[a-f0-9]{64}',diagnostic['reply_sha256']):
                            safe['reply_sha256']=diagnostic['reply_sha256']
                        if type(diagnostic.get('reply_counts')) is dict:
                            safe['reply_counts']={k:v for k,v in diagnostic['reply_counts'].items()
                                if k in {'matching_reply_count','existing_matching_reply_count'} and type(v) is int and 0<=v<=1000000}
                        stamp=diagnostic.get('observed_at_utc')
                        if type(stamp) is str and len(stamp)<=40:
                            try:
                                parsed=datetime.fromisoformat(stamp.replace('Z','+00:00'))
                                if parsed.tzinfo is not None and parsed.utcoffset().total_seconds()==0:safe['observed_at_utc']=stamp
                            except ValueError:pass
                    def record(current):
                        if current.get('run_id')==self.run_id:
                            current['native_recovery']={'source_id':source_id,'diagnostic':safe}
                        return current
                    self.control.queue.update_control(record)
                status=getattr(self.lane,'_public_native_session_status',lambda:None)()
                self.review.clear()
                self.control.pause()
                if status is not None:self.control.heartbeat(self.run_id,native_session_status=status)
                else:self.control.heartbeat(self.run_id)
                self.hold.clear()
