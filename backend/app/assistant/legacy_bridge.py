"""Connect the daily queue to the already verified model/import/send barriers.

The model lane never receives a page. The publication lane is called only by
the browser owner. Receipt reconciliation performs GETs, never a second import.
"""
from __future__ import annotations

from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
import re
import sqlite3
import threading
import time

from app.mention_agent.contracts import Binding
from app.mention_agent.imports import BackendImportClient, ImportTools, import_status_proposal
from app.mention_agent.publisher import ReplyPublisher
from app.mention_agent.service import AgentService, configured_database, open_store
from app.mention_agent.store import AgentStoreError
from .backend_knowledge import BackendKnowledgeTools
from .commands import CommandResolver
from .conversation import ConversationStore


@contextmanager
def _followup_owned_ro(store):
    """Existing owned ledgers only; never an ordinary RW getter/transaction."""
    from app.storage_guard import verified_readonly_connection
    with store._file_lock():
        store._guard()
        before = store.path.stat()
        with verified_readonly_connection(store.path, store._owned, guard=store._guard) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            if not db.in_transaction:
                db.execute('BEGIN')
            yield db
            store._guard()
            after = store.path.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError('followup_anchor_store_changed')


def recorded_usage(steps, *, elapsed_ms=None):
    """Only sum provider-reported fields; an absent field never means zero."""
    responses = [step for step in steps if step['kind'] == 'model_response']
    result = {'model_responses':len(responses)}
    if type(elapsed_ms) is int and elapsed_ms >= 0:
        result['elapsed_ms'] = elapsed_ms
    names = {'input_tokens':('prompt_tokens','input_tokens'),
             'output_tokens':('completion_tokens','output_tokens'),
             'total_tokens':('total_tokens',)}
    for target, aliases in names.items():
        values = []
        for step in responses:
            usage = step.get('metadata',{}).get('usage',{})
            value = next((usage[key] for key in aliases if isinstance(usage,dict) and key in usage), None)
            if type(value) is not int or value < 0:
                break
            values.append(value)
        if responses and len(values) == len(responses):
            result[target] = sum(values)
    return result


def merge_recorded_model_steps(*groups):
    """Join exact request identities, never every request in a shared scope."""
    result, seen = [], {}
    for group in groups:
        for original in group:
            step = {**original, 'metadata': dict(original.get('metadata', {}))}
            key = step['metadata'].get('budget_request_id')
            identity = (step['kind'], key) if step['kind'] in {'model_request', 'model_response'} and type(key) is str and key else None
            if identity is not None and identity in seen:
                prior = result[seen[identity]]
                if step['kind'] == 'model_response' and prior['metadata'].get('usage') != step['metadata'].get('usage'):
                    # Conflicting records are unknown, never cherry-picked or
                    # double-counted provider measurements.
                    prior['metadata']['usage'] = {}
                continue
            if identity is not None:
                seen[identity] = len(result)
            result.append(step)
    return result


def recorded_stage_timings(steps, saved=None):
    """Count only complete measured groups, never task timestamp differences."""
    result = {}
    saved = saved if isinstance(saved, dict) else {}
    value = saved.get('execution_ms')
    if type(value) is int and value >= 0:
        result['execution_ms'] = value
    groups = (
        ('model_ms', 'model_request', 'model_response', None),
        ('retrieval_ms', 'tool_call', 'tool_result', 'query_knowledge_base'),
    )
    for field, start_kind, end_kind, tool in groups:
        def selected(step, kind):
            return (step['kind'] == kind and (tool is None
                    or step.get('metadata', {}).get('tool_name') == tool))
        requests = [step for step in steps if selected(step, start_kind)]
        responses = [step for step in steps if selected(step, end_kind)]
        values = [step.get('metadata', {}).get('elapsed_ms') for step in responses]
        if requests and len(requests) == len(responses) and all(type(n) is int and n >= 0 for n in values):
            result[field] = sum(values)
    return result


