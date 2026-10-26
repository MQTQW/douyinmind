"""Native-driven UI admission; temporary stores and fake normal pages only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.assistant.browser_lane import BrowserLane
from app.assistant.native_lane import NativeLane, NativeLaneError
from app.assistant.native_source_store import NativeSourceStore
from app.assistant.native_events import project_page
from app.mention_receiver.browser import ReceiverInterventionRequired
from tests.test_assistant_product_bridge import system, CaptureAdapter
from tests.test_assistant_choice_controller import (case as native_choice_case, root as choice_root,
    publish_fact as choice_publish_fact, Adapter as ChoiceAdapter)


def prepared(tmp_path):
    binding, agent, envelope, registry, migrated, queue, conversation, control = system(tmp_path)
    adapter = CaptureAdapter(binding)
    lane = BrowserLane(adapter, registry, queue, conversation)
    notice = {'source_profile_url': binding.sender_profile_url,
              'exact_text': '@助手 根据编程知识库解释Git', 'semantic_fingerprint': 'synthetic-visible-notice'}
    return lane, adapter, registry, migrated, queue, conversation, notice


def test_native_helper_uses_real_UI_builder_then_caller_commits_before_thread(tmp_path):
    lane, adapter, registry, migrated, queue, conversation, notice = prepared(tmp_path)
    detail = adapter.detail(notice, keep_open=True)
    rows = lane.prepare_native_detail(notice, detail)
    assert len(rows) == 1
    envelope = rows[0]['envelope']
    assert envelope['source_mode'] == 'live_ui'
    assert envelope['native_comment_id'] is None and envelope['reference_quality'] == 'verified_ui'
    assert sum(queue.counts().values()) == 0
    task = queue.enqueue(**rows[0])
    assert conversation.task_scope(task['id']) is None
    lane.admitted([task])
    assert conversation.task_scope(task['id']) is not None
    assert lane.pending == {}


def test_native_helper_does_not_accept_native_candidate_as_UI_detail(tmp_path):
    lane, adapter, registry, migrated, queue, conversation, notice = prepared(tmp_path)
    with pytest.raises(ValueError):
        lane.prepare_native_detail(notice, {'source_kind': 'native_notice_candidate',
            'content': notice['exact_text'], 'native_comment_id': '8999999000000000011'})
    assert not lane.pending and sum(queue.counts().values()) == 0


def test_native_helper_fresh_binding_revoked_before_admission_stages_nothing(tmp_path):
    lane, adapter, registry, migrated, queue, conversation, notice = prepared(tmp_path)
    registry.revoke_user(migrated['user_id'])
    assert lane.prepare_native_detail(notice, adapter.detail(notice)) == []
    assert lane.last_native_issue_code == 'sender_not_verified'
    assert not lane.pending and sum(queue.counts().values()) == 0


@pytest.mark.parametrize('key,value', [('real_mention', False), ('video_verified', False),
                                     ('sender_verified', False), ('unique_count', 2)])
def test_native_helper_rejects_unverified_or_ambiguous_UI_snapshot(tmp_path, key, value):
    lane, adapter, registry, migrated, queue, conversation, notice = prepared(tmp_path)
    detail = deepcopy(adapter.detail(notice)); detail[key] = value
    with pytest.raises(ValueError): lane.prepare_native_detail(notice, detail)
    assert not lane.pending and sum(queue.counts().values()) == 0


def test_native_helper_never_stages_after_stop(tmp_path):
    lane, adapter, registry, migrated, queue, conversation, notice = prepared(tmp_path)
    lane.should_stop = lambda: True
    assert lane.prepare_native_detail(notice, adapter.detail(notice)) == []
    assert not lane.pending and sum(queue.counts().values()) == 0


def native_candidate(binding, **changes):
    value = {'namespace': 'douyin_web_v1', 'source_kind': 'native_notice_candidate',
        'sender_uid': '8900000000000000001', 'recipient_uid': '8900000000000000002',
        'sender_sec_uid': binding.sender_profile_url.rsplit('/', 1)[-1],
        'sender_profile_url': binding.sender_profile_url, 'receiver_profile_url': binding.receiver_profile_url,
        'video_id': '7999999000000000011', 'native_comment_id': '8900000000000000003',
        'content': '@助手 根据编程知识库解释Git', 'native_source_key': 'synthetic-native-key',
        'identity_link_revision': '1', 'root_comment_id': None, 'parent_comment_id': None,
        'parent_id_hint': None}
    value.update(changes)
    return value


class SourceStore:
    """Fake protocol only. Production persistence/receiver is separately tested."""
    def __init__(self, candidate, *, historical=False):
        self.candidate = deepcopy(candidate)
        self.state = 'historical' if historical else 'pending'
        self.plan = None; self.held = []; self.links = []; self.conflict = False
        self.valid = True; self.lookup = {}; self.transport_fresh = True

    def claim_ui_candidate(self, owner, lease_seconds=120):
        if self.state != 'pending': return None
        self.state = 'claimed'
        return self.claim()

    def claim(self):
        return {'source_id': 'synthetic-source', 'claim_token': 'synthetic-private-claim',
                'candidate': deepcopy(self.candidate)}

    def check_candidate(self, source_id, token):
        assert source_id == 'synthetic-source' and token == 'synthetic-private-claim'
        return {'valid': self.valid and self.transport_fresh, 'reason': 'fixed', 'semantic_conflict': self.conflict,
                'candidate': deepcopy(self.candidate)}

    def semantic_conflicts(self, candidate): return self.conflict
    def safe_status(self): return {'pending': int(self.state == 'pending')}
    def hold(self, source_id, token, code): self.state = 'held'; self.held.append(code)
    def defer(self, source_id, token, code): self.state = 'pending'
    def record_ui_plan(self, source_id, token, plan):
        assert self.state == 'claimed'; self.plan = deepcopy(plan); self.state = 'planned'
    def finish_ui_admission(self, source_id, token, task_id, reference_id):
        self.state = 'admitted'; self.links.append((task_id, reference_id))
    def recoverable_ui_plans(self):
        return [{**self.claim(), 'plan': deepcopy(self.plan)}] if self.state == 'planned' else []
    def view(self):
        return {**self.claim(), 'state': self.state, 'plan': deepcopy(self.plan), 'historical': self.state == 'historical',
                'task_id': self.links[-1][0] if self.links else None,
                'reference_id': self.links[-1][1] if self.links else None}
    def lookup_source_for_task(self, task_id):
        if self.links and self.links[-1][0] == task_id: return self.view()
        return deepcopy(self.lookup.get(task_id))
    def find_sources_by_ui_reference(self, reference_id):
        result = [self.view()] if self.plan and self.plan['reference_id'] == reference_id else []
        result += [deepcopy(row) for row in self.lookup.values()
                   if row.get('plan', {}).get('reference_id') == reference_id]
        return result
    def admission_current(self, source_id):
        return self.transport_fresh and self.admitted_source_current(source_id)
    def admitted_source_current(self, source_id):
        for row in self.lookup.values():
            if row.get('source_id') == source_id:
                return row.get('state') == 'admitted' and row.get('current') is True
        return self.state == 'admitted' and self.valid and not self.conflict


class NativeCapture(CaptureAdapter):
    def __init__(self, binding):
        super().__init__(binding); self.native_calls = []; self.observed_native = None
        self.before_return = lambda: None
        self.change_detail = lambda detail: detail

    def open_original_from_notifications(self, target, *, max_candidates, native_candidate_correlation):
        assert max_candidates == 3 and native_candidate_correlation is True
        self.native_calls.append(deepcopy(target))
        notice = {'source_profile_url': target['sender_profile_url'], 'exact_text': target['comment_text'],
                  'semantic_fingerprint': 'actually-read-visible-card'}
        detail = self.detail(notice, keep_open=True)
        detail['native_comment_id'] = self.observed_native
        detail['evidence']['native_comment_id'] = self.observed_native
        detail['native_correlation'] = {'source_native_comment_id': target['native_comment_id'],
            'observed_native_comment_id': self.observed_native,
            'quality': 'native_equal' if self.observed_native == target['native_comment_id'] else 'semantic_only'}
        detail['native_ui_notice'] = notice
        self.before_return()
        return self.change_detail(detail)


def native_system(tmp_path, **changes):
    binding, agent, envelope, registry, migrated, queue, conversation, control = system(tmp_path)
    adapter = NativeCapture(binding)
    base = BrowserLane(adapter, registry, queue, conversation)
    store = SourceStore(native_candidate(binding, **changes))
    receiver = SimpleNamespace(calls=0)
    def poll(): receiver.calls += 1; return []
    receiver.poll_once = poll
    lane = NativeLane(base, receiver, store); lane.run_id = 'fake-native-run'
    return lane, store, receiver, adapter, queue, conversation, registry, migrated


def test_native_semantic_join_keeps_DOM_none_and_only_actual_UI_fingerprint(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    rows = lane.collect()
    assert receiver.calls == 0 and len(rows) == 1
    env = rows[0]['envelope']
    assert env['source_mode'] == 'live_ui' and env['native_comment_id'] is None
    assert env['reference_quality'] == 'verified_ui'
    assert 'source_kind' not in env['evidence']
    assert store.plan['native_correlation']['quality'] == 'semantic_only'
    assert store.plan['native_correlation']['source_native_comment_id'] == store.candidate['native_comment_id']
    task = queue.enqueue(**rows[0]); lane.admitted([task])
    assert conversation.task_scope(task['id']) is not None
    assert store.state == 'admitted' and len(store.links) == 1
    assert lane.collect() == [] and len(adapter.native_calls) == 1


def test_native_equal_join_keeps_the_actual_DOM_native_id(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    adapter.observed_native = store.candidate['native_comment_id']
    rows = lane.collect()
    assert rows[0]['envelope']['native_comment_id'] == adapter.observed_native
    assert rows[0]['envelope']['reference_quality'] == 'native'
    assert store.plan['native_correlation']['quality'] == 'native_equal'


def test_native_poll_is_explicit_and_collect_does_not_poll_again(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    assert lane.poll_native()['received'] == 0 and receiver.calls == 1
    lane.collect()
    assert receiver.calls == 1


@pytest.mark.parametrize('code', ['session_expired', 'authorization_expired', 'invalid_session',
                                 'session_identity_mismatch', 'identity_not_verified', 'verification_required'])
def test_native_expired_identity_pauses_without_UI_or_fallback(tmp_path, code):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    class FixedError(Exception): pass
    error = FixedError(); error.code = code
    def fail(): raise error
    receiver.poll_once = fail
    with pytest.raises(ReceiverInterventionRequired): lane.poll_native()
    assert not adapter.native_calls and sum(queue.counts().values()) == 0


def test_native_historical_and_semantic_conflict_never_open_UI_or_enqueue(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    store.state = 'historical'
    assert lane.collect() == [] and not adapter.native_calls
    store.state = 'pending'; store.conflict = True
    assert lane.collect() == [] and not adapter.native_calls
    assert store.state == 'held' and sum(queue.counts().values()) == 0


def test_native_old_UI_potential_alias_is_held_before_any_new_UI(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    notice = {'source_profile_url': store.candidate['sender_profile_url'],
              'exact_text': store.candidate['content'], 'semantic_fingerprint': 'old-UI'}
    rows = lane.browser_lane.prepare_native_detail(notice, adapter.detail(notice))
    task = queue.enqueue(**rows[0]); lane.browser_lane.admitted([task])
    assert lane.collect() == [] and not adapter.native_calls
    assert store.held == ['native_legacy_alias_ambiguous']
    assert sum(queue.counts().values()) == 1


def test_native_source_identity_change_during_UI_cannot_be_admitted(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    adapter.before_return = lambda: store.candidate.update(sender_uid='8900000000000000099')
    assert lane.collect() == []
    assert store.state == 'held' and store.plan is None and sum(queue.counts().values()) == 0


@pytest.mark.parametrize('changed', [
    lambda d: {**d, 'native_correlation': None},
    lambda d: {**d, 'native_ui_notice': {'source_profile_url': d['sender_profile_url'], 'exact_text': d['command_text']}},
    lambda d: {**d, 'native_ui_notice': {**d['native_ui_notice'], 'pre_index': 0}},
    lambda d: {**d, 'native_comment_id': '8900000000000000099'},
])
def test_native_correlation_and_actual_notice_are_not_caller_guesses(tmp_path, changed):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    adapter.change_detail = changed
    assert lane.collect() == [] and store.state == 'held'
    assert store.plan is None and sum(queue.counts().values()) == 0


def test_native_crash_before_Queue_never_recreates_task_from_stale_UI_plan(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    assert len(lane.collect()) == 1
    lane.clear_buffer()
    assert lane.recover() == 0
    assert store.held == ['native_plan_missing_task']
    assert sum(queue.counts().values()) == 0 and len(adapter.native_calls) == 1


def test_native_crash_after_Queue_recovers_same_task_and_thread_without_UI(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    row = lane.collect()[0]; task = queue.enqueue(**row)
    assert conversation.task_scope(task['id']) is None
    lane.clear_buffer()
    assert lane.recover() == 1
    assert conversation.task_scope(task['id']) is not None
    assert store.links == [(task['id'], task['envelope']['reference_id'])]
    assert lane.recover() == 0 and len(adapter.native_calls) == 1
    assert sum(queue.counts().values()) == 1


def test_native_recovery_cannot_link_changed_authorization_or_scope(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    row = lane.collect()[0]; queue.enqueue(**row)
    lane.clear_buffer(); store.plan['queue_item']['authorization']['grant_revision'] += 1
    assert lane.recover() == 0 and store.held == ['native_plan_conflict']
    assert sum(queue.counts().values()) == 1


def test_native_stop_or_challenge_never_holds_event_as_false_failure(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    lane.should_stop = lambda: True
    assert lane.collect() == [] and store.state == 'pending'
    lane.should_stop = lambda: False
    def challenge(*args, **kwargs): raise ReceiverInterventionRequired('synthetic challenge')
    adapter.open_original_from_notifications = challenge
    with pytest.raises(ReceiverInterventionRequired): lane.collect()
    assert store.held == [] and adapter.page.keys == []


def test_native_browser_lane_cannot_be_called_from_worker_thread(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(NativeLaneError): pool.submit(lane.collect).result()
    assert not adapter.native_calls and store.state == 'pending'


def test_native_plain_reply_cannot_become_a_fake_mention_without_known_pending_root(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path,
        source_kind='native_reply_notice_candidate', content='存到编程库', parent_id_hint='8900000000000000099')
    assert lane.collect() == [] and store.held == ['native_reply_parent_unresolved']
    assert not adapter.native_calls and sum(queue.counts().values()) == 0


def test_native_plain_reply_wakes_only_unique_scoped_choice_not_other_waiting_root(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path,
        source_kind='native_reply_notice_candidate', content='存到编程库', parent_id_hint='8900000000000000099')
    root_env = {'sender_profile_url': store.candidate['sender_profile_url'],
        'receiver_profile_url': store.candidate['receiver_profile_url'], 'video_id': store.candidate['video_id'],
        'reference_id': 'saved-Root', 'native_comment_id': None}
    target = {'choice_id': 'a'*64, 'root_task_id': 'target-root', 'root_envelope': root_env, 'options': []}
    other = {'choice_id': 'b'*64, 'root_task_id': 'another-root', 'root_envelope': {**root_env, 'video_id': '7999999000000000099'}}
    store.lookup['target-root'] = {'source_id': 'synthetic-root-source', 'state': 'admitted', 'historical': False,
        'current': True, 'candidate': {**store.candidate, 'source_kind': 'native_notice_candidate',
        'native_comment_id': store.candidate['parent_id_hint']}}
    calls = []
    def empty_target_wakeup(a, run, *, choice_id, source_checkpoint):
        assert callable(source_checkpoint)
        calls.append(choice_id)
        return []  # No UI selection exists, so no pre-reservation callback runs.
    controller = SimpleNamespace(store=SimpleNamespace(list_waiting=lambda _: [other, target], list_asks_to_observe=lambda _: []),
        collect_pending=empty_target_wakeup, clear_buffer=lambda: None)
    lane.choice_controller = controller
    assert lane.collect() == []
    assert calls == ['a'*64]
    assert not adapter.native_calls and sum(queue.counts().values()) == 0


def test_native_continuous_authority_requires_durable_link_not_just_queued_plan(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    task = queue.enqueue(**lane.collect()[0])
    assert lane.authority_current(task) is False
    lane.admitted([task])
    assert lane.authority_current(task) is True
    store.conflict = True
    assert lane.authority_current(task) is False
    store.conflict = False; store.valid = False
    assert lane.authority_current(task) is False


def test_native_authority_cannot_borrow_link_for_changed_body_or_grant(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    task = queue.enqueue(**lane.collect()[0]); lane.admitted([task])
    altered = deepcopy(task); altered['authorization']['grant_revision'] += 1
    assert lane.authority_current(altered) is False
    altered = deepcopy(task); altered['envelope']['comment_text'] = '其他公开指令'
    assert lane.authority_current(altered) is False


def test_unrelated_real_legacy_UI_task_has_no_native_source_to_revoke(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    notice = {'source_profile_url': store.candidate['sender_profile_url'],
              'exact_text': store.candidate['content'], 'semantic_fingerprint': 'old-UI'}
    task = queue.enqueue(**lane.browser_lane.prepare_native_detail(notice, adapter.detail(notice))[0])
    assert lane.authority_current(task) is True
    task['envelope']['source_mode'] = 'isolated'
    assert lane.authority_current(task) is False


def choice_native_system(case):
    task, session, resolution = choice_root(case)
    choice_publish_fact(case, task, session)
    adapter = ChoiceAdapter(case, text='2', target=8)
    base = BrowserLane(adapter, case.registry, case.queue, case.history)
    base.choice_controller = case.controller
    candidate = native_candidate(resolution.binding, source_kind='native_reply_notice_candidate',
        content='2', video_id=task['envelope']['video_id'], parent_id_hint='8900000000000000099')
    store = SourceStore(candidate)
    item = {key: deepcopy(task[key]) for key in ('binding', 'envelope', 'kind', 'user_id', 'device_id', 'authorization')}
    item.update(video_title='', notice_fingerprint=None)
    store.lookup[task['id']] = {'source_id': 'synthetic-root-source', 'state': 'admitted', 'historical': False,
        'current': True, 'task_id': task['id'], 'reference_id': task['envelope']['reference_id'],
        'plan': {'reference_id': task['envelope']['reference_id'], 'envelope': task['envelope'], 'queue_item': item},
        'candidate': {**candidate, 'source_kind': 'native_notice_candidate',
            'content': task['envelope']['comment_text'], 'native_comment_id': candidate['parent_id_hint']}}
    lane = NativeLane(base, SimpleNamespace(poll_once=lambda: []), store)
    lane.run_id = 'synthetic-choice-native'
    return lane, store, task, session, adapter


def test_native_plain_reply_actual_choice_builder_keeps_realmention_false_and_one_child(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    rows = lane.collect()
    assert len(rows) == 1
    env = rows[0]['envelope']
    assert env['evidence']['source_kind'] == 'library_choice_reply'
    assert env['evidence']['real_mention'] is False
    assert env['native_comment_id'] is None and env['comment_text'] == '2'
    assert rows[0]['binding'].knowledge_base_id == 8
    child = case.queue.enqueue(**rows[0]); lane.admitted([child])
    current = case.store.get(session['choice_id'])
    assert current['state'] == 'handed_off' and current['child_task_id'] == child['id']
    assert store.state == 'admitted' and lane.authority_current(child)
    assert len(adapter.reads) == 1
    assert lane.collect() == []
    assert sum(case.queue.counts().values()) == 2  # Original ask plus one save child.


def test_native_choice_crash_respects_active_handoff_lease_then_records_existing_child(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    row = lane.collect()[0]; child = case.queue.enqueue(**row)
    lane.clear_buffer()
    assert lane.recover() == 0
    assert store.state == 'planned' and lane.authority_current(child) is False
    case.clock[0] += 31  # The normal persisted ChoiceStore lease is not stolen.
    assert lane.recover() == 1
    assert store.state == 'admitted'
    assert case.store.get(session['choice_id'])['child_task_id'] == child['id']
    assert lane.authority_current(child)
    assert len(adapter.reads) == 1  # Recovery links local facts, no second browser read.


def test_native_mixed_AT_selection_uses_choice_but_does_not_strip_unknown_at_label(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    store.candidate['source_kind'] = 'native_notice_candidate'
    assert len(lane.collect()) == 1  # Raw '2' is a unique frozen menu selection.
    assert adapter.reads == [session['choice_id']]


def test_native_mixed_AT_ambiguous_label_is_held_without_fake_mention(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    store.candidate.update(source_kind='native_notice_candidate', content='@unknown-label 2')
    assert lane.collect() == []
    assert store.held == ['native_mixed_role_unresolved']
    assert not adapter.reads


def actual_source_system(tmp_path):
    lane, fake, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    clock = [100.0]
    store = NativeSourceStore(tmp_path/'native-sources.sqlite3', fake.candidate['recipient_uid'],
        fake.candidate['receiver_profile_url'], create=True, clock=lambda: clock[0])
    for channel, group in ((9, '601'), (7, '2')):
        context = store.prepare_channel(channel, group)
        store.record_page(context, {'candidates': [], 'notices': [], 'fingerprint': 'synthetic-empty'},
            next_cursor=('0', '0'), has_more=False)
    assert store.arm_if_ready(True)
    clock[0] = 102.0; store.set_verified()
    def publish_fact(nid='8900000000000000004', text=None):
        candidate = fake.candidate
        raw = {'nid_str': nid, 'type': 45, 'user_id': candidate['recipient_uid'],
            'aweme_id': candidate['video_id'], 'create_time': '101',
            'at': {'sub_type': 2, 'aweme': {'aweme_id': candidate['video_id']},
                'user_info': {'uid': candidate['sender_uid'], 'sec_uid': candidate['sender_sec_uid']},
                'content': text or candidate['content'], 'comment_status': 1, 'comment_unvisible': 0,
                'schema_url': 'aweme://aweme/detail/'+candidate['video_id']+'?cid='+candidate['native_comment_id']}}
        value = {'status_code': 0, 'notice_list_v2': [raw], 'has_more': False, 'min_time': '0', 'max_time': '0'}
        projection = project_page(value, 9, receiver_uid=candidate['recipient_uid'],
            receiver_sec_uid=candidate['receiver_profile_url'].rsplit('/', 1)[-1],
            profile_allowed=lambda p: p == candidate['sender_profile_url'])
        return store.record_page(store.prepare_channel(9, '601'), projection,
            next_cursor=('0', '0'), has_more=False, allow_live=True)
    publish_fact()
    actual = NativeLane(lane.browser_lane, receiver, store); actual.run_id = lane.run_id
    return actual, store, adapter, queue, conversation, publish_fact, clock


def test_native_lane_real_temporary_source_store_plan_alias_restart_and_authority(tmp_path):
    lane, store, adapter, queue, conversation, publish, clock = actual_source_system(tmp_path)
    row = lane.collect()[0]
    task = queue.enqueue(**row)
    assert not lane.authority_current(task)
    lane.admitted([task])
    assert lane.authority_current(task)
    reopened = NativeSourceStore(store.path, store.receiver_uid, store.receiver_profile_url, clock=lambda: clock[0])
    second = NativeLane(lane.browser_lane, lane.native_receiver, reopened)
    assert second.collect() == [] and second.authority_current(task)
    assert sum(queue.counts().values()) == 1 and len(adapter.native_calls) == 1


def test_native_lane_real_temporary_source_conflict_blocks_future_effect_gates(tmp_path):
    lane, store, adapter, queue, conversation, publish, clock = actual_source_system(tmp_path)
    task = queue.enqueue(**lane.collect()[0]); lane.admitted([task])
    assert lane.authority_current(task)
    publish('8900000000000000005', text='同原生CID却不同正文')
    assert not lane.authority_current(task)
    assert queue.get_task(task['id'])['envelope']['comment_text'] == '@助手 根据编程知识库解释Git'
    assert sum(queue.counts().values()) == 1


def test_native_equal_prior_terminal_UI_task_is_linked_without_replay(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    adapter.observed_native = store.candidate['native_comment_id']
    notice = {'source_profile_url': store.candidate['sender_profile_url'],
              'exact_text': store.candidate['content'], 'semantic_fingerprint': 'old-exact-native-UI'}
    detail = adapter.detail(notice)
    detail['native_comment_id'] = adapter.observed_native
    detail['evidence']['native_comment_id'] = adapter.observed_native
    task = queue.enqueue(**lane.browser_lane.prepare_native_detail(notice, detail)[0])
    lane.browser_lane.admitted([task])
    # Synthetic terminal outcome in our temporary queue, without an external effect.
    with queue._tx() as db:
        db.execute("UPDATE assistant_tasks SET state='delivery_unknown' WHERE id=?", (task['id'],))
    row = lane.collect()[0]
    same = queue.enqueue(**row); lane.admitted([same])
    assert same['id'] == task['id'] and same['state'] == 'delivery_unknown'
    assert sum(queue.counts().values()) == 1
    assert queue.claim_next('execute') is None and queue.claim_next('publish') is None
    assert store.links == [(task['id'], task['envelope']['reference_id'])]


def test_native_queue_row_mutation_cannot_borrow_original_plan_or_start_billing(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    row = lane.collect()[0]
    row['envelope']['comment_text'] = '@助手 已被修改的另一条问句'
    task = queue.enqueue(**row)
    with pytest.raises(NativeLaneError): lane.admitted([task])
    assert lane.authority_current(task) is False and store.state == 'planned'


def test_native_unknown_network_failure_is_fixed_error_without_UI_fallback(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    def fail(): raise RuntimeError('synthetic secret-like upstream detail')
    receiver.poll_once = fail
    with pytest.raises(NativeLaneError) as error: lane.poll_native()
    assert str(error.value) == 'native_poll_unavailable'
    assert not adapter.native_calls and sum(queue.counts().values()) == 0


def test_native_mixed_AT_question_not_a_menu_selection_stays_actual_query(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path,
        parent_id_hint='8900000000000000099')
    root_env = {'sender_profile_url': store.candidate['sender_profile_url'],
        'receiver_profile_url': store.candidate['receiver_profile_url'], 'video_id': store.candidate['video_id'],
        'reference_id': 'saved-Root', 'native_comment_id': None}
    target = {'choice_id': 'a'*64, 'root_task_id': 'target-root', 'root_envelope': root_env,
              'options': [{'knowledge_base_id': 7, 'name': '编程库'}]}
    store.lookup['target-root'] = {'source_id': 'synthetic-root-source', 'state': 'admitted', 'historical': False,
        'current': True, 'candidate': {**store.candidate, 'native_comment_id': store.candidate['parent_id_hint']}}
    calls = []
    lane.choice_controller = SimpleNamespace(store=SimpleNamespace(list_waiting=lambda _: [target], list_asks_to_observe=lambda _: []),
        collect_pending=lambda *a, **kw: calls.append(1) or [], clear_buffer=lambda: None,
        known_selection_notice=lambda *a: False, selection_notice_requires_recheck=lambda *a: False)
    rows = lane.collect()
    assert len(rows) == 1 and rows[0]['kind'] == 'query' and not calls
    assert len(adapter.native_calls) == 1


@pytest.mark.parametrize('code', ['authorization_not_provisioned', 'capsule_identity_mismatch',
                                 'capsule_invalid', 'capsule_unprotect_failed'])
def test_native_unusable_capsule_requires_person_pause(tmp_path, code):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    class FixedError(Exception): pass
    error = FixedError(); error.code = code
    def fail(): raise error
    receiver.poll_once = fail
    with pytest.raises(ReceiverInterventionRequired): lane.poll_native()
    assert not adapter.native_calls and sum(queue.counts().values()) == 0


def test_native_choice_root_source_conflict_blocks_before_reading_selection(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    store.lookup[root_task['id']]['state'] = 'held_conflict'
    assert lane.collect() == []
    assert not adapter.reads and store.state == 'held'
    assert sum(case.queue.counts().values()) == 1


def test_native_choice_root_source_revocation_blocks_child_continuous_authority(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    child = case.queue.enqueue(**lane.collect()[0]); lane.admitted([child])
    assert lane.authority_current(child)
    store.lookup[root_task['id']]['current'] = False
    assert not lane.authority_current(child)


def test_native_choice_root_scope_cannot_be_borrowed_from_another_queue_task(native_choice_case):
    case = native_choice_case
    lane, store, root_task, session, adapter = choice_native_system(case)
    child = case.queue.enqueue(**lane.collect()[0]); lane.admitted([child])
    altered = deepcopy(child)
    altered['envelope']['evidence']['root_reference_id'] = 'another-public-root'
    assert not lane.authority_current(altered)


def test_native_admitted_task_survives_receiver_freshness_expiry_with_exact_scope(tmp_path):
    lane, store, adapter, queue, conversation, publish, clock = actual_source_system(tmp_path)
    task = queue.enqueue(**lane.collect()[0]); lane.admitted([task])
    source = store.lookup_source_for_task(task['id'])
    clock[0] += 121
    assert store.admission_current(source['source_id']) is False
    assert store.admitted_source_current(source['source_id']) is True
    assert lane.authority_current(task) is True
    store.pause('network_timeout')
    assert store.admission_current(source['source_id']) is False
    assert lane.authority_current(task) is True
    altered = deepcopy(task); altered['authorization']['grant_revision'] += 1
    assert lane.authority_current(altered) is False


def test_native_new_UI_admission_still_rejects_expired_receiver_before_plan(tmp_path):
    lane, store, adapter, queue, conversation, publish, clock = actual_source_system(tmp_path)
    adapter.before_return = lambda: clock.__setitem__(0, clock[0] + 121)
    assert lane.collect() == []
    assert sum(queue.counts().values()) == 0
    assert store.recoverable_ui_plans() == []


def test_native_durable_intent_ignores_transport_pause_but_not_source_conflict(tmp_path):
    lane, store, receiver, adapter, queue, conversation, registry, migrated = native_system(tmp_path)
    task = queue.enqueue(**lane.collect()[0]); lane.admitted([task])
    store.transport_fresh = False
    assert lane.authority_current(task) is True
    store.conflict = True
    assert lane.authority_current(task) is False
