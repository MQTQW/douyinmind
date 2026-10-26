from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.assistant.queue import AssistantQueue, QueueError
from app.mention_agent.contracts import Binding
from tests.test_library_choice_evidence import built

A = 'https://www.douyin.com/user/queue-all-sender'
B = 'https://www.douyin.com/user/queue-all-receiver'


def all_binding():
    return Binding('owner-all-binding', 'queue-local', A, B, None, True, query_scope='all_local')


def root_envelope(binding):
    return {'command_id': 'actual-root-command', 'reference_id': 'actual-root-reference',
            'reference_quality': 'verified_ui', 'native_comment_id': None,
            'sender_profile_url': binding.sender_profile_url, 'receiver_profile_url': binding.receiver_profile_url,
            'video_id': '1234567890123456789', 'comment_text': '@B 帮我保存这个视频', 'source_mode': 'live_ui',
            'evidence': {'real_mention': True, 'sender_verified': True, 'video_verified': True,
                         'unique_count': 1, 'observed_at_utc': datetime.now(timezone.utc).isoformat()}}


def authorization(binding):
    value = {'registry_id': 'registry-test', 'user_id': 'user-test', 'device_id': 'device-test',
             'grant_id': 'grant-test', 'user_revision': 1, 'device_revision': 1,
             'grant_revision': 1, 'grant_version': 1, 'binding_id': binding.binding_id,
             'instance_id': binding.instance_id, 'knowledge_base_id': binding.knowledge_base_id,
             'action': 'query', 'public_reply_allowed': True}
    if binding.is_all_local:
        value['query_scope'] = 'all_local'
    return value


def test_all_none_is_an_explicit_query_scope_in_queue_and_public_projection(tmp_path):
    q = AssistantQueue(tmp_path / 'queue.sqlite3')
    binding = all_binding()
    row = q.enqueue(binding, root_envelope(binding), kind='query', authorization=authorization(binding))
    public = q.public_task(row)
    assert row['knowledge_base_id'] is None
    assert public['query_scope'] == 'all_local'
    assert public['scope_label'] == '全部入库资料'


@pytest.mark.parametrize('mutation', ['missing_scope', 'wrong_scope', 'import_action', 'borrowed_library_id'])
def test_all_scope_cannot_borrow_library_authorization_or_save_permission(tmp_path, mutation):
    q = AssistantQueue(tmp_path / 'queue.sqlite3')
    binding = all_binding()
    proof = authorization(binding)
    if mutation == 'missing_scope': proof.pop('query_scope')
    elif mutation == 'wrong_scope': proof['query_scope'] = 'library'
    elif mutation == 'import_action': proof['action'] = 'import'
    else: proof['knowledge_base_id'] = 7
    with pytest.raises(QueueError):
        q.enqueue(binding, root_envelope(binding), authorization=proof)
    assert all(value == 0 for value in q.counts().values())


def test_choice_parent_cannot_change_under_same_native_selection_reference(tmp_path):
    q = AssistantQueue(tmp_path / 'queue.sqlite3')
    binding, envelope = built(native='9876543210123456789')
    first = q.enqueue(binding, envelope, kind='save', authorization=authorization(binding))
    changed = deepcopy(envelope)
    changed['evidence']['ask_reply_ref'] = 'verified-ui:different-B-question'
    with pytest.raises(QueueError, match='original_comment_conflict'):
        q.enqueue(binding, changed, kind='save', authorization=authorization(binding))
    observed_again = deepcopy(envelope)
    observed_again['evidence']['observed_at_utc'] = '2026-10-22T07:00:00+00:00'
    assert q.enqueue(binding, observed_again, kind='save', authorization=authorization(binding))['id'] == first['id']
    assert q.counts()['queued'] == 1


def waiting_result(**changes):
    return {'state': 'waiting_library', 'agent_task_id': 'synthetic-ask-agent-task',
            'reply_state': 'b_view_visible', 'send_token_present': True,
            'safe_to_publish': False, 'binding_active': True, 'public_reply_allowed': True,
            'details': {'library_choice': {'choice_id': 'a' * 64, 'state': 'waiting_library'}}, **changes}


def publish_claim(q):
    binding = all_binding()
    task = q.enqueue(binding, root_envelope(binding), kind='clarify', authorization=authorization(binding))
    execute = q.claim_next('execute', run_id='run-test')
    q.finish(task['id'], execute['execution_token'], 'execute', {
        'state': 'answer_ready', 'agent_task_id': 'synthetic-ask-agent-task', 'reply_state': 'ready',
        'send_token_present': False, 'safe_to_publish': True, 'binding_active': True, 'public_reply_allowed': True})
    return task, q.claim_next('publish', run_id='run-test')


def test_waiting_library_poll_never_reserves_a_second_public_ask(tmp_path, monkeypatch):
    import app.assistant.queue as queue_module
    clock = [100000.0]
    monkeypatch.setattr(queue_module.time, 'time', lambda: clock[0])
    q = AssistantQueue(tmp_path / 'queue.sqlite3')
    task, claim = publish_claim(q)
    row = q.finish(task['id'], claim['publication_token'], 'publish', waiting_result())
    assert row['state'] == 'waiting_library'
    publication = row['publication_token']
    assert q.public_task(row)['stages']['processing'] == 'not_started'
    assert q.claim_next('publish', run_id='run-test') is None
    assert q.claim_waiting() is None
    clock[0] += 46
    poll = q.claim_waiting()
    assert poll['id'] == task['id']
    row = q.apply_reconciliation(task['id'], waiting_result(), poll_token=poll['poll_token'])
    assert row['state'] == 'waiting_library' and row['publication_token'] == publication
    with pytest.raises(QueueError, match='resume_not_safe'):
        q.resume(task['id'])
    assert q.claim_next('publish', run_id='run-test') is None
    clock[0] += 46
    poll = q.claim_waiting()
    done = q.apply_reconciliation(task['id'], waiting_result(state='b_view_visible'), poll_token=poll['poll_token'])
    assert done['state'] == 'b_view_visible' and done['publication_token'] == publication
    assert q.apply_reconciliation(task['id'], {'state': 'answer_ready'})['state'] == 'b_view_visible'


@pytest.mark.parametrize('changes', [{'send_token_present': False}, {'reply_state': 'ready'},
                                     {'binding_active': False}, {'safe_to_publish': True}])
def test_unverified_ask_cannot_claim_it_is_waiting_after_publication(tmp_path, changes):
    q = AssistantQueue(tmp_path / 'queue.sqlite3')
    task, claim = publish_claim(q)
    row = q.finish(task['id'], claim['publication_token'], 'publish', waiting_result(**changes))
    assert row['state'] == 'needs_reconcile'