def video_processing_timing(value, job):
    """An optional receipt projection is never allowed to change the job scope."""
    if (not isinstance(value, dict) or not isinstance(job, dict)
            or set(value) != {'run_id','request_id','video_id','stages_seconds'}
            or value.get('request_id') != job.get('job_id')
            or value.get('video_id') != job.get('video_id')
            or not isinstance(value.get('run_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value['run_id'])):
        return None
    stages = value.get('stages_seconds')
    def valid_seconds(number):
        try:
            return type(number) in {int,float} and math.isfinite(number) and number >= 0
        except (ValueError, OverflowError):
            return False
    if (not isinstance(stages, dict) or not stages or set(stages)-{'download','recognition','indexing'}
            or any(not valid_seconds(n) for n in stages.values())):
        return None
    return {**{key:value[key] for key in ('run_id','request_id','video_id')}, 'stages_seconds':dict(stages)}


class LegacyBridge:
    def __init__(self, registry, queue, control, *, agent_path, conversation=None, adapter=None,
                 database=None, import_client=None, model_factory=None, knowledge_factory=None,
                 admission_lock=None, should_stop=None, usage_budget=None, choice_store=None,
                 choice_controller=None, source_authorizer=None, semantic_store=None,
                 command_resolver=None):
        if source_authorizer is not None and not callable(source_authorizer):
            raise ValueError('invalid_source_authorizer')
        self.source_authorizer = source_authorizer
        self.registry, self.queue, self.control = registry, queue, control
        self.database = Path(database or configured_database())
        self.agent_path = Path(agent_path).absolute()
        if not self.agent_path.exists() and any(queue.counts().values()):
            raise ValueError('prior_agent_ledger_missing')
        self.agent = open_store(self.agent_path, self.database)
        self.conversation = conversation or ConversationStore(queue.path.parent/'conversation.sqlite3',
            protected_paths=(self.database, self.agent_path))
        self.adapter = adapter
        self.browser_owner_thread_id = threading.get_ident()
        self.import_client = import_client or BackendImportClient()
        self.model_factory = model_factory
        self.knowledge_factory = knowledge_factory
        self.usage_budget = usage_budget
        self.semantic_store = semantic_store
        self.resolver = command_resolver or CommandResolver(registry)
        self._followup_anchor_guard = threading.local()
        if usage_budget is not None:
            from .usage_model import sync_existing_usage
            sync_existing_usage(usage_budget, self.agent,
                trusted_scope_for_task=semantic_store.agent_budget_scope if semantic_store is not None else None)
        self.admission_lock = admission_lock or threading.RLock()
        self.should_stop = should_stop or (lambda: self.control.snapshot().get('desired_state') == 'stopped')
        self.choices = choice_controller
        if self.choices is None:
            from .library_choice import LibraryChoiceStore, MARKER_NAME
            choice_path = queue.path.parent / 'library-choice.sqlite3'
            if choice_store is None and (choice_path.exists() or choice_path.is_symlink()
                    or (choice_path.parent / MARKER_NAME).exists()):
                choice_store = LibraryChoiceStore(choice_path, protected_paths=(self.database, self.agent_path, queue.path))
            if choice_store is not None:
                from .choice_controller import ChoiceController
                self.choices = ChoiceController(registry, queue, self.agent, choice_store,
                    conversation=self.conversation, should_stop=self.should_stop,
                    admission_lock=self.admission_lock)

    def _binding(self, task):
        return Binding.from_dict(task['binding'])

    def _authorized(self, task):
        binding = self._binding(task)
        if self.should_stop() or not self.agent_path.is_file():
            return False
        if self.source_authorizer is not None:
            try:
                if self.source_authorizer(task) is not True:
                    return False
            except Exception:
                return False
        authorization = task.get('authorization')
        fresh = (self.registry.authorize_snapshot(authorization)
                 if isinstance(authorization, dict) else None)
        authorized = fresh == binding and binding.active
        proof = task.get('envelope', {}).get('evidence', {})
        if authorized and proof.get('source_kind') == 'conversation_followup':
            try:
                from app.mention_agent.followup_evidence import validate_followup_envelope
                validate_followup_envelope(task['envelope'], binding)
                anchor = self._followup_root_anchor(proof['root_task_id'])
                authorized = bool(self.semantic_store is not None and anchor is not None
                    and anchor['root_agent_task_id'] == proof['root_agent_task_id']
                    and anchor['prior_reply_sha256'] == proof['prior_reply_sha256']
                    and anchor['root_envelope']['reference_id'] == proof['root_reference_id']
                    and anchor['root_envelope']['comment_text'] == proof['root_comment_text']
                    and all(anchor['root_envelope'][key] == task['envelope'][key] for key in
                            ('sender_profile_url','receiver_profile_url','video_id'))
                    and task.get('kind') != 'save')
            except Exception:
                authorized = False
        if authorized and self.choices is not None:
            authorized = self.choices.authority_current(task)
        if authorized and self.semantic_store is not None:
            try:
                authorized = self._semantic_permission_current(task)
            except Exception:
                authorized = False
        return authorized

    def _resolution(self, task):
        if not self._authorized(task):
            return None
        if task['envelope'].get('evidence', {}).get('source_kind') == 'library_choice_reply':
            return self.choices.resolution_for_selection(task) if self.choices is not None else None
        thread=self.conversation.task_scope(task['id'])
        proof=json.loads(thread['proof']) if thread else {}
        preferred=task['binding_id'] if proof.get('quality')=='verified_ancestor' else None
        try:
            receipt = self._semantic_receipt(task)
            if task['envelope'].get('evidence', {}).get('source_kind') == 'conversation_followup' and receipt is None:
                return None
            resolution = (self.resolver.resolve_verified_intent(task['envelope'], receipt, preferred_binding_id=preferred)
                if receipt is not None else self.resolver.resolve(task['envelope'], preferred_binding_id=preferred))
        except Exception:
            return None
        return resolution if (resolution.binding == self._binding(task)
                              and resolution.authorization == task.get('authorization')) else None

    def _semantic_receipt(self, task):
        if self.semantic_store is None:
            return None
        return self.semantic_store.get_recorded_decision(task['envelope'],
            binding=self._binding(task), queue_task_id=task['id'])

    def _semantic_owner_scope(self, task):
        binding, envelope = self._binding(task), task['envelope']
        proof = envelope.get('evidence', {})
        return {'user_id': task['user_id'], 'device_id': task['device_id'], 'instance_id': binding.instance_id,
            'sender_profile_url': binding.sender_profile_url, 'receiver_profile_url': binding.receiver_profile_url,
            'video_id': envelope['video_id'], 'reference_id': envelope['reference_id'],
            'root_reference_id': (proof['root_reference_id'] if proof.get('source_kind') == 'library_choice_reply'
                                  else envelope['reference_id'])}

    def _semantic_accounting(self, task):
        if self.semantic_store is None:
            return None
        owner = self._semantic_owner_scope(task)
        record = self.semantic_store.get_recorded_accounting(task['envelope'], binding=self._binding(task),
            queue_task_id=task['id'], owner_scope=owner)
        if record is not None and (type(record) is not dict or record.get('dispatchable') is not False
                or record.get('owner_scope') != owner
                or record.get('state') not in {'reserved', 'unknown', 'held', 'budget_blocked', 'complete'}
                or type(record.get('request_key')) is not str or not record['request_key']
                or record.get('request_keys') != [record['request_key']]):
            raise ValueError('semantic_accounting_scope_conflict')
        return record

    def _semantic_permission_current(self, task):
        """A cost observation can only deny execution, never authorize it."""
        accounting, receipt = self._semantic_accounting(task), self._semantic_receipt(task)
        if accounting is None:
            return receipt is None and task['envelope'].get('evidence', {}).get('source_kind') != 'conversation_followup'
        return bool(accounting['state'] == 'complete' and receipt is not None
            and receipt.plan_id == accounting['plan_id']
            and receipt.budget_scope_id == accounting['budget_scope_id']
            and receipt.request_key in accounting['request_keys'])

    def _followup_root_anchor(self, task_id):
        """One published B reply and its actual real-@ ancestor, no effects."""
        if type(task_id) is not str or not re.fullmatch(r'[a-f0-9]{32}', task_id):
            return None
        previous = getattr(self._followup_anchor_guard, 'tasks', ())
        if task_id in previous or len(previous) >= 8:
            return None
        self._followup_anchor_guard.tasks = (*previous, task_id)
        try:
            with _followup_owned_ro(self.queue) as db:
                row = db.execute('SELECT * FROM assistant_tasks WHERE id=?', (task_id,)).fetchone()
                task = self.queue._raw(row)
            if (task is None or task['state'] != 'b_view_visible' or not task.get('agent_task_id')
                    or not self._authorized(task) or not self._binding(task).public_reply_allowed):
                return None
            envelope, binding = task['envelope'], self._binding(task)
            if envelope.get('source_mode') != 'live_ui':
                return None
            ancestor = envelope
            proof = envelope.get('evidence', {})
            if proof.get('source_kind') in {'library_choice_reply', 'conversation_followup'}:
                reference = proof.get('root_reference_id')
                with _followup_owned_ro(self.queue) as db:
                    ancestors = db.execute("SELECT * FROM assistant_tasks WHERE json_extract(envelope,'$.reference_id')=? AND json_extract(envelope,'$.source_mode')='live_ui' AND json_extract(envelope,'$.sender_profile_url')=? AND json_extract(envelope,'$.receiver_profile_url')=? AND json_extract(envelope,'$.video_id')=?",
                        (reference, envelope['sender_profile_url'], envelope['receiver_profile_url'], envelope['video_id'])).fetchall()
                    if len(ancestors) != 1:
                        return None
                    original = self.queue._raw(ancestors[0])
                if (original['user_id'] != task['user_id'] or original['device_id'] != task['device_id']
                        or original['binding']['instance_id'] != binding.instance_id):
                    return None
                ancestor = original['envelope']
            actual = ancestor.get('evidence', {})
            if (actual.get('real_mention') is not True or actual.get('sender_verified') is not True
                    or actual.get('video_verified') is not True or type(actual.get('unique_count')) is not int
                    or actual['unique_count'] != 1 or ancestor.get('source_mode') != 'live_ui'
                    or actual.get('source_kind') in {'library_choice_reply','conversation_followup'}):
                return None
            with _followup_owned_ro(self.agent) as db:
                agent = db.execute('SELECT task_id,binding_id,envelope,state FROM agent_tasks WHERE task_id=?', (task['agent_task_id'],)).fetchone()
                reply = db.execute('SELECT reply_id,proposal,target,state,created_at,send_token IS NOT NULL AS reserved FROM agent_replies WHERE task_id=?', (task['agent_task_id'],)).fetchone()
                active_binding = db.execute('SELECT payload FROM agent_bindings WHERE binding_id=?', (task['binding_id'],)).fetchone()
                if (agent is None or reply is None or active_binding is None or not reply['reserved']
                        or agent['binding_id'] != task['binding_id']
                        or json.loads(active_binding[0]) != binding.to_dict()):
                    return None
                from app.mention_agent.store import _canonical_reference
                if _canonical_reference(json.loads(agent['envelope'])) != _canonical_reference(envelope):
                    return None
                if reply['state'] == 'delivery_unknown':
                    if agent['state'] != 'delivery_unknown' or self.agent._checked_reply_readback(db, task['agent_task_id']) is None:
                        return None
                elif reply['state'] != 'b_view_visible' or agent['state'] != 'b_view_visible':
                    return None
                proposal = json.loads(reply['proposal'])
                text = proposal.get('text')
                if proposal.get('kind') not in {'answer','import_status'} or type(text) is not str or not 1 <= len(text) <= 8000:
                    return None
                target = json.loads(reply['target'])
                expected = {k:v for k,v in envelope.items() if k != 'evidence'}
                expected.update(binding_id=binding.binding_id, instance_id=binding.instance_id, knowledge_base_id=binding.knowledge_base_id)
                if binding.is_all_local:
                    expected['query_scope'] = 'all_local'
                if proof.get('source_kind') == 'library_choice_reply':
                    expected['choice_evidence'] = proof
                elif proof.get('source_kind') == 'conversation_followup':
                    expected['followup_evidence'] = proof
                if target != expected:
                    return None
            return {'root_task_id':task['id'], 'root_agent_task_id':task['agent_task_id'],
                'root_envelope':deepcopy(ancestor), 'prior_reply_text':text,
                'prior_reply_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'prior_reply_native_comment_id':None, 'instance_id':binding.instance_id,
                'user_id':task['user_id'], 'device_id':task['device_id']}
        except Exception:
            return None
        finally:
            self._followup_anchor_guard.tasks = previous

    def followup_anchor_current(self, root_task_id, prior_reply_sha256):
        if type(prior_reply_sha256) is not str or not re.fullmatch(r'[a-f0-9]{64}', prior_reply_sha256):
            return False
        anchor = self._followup_root_anchor(root_task_id)
        return anchor is not None and anchor['prior_reply_sha256'] == prior_reply_sha256

    def followup_anchors(self, candidate):
        """At most three same-owner published locators; never infer DOM parent."""
        try:
            if type(candidate) is not dict or candidate.get('source_kind') != 'native_reply_notice_candidate':
                return []
            sender, receiver, video = (candidate[k] for k in ('sender_profile_url','receiver_profile_url','video_id'))
            hint = candidate.get('parent_id_hint')
            if type(hint) is not str or not re.fullmatch(r'[1-9][0-9]{0,29}', hint):
                return []
            snapshot = self.registry.snapshot()
            users = [u for u in snapshot['users'] if u['sender_profile_url'] == sender and u['receiver_profile_url'] == receiver
                     and u.get('active') is True and u.get('verification_status') == 'verified']
            if len(users) != 1:
                return []
            user = users[0]
            devices = [d for d in user['devices'] if d.get('is_local') is True and d.get('enabled') is True
                       and not d.get('revoked') and d.get('verification_status') == 'verified'
                       and (not user.get('default_device_id') or d['device_id'] == user['default_device_id'])]
            if len(devices) != 1:
                return []
            device = devices[0]
            with _followup_owned_ro(self.queue) as db:
                roots = [self.queue._raw(r) for r in db.execute("SELECT * FROM assistant_tasks WHERE state='b_view_visible' AND user_id=? AND device_id=? AND json_extract(binding,'$.instance_id')=? AND json_extract(envelope,'$.sender_profile_url')=? AND json_extract(envelope,'$.receiver_profile_url')=? AND json_extract(envelope,'$.video_id')=? ORDER BY updated_at DESC LIMIT 24",
                    (user['user_id'], device['device_id'], device['instance_id'], sender, receiver, video))]
            result = []
            for task in roots:
                anchor = self._followup_root_anchor(task['id'])
                if anchor is None:
                    continue
                native = anchor['root_envelope'].get('native_comment_id')
                if native != hint:
                    # A UI-null native ID may use only this root's already
                    # admitted native row as a locator, never unrelated history.
                    from .native_source_store import NativeSourceStore
                    path = self.queue.path.parent/'native-receiver'/'sources.sqlite3'
                    if not path.is_file():
                        continue
                    native_store = NativeSourceStore(path, candidate['recipient_uid'], receiver)
                    with native_store._connection() as db:
                        rows = db.execute("SELECT cid,candidate FROM sources WHERE state='admitted' AND historical=0 AND (task_id=? OR reference_id=?)",
                            (task['id'], anchor['root_envelope']['reference_id'])).fetchall()
                        matches = [r for r in rows if r[0] == hint and json.loads(r[1]).get('sender_profile_url') == sender
                                   and json.loads(r[1]).get('receiver_profile_url') == receiver
                                   and json.loads(r[1]).get('video_id') == video]
                        if len(matches) != 1:
                            continue
                result.append(anchor)
                if len(result) == 3:
                    break
            return result
        except Exception:
            return []

    def _semantic_steps(self, task):
        """Exact comment billing, including paid but non-executable plans."""
        accounting = self._semantic_accounting(task)
        if accounting is None:
            return []
        def stamp(row, name):
            value = row.get(name) if row else None
            if type(value) is not str:
                return None
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            except (ValueError, OverflowError):
                return None
        steps = []
        for key in accounting['request_keys']:
            row = self.usage_budget.get_request(key) if self.usage_budget is not None else None
            if row is not None and (row['task_id'] != accounting['budget_scope_id'] or row.get('origin') != 'live'):
                raise ValueError('semantic_budget_scope_conflict')
            common = {'budget_request_id': key, 'phase': 'semantic_planning', 'name': '理解指令'}
            steps.append({'kind': 'model_request', 'metadata': {**common, 'status': accounting['state']},
                          'created_at': stamp(row, 'admitted_at_utc')})
            if row is not None and (row['state'] == 'settled' or row.get('outcome') == 'completed'):
                usage = ({name: row[name] for name in ('input_tokens', 'output_tokens', 'total_tokens')
                          if row.get(name) is not None} if row['state'] == 'settled' else {})
                steps.append({'kind': 'model_response', 'metadata': {**common, 'status': accounting['state'], 'usage': usage},
                              'created_at': stamp(row, 'updated_at_utc')})
        return merge_recorded_model_steps(steps)

    def _find(self, task):
        if task.get('agent_task_id'):
            result = self.agent.get_task(task['agent_task_id'])
            if result is None:
                raise ValueError('linked_agent_task_missing')
            if result['binding_id'] != task['binding_id']:
                raise ValueError('agent_scope_conflict')
            expected=self.agent.lookup_existing(task['binding_id'],task['envelope'])
            if expected is None or expected['task_id']!=result['task_id']:
                raise ValueError('agent_original_comment_conflict')
            return result
        return self.agent.lookup_existing(task['binding_id'], task['envelope'])

    @staticmethod
    def _details(reply, steps, name):
        allowed = {'round','tool_name','name','status','source_count','elapsed_ms','knowledge_base_id',
                   'code','error_type','http_status','source_ids','kind','count'}
        public_steps = []
        semantic = [step for step in steps if step.get('metadata', {}).get('phase') == 'semantic_planning'
                    and step['kind'] == 'model_response']
        display = (semantic[-1:] + [step for step in steps
            if step.get('metadata', {}).get('phase') != 'semantic_planning'][-31:]) if semantic else steps[-32:]
        for step in display:
            if step.get('metadata', {}).get('phase') == 'semantic_planning' and step['kind'] == 'model_request':
                continue  # One visible planning step; its response carries usage.
            metadata = {key:value for key,value in step['metadata'].items() if key in allowed}
            public_kind = step['kind']
            if public_kind == 'reply_readback_confirmation':
                public_kind = 'decision'
                metadata = {'kind':'reply_readback_confirmation','status':'b_view_visible'}
            elif step.get('metadata', {}).get('phase') == 'semantic_planning':
                public_kind = 'decision'
                metadata = {'kind': 'semantic_planning', 'name': '理解指令',
                    'status': step['metadata'].get('status')}
                measured = recorded_usage([step])
                tokens = {key: measured[key] for key in ('input_tokens', 'output_tokens', 'total_tokens') if key in measured}
                if tokens:
                    metadata['usage'] = tokens
            public_steps.append({'kind':public_kind, 'metadata':metadata, 'created_at':step['created_at']})
        return {'reply_text':reply['text'] if reply else '', 'library_name':name,
                'sources':reply['sources'] if reply else [], 'steps':public_steps}

    def _snapshot(self, task, *, override=None):
        binding = self._binding(task)
        authorized = self._authorized(task)
        old = self._find(task)
        semantic_steps = self._semantic_steps(task)
        if old is None:
            result = {'state':'queued' if authorized else 'needs_review',
                'reason_code':None if authorized else 'binding_changed', 'model_started':False,
                'ingestion_submitted':False, 'send_token_present':False, 'safe_to_execute':authorized,
                'safe_to_publish':False, 'binding_active':authorized,
                'public_reply_allowed':authorized and binding.public_reply_allowed}
            if semantic_steps:
                result.update(usage=recorded_usage(semantic_steps), details=self._details(None, semantic_steps, ''))
            return self.choices.decorate_result(task, result) if self.choices is not None else result
        task_id = old['task_id']
        reply, steps, job = self.agent.get_reply(task_id), self.agent.list_steps(task_id), self.agent.get_task_ingestion(task_id)
        saved_timings = task.get('details',{}).get('timings',{})
        timings = recorded_stage_timings(steps, saved_timings)
        processing = video_processing_timing(saved_timings.get('video_processing') if isinstance(saved_timings,dict) else None, job)
        if processing is not None:
            timings['video_processing'] = processing
        display_steps = merge_recorded_model_steps(semantic_steps, steps)
        usage = recorded_usage(display_steps, elapsed_ms=timings.get('execution_ms'))
        name = ''
        grant = self.registry.get_grant(task.get('authorization',{}).get('grant_id')) if task.get('authorization') else None
        if grant:
            name = grant['name']
        if binding.is_all_local:
            name = '全部入库资料'
        model_started = any(step['kind'] == 'model_request' for step in steps)
        submitted = bool(job and job.get('dispatch_token'))
        token_present = bool(reply and reply['send_token'])
        readback = None
        readback_getter = getattr(self.agent, 'get_reply_readback', None)
        if token_present and callable(readback_getter):
            readback = readback_getter(task_id)
        state = override or old['state']
        if state == 'send_reserved':
            state = 'delivery_unknown'
        if state == 'answer_ready' and job and job['state'] in {'accepted','processing','reserved','submission_unknown'}:
            state = 'waiting_ingestion'
        if state == 'answer_ready' and not binding.public_reply_allowed:
            state = 'completed_private'
        if readback is not None:
            state = 'b_view_visible'
        if not authorized and state not in {'b_view_visible','delivery_unknown','verification_required','failed_no_submit'}:
            state = 'needs_review'
        result = {'state':state, 'agent_task_id':task_id, 'reason_code':old.get('reason_code'),
            'usage':usage, 'details':self._details(reply,display_steps,name), 'model_started':model_started,
            'ingestion_submitted':submitted, 'send_token_present':token_present,
            'safe_to_execute':authorized and old['state']=='queued' and not model_started and not submitted and not token_present,
            'safe_to_publish':authorized and binding.public_reply_allowed and old['state']=='answer_ready'
                              and bool(reply and reply['state']=='ready') and not token_present
                              and (not job or job['state'] in {'succeeded','failed','failed_no_submit'}),
            'binding_active':authorized, 'public_reply_allowed':authorized and binding.public_reply_allowed}
        if timings:
            result['details']['timings'] = timings
        if not authorized:
            result['reason_code'] = 'binding_changed'
        if job:
            result['import_state'] = job['state']
        if reply:
            result['reply_state'] = 'b_view_visible' if readback is not None else reply['state']
        original_preflight_code = old.get('reason_code')
        fresh_target_recovery = bool(override is None and authorized and binding.public_reply_allowed and
            old['state']=='needs_review' and original_preflight_code=='original_notification_not_unique'
            and reply and reply['state']=='needs_review' and not token_present
            and not self.agent.list_reply_hold_releases(task_id)
            and self.control.snapshot().get('receiver_state')=='ready')
        if fresh_target_recovery:
            # The old HTTP service already understands this recoverable class.
            # Keep the exact failure in the Agent ledger and public detail;
            # only normalize the product recovery category, never its proof.
            result['reason_code'] = 'preflight_failed'
            result['details']['steps'] = result['details'].get('steps', [])[-31:] + [{
                'kind':'error','metadata':{'code':original_preflight_code},
                'created_at':old.get('updated_at')}]
        result['can_operator_resume'] = bool(authorized and binding.public_reply_allowed and
            old['state']=='needs_review' and old.get('reason_code') in {'preflight_failed','preflight_verification_required'} and reply and
            reply['state']=='needs_review' and not token_present and
            not self.agent.list_reply_hold_releases(task_id)
            and (old.get('reason_code')!='preflight_verification_required'
                 or self.control.snapshot().get('receiver_state')=='ready'))
        if fresh_target_recovery:
            result['can_operator_resume'] = True
        if authorized and self._can_resume_budget(task, old):
            result['can_operator_resume'] = True
        result['can_operator_retry_query'] = bool(authorized and task.get('kind')=='query'
            and not task.get('publication_token') and old['state']=='needs_review'
            and old.get('reason_code')=='knowledge_query_failed' and not reply and job is None
            and self._resolution(task) is not None and self.agent.can_retry_failed_query(task_id))
        return self.choices.decorate_result(task, result) if self.choices is not None else result

    def _can_resume_budget(self, task, old):
        if (self.usage_budget is None or old.get('state') != 'needs_review'
                or old.get('reason_code') != 'model_budget_exceeded'
                or self.control.snapshot().get('desired_state') != 'running'):
            return False
        try:
            current = self.queue.get_task(task['id'])
            if (current is None or current['state'] not in {'needs_review', 'needs_reconcile'}
                    or current.get('lease_until') is not None or current.get('publication_token')
                    or current.get('agent_task_id') != old['task_id']
                    or self._resolution(current) is None):
                return False
            scope = (self.semantic_store.agent_budget_scope(old['task_id'], envelope=old['envelope'], binding=self._binding(task))
                     if self.semantic_store is not None else None)
            totals = self.usage_budget.summary(scope or old['task_id'])['task']
            return totals['request_count'] == 0 and self.agent.can_resume_unstarted_budget(old['task_id'])
        except Exception:
            return False  # Unknown cross-ledger state is never permission.

    def execute(self, task):
        started = time.monotonic()
        with self.admission_lock:
            resolution = self._resolution(task)
            if resolution is None:
                return {'state':'needs_review','reason_code':'binding_changed'}
            binding = resolution.binding
            retry_action_id = task.get('_query_retry_action_id')
            if retry_action_id is not None and (resolution.action!='query' or not self.queue.query_retry_execution_current(
                    task['id'],retry_action_id,task.get('execution_token'))):
                return {'state':'needs_review','reason_code':'query_retry_scope_changed'}
            if self.conversation.task_scope(task['id']) is None:
                return {'state':'needs_review','reason_code':'comment_thread_not_captured'}
            history = self.conversation.context_for(task['id'],binding)
        if resolution.action == 'clarify':
            choice_intent = resolution.choice_intent or {}
            is_choice = choice_intent.get('kind') == 'save_library_choice'
            eligible = any(item.get('can_import') is True and isinstance(item.get('authorization'), dict)
                           for item in resolution.choice_options) if is_choice else False
            if is_choice and eligible and self.choices is None:
                return {'state': 'needs_review', 'reason_code': 'library_choice_store_not_initialized'}
            choice = self.choices.prepare_ask(task, resolution) if self.choices is not None else None
            if is_choice and eligible and choice is None:
                return {'state': 'needs_review', 'reason_code': 'library_choice_preparation_unconfirmed'}
            clarification = resolution.clarification or '请明确选择一个允许的知识库。'
            if is_choice and not eligible:
                from .choice_intent import choice_prompt
                clarification = choice_prompt(resolution.choice_options, catalog_count=resolution.catalog_count,
                                               catalog_scope=resolution.catalog_scope)
            self.agent.put_binding(binding.to_dict())
            old = self.agent.enqueue(binding.binding_id,task['envelope'])
            receipt = self._semantic_receipt(task)
            if receipt is not None:
                self.semantic_store.bind_agent_budget(receipt, old['task_id'], task['envelope'], binding)
            claim = self.agent.claim(old['task_id'])
            if claim is not None:
                self.agent.complete(old['task_id'],claim['claim_token'],
                    {'kind':'clarification','text':choice['ask_text'] if choice else clarification,
                     'source_ids':[],'sources':[]})
            return self._with_execution_time(self._snapshot(task), started)
        current_video = r'(?:这个|当前|这条|本条|本|该)\s*视频'
        body=task['envelope']['comment_text']
        focus=bool(re.search(current_video,body) and not re.search(
            r'(?:不要|不用|不必|别|不是|并非).{0,8}'+current_video+r'|对比|比较|另一个视频|其他视频|其它视频',body))
        knowledge=(self.knowledge_factory(binding) if self.knowledge_factory is not None else
                   BackendKnowledgeTools(self.database,binding.knowledge_base_id,task['id'],
                       focus_video_id=task['envelope']['video_id'] if focus and resolution.action=='query' else None,
                       execution_token=task['execution_token'], query_scope=binding.query_scope))
        options = {'knowledge':knowledge, 'database':self.database,
                   'allow_import':resolution.action=='save',
                   'scope_authorizer':lambda current: current==binding and self._authorized(task),
                   'allowed_library_names':tuple(resolution.library.get('accepted_names',resolution.library.get('aliases',[]))),
                   'conversation_context':history, 'allow_private_execution':True}
        if self.usage_budget is not None:
            options['usage_budget'] = self.usage_budget
        semantic_receipt = self._semantic_receipt(task)
        if semantic_receipt is not None:
            scope = semantic_receipt.budget_scope_id
            def link_budget(agent_task_id, envelope, current_binding, budget_scope_id):
                if current_binding != binding or envelope != task['envelope'] or budget_scope_id != scope:
                    return False
                self.semantic_store.bind_agent_budget(semantic_receipt, agent_task_id, envelope, current_binding)
                return True
            options.update(budget_scope_id=scope, budget_scope_linker=link_budget)
            options.update(trusted_semantic_decision=semantic_receipt,
                trusted_semantic_grant_id=resolution.library.get('grant_id'),
                trusted_semantic_owner_scope=self._semantic_owner_scope(task))
        if retry_action_id is not None:
            options['scope_authorizer'] = lambda current: (current==binding and self._authorized(task)
                and self.queue.query_retry_execution_current(task['id'],retry_action_id,task.get('execution_token')))
        if self.model_factory is not None:
            options['model'] = self.model_factory()
        if resolution.action == 'save':
            if task['envelope'].get('evidence', {}).get('source_kind') == 'library_choice_reply':
                proof = task['envelope']['evidence']
                options['trusted_save_selection'] = {
                    'choice_id': proof['choice_id'], 'root_comment_text': proof['root_comment_text'],
                    'selection_reference_id': task['envelope']['reference_id'],
                    'selected_library_name': resolution.library['name']}
            def explicit_save(envelope, current):
                fresh = self._resolution(task)
                return (current == binding and fresh is not None and fresh.action == 'save'
                        and envelope == task['envelope'])
            options.update(import_wait_seconds=0, import_should_stop=self.should_stop,
                automatic_import_receipt=True,
                explicit_save_check=explicit_save,
                import_factory=lambda *args,**kwargs: ImportTools(*args,**kwargs,client=self.import_client))
        process_options = {'query_retry_action_id':retry_action_id} if retry_action_id is not None else {}
        AgentService(self.agent,binding,**options).process(task['envelope'],video_title=task.get('video_title'),**process_options)
        result = self._with_execution_time(self._snapshot(task), started)
        if retry_action_id is not None:
            prior_ms = task.get('details',{}).get('timings',{}).get('execution_ms')
            if type(prior_ms) is int and prior_ms>=0:
                result['details']['timings']['execution_ms'] += prior_ms
                result['usage']['elapsed_ms'] = result['details']['timings']['execution_ms']
            else:
                # No measured first-attempt total: don't invent the full total.
                result['details']['timings'].pop('execution_ms',None)
                result['usage'].pop('elapsed_ms',None)
        return result

    @staticmethod
    def _with_execution_time(result, started):
        elapsed = max(0, int((time.monotonic()-started)*1000))
        result.setdefault('details',{}).setdefault('timings',{})['execution_ms'] = elapsed
        result.setdefault('usage',{})['elapsed_ms'] = elapsed
        return result

    def reconcile(self, task):
        old = self._find(task)
        if old is None or not self._authorized(task):
            return self._snapshot(task)
        job = self.agent.get_task_ingestion(old['task_id'])
        reply = self.agent.get_reply(old['task_id'])
        processing = None
        if job is not None and reply and reply['kind']=='import_status' and not reply['send_token']:
            try:
                response = self.import_client.status(job['job_id'])
            except Exception:
                response = None
            if response is None:
                result = self._snapshot(task)
                if (result.get('binding_active') is True and result['state'] not in
                        {'b_view_visible','delivery_unknown','verification_required','failed_no_submit'}):
                    result.update(state='waiting_ingestion',safe_to_publish=False,reason_code='ingestion_status_unavailable')
                return result
            try:
                expected = {'request_id':job['job_id'],'binding_id':job['binding_id'],
                    'instance_id':job['instance_id'],'video_id':job['video_id'],'knowledge_base_id':job['knowledge_base_id']}
                if any(response.get(key)!=value for key,value in expected.items()):
                    raise ValueError('receipt_scope_conflict')
                processing = video_processing_timing(response.get('video_processing'), job)
                evidence = {'backend_request_id':job['job_id'],'backend_state':response.get('state'),
                    **{key:response.get(key) for key in ('binding_id','instance_id','knowledge_base_id','video_id',
                                                       'membership_exists','cache_status','indexed_content_chunks')}}
                observed = self.agent.observe_ingestion(job['job_id'],evidence)
                if observed['state'] in {'succeeded','failed','failed_no_submit'} and reply['state']=='ready':
                    grant = self.registry.get_grant(task['authorization']['grant_id'])
                    if grant is None or not self._authorized(task):
                        raise ValueError('binding_changed')
                    self.agent.refresh_unsent_import_reply(old['task_id'],import_status_proposal(observed,grant['name']))
            except (ValueError,AgentStoreError):
                result = self._snapshot(task)
                if result['state'] not in {'b_view_visible','delivery_unknown','verification_required','failed_no_submit'}:
                    result.update(state='needs_review',safe_to_publish=False,reason_code='ingestion_receipt_conflict')
                return result
            except Exception:
                result = self._snapshot(task)
                if result.get('binding_active') is True and result['state'] not in {'b_view_visible','delivery_unknown','verification_required','failed_no_submit'}:
                    result.update(state='waiting_ingestion',safe_to_publish=False,reason_code='ingestion_status_unavailable')
                return result
        result = self._snapshot(task)
        if processing is not None:
            result['details'].setdefault('timings',{})['video_processing'] = processing
        return result

    def operator_resume(self, task):
        if task.get('reason_code') == 'model_budget_exceeded':
            with self.admission_lock:
                old = self._find(task)
                if old is None or not self._can_resume_budget(task, old):
                    raise ValueError('resume_not_safe')
                self.agent.resume_unstarted_budget(old['task_id'],
                    binding=self._binding(task).to_dict(), envelope=task['envelope'])
                return
        result = self._snapshot(task)
        if result.get('can_operator_resume') is True:
            verification=None
            old = self._find(task)
            needs_target_recheck = bool(old and old.get('reason_code')=='original_notification_not_unique')
            if result.get('reason_code')=='preflight_verification_required' or needs_target_recheck:
                if self.adapter is None or threading.get_ident()!=self.browser_owner_thread_id:
                    raise ValueError('browser_recheck_required')
                if self.should_stop() or (needs_target_recheck and self.control.snapshot().get('desired_state')!='running'):
                    raise ValueError('browser_recheck_required')
                reply=self.agent.get_reply(result['agent_task_id'])
                self.adapter.check_intervention()
                verification=self.adapter.prepare_reply(reply['target'],reply['text'])
                if self.should_stop():
                    raise ValueError('browser_recheck_required')
                self.adapter.check_intervention()
                if self.should_stop():
                    raise ValueError('browser_recheck_required')
                self.adapter.clear_prepared_reply(reply['target'],reply['text'])
                if (self.should_stop() or not self._authorized(task)
                        or (needs_target_recheck and self.control.snapshot().get('desired_state')!='running')):
                    raise ValueError('binding_changed')
            self.agent.release_held_reply(result['agent_task_id'],'operator_resume_preflight_failure',verification=verification)
            return
        # Unstarted work or a ready unsent draft needs no legacy reset at all.
        if not result.get('safe_to_execute') and not result.get('safe_to_publish'):
            raise ValueError('resume_not_safe')

    def inspect_existing_reply(self,task_id):
        """Explicit owner-only B-view inspection; never changes delivery state.

        Ordinary targets measure only the currently loaded original-thread
        DOM. A zero count is not proof of absence or A-side delivery. Choice
        targets retain their existing bounded, strict read-only observer.
        """
        import hashlib
        import sqlite3
        from datetime import timezone
        from app.storage_guard import verified_readonly_connection
        from app.mention_agent.store import _canonical_reference
        from app.mention_receiver.browser import ReceiverInterventionRequired
        diagnostic={'stage':'reply_inspect_scope','code':'checking','scope':'b_view_only',
                    'b_view_visible_now':False,'a_visibility_verified':False}
        self.last_reply_inspection_diagnostic=diagnostic
        lookup_started=False
        def paused():
            state=self.control.snapshot()
            return state.get('state')==state.get('desired_state')=='paused' and not self.should_stop()
        def frozen(task):
            """No AgentStore getter/BEGIN IMMEDIATE or schema initialization."""
            with self.agent._file_lock():
                self.agent._guard()
                with verified_readonly_connection(self.agent.path,self.agent._owned,guard=self.agent._guard) as db:
                    db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
                    identifier=task.get('agent_task_id')
                    if type(identifier) is not str:raise ValueError()
                    agent=db.execute('SELECT * FROM agent_tasks WHERE task_id=?',(identifier,)).fetchone()
                    reply=db.execute('SELECT * FROM agent_replies WHERE task_id=?',(identifier,)).fetchone()
                    binding=self.agent._binding_row(db,task['binding_id'])
                    if (agent is None or reply is None or binding!=self._binding(task).to_dict()
                            or agent['binding_id']!=task['binding_id'] or agent['state']!='b_view_visible'
                            or reply['state']!='b_view_visible' or type(reply['send_token']) is not str
                            or not re.fullmatch(r'[a-f0-9]{48}',reply['send_token'])):raise ValueError()
                    envelope=json.loads(agent['envelope'])
                    if _canonical_reference(envelope)!=_canonical_reference(task['envelope']):raise ValueError()
                    peers=self.agent._reference_rows(db,task['binding_id'],task['envelope']['reference_id'])
                    if len(peers)!=1 or peers[0]['task_id']!=identifier:raise ValueError()
                    direct=db.execute('SELECT task_id FROM agent_tasks WHERE command_id=?',(task['envelope']['command_id'],)).fetchone()
                    alias=self.agent._alias_task(db,task['envelope']['command_id'])
                    if any(row is not None and row['task_id']!=identifier for row in (direct,alias)):raise ValueError()
                    target=json.loads(reply['target']);proposal=json.loads(reply['proposal'])
                    evidence=json.loads(reply['evidence']) if reply['evidence'] else {}
                    expected={k:v for k,v in envelope.items() if k!='evidence'}
                    expected.update(binding_id=agent['binding_id'],instance_id=binding['instance_id'],
                                    knowledge_base_id=binding['knowledge_base_id'])
                    if binding.get('query_scope')=='all_local':expected['query_scope']='all_local'
                    if envelope.get('evidence',{}).get('source_kind')=='library_choice_reply':
                        expected['choice_evidence']=envelope['evidence']
                    text=proposal.get('text')
                    if (target!=expected or target.get('source_mode')!='live_ui'
                            or type(text) is not str or not text.strip() or len(text)>280
                            or evidence.get('submit_gate_passed') is not True
                            or evidence.get('reply_sha256')!=hashlib.sha256(text.encode()).hexdigest()):raise ValueError()
                    rows={'agent':dict(agent),'reply':dict(reply),'binding':binding}
                    return target,text,json.dumps(rows,sort_keys=True,ensure_ascii=False,allow_nan=False)
        try:
            if (type(task_id) is not str or not re.fullmatch(r'[a-f0-9]{32}',task_id)
                    or self.adapter is None or threading.get_ident()!=self.browser_owner_thread_id or not paused()):
                raise ValueError()
            with self.admission_lock:
                task=self.queue.get_task(task_id)
                if (not task or task.get('state')!='b_view_visible' or not self._authorized(task)
                        or type(task.get('publication_token')) is not str
                        or not re.fullmatch(r'[a-f0-9]{48}',task['publication_token'])):raise ValueError()
                target,text,rows_before=frozen(task)
                task_before=json.dumps(task,sort_keys=True,ensure_ascii=False,allow_nan=False)
                diagnostic['reply_sha256']=hashlib.sha256(text.encode()).hexdigest()
                diagnostic['stage']='reply_inspect_original_lookup'
                self.adapter.last_original_lookup={};lookup_started=True
                self.adapter.check_intervention()
                if target.get('choice_evidence') is not None:
                    from app.mention_receiver.choice_browser import observe_existing_choice_reply
                    observation=observe_existing_choice_reply(self.adapter,target,text)
                    visible=(type(observation) is dict and observation.get('outcome')=='b_view_visible'
                        and observation.get('exact_body_in_original_thread') is True)
                    counts=observation.get('counts',{}) if type(observation) is dict else {}
                else:
                    self.adapter.open_original_from_notifications(target,max_candidates=3)
                    if not paused() or not self._authorized(task):raise ValueError()
                    diagnostic['stage']='reply_inspect_current_dom'
                    value=self.adapter._reply_state(target,text)
                    if (type(value) is not dict or value.get('ok') is not True
                            or value.get('current_receiver_verified') is not True):raise ValueError()
                    counts={key:value.get(key) for key in ('matching_reply_count','existing_matching_reply_count')}
                    if any(type(n) is not int or not 0<=n<=1000000 for n in counts.values()):raise ValueError()
                    visible=counts['matching_reply_count']==counts['existing_matching_reply_count']==1
                self.adapter.check_intervention()
                diagnostic['reply_counts']={k:v for k,v in counts.items()
                    if k in {'matching_reply_count','existing_matching_reply_count'} and type(v) is int and 0<=v<=1000000}
                diagnostic['stage']='reply_inspect_after_read'
                current=self.queue.get_task(task_id)
                if (not paused() or not self._authorized(task) or not current
                        or json.dumps(current,sort_keys=True,ensure_ascii=False,allow_nan=False)!=task_before
                        or frozen(current)[2]!=rows_before):raise ValueError()
                diagnostic.update(stage='reply_inspect_complete',code='reply_visible_now' if visible else 'reply_not_observed',
                    b_view_visible_now=visible,observed_at_utc=datetime.now(timezone.utc).isoformat())
                return {'source_id':'assistant-task:'+task_id,'status':'assistant_reply_inspected','dispatchable':False}
        except Exception as error:
            kinds={'ReceiverInterventionRequired','ReceiverDetailError','ReceiverPageError','ReplyPreflightError',
                   'AgentStoreError','ValueError','KeyError','TypeError','TimeoutError'}
            diagnostic['exception_type']=type(error).__name__ if type(error).__name__ in kinds else 'local_error'
            diagnostic['code']='reply_inspection_unconfirmed'
            if isinstance(error,ReceiverInterventionRequired):raise
            raise ValueError('native_recovery_scope_unavailable') from None
        finally:
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

    def observe_existing_delivery(self, task):
        """Confirm one exact already-attempted choice reply through normal UI.

        This method does not generate text, submit an import, prepare a draft,
        clear a composer, invoke a send gate or reset a previous send result.
        """
        if (self.adapter is None or threading.get_ident()!=self.browser_owner_thread_id
                or self.should_stop() or self.control.snapshot().get('desired_state')!='running'
                or task.get('state')!='delivery_unknown'):
            raise ValueError('readback_unavailable')
        with self.admission_lock:
            old = self._find(task)
            if old is None or not self._authorized(task):
                raise ValueError('readback_scope_changed')
            reply = self.agent.get_reply(old['task_id'])
            if (not reply or reply['state']!='delivery_unknown' or not reply.get('send_token')
                    or reply.get('target',{}).get('source_mode')!='live_ui'
                    or not reply.get('target',{}).get('choice_evidence')):
                raise ValueError('readback_not_supported')
            recorded = self.agent.get_reply_readback(old['task_id'])
            if recorded is not None:
                return recorded
        from app.mention_receiver.choice_browser import observe_existing_choice_reply
        self.adapter.check_intervention()
        observation = observe_existing_choice_reply(self.adapter, reply['target'], reply['text'])
        self.adapter.check_intervention()
        if (self.should_stop() or self.control.snapshot().get('desired_state')!='running'
                or not self._authorized(task)):
            raise ValueError('readback_scope_changed')
        if (not isinstance(observation, dict) or observation.get('outcome')!='b_view_visible'
                or observation.get('exact_body_in_original_thread') is not True):
            return None
        with self.admission_lock:
            current = self._find(task)
            if current is None or current['task_id']!=old['task_id'] or not self._authorized(task):
                raise ValueError('readback_scope_changed')
            return self.agent.record_reply_readback(old['task_id'], observation)

    def get_recorded_delivery(self, task):
        """Read an immutable observed fact without touching any browser page."""
        if (self.should_stop() or self.control.snapshot().get('desired_state')!='running'
                or task.get('state')!='delivery_unknown'):
            return None
        with self.admission_lock:
            old = self._find(task)
            if old is None or not self._authorized(task):
                return None
            return self.agent.get_reply_readback(old['task_id'])

    def operator_retry_query(self, task, action_id):
        with self.admission_lock:
            if (self.control.snapshot().get('desired_state')!='running' or task.get('kind')!='query'
                    or task.get('publication_token') or task.get('state')!='needs_review'
                    or task.get('reason_code')!='knowledge_query_failed'):
                raise ValueError('query_retry_not_safe')
            resolution=self._resolution(task)
            old=self._find(task)
            intent=self.queue.get_query_retry(task['id'])
            if (resolution is None or resolution.action!='query' or old is None or intent is None
                    or intent['action_id']!=action_id or intent['state']!='reserved'
                    or intent['agent_task_id']!=old['task_id']):
                raise ValueError('query_retry_scope_changed')
            retry=self.agent.arm_failed_query_retry(old['task_id'],action_id,
                binding=resolution.binding.to_dict(),envelope=task['envelope'])
            if retry['state']!='armed':
                raise ValueError('query_retry_already_used')
            return self._snapshot(task)

    def publish(self, task):
        if self.adapter is None or threading.get_ident()!=self.browser_owner_thread_id:
            return {'state':'needs_review','reason_code':'publisher_unavailable'}
        result = self.reconcile(task)
        if not result.get('safe_to_publish'):
            return result
        if self.choices is not None and not self.choices.before_publish(task):
            return self._snapshot(task)
        binding = self._binding(task)
        outcome = ReplyPublisher(self.agent,binding,self.adapter,self.agent_path.parent/'reply-evidence',
            should_stop=self.should_stop,scope_authorizer=lambda current:current==binding and self._authorized(task),
            publication_allowed=lambda:self.control.snapshot().get('desired_state')=='running'
            ).publish(result['agent_task_id'])
        if self.choices is not None:
            self.choices.after_publish(task, outcome)
        held=self.agent.get_task(result['agent_task_id'])
        preflight_verification=(outcome['state']=='verification_required' and held['state']=='needs_review'
                                and held.get('reason_code')=='preflight_verification_required')
        # Reconcile may have just observed exact ingestion telemetry, including
        # an already indexed video that never entered the waiting poll lane.
        snapshot_task = {**task, 'details':result.get('details',task.get('details',{}))}
        snapshot = self._snapshot(snapshot_task,override=None if outcome['state']=='not_sendable' or preflight_verification else outcome['state'])
        if outcome.get('reason_code')=='publication_paused':
            snapshot['reason_code']='publication_paused'
        if outcome['state']=='b_view_visible':
            self.conversation.record_published(task['id'],binding,self.agent.get_reply(result['agent_task_id']))
        return snapshot
