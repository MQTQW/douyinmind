"""Normal-page intake and optional read-only intent planning; never imports."""
from __future__ import annotations

import hashlib
import json
import re

from app.mention_agent.contracts import canonical_json, public_profile
from app.mention_agent.intake import make_envelope
from app.mention_receiver.browser import ReceiverInterventionRequired, ReceiverPageError, ReceiverDetailError, SELF_URL
from .commands import CommandResolver
from .integration import active_local_bindings


def notice_fingerprint(notice):
    if not isinstance(notice.get('semantic_fingerprint'),str) or not notice['semantic_fingerprint'].strip():
        raise ValueError('incomplete_notice_fingerprint')
    value={key:notice[key] for key in ('source_profile_url','exact_text','semantic_fingerprint')}
    value['exact_text']=re.sub(r'\s+','',value['exact_text'])
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class BrowserLane:
    def __init__(self, adapter, registry, queue, conversation):
        self.adapter,self.registry,self.queue,self.conversation=adapter,registry,queue,conversation
        self.capture_id='receiver-'+hashlib.sha256(adapter.expected_profile_url.encode()).hexdigest()[:40]
        self.pending={}
        self.last_baseline_created=False
        self.last_detail_failure_count=0
        self._buffered_notices=[]
        self.should_stop=lambda:False
        self.choice_controller = None
        self.run_id = None
        self.semantic_resolution_provider = None

    def touch_devices(self):
        touched=set()
        for item in active_local_bindings(self.registry,self.adapter.expected_profile_url):
            device_id=item['device']['device_id']
            if device_id not in touched:
                self.registry.mark_local_device_online(device_id)
                touched.add(device_id)

    @staticmethod
    def _issue_proof(notice,detail,receiver):
        proof=detail.get('evidence',{})
        if (detail.get('ok') is not True or not all(detail.get(key) is True and proof.get(key) is True
                for key in ('real_mention','sender_verified','video_verified'))
                or detail.get('unique_count')!=1 or proof.get('unique_count')!=1
                or public_profile(detail.get('sender_profile_url'))!=public_profile(notice['source_profile_url'])
                or public_profile(detail.get('receiver_profile_url'))!=receiver
                or proof.get('sender_profile_url')!=detail['sender_profile_url']
                or proof.get('receiver_profile_url')!=detail['receiver_profile_url']
                or proof.get('video_id')!=detail.get('video_id')
                or not re.fullmatch(r'\d{5,30}',detail.get('video_id',''))
                or re.sub(r'\s+','',detail.get('command_text',''))!=re.sub(r'\s+','',notice['exact_text'])):
            raise ValueError('unrouted_detail_unverified')
        return {'real_mention':True,'sender_verified':True,'receiver_verified':True,'video_verified':True,'unique_count':1}

    def _record_issue(self,notice,detail,fingerprint,reason):
        from .queue import ISSUE_LABELS
        reason=reason if reason in ISSUE_LABELS else 'authorization_unavailable'
        evidence=self._issue_proof(notice,detail,self.adapter.expected_profile_url)
        self.queue.record_intake_issue(fingerprint,notice['source_profile_url'],self.adapter.expected_profile_url,
            detail['command_text'],video_id=detail['video_id'],reason_code=reason,evidence=evidence)
        self.queue.remember_notice(self.capture_id,fingerprint,reason=reason)

    @property
    def has_buffered_notices(self):
        return bool(self._buffered_notices)

    def clear_buffer(self):
        # Only transient candidates/proofs are cleared. Durable admissions and
        # remembered notices are never removed or rolled back.
        self._buffered_notices=[]
        self.pending.clear()
        if self.choice_controller is not None:
            clear = getattr(self.choice_controller, 'clear_buffer', None)
            if callable(clear):
                clear()

    def _scan_candidates(self):
        self.last_detail_failure_count=0
        self.adapter.begin_cycle()
        self.adapter.check_intervention()
        if self.should_stop():
            return
        self.adapter.page.goto(SELF_URL,wait_until='load',timeout=60000)
        if self.should_stop():
            return
        identity=self.adapter.identity()
        if identity is None:
            raise ReceiverPageError('助手主页身份尚未就绪')
        self.touch_devices()
        notices=self.adapter.notifications()
        if self.should_stop():
            return
        fingerprints=[notice_fingerprint(notice) for notice in notices]
        baseline=self.queue.ensure_capture_baseline(self.capture_id,fingerprints,ready=True)
        self.last_baseline_created=baseline['created']
        if self.last_baseline_created:
            return
        # One candidate snapshot per page navigation. Later DOM reads only
        # reopen/revalidate the original candidate; they never expand the batch.
        for notice,fingerprint in reversed(list(zip(notices,fingerprints))):
            if self.should_stop():
                return
            if self.queue.has_seen_notice(self.capture_id,fingerprint):
                continue
            if self.queue.notice_is_deferred(self.capture_id,fingerprint):
                continue
            user=self.registry.find_user(notice['source_profile_url'],self.adapter.expected_profile_url)
            if not user or not user['active'] or user['verification_status']!='verified':
                self.queue.remember_notice(self.capture_id,fingerprint,reason='sender_not_verified')
                continue
            self._buffered_notices.append((dict(notice),fingerprint))
            if len(self._buffered_notices)==3:
                break

    def collect(self):
        # The caller must durably enqueue and bind this row before asking for
        # the next detail. Never navigate away from an unhanded-off proof.
        if self.pending:
            raise ValueError('prior_notice_admission_required')
        try:
            self.adapter.check_intervention()
            if self.should_stop():
                self.clear_buffer()
                return []
            if not self._buffered_notices:
                self.last_baseline_created = False
                self.last_detail_failure_count = 0
                # A reply choosing a save library may not create a new @ notice.
                # Inspect at most one already known, persistent original root
                # before ordinary new notices can mistake the selection for QA.
                if self.choice_controller is not None:
                    choice_rows = self.choice_controller.collect_pending(self.adapter, self.run_id or 'local')
                    if choice_rows:
                        return choice_rows
                self._scan_candidates()
            if not self._buffered_notices or self.should_stop():
                self.clear_buffer()
                return []
            notice,fingerprint=self._buffered_notices.pop(0)
            # Authorization can change after the snapshot. It is checked again
            # before any detail opens, independently of the old candidate list.
            user=self.registry.find_user(notice['source_profile_url'],self.adapter.expected_profile_url)
            if not user or not user['active'] or user['verification_status']!='verified':
                self.queue.remember_notice(self.capture_id,fingerprint,reason='sender_not_verified')
                return []
            if self.adapter.identity() is None:
                raise ReceiverPageError('助手主页身份尚未就绪')
            preliminary=(self.registry.default_binding(notice['source_profile_url'],self.adapter.expected_profile_url,'query')
                         or self.registry.default_binding(notice['source_profile_url'],self.adapter.expected_profile_url,'import'))
            self.adapter.check_intervention()
            if self.should_stop():
                self.clear_buffer()
                return []
            # Reuse the established popup strategy; do not duplicate private
            # hover logic. Fresh results cannot add new candidates to this batch.
            self.adapter.notifications()
            if self.should_stop():
                self.clear_buffer()
                return []
            try:
                detail=self.adapter.detail(notice,keep_open=True)
            except ReceiverDetailError:
                # A missing/unverifiable original is local to this candidate.
                # Never close a challenge or navigate after a stop request.
                if self.should_stop():
                    self.clear_buffer()
                    return []
                self.adapter.check_intervention()
                if self.choice_controller is not None and self.choice_controller.known_selection_notice(notice, None, self.adapter):
                    self.queue.remember_notice(self.capture_id, fingerprint, reason='library_choice_already_received')
                    if not self.should_stop():
                        self.adapter.check_intervention()
                        self.adapter.page.keyboard.press('Escape')
                    return []
                if self.should_stop():
                    self.clear_buffer()
                    return []
                self.adapter.page.keyboard.press('Escape')
                self.adapter.check_intervention()
                self.queue.defer_unverified_notice(self.capture_id,fingerprint)
                self.last_detail_failure_count+=1
                return []
            try:
                if self.should_stop():
                    self.clear_buffer()
                    return []
                if self.choice_controller is not None and self.choice_controller.known_selection_notice(notice, detail, self.adapter):
                    self.queue.remember_notice(self.capture_id, fingerprint, reason='library_choice_already_received')
                    return []
                if self.choice_controller is not None and self.choice_controller.selection_notice_requires_recheck(notice, detail):
                    # Same-looking text without an exact current parent is
                    # neither a received selection nor a fresh paid question.
                    self.queue.defer_unverified_notice(self.capture_id, fingerprint)
                    self.last_detail_failure_count += 1
                    return []
                if preliminary is None:
                    reason='remote_device_not_connected' if any(not device['is_local'] and
                        device['device_id']==user.get('default_device_id') for device in user['devices']) else 'verified_local_binding_required'
                    self._record_issue(notice,detail,fingerprint,reason)
                    return []
                envelope=make_envelope(notice,detail,preliminary)
                preferred=None
                for choice in active_local_bindings(self.registry,self.adapter.expected_profile_url):
                    binding=choice['binding']
                    if binding.sender_profile_url!=preliminary.sender_profile_url or binding.instance_id!=preliminary.instance_id:
                        continue
                    roots=self.conversation.roots(binding,envelope['video_id'])
                    if roots:
                        proof=self.adapter.conversation_reference(envelope,roots)
                        if proof['quality']=='verified_ancestor':
                            if preferred is not None and preferred!=binding.binding_id:
                                preferred=None
                                break
                            preferred=binding.binding_id
                resolver=CommandResolver(self.registry)
                resolution=resolver.resolve(envelope,preferred_binding_id=preferred)
                if resolution.binding is None or resolution.authorization is None:
                    self._record_issue(notice,detail,fingerprint,resolution.reason_code or 'verified_local_binding_required')
                    return []
                envelope=make_envelope(notice,detail,resolution.binding)
                roots=self.conversation.roots(resolution.binding,envelope['video_id'])
                thread=self.adapter.conversation_reference(envelope,roots)
                if self.should_stop():
                    self.clear_buffer()
                    return []
                self.pending[envelope['reference_id']]={'thread':thread,'fingerprint':fingerprint}
                return [{'binding':resolution.binding,'envelope':envelope,'video_title':detail.get('video_title','')[:512],
                         'kind':resolution.action,'notice_fingerprint':fingerprint,'user_id':resolution.user_id,
                         'device_id':resolution.device_id,'authorization':resolution.authorization}]
            finally:
                # Stop and challenge scenes receive no cleanup keypress. An
                # ordinary completed detail returns to self for the next item.
                if not self.should_stop():
                    self.adapter.check_intervention()
                    self.adapter.page.keyboard.press('Escape')
        except Exception:
            self.clear_buffer()
            raise

    def admitted(self,tasks):
        from app.mention_agent.contracts import Binding
        if self.choice_controller is not None:
            self.choice_controller.admitted(tasks)
        for task in tasks:
            pending=self.pending.get(task['envelope']['reference_id'])
            if pending is None:
                continue
            thread=pending['thread']
            if self.conversation.task_scope(task['id']) is None:
                self.conversation.bind_task(task['id'],Binding.from_dict(task['binding']),task['envelope'],
                                            thread['root_reference_id'],thread)
            if pending.get('fingerprint') is not None:
                self.queue.remember_notice(self.capture_id,pending['fingerprint'])
            self.pending.pop(task['envelope']['reference_id'],None)

    def prepare_native_detail(self, notice, detail, *, semantic_checkpoint=None, native_candidate=None):
        """Stage an ACTUAL fresh UI detail driven by a separate native event.

        This does not accept native JSON as UI evidence and never enqueues.
        Native provenance/alias/history checks belong to NativeLane's durable
        store; this method retains the ordinary UI builder and fresh grant
        resolver. The caller must complete admitted() before opening more UI.
        """
        if self.pending:
            raise ValueError('prior_notice_admission_required')
        self.last_native_issue_code = None
        if self.should_stop():
            return []
        self.adapter.check_intervention()
        fingerprint = notice_fingerprint(notice)
        sender = public_profile(notice.get('source_profile_url'))
        receiver = self.adapter.expected_profile_url
        user = self.registry.find_user(sender, receiver)
        if not user or not user['active'] or user['verification_status'] != 'verified':
            self.last_native_issue_code = 'sender_not_verified'
            return []
        preliminary = self.registry.intake_binding(sender, receiver)
        if preliminary is None:
            self.last_native_issue_code = 'verified_local_binding_required'
            return []
        envelope = make_envelope(notice, detail, preliminary)
        if self.choice_controller is not None:
            if self.choice_controller.known_selection_notice(notice, detail, self.adapter):
                self.last_native_issue_code = 'library_choice_already_received'
                return []
            if self.choice_controller.selection_notice_requires_recheck(notice, detail):
                self.last_native_issue_code = 'choice_original_not_verified'
                return []
        preferred = None
        for choice in active_local_bindings(self.registry, receiver):
            binding = choice['binding']
            if binding.sender_profile_url != preliminary.sender_profile_url or binding.instance_id != preliminary.instance_id:
                continue
            roots = self.conversation.roots(binding, envelope['video_id'])
            if roots:
                proof = self.adapter.conversation_reference(envelope, roots)
                if proof['quality'] == 'verified_ancestor':
                    if preferred is not None and preferred != binding.binding_id:
                        preferred = None
                        break
                    preferred = binding.binding_id
        if self.semantic_resolution_provider is None:
            resolution = CommandResolver(self.registry).resolve(envelope, preferred_binding_id=preferred)
        else:
            semantic_options={'preferred_binding_id':preferred,'source_checkpoint':semantic_checkpoint}
            if native_candidate is not None:semantic_options['native_candidate']=native_candidate
            resolution=self.semantic_resolution_provider(envelope,**semantic_options)
            if resolution is None or self.should_stop():return []
        if resolution.binding is None or resolution.authorization is None:
            self.last_native_issue_code = resolution.reason_code or 'verified_local_binding_required'
            return []
        if self.registry.authorize_snapshot(resolution.authorization) != resolution.binding:
            self.last_native_issue_code = 'binding_changed'
            return []
        envelope = make_envelope(notice, detail, resolution.binding)
        roots = self.conversation.roots(resolution.binding, envelope['video_id'])
        thread = self.adapter.conversation_reference(envelope, roots)
        if self.should_stop():
            return []
        self.adapter.check_intervention()
        self.pending[envelope['reference_id']] = {'thread': thread, 'fingerprint': fingerprint}
        return [{'binding': resolution.binding, 'envelope': envelope,
                 'video_title': detail.get('video_title', '')[:512], 'kind': resolution.action,
                 'notice_fingerprint': fingerprint, 'user_id': resolution.user_id,
                 'device_id': resolution.device_id, 'authorization': resolution.authorization}]
