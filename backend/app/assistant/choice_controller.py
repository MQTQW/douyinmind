"""Trusted orchestration of one durable ask, real A selection, and save child.

No model or send capability lives here. Browser observations are provided only
by the owner lane. Every cross-ledger call occurs after ChoiceStore released
its own short transaction; admission_lock must be the shared reentrant lock.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import threading
import time

from app.mention_agent.contracts import Binding, canonical_json, validate_envelope
from app.mention_agent.choice_evidence import (choice_ask_reference, choice_reference_id,
    make_choice_envelope, validate_choice_envelope)
from app.mention_receiver.browser import ReceiverDetailError, ReceiverInterventionRequired, ReceiverPageError
from .choice_intent import (choice_prompt, selection_forms, parse_library_selection,
    semantic_choice_catalog,semantic_choice_owner)
from .commands import CommandResolver, Resolution, library_forms
from .library_choice import LibraryChoiceError
from .reply_style import reply_body_budget, style_reply_text


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _identity(envelope):
    value=deepcopy(envelope)
    for name in ('observed_at_utc','diagnostics'):value.get('evidence',{}).pop(name,None)
    return value


class ChoiceController:
    def __init__(self,registry,queue,agent,choice_store,*,conversation,should_stop=None,admission_lock=None,clock=None):
        self.registry,self.queue,self.agent,self.store=registry,queue,agent,choice_store
        self.conversation=conversation
        self.should_stop=should_stop or (lambda:False)
        self.admission_lock=admission_lock or threading.RLock()
        self.clock=clock or time.time
        self.pending={}
        self.last_issue_code=None
        self._notice_recheck_key=None
        self.semantic_choice_planner=None
        self.semantic_choice_receipt_loader=None
        self.semantic_root_resolution_provider=None
        self.last_semantic_choice_status=None
        self.intent_origin_for_task=None

    def _origin_envelope(self,task):
        if task.get('envelope',{}).get('evidence',{}).get('source_kind')!='intent_clarification_reply':
            return task['envelope']
        origin=self.intent_origin_for_task(task) if callable(self.intent_origin_for_task) else None
        if not isinstance(origin,dict) or not isinstance(origin.get('envelope'),dict):
            raise ValueError('clarification_choice_origin_unverified')
        return origin['envelope']

    def _root_resolution(self,task):
        provider=self.semantic_root_resolution_provider
        return provider(task) if callable(provider) else CommandResolver(self.registry).resolve(task['envelope'])

    def clear_buffer(self):
        self.pending.clear()  # Durable selected/claimed intents remain recoverable.
        self._notice_recheck_key=None

    @staticmethod
    def _notice_key(notice,detail):
        return _digest({'sender':notice.get('source_profile_url'),'text':notice.get('exact_text'),
            'video':detail.get('video_id') if isinstance(detail,dict) else None,
            'native':detail.get('native_comment_id') if isinstance(detail,dict) else None})

    def selection_notice_requires_recheck(self,notice,detail):
        return self._notice_recheck_key==self._notice_key(notice,detail)

    def _fresh_time(self,proof):
        try:
            observed=datetime.fromisoformat(proof['observed_at_utc'].replace('Z','+00:00'))
            return observed.tzinfo is not None and -5<=self.clock()-observed.timestamp()<=120
        except (KeyError,TypeError,ValueError,OverflowError):return False

    @staticmethod
    def _binding(task):return Binding.from_dict(task['binding'])

    def _authorized_task(self,task):
        if not isinstance(task,dict):return False
        try:
            binding=self._binding(task);validate_envelope(task['envelope'],binding)
            auth=task['authorization']
            return (binding.active and isinstance(auth,dict)
                and self.registry.authorize_snapshot(auth)==binding
                and task.get('user_id')==auth['user_id'] and task.get('device_id')==auth['device_id'])
        except (ValueError,TypeError,KeyError,AttributeError):return False

    @staticmethod
    def _menu(resolution, reply_prefix=''):
        body = choice_prompt(resolution.choice_options,catalog_count=resolution.catalog_count,
            catalog_scope=resolution.catalog_scope,max_chars=reply_body_budget(280, reply_prefix))
        return style_reply_text(body, reply_prefix, maximum=280)

    @staticmethod
    def _ask_ref(task,resolution,ask_text):
        return 'choice-ask-intent:'+_digest({'task_id':task['id'],'binding':task['binding'],
            'authorization':task['authorization'],'reference_id':task['envelope']['reference_id'],
            'comment_text':task['envelope']['comment_text'],'ask_text':ask_text,
            'catalog':resolution.choice_intent})

    def _eligible_options(self,resolution):
        result=[]
        for index,item in enumerate(resolution.choice_options,1):
            if item.get('can_import') is not True:continue
            binding=Binding.from_dict(item['binding']);auth=item['authorization']
            if (binding.is_all_local or binding.knowledge_base_id!=item['knowledge_base_id']
                    or self.registry.authorize_snapshot(auth,action='import')!=binding
                    or auth.get('user_id')!=resolution.user_id or auth.get('device_id')!=resolution.device_id):
                raise ValueError('choice_option_authorization_changed')
            result.append({'knowledge_base_id':item['knowledge_base_id'],'name':item['name'],
                'authorization':deepcopy(auth),'ordinal':item.get('ordinal',index)})
        return result

    def _hold(self,session,reason='binding_changed'):
        self.last_issue_code=reason
        self.store.hold_waiting(session['choice_id'],reason)

    def _fresh_root(self,session):
        task=self.queue.get_task(session['root_task_id'])
        try:
            origin=self._origin_envelope(task) if task else None
        except (ValueError,TypeError,KeyError,AttributeError):
            return None
        if (not task or task.get('state') in {'cancelled','failed'} or not self._authorized_task(task)
                or task.get('binding')!=session['root_binding']
                or task.get('authorization')!=session['authorization_snapshot']
                or _identity(origin)!=_identity(session['root_envelope'])):
            return None
        try:
            resolution=self._root_resolution(task)
            prefix = self.agent.reply_prefix('assistant:' + task['id']) or ''
            if (resolution.reason_code!='save_library_required' or resolution.action!='clarify'
                    or resolution.binding!=self._binding(task) or resolution.authorization!=task['authorization']
                    or not resolution.choice_intent or resolution.choice_intent.get('catalog_public_reply_allowed') is not True
                    or self._menu(resolution, prefix)!=session['ask_text']
                    or self._ask_ref(task,resolution,session['ask_text'])!=session['ask_ref']
                    or self._eligible_options(resolution)!=session['options']):return None
        except (ValueError,TypeError,KeyError,AttributeError):return None
        return task,resolution

    def prepare_ask(self,task,resolution,*,reply_prefix=''):
        if (self.should_stop() or not isinstance(resolution,Resolution) or resolution.reason_code!='save_library_required'
                or not resolution.choice_intent or not self._authorized_task(task)):return None
        existing=self.store.get_for_root_task(task['id'])
        if existing is not None:
            if self._fresh_root(existing) is None:
                self._hold(existing);return None
            return existing if existing['state'] not in {'needs_review','cancelled'} else None
        try:
            current=self._root_resolution(task)
            if (current.reason_code!='save_library_required' or current.binding!=self._binding(task)
                    or current.authorization!=task['authorization'] or current.choice_intent!=resolution.choice_intent
                    or current.choice_intent.get('catalog_public_reply_allowed') is not True
                    or current.binding.public_reply_allowed is not True):return None
            options=self._eligible_options(current)
            if not options:return None
            prefix = self.agent.freeze_reply_prefix('assistant:' + task['id'], reply_prefix)
            ask=self._menu(current, prefix)
            return self.store.create(root_task_id=task['id'],root_binding=task['binding'],root_envelope=self._origin_envelope(task),
                ask_ref=self._ask_ref(task,current,ask),ask_text=ask,options=options,authorization=task['authorization'])
        except (ValueError,TypeError,KeyError,AttributeError):
            self.last_issue_code='choice_prepare_not_authorized';return None

    def _agent_reply(self,task,session):
        try:
            expected=self.agent.lookup_existing(task['binding_id'],task['envelope'])
            if expected is None or task.get('agent_task_id') not in (None,expected['task_id']):return None
            reply=self.agent.get_reply(expected['task_id'])
            if (not reply or reply['kind']!='clarification' or reply['text']!=session['ask_text']
                    or expected['binding_id']!=task['binding_id']):return None
            target=reply['target']
            if any(target.get(key)!=task['envelope'][key] for key in task['envelope'] if key!='evidence'):return None
            return expected,reply
        except (ValueError,TypeError,KeyError):return None

    def before_publish(self,task):
        if self.should_stop():return False
        if task.get('envelope',{}).get('evidence',{}).get('source_kind')=='library_choice_reply':
            return self.resolution_for_selection(task) is not None
        session=self.store.get_for_root_task(task['id'])
        if session is None:
            try:
                resolution=self._root_resolution(task)
                return not (resolution.reason_code=='save_library_required' and resolution.choice_intent
                    and resolution.choice_intent.get('eligible_count',0)>0)
            except (ValueError,TypeError,KeyError,AttributeError):return False
        if self._fresh_root(session) is None:
            self._hold(session);return False
        observed=self._agent_reply(task,session)
        return bool(session['state'] in {'awaiting_ask','ask_held'} and observed
            and observed[1]['state']=='ready' and not observed[1].get('send_token'))

    def authority_current(self,task):
        """Last-gate scope check, valid both before and after reservation."""
        if self.should_stop() or not self._authorized_task(task):return False
        if task.get('envelope',{}).get('evidence',{}).get('source_kind')=='library_choice_reply':
            return self.resolution_for_selection(task) is not None
        session=self.store.get_for_root_task(task['id'])
        return (session is None or session['state'] not in {'needs_review','cancelled'}
            and self._fresh_root(session) is not None)

    def _published_reply(self, task, kind):
        """Verify an already spent direct B-visible send; no new capability."""
        from app.mention_agent.store import _verified_target
        old = self.agent.lookup_existing(task['binding_id'], task['envelope'])
        if (old is None or task.get('agent_task_id') != old['task_id'] or old['state'] != 'b_view_visible'):
            return None
        reply = self.agent.get_reply(old['task_id'])
        if (not reply or reply['kind'] != kind or reply['state'] != 'b_view_visible'
                or re.fullmatch(r'[a-f0-9]{48}', reply.get('send_token') or '') is None):
            return None
        proof, verification = reply.get('evidence'), reply.get('verification')
        if (not isinstance(proof, dict) or proof.get('submit_gate_passed') is not True
                or proof.get('exact_body_in_original_thread') is not True
                or proof.get('reply_sha256') != hashlib.sha256(reply['text'].encode()).hexdigest()
                or not _verified_target(reply['target'], verification, task['binding'])):
            return None
        for record in (proof, verification):
            observed = datetime.fromisoformat(record['observed_at_utc'].replace('Z', '+00:00'))
            if observed.tzinfo is None:
                return None
        if any(reply['target'].get(key) != task['envelope'][key] for key in task['envelope'] if key != 'evidence'):
            return None
        if any(reply['target'].get(key) != task['binding'][key] for key in ('binding_id','instance_id','knowledge_base_id')):
            return None
        return reply

    def published_followup_anchor_current(self, task):
        """Historical proof for a completed choice child, never dispatch authority.

        Pending menus still use authority_current/_fresh_root. A spent reply's
        frozen menu is checked against its original ledgers, not today's catalog.
        """
        try:
            if (self.should_stop() or not isinstance(task, dict) or task.get('state') != 'b_view_visible'
                    or task.get('envelope', {}).get('evidence', {}).get('source_kind') != 'library_choice_reply'
                    or re.fullmatch(r'[a-f0-9]{48}', task.get('publication_token') or '') is None
                    or not self._authorized_task(task)):
                return False
            current = self.queue.get_task(task['id'])
            if (not current or current['state'] != 'b_view_visible'
                    or any(current.get(key) != task.get(key) for key in
                           ('immutable','binding','authorization','user_id','device_id','agent_task_id','publication_token'))
                    or _identity(current['envelope']) != _identity(task['envelope'])):
                return False
            session = self._session_for_task(current)
            if not session or session['state'] != 'handed_off' or session['child_task_id'] != current['id']:
                return False
            frozen = self._frozen_selected_resolution(session)
            if (frozen is None or not self._matches_child(current, session, frozen)
                    or self.registry.authorize_snapshot(frozen.authorization, action='import') != frozen.binding):
                return False
            root = self.queue.get_task(session['root_task_id'])
            if (not root or root['state'] not in {'b_view_visible','waiting_library'}
                    or not self._authorized_task(root) or root['binding'] != session['root_binding']
                    or root['authorization'] != session['authorization_snapshot']
                    or _identity(self._origin_envelope(root)) != _identity(session['root_envelope'])):
                return False
            identity = {'instance_id':session['root_binding']['instance_id'],
                'sender':session['root_binding']['sender_profile_url'],'receiver':session['root_binding']['receiver_profile_url'],
                'video_id':session['root_envelope']['video_id'],'source_mode':session['root_envelope']['source_mode'],
                'root_reference_id':session['root_envelope']['reference_id']}
            if session['choice_id'] != _digest({'kind':'library_choice_v1','root':identity,'ask_ref':session['ask_ref'],
                    'options':session['options'],'authorization_snapshot':session['authorization_snapshot']}):
                return False
            selection = session['selection_envelope']
            selection_identity = {'instance_id':identity['instance_id'],'sender':identity['sender'],'receiver':identity['receiver'],
                'source_mode':selection['source_mode'],'reference':selection['native_comment_id'] if selection['reference_quality']=='native' else session['selection_ref'],
                'quality':selection['reference_quality']}
            selection_key = _digest(selection_identity)
            with self.store._db() as db:  # Owned read-only transaction; never hold/requeue/repair history.
                sealed = db.execute('SELECT immutable FROM choice_sessions WHERE choice_id=?', (session['choice_id'],)).fetchone()
                refs = db.execute('SELECT * FROM choice_selection_refs WHERE choice_id=?', (session['choice_id'],)).fetchall()
            expected = {key:session[key] for key in ('root_task_id','root_binding','root_envelope','authorization_snapshot','ask_ref','ask_text','options')}
            if (sealed is None or json.loads(sealed['immutable']) != expected or len(refs) != 1
                    or refs[0]['selection_key'] != selection_key or session['selection_key'] != selection_key
                    or refs[0]['envelope_hash'] != _digest(_identity(selection))
                    or session['child_key'] != 'choice-child-' + _digest({'choice_id':session['choice_id'],
                        'selection_key':selection_key,'knowledge_base_id':session['selected_kb_id']})):
                return False
            ask = self._published_reply(root, 'clarification')
            child = self._published_reply(current, 'import_status')
            if (ask is None or child is None or ask['text'] != session['ask_text']
                    or child.get('import_state') not in {'succeeded','failed','failed_no_submit'}):
                return False
            proof = session['ask_proof']
            expected_ask = {'source_kind':'library_choice_ask','sender_profile_url':identity['sender'],
                'receiver_profile_url':identity['receiver'],'ask_author_profile_url':identity['receiver'],
                'video_id':identity['video_id'],'root_reference_id':identity['root_reference_id'],
                'root_comment_text':session['root_envelope']['comment_text'],'ask_ref':session['ask_ref'],
                'ask_text':session['ask_text'],'ask_reply_ref':session['ask_reply_ref']}
            if (not isinstance(proof, dict) or any(proof.get(key) != value for key,value in expected_ask.items())
                    or proof.get('receiver_verified') is not True or proof.get('exact_body_in_original_thread') is not True
                    or type(proof.get('unique_count')) is not int or proof['unique_count'] != 1
                    or proof.get('reference_quality') not in {'native','verified_ui'}
                    or proof.get('observed_at_utc') != ask['evidence'].get('observed_at_utc')
                    or session['ask_reply_ref'] != choice_ask_reference(root_reference_id=identity['root_reference_id'],
                        ask_ref=session['ask_ref'],ask_text=session['ask_text'],receiver_profile_url=identity['receiver'],video_id=identity['video_id'])):
                return False
            native = proof.get('native_comment_id')
            if (proof['reference_quality'] == 'native' and (type(native) is not str or re.fullmatch(r'[0-9]{5,30}',native) is None)
                    or proof['reference_quality'] == 'verified_ui' and native is not None):
                return False
            return True
        except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OSError):
            return False

    def after_publish(self,task,outcome):
        session=self.store.get_for_root_task(task['id'])
        if session is None or session['state'] not in {'awaiting_ask','ask_unknown','ask_held'}:return
        value=self._agent_reply(task,session)
        if not value:return
        old,reply=value
        if reply['state']=='b_view_visible':
            proof=reply.get('evidence');verification=reply.get('verification')
            if (old['state']!='b_view_visible' or not reply.get('send_token') or not isinstance(proof,dict)
                    or proof.get('submit_gate_passed') is not True or proof.get('exact_body_in_original_thread') is not True
                    or proof.get('reply_sha256')!=hashlib.sha256(session['ask_text'].encode()).hexdigest()
                    or not isinstance(verification,dict) or verification.get('current_receiver_verified') is not True
                    or not self._fresh_time(proof)):return
            if self._fresh_root(session) is None:self._hold(session);return
            root=session['root_envelope'];binding=session['root_binding']
            ref=choice_ask_reference(root_reference_id=root['reference_id'],ask_ref=session['ask_ref'],
                ask_text=session['ask_text'],receiver_profile_url=binding['receiver_profile_url'],video_id=root['video_id'])
            ask_proof={'source_kind':'library_choice_ask','sender_profile_url':binding['sender_profile_url'],
                'receiver_profile_url':binding['receiver_profile_url'],'ask_author_profile_url':binding['receiver_profile_url'],
                'receiver_verified':True,'video_id':root['video_id'],'root_reference_id':root['reference_id'],
                'root_comment_text':root['comment_text'],'ask_ref':session['ask_ref'],'ask_text':session['ask_text'],
                'ask_reply_ref':ref,'unique_count':1,'exact_body_in_original_thread':True,
                'reference_quality':'verified_ui','native_comment_id':None,'observed_at_utc':proof['observed_at_utc']}
            self.store.record_ask_outcome(session['choice_id'],ask_ref=session['ask_ref'],outcome='b_view_visible',
                ask_reply_ref=ref,proof=ask_proof)
        elif reply.get('send_token') and reply['state'] in {'reserved','delivery_unknown','verification_required'}:
            self.store.record_ask_outcome(session['choice_id'],ask_ref=session['ask_ref'],outcome='delivery_unknown')
        elif reply['state'] in {'needs_review','failed_no_submit'}:
            self.store.record_ask_outcome(session['choice_id'],ask_ref=session['ask_ref'],outcome='failed_no_submit')

    def decorate_result(self,task,result):
        session=self.store.get_for_root_task(task['id'])
        if session is None:return result
        self.after_publish(task,result)
        session=self.store.get(session['choice_id']);value=deepcopy(result)
        invalid=session['state'] in {'awaiting_ask','ask_unknown','ask_held','waiting_library','selected','handoff_claimed'} and self._fresh_root(session) is None
        if invalid:
            self._hold(session);session=self.store.get(session['choice_id'])
            value.update(state='needs_review',reason_code='binding_changed',safe_to_execute=False,safe_to_publish=False)
        summary={'choice_id':session['choice_id'],'state':session['state'],'option_count':len(session['options']),
            'selected_knowledge_base_id':session['selected_kb_id'],'child_task_id':session['child_task_id']}
        value.setdefault('details',{})['library_choice']=summary
        if invalid:return value
        if session['state'] in {'waiting_library','selected','handoff_claimed'}:
            value.update(state='waiting_library',safe_to_execute=False,safe_to_publish=False)
        elif session['state']=='handed_off':
            value.update(state='b_view_visible',safe_to_execute=False,safe_to_publish=False)
        elif session['state']=='needs_review':
            value.update(state='needs_review',reason_code='binding_changed',safe_to_execute=False,safe_to_publish=False)
        return value

    def _frozen_selected_resolution(self,session):
        if session['state'] not in {'selected','handoff_claimed','handed_off'}:return None
        auth=session['selected_authorization'];root=session['root_binding']
        option=next((item for item in session['options'] if item['knowledge_base_id']==session['selected_kb_id']),None)
        if (option is None or option['authorization']!=auth or auth['action']!='import'
                or auth['knowledge_base_id']!=session['selected_kb_id']):return None
        binding=Binding.from_dict({'binding_id':auth['binding_id'],'instance_id':auth['instance_id'],
            'sender_profile_url':root['sender_profile_url'],'receiver_profile_url':root['receiver_profile_url'],
            'knowledge_base_id':session['selected_kb_id'],'public_reply_allowed':auth['public_reply_allowed'],'active':True})
        validate_choice_envelope(session['selection_envelope'],binding,root_binding=session['root_binding'],
            root_envelope=session['root_envelope'],ask_ref=session['ask_ref'],ask_reply_ref=session['ask_reply_ref'],
            ask_text=session['ask_text'],choice_id=session['choice_id'])
        trusted={'choice_id':session['choice_id'],'root_comment_text':session['root_envelope']['comment_text'],
            'selection_reference_id':session['selection_ref'],'selected_library_name':option['name']}
        return Resolution('save',binding=binding,authorization=deepcopy(auth),user_id=auth['user_id'],device_id=auth['device_id'],
            route_state='blocked',reason_code='library_choice_source_fact',choice_intent=trusted,
            library={'knowledge_base_id':binding.knowledge_base_id,'name':option['name'],'accepted_names':[],
                     'grant_id':auth['grant_id']})

    def _selected_resolution(self,session):
        frozen=self._frozen_selected_resolution(session)
        if frozen is None or self._fresh_root(session) is None:return None
        binding=self.registry.authorize_snapshot(frozen.authorization,action='import')
        grant=self.registry.get_grant(frozen.authorization['grant_id'])
        if binding!=frozen.binding or not grant or grant.get('name')!=frozen.library['name']:return None
        return replace(frozen,route_state='ready',reason_code='library_choice_selected',
            library={**frozen.library,'accepted_names':library_forms(grant)})

    def _session_for_task(self,task):
        try:
            binding=self._binding(task);env=task['envelope']
            if env.get('evidence',{}).get('source_kind')!='library_choice_reply':return None
            session=self.store.get_for_selection_reference(env['sender_profile_url'],env['receiver_profile_url'],
                env['source_mode'],env['reference_id'],binding.instance_id)
            return session
        except (ValueError,TypeError,KeyError):return None

    @staticmethod
    def _matches_child(task,session,resolution):
        return (task.get('kind')=='save' and task.get('binding')==resolution.binding.to_dict()
            and task.get('authorization')==resolution.authorization and task.get('user_id')==resolution.user_id
            and task.get('device_id')==resolution.device_id
            and _identity(task['envelope'])==_identity(session['selection_envelope'])
            and session.get('child_task_id') in (None,task['id']))

    def resolution_for_selection(self,task):
        if self.should_stop():return None
        session=self._session_for_task(task)
        if session is None or not self._authorized_task(task):return None
        try:
            current=self.queue.get_task(task['id'])
            if (not current or current.get('state') in {'cancelled','failed'}
                    or current.get('binding')!=task['binding'] or current.get('authorization')!=task.get('authorization')
                    or _identity(current['envelope'])!=_identity(task['envelope'])
                    or current.get('immutable')!=task.get('immutable')):return None
            resolution=self._selected_resolution(session)
            return resolution if resolution and self._matches_child(task,session,resolution) else None
        except (ValueError,TypeError,KeyError,AttributeError):return None

    def _kwargs(self,session,resolution):
        root=self.queue.get_task(session['root_task_id'])
        return {'binding':resolution.binding,'envelope':deepcopy(session['selection_envelope']),'kind':'save',
            'video_title':root.get('video_title','') if root else '', 'user_id':resolution.user_id,
            'device_id':resolution.device_id,'authorization':deepcopy(resolution.authorization)}

    def _link(self,task,session,token,resolution):
        if not self._matches_child(task,session,resolution):raise ValueError('choice_child_original_scope_conflict')
        proof={'quality':'verified_ancestor','root_reference_id':session['root_envelope']['reference_id']}
        self.conversation.bind_task(task['id'],resolution.binding,task['envelope'],proof['root_reference_id'],proof)
        self.store.finish_handoff(session['choice_id'],token,task['id'])
        self.pending.pop(session['selection_ref'],None)

    def _stage(self,session,run_id):
        frozen=self._frozen_selected_resolution(session)
        if frozen is None:return None
        existing=self.queue.lookup_by_source_reference(frozen.binding.sender_profile_url,frozen.binding.receiver_profile_url,
            session['selection_envelope']['source_mode'],session['selection_ref'])
        # Recording an already admitted source is an observation, including
        # after revocation; it grants no new model/import/publication work.
        resolution=frozen if existing is not None else self._selected_resolution(session)
        if resolution is None:return None
        previous=self.pending.get(session['selection_ref'])
        if previous and previous['lease_until']>self.clock():claim=previous
        else:
            owned=self.store.claim_handoff(session['choice_id'],run_id=run_id)
            if owned is None:return None
            claim={'choice_id':session['choice_id'],'token':owned['handoff_token'],'lease_until':owned['handoff_lease_until']}
        if existing is not None:
            self._link(existing,session,claim['token'],resolution);return []
        self.pending[session['selection_ref']]=claim
        return [self._kwargs(session,resolution)]

    def admitted(self,tasks):
        with self.admission_lock:
            for task in tasks:
                env=task.get('envelope',{});pending=self.pending.get(env.get('reference_id'))
                if pending is None:continue
                session=self.store.get(pending['choice_id']);resolution=self._selected_resolution(session)
                if resolution is None:raise ValueError('choice_handoff_authorization_changed')
                self._link(task,session,pending['token'],resolution)

    def _was_submitted(self,session):
        task=self.queue.get_task(session['root_task_id'])
        observed=self._agent_reply(task,session) if task else None
        return bool(observed and observed[1].get('send_token') and observed[1]['state'] in {
            'reserved','delivery_unknown','verification_required','b_view_visible'})

    def _recheck_selected_handoff(self,adapter,session,run_id,source_checkpoint):
        """Fresh read of an unspent saved selection; never reserve it twice."""
        frozen=self._frozen_selected_resolution(session)
        if frozen is None:return []
        existing=self.queue.lookup_by_source_reference(frozen.binding.sender_profile_url,frozen.binding.receiver_profile_url,
            session['selection_envelope']['source_mode'],session['selection_ref'])
        if existing is not None:
            return self._stage(session,run_id) or []  # Observe an existing immutable task, never execute a new one.
        resolution=self._selected_resolution(session)
        if resolution is None or session.get('child_task_id') is not None:return []
        cleanup_allowed=False
        try:
            semantic_record=self.store.semantic_selection_record(session['choice_id'])
            if semantic_record is not None:
                details=adapter.read_choice_replies(session,[],keep_open=True,
                    candidate_text=session['selection_envelope']['evidence'].get('selection_value',session['selection_envelope']['comment_text']))
            else:
                details=adapter.read_choice_replies(session,list(selection_forms(session['options'])),keep_open=True)
            cleanup_allowed=True
            if self.should_stop():return []
            adapter.check_intervention()
            if not isinstance(details,list) or len(details)!=1:return []
            detail=details[0];proof=detail.get('evidence',{})
            if not self._fresh_time(proof):return []
            candidate={'source_profile_url':detail['sender_profile_url'],'exact_text':detail['command_text'],
                'semantic_fingerprint':'choice-selected-fresh-read'}
            fresh=make_choice_envelope(candidate,detail,resolution.binding,source_mode=session['selection_envelope']['source_mode'])
            fresh['evidence']['observed_at_utc']=proof['observed_at_utc']
            if _identity(fresh)!=_identity(session['selection_envelope']):return []
            if source_checkpoint is not None and source_checkpoint(deepcopy(fresh)) is not True:return []
            if self.should_stop() or not self._fresh_time(proof) or self._selected_resolution(session) is None:return []
            adapter.check_intervention()
            if self.should_stop():return []
            current=self.store.get(session['choice_id'])
            if (current is None or current['state'] not in {'selected','handoff_claimed'}
                    or current.get('child_task_id') is not None
                    or _identity(current['selection_envelope'])!=_identity(session['selection_envelope'])):return []
            # The old frozen envelope/time remains the task source. Fresh UI
            # evidence is only a gate; _stage normally reclaims expired lease.
            return self._stage(current,run_id) or []
        except ReceiverInterventionRequired:raise
        except ReceiverDetailError:return []
        except (ValueError,TypeError,KeyError):return []
        finally:
            if cleanup_allowed and not self.should_stop():
                adapter.check_intervention()
                if not self.should_stop():adapter.page.keyboard.press('Escape')

    @staticmethod
    def _semantic_candidate(session,candidate):
        required={'sender_profile_url','receiver_profile_url','video_id','native_comment_id',
                  'comment_text','choice_id','root_reference_id'}
        return (isinstance(candidate,dict) and set(candidate)==required
            and candidate['choice_id']==session['choice_id']
            and candidate['root_reference_id']==session['root_envelope']['reference_id']
            and all(candidate[key]==session['root_envelope'][key] for key in
                    ('sender_profile_url','receiver_profile_url','video_id'))
            and type(candidate['comment_text']) is str and bool(candidate['comment_text'].strip())
            and len(candidate['comment_text'])<=4000
            and type(candidate['native_comment_id']) is str
            and re.fullmatch(r'[1-9][0-9]{4,29}',candidate['native_comment_id']) is not None)

    def _semantic_envelope(self,session,detail,binding):
        proof=detail.get('evidence',{})
        if not self._fresh_time(proof):raise ReceiverDetailError('选库回复证据已过期',{})
        candidate={'source_profile_url':detail['sender_profile_url'],'exact_text':detail['command_text'],
            'semantic_fingerprint':'choice-public-observation:'+_digest({'choice_id':session['choice_id'],
                'text':detail['command_text'],'native':detail.get('native_comment_id')})}
        envelope=make_choice_envelope(candidate,detail,binding,source_mode=session['root_envelope']['source_mode'])
        envelope['evidence']['observed_at_utc']=proof['observed_at_utc']
        return envelope

    def _collect_semantic_choice(self,adapter,run_id,choice_id,source_checkpoint,selection_candidate):
        from .semantic_plan_store import ui_semantic_digest,verify_semantic_decision,SemanticPlanError
        status={'choice_id':choice_id,'parent_proved':False,'model_started':False,'selection_reserved':False,
                'fallback_allowed':False}
        self.last_semantic_choice_status=status
        cleanup_allowed=False
        try:
            with self.admission_lock:
                session=self.store.get(choice_id)
                if session is None or not self._semantic_candidate(session,selection_candidate):return []
                if session['state'] in {'selected','handoff_claimed'}:
                    status['parent_proved']=True
                    return self._recheck_selected_handoff(adapter,session,run_id,source_checkpoint)
                if session['state'] not in {'waiting_library','awaiting_ask','ask_unknown','ask_held'}:return []
                if self._fresh_root(session) is None:
                    status['fallback_allowed']=True;return []
                if session['state']!='waiting_library' and not self._was_submitted(session):return []
            if self.should_stop():return []
            adapter.check_intervention()
            details=adapter.read_choice_replies(session,[],keep_open=True,candidate_text=selection_candidate['comment_text'])
            cleanup_allowed=True
            if self.should_stop():return []
            adapter.check_intervention()
            if not isinstance(details,list) or len(details)>1:
                raise ReceiverDetailError('选库回复未能唯一核对',{})
            if not details:
                # No prompt/selection/event/model mutation: a genuinely new AT
                # outside this old Root may still take the ordinary mention path.
                status['fallback_allowed']=True;return []
            detail=details[0];proof=detail.get('evidence',{})
            if (detail.get('native_comment_id') not in (None,selection_candidate['native_comment_id'])
                    or not self._fresh_time(proof)):return []
            with self.admission_lock:
                if self._fresh_root(session) is None:return []
                first=session['options'][0]
                carrier=self.registry.authorize_snapshot(first['authorization'],action='import')
                if carrier is None:return []
            # This existing builder validates UI identity only. Its temporary
            # carrier command_id is ignored by ui_semantic_digest, never queued.
            observed=self._semantic_envelope(session,detail,carrier)
            if source_checkpoint is None or source_checkpoint(deepcopy(observed)) is not True:return []
            status['parent_proved']=True
            owner=semantic_choice_owner(session,observed);catalog=semantic_choice_catalog(session['options'])
            digest=ui_semantic_digest(observed)
            def scope_current():
                if self.should_stop():return False
                current=self.store.get(choice_id)
                return bool(current and current['revision']==session['revision']
                    and current['state'] in {'waiting_library','awaiting_ask','ask_unknown','ask_held'}
                    and self._fresh_root(current) is not None
                    and source_checkpoint(deepcopy(observed)) is True)
            if not scope_current():return []
            status['model_started']=True
            receipt=self.semantic_choice_planner(source_key='choice-semantic-'+_digest({
                'choice_id':choice_id,'native_comment_id':selection_candidate['native_comment_id']}),
                frozen_ui_digest=digest,owner_scope=owner,
                catalog_snapshot=catalog,mode='choice',original_text=observed['comment_text'],
                scope_authorizer=scope_current,should_stop=self.should_stop)
            decision=verify_semantic_decision(receipt,mode='choice',original_text=observed['comment_text'],
                frozen_ui_digest=digest,owner_scope=owner,catalog_snapshot=catalog)
            if decision.get('action')!='save' or decision.get('target_state')!='one':
                self.last_issue_code='choice_semantic_not_single_save';return []
            selected=next((item for item in session['options'] if item['authorization']['grant_id']==decision['grant_id']),None)
            if selected is None:return []
            binding=self.registry.authorize_snapshot(selected['authorization'],action='import')
            if binding is None:return []
            # Paid interpretation cannot turn an old UI snapshot into a new
            # source: reread current DOM and preserve original text/native ID.
            details=adapter.observe_current_choice_replies(session,[],candidate_text=selection_candidate['comment_text'])
            if not isinstance(details,list) or len(details)!=1:return []
            fresh=self._semantic_envelope(session,details[0],binding)
            if ui_semantic_digest(fresh)!=digest:return []
            if source_checkpoint(deepcopy(fresh)) is not True:return []
            with self.admission_lock:
                current=self.store.get(choice_id)
                if (self.should_stop() or current is None or current['revision']!=session['revision']
                        or self._fresh_root(current) is None or not self._fresh_time(fresh['evidence'])
                        or self.registry.authorize_snapshot(selected['authorization'],action='import')!=binding):return []
                ask_proof=getattr(adapter,'last_choice_ask_proof',None)
                if current['state']!='waiting_library':
                    if ask_proof is None:return []
                    self.store.record_ask_outcome(choice_id,ask_ref=current['ask_ref'],outcome='b_view_visible',
                        ask_reply_ref=ask_proof.get('ask_reply_ref'),proof=ask_proof)
                    current=self.store.get(choice_id)
                if current['state']!='waiting_library':return []
                reserved=self.store.reserve_selection(choice_id,expected_revision=current['revision'],
                    selection_ref=fresh['reference_id'],selection_envelope=fresh,
                    selected_kb_id=selected['knowledge_base_id'],selected_authorization=selected['authorization'],
                    semantic_decision=receipt)
                status['selection_reserved']=True
                return self._stage(reserved,run_id) or []
        except ReceiverInterventionRequired:raise
        except ReceiverDetailError:
            if not status['parent_proved'] and not status['model_started'] and not status['selection_reserved']:
                status['fallback_allowed']=True
            self.last_issue_code='choice_original_not_verified';return []
        except ReceiverPageError:raise
        except (SemanticPlanError,ValueError,TypeError,KeyError):
            self.last_issue_code='choice_semantic_unconfirmed';return []
        finally:
            if cleanup_allowed and not self.should_stop():
                adapter.check_intervention()
                if not self.should_stop():adapter.page.keyboard.press('Escape')

    def semantic_decision_for_selection(self,task):
        if not callable(self.semantic_choice_receipt_loader):return None
        session=self._session_for_task(task)
        if session is None:return None
        record=self.store.semantic_selection_record(session['choice_id'])
        if record is None:return None
        from .semantic_plan_store import verify_semantic_decision,ui_semantic_digest
        receipt=self.semantic_choice_receipt_loader(task['envelope'],binding=task['binding'])
        metadata=verify_semantic_decision(receipt,mode='choice',original_text=task['envelope']['comment_text'],
            grant_id=session['selected_authorization']['grant_id'],frozen_ui_digest=ui_semantic_digest(task['envelope']),
            owner_scope=semantic_choice_owner(session,task['envelope']),catalog_snapshot=semantic_choice_catalog(session['options']))
        return receipt if metadata['plan_id']==record['plan_id'] else None

    def collect_pending(self,adapter,run_id,*,choice_id=None,source_checkpoint=None,selection_candidate=None):
        # Native replies wake one already correlated local choice session.
        # They never authorize sweeping an unrelated older pending thread.
        if choice_id is not None and (not isinstance(choice_id,str) or not re.fullmatch(r'[a-f0-9]{64}',choice_id)):
            raise ValueError('invalid_choice_wakeup_id')
        if source_checkpoint is not None and not callable(source_checkpoint):
            raise ValueError('invalid_choice_source_checkpoint')
        if (callable(self.semantic_choice_planner) and selection_candidate is not None
                and choice_id is not None):
            return self._collect_semantic_choice(adapter,run_id,choice_id,source_checkpoint,selection_candidate)
        with self.admission_lock:
            if self.should_stop():return []
            adapter.check_intervention()
            receiver=adapter.expected_profile_url
            if choice_id is None:
                for session in self.store.list_pending_handoffs():
                    if session['root_binding']['receiver_profile_url']!=receiver:continue
                    return self._recheck_selected_handoff(adapter,session,run_id,source_checkpoint)
                candidates=self.store.list_asks_to_observe(receiver)+self.store.list_waiting(receiver)
            else:
                # A new matching native reply is a wakeup, so it need not wait
                # for an earlier empty scan's backoff. All original, fresh DOM,
                # current authorization and selection checks below still run.
                selected=self.store.get(choice_id)
                if (selected is not None and selected['root_binding']['receiver_profile_url']==receiver
                        and selected['state'] in {'selected','handoff_claimed'}):
                    return self._recheck_selected_handoff(adapter,selected,run_id,source_checkpoint)
                candidates=[selected] if (selected is not None
                    and selected['root_binding']['receiver_profile_url']==receiver
                    and selected['state'] in {'waiting_library','awaiting_ask','ask_unknown','ask_held'}) else []
            candidates.sort(key=lambda row:(row.get('next_check_at',0),row['created_at'],row['choice_id']))
            for session in candidates:
                if self.should_stop():return []
                if self._fresh_root(session) is None:self._hold(session);continue
                if session['state']!='waiting_library' and not self._was_submitted(session):
                    adapter.check_intervention()
                    if self.should_stop():return []
                    self.store.defer_waiting_recheck(session['choice_id'],seconds=45);continue
                adapter.check_intervention()
                cleanup_allowed=False
                try:
                    details=adapter.read_choice_replies(session,list(selection_forms(session['options'])),keep_open=True)
                    cleanup_allowed=True
                    if self.should_stop():return []
                    adapter.check_intervention()
                    if self._fresh_root(session) is None:self._hold(session);return []
                    ask_proof=getattr(adapter,'last_choice_ask_proof',None)
                    if ask_proof is not None:
                        self.store.record_ask_outcome(session['choice_id'],ask_ref=session['ask_ref'],outcome='b_view_visible',
                            ask_reply_ref=ask_proof.get('ask_reply_ref'),proof=ask_proof)
                    session=self.store.get(session['choice_id'])
                    if session['state']!='waiting_library':return []
                    if not isinstance(details,list) or len(details)>1:raise ReceiverDetailError('选库回复未能唯一核对',{})
                    if not details:
                        adapter.check_intervention()
                        if not self.should_stop():self.store.defer_waiting_recheck(session['choice_id'],seconds=45)
                        return []
                    detail=details[0];proof=detail.get('evidence',{})
                    if not self._fresh_time(proof):raise ReceiverDetailError('选库回复证据已过期',{})
                    selected=parse_library_selection(proof.get('selection_value',detail.get('command_text')),session['options'])
                    if selected is None:
                        adapter.check_intervention()
                        if not self.should_stop():self.store.defer_waiting_recheck(session['choice_id'],seconds=45)
                        return []
                    binding=self.registry.authorize_snapshot(selected['authorization'],action='import')
                    if binding is None:return []
                    candidate={'source_profile_url':detail['sender_profile_url'],'exact_text':detail['command_text'],
                        'semantic_fingerprint':'choice-public-observation:'+_digest({'choice_id':session['choice_id'],
                            'text':detail['command_text'],'native':detail.get('native_comment_id')})}
                    envelope=make_choice_envelope(candidate,detail,binding,source_mode=session['root_envelope']['source_mode'])
                    envelope['evidence']['observed_at_utc']=proof['observed_at_utc']
                    if self._fresh_root(session) is None:self._hold(session);return []
                    # Native source identity may have aged while opening and
                    # reading this actual thread. Recheck it before reserving a
                    # choice, so a failure cannot leave a half-staged selection.
                    if source_checkpoint is not None:
                        if source_checkpoint(deepcopy(envelope)) is not True:return []
                        if (self.should_stop() or not self._fresh_time(proof)
                                or self._fresh_root(session) is None):return []
                    reserved=self.store.reserve_selection(session['choice_id'],expected_revision=session['revision'],
                        selection_ref=envelope['reference_id'],selection_envelope=envelope,selected_kb_id=selected['knowledge_base_id'],
                        selected_authorization=selected['authorization'])
                    return self._stage(reserved,run_id) or []
                except ReceiverInterventionRequired:raise
                except ReceiverDetailError:
                    cleanup_allowed=True
                    if self.should_stop():return []
                    adapter.check_intervention()
                    if self.should_stop():return []
                    self.store.defer_waiting_recheck(session['choice_id'],seconds=120)
                    self.last_issue_code='choice_original_not_verified';return []
                except ReceiverPageError:raise
                finally:
                    if cleanup_allowed and not self.should_stop():
                        adapter.check_intervention()
                        if not self.should_stop():adapter.page.keyboard.press('Escape')
            return []

    def known_selection_notice(self,notice,detail,adapter):
        """Do not infer notification identity from body/cover or a stale modal."""
        self._notice_recheck_key=None
        if self.should_stop() or not isinstance(detail,dict) or detail.get('ok') is not True:return False
        if (detail.get('unique_count')!=1 or type(detail.get('unique_count')) is not int
                or detail.get('sender_profile_url')!=notice.get('source_profile_url')
                or detail.get('receiver_profile_url')!=adapter.expected_profile_url
                or any(detail.get(name,detail.get('evidence',{}).get(name)) is not True
                       for name in ('sender_verified','video_verified'))
                or ''.join(detail.get('command_text','').split())!=''.join(notice.get('exact_text','').split())):return False
        observed=self.store.list_observed_selections(adapter.expected_profile_url,sender=detail['sender_profile_url'],video_id=detail['video_id'])
        for session in observed:
            saved=session['selection_envelope']
            if saved['comment_text']!=detail['command_text']:continue
            if saved['native_comment_id'] is not None and detail.get('native_comment_id')!=saved['native_comment_id']:continue
            incoming=detail.get('evidence',{})
            if incoming.get('source_kind')=='library_choice_reply' and any(incoming.get(key)!=session[name] for key,name in (
                    ('choice_id','choice_id'),('ask_ref','ask_ref'),('ask_reply_ref','ask_reply_ref'),('ask_text','ask_text'))):
                continue
            if incoming.get('source_kind')=='library_choice_reply' and (incoming.get('root_reference_id')!=session['root_envelope']['reference_id']
                    or incoming.get('root_comment_text')!=session['root_envelope']['comment_text']):continue
            self._notice_recheck_key=self._notice_key(notice,detail)
            if detail.get('native_comment_id') is None and incoming.get('source_kind')!='library_choice_reply':
                # Another parent can show the same A/body/video in another
                # frame. A later view of this known root cannot identify that
                # earlier UI-only source. Keep it unconsumed and unexecuted.
                continue
            frozen=self._frozen_selected_resolution(session)
            if frozen is None:continue
            binding=frozen.binding
            if incoming.get('source_kind')=='library_choice_reply':
                try:
                    if not self._fresh_time(incoming):continue
                    identified=make_choice_envelope({'source_profile_url':detail['sender_profile_url'],
                        'exact_text':detail['command_text'],'semantic_fingerprint':'choice-current-detail'},detail,binding,
                        source_mode=saved['source_mode'])
                    validate_choice_envelope(identified,binding,root_binding=session['root_binding'],
                        root_envelope=session['root_envelope'],ask_ref=session['ask_ref'],ask_reply_ref=session['ask_reply_ref'],
                        ask_text=session['ask_text'],choice_id=session['choice_id'])
                except (ValueError,TypeError,KeyError):continue
            adapter.check_intervention()
            try:details=adapter.observe_current_choice_replies(session,list(selection_forms(session['options'])))
            except ReceiverDetailError:continue
            if self.should_stop():return False
            if not isinstance(details,list) or len(details)!=1:continue
            item=details[0];proof=item.get('evidence',{})
            if not self._fresh_time(proof):continue
            # Deduplication is a source fact even after permission revocation.
            candidate={'source_profile_url':item['sender_profile_url'],'exact_text':item['command_text'],
                'semantic_fingerprint':'choice-current-only-observation'}
            try:
                fresh=make_choice_envelope(candidate,item,binding,source_mode=saved['source_mode'])
                validate_choice_envelope(fresh,binding,root_binding=session['root_binding'],root_envelope=session['root_envelope'],
                    ask_ref=session['ask_ref'],ask_reply_ref=session['ask_reply_ref'],ask_text=session['ask_text'],choice_id=session['choice_id'])
            except (ValueError,KeyError,TypeError):continue
            if (fresh['comment_text']!=saved['comment_text'] or fresh['video_id']!=saved['video_id']
                    or detail.get('native_comment_id')!=fresh['native_comment_id']):continue
            ref=choice_reference_id(sender=fresh['sender_profile_url'],receiver=fresh['receiver_profile_url'],
                video_id=fresh['video_id'],native_comment_id=saved['native_comment_id'],comment_text=fresh['comment_text'],
                root_reference_id=session['root_envelope']['reference_id'],ask_reply_ref=session['ask_reply_ref'])
            if ref==saved['reference_id']:
                self._notice_recheck_key=None
                return True
        return False
