"""Future-only, owner-verified intent clarification -> ordinary save/Choice.

There is no generic clarification whitelist and no import/send capability here.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time

from app.mention_agent.contracts import Binding, canonical_json
from app.mention_agent.clarification_evidence import (SOURCE_KIND, make_clarification_envelope,
    validate_clarification_envelope)
from app.mention_agent.store import _verified_target
from app.mention_receiver.browser import ReceiverInterventionRequired
from .clarification_sessions import ClarificationError, digest, immutable_envelope
from .semantic_resolution import owner_scope_from_envelope, catalog_snapshot_from_grants, semantic_source_key
from .semantic_plan_store import ui_semantic_digest, verify_semantic_decision, RecordedSemanticDecision
from .semantic_intent_model import ClarificationIntentModel


class ClarificationContinuation:
    def __init__(self, store, bridge, lane, interpreter, *, clock=None):
        self.store, self.bridge, self.lane, self.interpreter = store, bridge, lane, interpreter
        self.clock = clock or time.time
        self._recovery_cursor = None
        self.last_recovery_diagnostic = None

    def _admit_existing(self,session_id,source_id,task):
        try:return self.store.admitted(session_id,source_id,task)
        except ClarificationError as error:
            if error.code=='clarification_feature_disabled':return None
            raise

    def _source(self, source_id=None, task_id=None):
        with self.lane.source_store._connection() as db:
            if source_id is not None:
                rows = db.execute('SELECT * FROM sources WHERE source_id=?', (source_id,)).fetchall()
            else:
                rows = db.execute('SELECT * FROM sources WHERE task_id=?', (task_id,)).fetchall()
        if len(rows) != 1: return None
        value = dict(rows[0]); value['candidate'] = json.loads(value['candidate'])
        value['historical'] = bool(value['historical'])
        return value

    def admitted(self, tasks):
        if self.store.enabled is not True:return
        for task in tasks:
            proof = task['envelope'].get('evidence', {})
            if proof.get('source_kind') == SOURCE_KIND:
                session = self.store.get(proof['clarification_session_id'])
                self._admit_existing(session['session_id'], session['response_source_id'], task)
                continue
            if proof.get('source_kind') is not None or task.get('kind') != 'clarify': continue
            source = self._source(task_id=task['id'])
            if source is None: continue
            resolution = self.bridge._resolution(task)
            if resolution is None or resolution.reason_code != 'semantic_intent_unclear': continue
            receipt=self.bridge._semantic_receipt(task)
            if type(receipt) is not RecordedSemanticDecision:continue
            try:
                self.store.arm(task, source, reason_code=resolution.reason_code,semantic_plan_id=receipt.plan_id)
            except ClarificationError as error:
                if error.code not in {'clarification_root_not_new','clarification_feature_disabled'}: raise

    def recover_links(self, tasks):
        """Repair only an already planned new-type handoff; never arm old roots."""
        result = self._recovery_result()
        if not self._recovery_active() or self.store.enabled is not True:return result
        for task in tasks:
            if not self._recovery_active():break
            session = None
            try:
                proof=task.get('envelope',{}).get('evidence',{})
                if proof.get('source_kind')!=SOURCE_KIND:continue
                session=self.store.get(proof['clarification_session_id'])
                if session is not None:
                    self._reconcile_planned(session, result, expected_task=task)
            except ReceiverInterventionRequired:
                raise
            except Exception:
                self._recovery_error(session, result)
        return result

    def after_publish(self, task):
        session = self.store.for_root(task['id'])
        if session is None: return
        current = self.bridge.queue.get_task(task['id'])
        agent_id = current.get('agent_task_id') if current else task.get('agent_task_id')
        if not agent_id:
            old = self.bridge._find(task)
            agent_id = old['task_id'] if old else None
        reply = self.bridge.agent.get_reply(agent_id) if agent_id else None
        if (reply is not None and reply.get('state') == 'b_view_visible'
                and _verified_target(reply['target'], reply.get('verification'), task['binding'])):
            self.store.published(session['session_id'], reply)

    def _root_current(self, session):
        if session is None or self.store.enabled is not True or self.bridge.should_stop(): return False
        root = session['root']
        if (root['activated_at'] != self.store.activated_at or root['capability'] != 'intent_clarification_v1'
                or session['state']!='handed_off' and self.clock() - session['created_at'] > 86400): return False
        task = self.bridge.queue.get_task(root['id'])
        if (task is None or task['state'] != 'b_view_visible'
                or any(task.get(key) != root[key] for key in ('binding', 'envelope', 'authorization', 'user_id', 'device_id'))): return False
        # The old paid interpretation stays frozen. Only its existing receipt
        # and current authorization are checked; today's catalog cannot replan
        # an already published generic clarification.
        if not self.bridge._authorized(task):return False
        receipt=self.bridge._semantic_receipt(task)
        if type(receipt) is not RecordedSemanticDecision or receipt.plan_id!=root['semantic_plan_id']:
            return False
        try:
            old=verify_semantic_decision(receipt,mode='root',original_text=task['envelope']['comment_text'],
                frozen_ui_digest=ui_semantic_digest(task['envelope']),owner_scope=self.bridge._semantic_owner_scope(task))
            if old['action']!='clarify' or old['target_state'] not in {'unspecified','one'}:return False
        except Exception:return False
        source = self._source(root['source_id'])
        if (source is None or source['state'] != 'admitted' or source['task_id'] != root['id']
                or source['historical'] or source['first_seen'] < self.store.activated_at): return False
        reply = self.bridge.agent.get_reply(task.get('agent_task_id'))
        agent_task = self.bridge.agent.get_task(task.get('agent_task_id'))
        published = session['published_reply']
        return bool(published and reply and agent_task and agent_task.get('state')=='b_view_visible'
            and task.get('agent_task_id')==published['agent_task_id']
            and reply.get('state') == 'b_view_visible'
            and reply.get('kind') == 'clarification' and reply.get('send_token')
            and hashlib.sha256(reply['send_token'].encode()).hexdigest()==published['send_fingerprint']
            and reply['reply_id'] == published['reply_id'] and reply['text'] == published['text']
            and hashlib.sha256(reply['text'].encode()).hexdigest() == published['text_sha256']
            and reply['target'] == published['target'] and reply['evidence'] == published['evidence']
            and _verified_target(reply['target'], reply.get('verification'), task['binding']))

    def _anchor(self, session):
        task = self.bridge.queue.get_task(session['root']['id'])
        return {'root_task_id': task['id'], 'root_agent_task_id': task['agent_task_id'],
            'root_envelope': deepcopy(session['root']['envelope']),
            'prior_reply_text': session['published_reply']['text'],
            'prior_reply_sha256': session['published_reply']['text_sha256'],
            'instance_id': task['binding']['instance_id']}

    def prepare(self, claim, *, checkpoint, read):
        """Return None when unrelated; handled failures never fall through."""
        if self.store.enabled is not True:return None
        candidate = claim['candidate']
        sessions = self.store.matching(candidate)
        if not sessions: return None
        if len(sessions) != 1: return [], None, None, 'clarification_parent_ambiguous'
        session = sessions[0]
        source = self._source(claim['source_id'])
        if (not checkpoint() or source is None or source['historical']
                or source['first_seen'] < self.store.activated_at or source['attempts'] != 1
                or source['state'] != 'claimed' or source['plan'] is not None
                or source['task_id'] is not None or source['reference_id'] is not None
                or any(source['candidate'].get(key)!=candidate.get(key) for key in
                    ('sender_profile_url','receiver_profile_url','video_id','native_comment_id','content','source_kind'))
                or session['state'] != 'awaiting_reply' or not self._root_current(session)):
            return [], None, None, 'clarification_source_not_new_or_current'
        detail = read(self._anchor(session), deepcopy(candidate))
        carrier = self.bridge.registry.intake_binding(candidate['sender_profile_url'], candidate['receiver_profile_url'])
        if carrier is None: return [], None, None, 'clarification_scope_changed'
        envelope = make_clarification_envelope(detail, carrier, session)
        stamp = datetime.fromisoformat(envelope['evidence']['observed_at_utc'].replace('Z', '+00:00')).timestamp()
        if (not -5 <= self.clock() - stamp <= 120
                or any(envelope.get(key)!=candidate.get(key) for key in ('sender_profile_url','receiver_profile_url','video_id'))
                or ''.join(envelope['comment_text'].split()) != ''.join(candidate['content'].split())
                or envelope['native_comment_id'] not in (None, candidate['native_comment_id'])
                or not checkpoint() or not self._root_current(session)):
            return [], None, None, 'clarification_ui_unverified'
        if self.store.enabled is not True:return [],None,None,'clarification_feature_disabled'
        try:session = self.store.claim_response(session['session_id'], claim['source_id'], envelope)
        except ClarificationError as error:
            if error.code=='clarification_feature_disabled':return [],None,None,'clarification_feature_disabled'
            raise
        def current():
            value = self.store.get(session['session_id'])
            return bool(checkpoint() and value and value['state'] == 'responding'
                and value['response_source_id'] == claim['source_id']
                and value['response_hash'] == digest(immutable_envelope(envelope))
                and self._root_current(value))
        context = self.interpreter._context(envelope)
        if context is None or not current(): return [], None, None, 'clarification_scope_changed'
        user, device, grants = context
        owner = owner_scope_from_envelope(envelope, user, device)
        catalog = catalog_snapshot_from_grants(grants)
        def authorized():
            live = self.interpreter._context(envelope)
            return bool(current() and live and owner_scope_from_envelope(envelope, live[0], live[1]) == owner
                and catalog_snapshot_from_grants(live[2]) == catalog)
        receipt = self.interpreter.store.run_once(source_key=semantic_source_key(envelope),
            frozen_ui_digest=ui_semantic_digest(envelope), owner_scope=owner, catalog_snapshot=catalog,
            mode='root', original_text=envelope['comment_text'],
            model=ClarificationIntentModel(self.interpreter.model,session,ui_semantic_digest(envelope)),
            budget=self.interpreter.budget, scope_authorizer=authorized, should_stop=self.interpreter.should_stop)
        metadata = verify_semantic_decision(receipt, mode='root', original_text=envelope['comment_text'],
            frozen_ui_digest=ui_semantic_digest(envelope), owner_scope=owner, catalog_snapshot=catalog)
        if metadata['action'] not in session['root']['allowed_actions']:
            return [], None, None, 'clarification_action_not_allowed'
        if not authorized(): return [], None, None, 'clarification_scope_changed'
        fresh = make_clarification_envelope(read(self._anchor(session), deepcopy(candidate)), carrier, session)
        if ui_semantic_digest(fresh) != ui_semantic_digest(envelope) or not authorized():
            return [], None, None, 'clarification_ui_changed_after_meaning'
        resolution = self.interpreter.resolver.resolve_verified_intent(envelope, receipt)
        if (resolution.binding is None or resolution.authorization is None
                or resolution.action not in {'save', 'clarify'}
                or self.bridge.registry.authorize_snapshot(resolution.authorization) != resolution.binding):
            return [], None, None, 'clarification_scope_changed'
        envelope = make_clarification_envelope(detail, resolution.binding, session)
        row = {'binding': resolution.binding, 'envelope': envelope, 'video_title': detail.get('video_title', '')[:512],
            'kind': resolution.action, 'notice_fingerprint': None, 'user_id': resolution.user_id,
            'device_id': resolution.device_id, 'authorization': resolution.authorization}
        serial = {**row, 'binding': resolution.binding.to_dict()}
        if self.store.enabled is not True:return [],None,None,'clarification_feature_disabled'
        try:self.store.plan(session['session_id'], claim['source_id'], envelope, metadata, serial)
        except ClarificationError as error:
            if error.code=='clarification_feature_disabled':return [],None,None,'clarification_feature_disabled'
            raise
        thread = {'quality': 'verified_ancestor', 'root_reference_id': session['root']['envelope']['reference_id']}
        correlation = {'source_native_comment_id': candidate['native_comment_id'],
            'observed_native_comment_id': envelope['native_comment_id'],
            'quality': 'native_equal' if envelope['native_comment_id'] == candidate['native_comment_id'] else 'semantic_only'}
        self.lane.browser_lane.pending[envelope['reference_id']] = {'thread': thread, 'fingerprint': None}
        return [row], thread, correlation, None

    def authority_current(self, task):
        proof = task.get('envelope', {}).get('evidence', {})
        if proof.get('source_kind') != SOURCE_KIND: return True
        try:
            validate_clarification_envelope(task['envelope'], Binding.from_dict(task['binding']))
            session = self.store.get(proof['clarification_session_id'])
            if (session is None or session['state'] != 'handed_off' or session['child_task_id'] != task['id']
                    or session['immutable_hash'] != proof['clarification_root_hash']
                    or session['response_hash'] != digest(immutable_envelope(task['envelope']))
                    or not self._root_current(session)): return False
            item=session['queue_item']
            if not isinstance(item,dict) or any(task.get(key)!=item.get(key) for key in
                    ('kind','binding','authorization','user_id','device_id')):return False
            source = self._source(session['response_source_id'])
            return bool(source and source['state'] == 'admitted' and source['task_id'] == task['id'])
        except Exception:
            return False

    def origin_for_task(self, task):
        if task.get('envelope', {}).get('evidence', {}).get('source_kind') != SOURCE_KIND: return None
        if not self.authority_current(task): return None
        session = self.store.get(task['envelope']['evidence']['clarification_session_id'])
        return deepcopy(session['root'])

    def _review(self, session, reason):
        """A CAS on new session metadata only; never touch task/effect ledgers."""
        if not self._recovery_active():return False
        with self.store._db(write=True) as db:
            row=db.execute('SELECT * FROM clarification_sessions WHERE session_id=?',(session['session_id'],)).fetchone()
            if row is None or row['revision']!=session['revision'] or row['state']!=session['state']:return False
            db.execute("UPDATE clarification_sessions SET state='needs_review',reason_code=?,revision=revision+1,updated_at=? WHERE session_id=? AND revision=?",
                (reason,self.clock(),session['session_id'],session['revision']))
            self.store._event(db,session['session_id'],'existing_ledgers_need_review',{'reason_code':reason})
            return True

    @staticmethod
    def _recovery_result():
        return {'links_repaired':0,'publications_observed':0,'needs_review':0,'errors':0}

    def _recovery_active(self):
        if self.bridge.should_stop():return False
        control = getattr(self.bridge, 'control', None)
        return control is None or control.snapshot().get('desired_state') == 'running'

    def _recovery_error(self, session, result):
        result['errors'] += 1
        recorded = False
        if session is not None and session.get('state') in {'awaiting_publication','planned'}:
            try:
                recorded = self._review(session, 'clarification_recovery_record_unavailable')
            except ReceiverInterventionRequired:
                raise
            except Exception:
                pass  # An unreadable store is not a successful review write.
        result['needs_review'] += bool(recorded)
        self.last_recovery_diagnostic = {'code':'clarification_recovery_record_unavailable',
                                        'review_recorded':bool(recorded)}

    def _reconcile_planned(self, session, result, *, expected_task=None):
        """Both recovery entries use the same pre-execution handoff gate."""
        if (session['state'] != 'planned' or not self._recovery_active()
                or self.store.enabled is not True):return
        root=session['root']
        if (root.get('capability')!='intent_clarification_v1' or root.get('activated_at')!=self.store.activated_at
                or root.get('created_at',0)<self.store.activated_at):return
        item=session['queue_item'];env=item.get('envelope') if isinstance(item,dict) else None
        if not env:
            result['needs_review']+=self._review(session,'clarification_recovery_plan_missing');return
        task=self.bridge.queue.lookup_by_source_reference(env['sender_profile_url'],env['receiver_profile_url'],env['source_mode'],env['reference_id'])
        source=self._source(session['response_source_id'])
        if source and source['state']=='planned':return  # Native Source recovery owns this window.
        source_plan=json.loads(source['plan']) if source and isinstance(source.get('plan'),str) else source.get('plan') if source else None
        source_item=source_plan.get('queue_item') if isinstance(source_plan,dict) else None
        source_exact=bool(source_item and source_plan.get('reference_id')==env['reference_id']
            and immutable_envelope(source_plan['envelope'])==immutable_envelope(env)
            and immutable_envelope(source_item['envelope'])==immutable_envelope(env)
            and not any(source_item.get(key)!=item.get(key) for key in
                ('kind','binding','authorization','user_id','device_id'))
            and self.lane.source_store.admitted_source_current(source['source_id']) is True)
        if (task is None or source is None or source['state']!='admitted' or source['historical']
                or source['first_seen']<self.store.activated_at
                or source['task_id']!=task['id'] or source['reference_id']!=env['reference_id']
                or not source_exact
                or any(task.get(key)!=item.get(key) for key in ('kind','binding','authorization','user_id','device_id'))
                or immutable_envelope(task['envelope'])!=immutable_envelope(env)
                or expected_task is not None and (expected_task.get('id')!=task['id']
                    or any(expected_task.get(key)!=task.get(key) for key in
                        ('kind','binding','authorization','user_id','device_id'))
                    or immutable_envelope(expected_task['envelope'])!=immutable_envelope(env))
                or not self._root_current(session)):
            result['needs_review']+=self._review(session,'clarification_recovery_link_unconfirmed');return
        if task.get('state') in {'executing','publishing'}:return
        if (task.get('state')!='queued'
                or any(task.get(key) for key in ('execution_token','publication_token','lease_until','owner_run_id'))
                or task.get('execution_attempts',0)!=0 or task.get('publish_attempts',0)!=0):
            result['needs_review']+=self._review(session,'clarification_recovery_effect_unconfirmed');return
        if task.get('agent_task_id') or self.bridge._find(task) is not None:
            result['needs_review']+=self._review(session,'clarification_recovery_agent_effect_present');return
        binding=Binding.from_dict(task['binding'])
        receipt=self.bridge._semantic_receipt(task)
        if (self.bridge.registry.authorize_snapshot(task.get('authorization'))!=binding
                or type(receipt) is not RecordedSemanticDecision or receipt.plan_id!=session['semantic_plan_id']):
            result['needs_review']+=self._review(session,'clarification_recovery_permission_changed');return
        metadata=verify_semantic_decision(receipt,mode='root',original_text=env['comment_text'],
            frozen_ui_digest=ui_semantic_digest(env),owner_scope=self.bridge._semantic_owner_scope(task))
        if metadata!=session['decision']:
            result['needs_review']+=self._review(session,'clarification_recovery_meaning_changed');return
        if self._recovery_active() and self.store.enabled is True:
            # Receipt/permission checks can block. Fence a concurrently claimed
            # task or changed source again before repairing the session link.
            if (self.bridge.queue.get_task(task['id'])!=task
                    or self._source(session['response_source_id'])!=source
                    or self.bridge._find(task) is not None
                    or self.bridge.registry.authorize_snapshot(task.get('authorization'))!=binding
                    or not self._root_current(session)):
                result['needs_review']+=self._review(session,'clarification_recovery_link_changed');return
            if not self._recovery_active():return
            if self._admit_existing(session['session_id'],session['response_source_id'],task) is not None:
                result['links_repaired']+=1

    def _reconcile_session(self, session, result):
        root=session['root']
        if (root.get('capability')!='intent_clarification_v1' or root.get('activated_at')!=self.store.activated_at
                or root.get('created_at',0)<self.store.activated_at):return
        if session['state']=='planned':
            self._reconcile_planned(session, result);return
        task=self.bridge.queue.get_task(root['id'])
        if task is None:
            result['needs_review']+=self._review(session,'clarification_recovery_root_missing');return
        if any(task.get(key)!=root.get(key) for key in ('binding','envelope','authorization','user_id','device_id')):
            result['needs_review']+=self._review(session,'clarification_recovery_root_changed');return
        source=self._source(root['source_id'])
        if (source is None or source['state']!='admitted' or source['task_id']!=task['id'] or source['historical']):
            result['needs_review']+=self._review(session,'clarification_recovery_source_changed');return
        old=self.bridge._find(task)
        reply=self.bridge.agent.get_reply(old['task_id']) if old else None
        if reply and old['state']=='b_view_visible' and reply.get('state')=='b_view_visible':
            # Only record an existing exact effect, including while disabled.
            # Using this record later still requires current authorization.
            if not self._recovery_active():return
            self.after_publish(task)
            if self.store.get(session['session_id'])['state']=='awaiting_reply':
                result['publications_observed']+=1
        elif reply and reply.get('send_token') and reply.get('state') in {'reserved','delivery_unknown','verification_required'}:
            result['needs_review']+=self._review(session,'clarification_recovery_publication_unconfirmed')

    def reconcile_existing(self, *, limit=20):
        """Fair, bounded observation of existing records; never replay effects."""
        if type(limit) is not int or not 1<=limit<=100:raise ClarificationError('clarification_recovery_limit')
        result=self._recovery_result()
        if not self._recovery_active():return result
        with self.store._db() as db:
            sql="SELECT * FROM clarification_sessions WHERE state IN ('awaiting_publication','planned')"
            cursor=getattr(self,'_recovery_cursor',None)
            rows=[]
            if cursor is not None:
                rows=db.execute(sql+' AND (created_at>? OR (created_at=? AND session_id>?)) ORDER BY created_at,session_id LIMIT ?',
                    (cursor[0],cursor[0],cursor[1],limit)).fetchall()
            if not rows:
                self._recovery_cursor=None
                rows=db.execute(sql+' ORDER BY created_at,session_id LIMIT ?',(limit,)).fetchall()
        for row in rows:
            if not self._recovery_active():break
            # Advance before decoding, so one malformed record cannot starve
            # every later record. No durable session/effect is reset to fresh.
            self._recovery_cursor=(row['created_at'],row['session_id'])
            session={key:row[key] for key in ('session_id','revision','state')}
            try:
                session=self.store._view(row)
                self._reconcile_session(session, result)
            except ReceiverInterventionRequired:
                raise
            except Exception:
                self._recovery_error(session, result)
        return result
